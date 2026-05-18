"""
Account management routes:
  /accounts, /accounts/new, /accounts/<id>/edit,
  /accounts/<id>/archive, /api/accounts/batch
"""
import logging
from datetime import datetime

from flask import render_template, request, flash, redirect, url_for, jsonify

from app.routes import main_bp
from app.routes.helpers import (
    _validate_account_form,
    _account_from_form,
    _form_values_from_account,
    _form_values_from_post,
    _get_account_allocations,
    _save_account_allocations,
    _compute_holdings_value,
    _get_app_setting,
)
from app.models import Account, AccountSnapshot, Holding
from app import db
from app.account_categories import (
    ALL_CATEGORIES as ACCOUNT_CATEGORIES,
    INVESTMENT_CATS,
    ALLOCATION_CLASSES,
)
from app.metrics_service import recalculate_metrics as _recalculate_metrics
from sqlalchemy import func

logger = logging.getLogger(__name__)


_FORM_DEFAULTS = {
    'name': '', 'institution': '', 'category': '', 'account_type': 'asset',
    'tax_status': '', 'is_liquid': True, 'include_in_networth': True,
    'is_active': True, 'account_number': '', 'display_color': '#6c757d', 'notes': '',
    'paired_liability_id': '', 'apy': '', 'expected_dividend_yield': '',
}


@main_bp.route('/accounts')
def accounts():
    """View all accounts"""
    show = request.args.get('show', 'active')
    sort = request.args.get('sort', 'institution')
    dir_ = request.args.get('dir', 'asc')
    desc = (dir_ == 'desc')
    q = Account.query
    if show == 'archived':
        q = q.filter(Account.is_active == False)
    elif show == 'all':
        pass
    else:
        q = q.filter(Account.is_active == True)
    if sort == 'name':
        q = q.order_by(Account.name.desc() if desc else Account.name)
    elif sort == 'type':
        q = q.order_by(Account.account_type.desc() if desc else Account.account_type, Account.name)
    elif sort == 'category':
        q = q.order_by(Account.category.desc() if desc else Account.category, Account.name)
    elif sort == 'tax_status':
        q = q.order_by(Account.tax_status.desc() if desc else Account.tax_status, Account.name)
    elif sort == 'institution':
        q = q.order_by(Account.institution.desc() if desc else Account.institution, Account.name)
    elif sort == 'balance':
        q = q.order_by(Account.display_order, Account.name)
    else:
        q = q.order_by(Account.institution, Account.name)
    all_accounts = q.all()
    latest_dates = dict(
        db.session.query(AccountSnapshot.account_id, func.max(AccountSnapshot.snapshot_date))
        .group_by(AccountSnapshot.account_id).all()
    )
    latest_balances = {}
    for acct_id, max_date in latest_dates.items():
        snap = AccountSnapshot.query.filter_by(account_id=acct_id, snapshot_date=max_date).first()
        if snap:
            latest_balances[acct_id] = snap.balance
    if sort == 'balance':
        all_accounts.sort(key=lambda a: latest_balances.get(a.id, -1), reverse=not desc)
    account_names = {a.id: a.name for a in Account.query.all()}
    holdings_values = {
        a.id: _compute_holdings_value(a.id)
        for a in all_accounts if a.category in INVESTMENT_CATS
    }
    return render_template('accounts.html', accounts=all_accounts, show=show, sort=sort, dir=dir_,
                           latest_balances=latest_balances, account_names=account_names,
                           holdings_values=holdings_values, investment_cats=INVESTMENT_CATS)


@main_bp.route('/accounts/new', methods=['GET', 'POST'])
def account_new():
    """Create a new account"""
    current_month = datetime.today().strftime('%Y-%m')
    mortgage_accounts = Account.query.filter_by(category='mortgage', is_active=True).order_by(Account.name).all()
    if request.method == 'POST':
        errors = _validate_account_form(request.form)
        if errors:
            return render_template('account_form.html', errors=errors,
                                   values=_form_values_from_post(), account=None,
                                   categories=ACCOUNT_CATEGORIES, current_month=current_month,
                                   mortgage_accounts=mortgage_accounts,
                                   investment_cats=INVESTMENT_CATS,
                                   allocation_classes=ALLOCATION_CLASSES)
        account = _account_from_form(request.form)
        db.session.add(account)
        db.session.commit()
        logger.info('Account created: id=%d, name=%s, category=%s', account.id, account.name, account.category)
        opening_balance = request.form.get('opening_balance', '').strip()
        if opening_balance:
            try:
                bal = float(opening_balance)
                month_str = request.form.get('opening_month', current_month)
                month_date = datetime.strptime(month_str, '%Y-%m').date().replace(day=1)
                snap = AccountSnapshot(account_id=account.id, snapshot_date=month_date, balance=bal)
                db.session.add(snap)
                db.session.commit()
                _recalculate_metrics(month_date)
            except Exception as e:
                logger.warning('Opening balance skipped for account id=%d: %s', account.id, e)
        flash(f'Account "{account.name}" created successfully.', 'success')
        return redirect(url_for('main.accounts'))
    return render_template('account_form.html', errors=[], values=_FORM_DEFAULTS,
                           account=None, categories=ACCOUNT_CATEGORIES, current_month=current_month,
                           mortgage_accounts=mortgage_accounts,
                           investment_cats=INVESTMENT_CATS,
                           allocation_classes=ALLOCATION_CLASSES)


@main_bp.route('/accounts/<int:account_id>/edit', methods=['GET', 'POST'])
def account_edit(account_id):
    """Edit an existing account"""
    account = Account.query.get_or_404(account_id)
    latest_snap = (AccountSnapshot.query
                   .filter_by(account_id=account_id)
                   .order_by(AccountSnapshot.snapshot_date.desc())
                   .first())
    snap_count = AccountSnapshot.query.filter_by(account_id=account_id).count()
    mortgage_accounts = Account.query.filter(
        Account.category == 'mortgage',
        Account.is_active == True,
        Account.id != account_id,
    ).order_by(Account.name).all()
    if request.method == 'POST':
        errors = _validate_account_form(request.form, existing_id=account_id)
        if errors:
            return render_template('account_form.html', errors=errors,
                                   values=_form_values_from_post(), account=account,
                                   categories=ACCOUNT_CATEGORIES,
                                   latest_snap=latest_snap, snap_count=snap_count,
                                   mortgage_accounts=mortgage_accounts,
                                   allocations=_get_account_allocations(account_id),
                                   investment_cats=INVESTMENT_CATS,
                                   allocation_classes=ALLOCATION_CLASSES)
        _account_from_form(request.form, account)
        if account.category in INVESTMENT_CATS:
            _save_account_allocations(account_id, request.form)
        db.session.commit()
        logger.info('Account updated: id=%d, name=%s', account.id, account.name)

        # Refresh stale holding prices on save
        if account.category in INVESTMENT_CATS:
            from app.price_service import get_price, is_stale
            from datetime import datetime, timezone
            stale_holdings = [
                h for h in Holding.query.filter_by(account_id=account_id, is_active=True).all()
                if is_stale(h.last_fetched)
            ]
            if stale_holdings:
                for h in stale_holdings:
                    try:
                        price, name = get_price(h.ticker)
                        h.last_price = price
                        h.name = name
                        h.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
                    except Exception as e:
                        logger.warning('Price refresh on save failed: ticker=%s error=%s', h.ticker, e)
                db.session.commit()
                logger.info('Price refresh on save: account_id=%d updated=%d', account_id, len(stale_holdings))

        flash(f'Account "{account.name}" updated successfully.', 'success')
        return redirect(url_for('main.accounts'))
    holdings_value = _compute_holdings_value(account_id) if account.category in INVESTMENT_CATS else None
    classification_enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    return render_template('account_form.html', errors=[], values=_form_values_from_account(account),
                           account=account, categories=ACCOUNT_CATEGORIES,
                           latest_snap=latest_snap, snap_count=snap_count,
                           mortgage_accounts=mortgage_accounts,
                           allocations=_get_account_allocations(account_id),
                           investment_cats=INVESTMENT_CATS,
                           allocation_classes=ALLOCATION_CLASSES,
                           holdings_value=holdings_value,
                           classification_enabled=classification_enabled)


@main_bp.route('/accounts/<int:account_id>/archive', methods=['POST'])
def account_archive(account_id):
    """Toggle active/archived status"""
    account = Account.query.get_or_404(account_id)
    account.is_active = not account.is_active
    db.session.commit()
    action = 'unarchived' if account.is_active else 'archived'
    logger.info('Account %s: id=%d, name=%s', action, account.id, account.name)
    return jsonify({'success': True, 'is_active': account.is_active})


@main_bp.route('/api/accounts/batch', methods=['POST'])
def api_accounts_batch():
    """Create multiple accounts atomically (used by onboarding wizard)."""
    data = request.get_json()
    if not data or 'accounts' not in data or not isinstance(data['accounts'], list):
        return jsonify({'error': 'accounts array required'}), 400

    accounts_data = data['accounts']
    if not accounts_data:
        return jsonify({'error': 'At least one account is required'}), 400

    # Validate all entries before writing anything
    all_errors = {}
    names_seen = set()
    for i, item in enumerate(accounts_data):
        errs = _validate_account_form(item)
        # Also check for duplicate names within the submitted list
        name_lower = (item.get('name') or '').strip().lower()
        if name_lower and name_lower in names_seen:
            errs.append('Duplicate account name in submission.')
        names_seen.add(name_lower)
        if errs:
            all_errors[i] = errs

    if all_errors:
        return jsonify({'errors': all_errors}), 422

    current_month = datetime.today().strftime('%Y-%m')
    created = []
    months_with_snapshots = set()

    try:
        for item in accounts_data:
            account = _account_from_form(item)
            db.session.add(account)
            db.session.flush()  # populate account.id within transaction

            opening_balance = str(item.get('opening_balance', '')).strip()
            if opening_balance:
                bal = float(opening_balance)
                om = item.get('opening_month', current_month) or current_month
                month_date = datetime.strptime(om, '%Y-%m').date().replace(day=1)
                db.session.add(AccountSnapshot(
                    account_id=account.id,
                    snapshot_date=month_date,
                    balance=bal,
                ))
                months_with_snapshots.add(month_date)

            created.append({
                'id': account.id,
                'name': account.name,
                'account_type': account.account_type,
                'category': account.category,
            })

        db.session.commit()

        for month_date in sorted(months_with_snapshots):
            _recalculate_metrics(month_date)

        logger.info('Batch account creation: %d accounts created via onboarding', len(created))
        return jsonify({'created': created}), 201

    except Exception as e:
        db.session.rollback()
        logger.error('Batch account creation failed: %s', e)
        return jsonify({'error': 'Failed to create accounts. No changes were saved.'}), 500
