"""
Main application routes
"""
import logging
import os
import re
import csv
from flask import Blueprint, render_template, jsonify, request, flash, redirect, url_for, make_response
from werkzeug.utils import secure_filename
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric, ImportLog, AssetAllocation, AppSetting, Holding, HoldingAllocation, TickerClassification, RecurringEntry, DividendData
from app import db
from app.import_processor import process_csv, preview_csv
from app import projections as proj
from datetime import datetime, date
from decimal import Decimal
from dateutil.relativedelta import relativedelta
from sqlalchemy import func
import io

main_bp = Blueprint('main', __name__)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _parse_month_str(month_str):
    """Parse 'YYYY-MM' string to a date(year, month, 1). Returns None on failure."""
    try:
        return datetime.strptime(month_str, '%Y-%m').date().replace(day=1)
    except (ValueError, AttributeError):
        return None


def _recalculate_metrics(month_date):
    """
    Recalculate and upsert CalculatedMetric for the given month_date.
    Called automatically after any AccountSnapshot or SpendingEntry write.
    """
    # Sum assets (account_type='asset' AND include_in_networth=True)
    asset_snapshots = (
        db.session.query(AccountSnapshot)
        .join(Account)
        .filter(
            AccountSnapshot.snapshot_date == month_date,
            Account.account_type == 'asset',
            Account.include_in_networth == True
        ).all()
    )
    total_assets = sum((s.balance for s in asset_snapshots), Decimal('0'))

    # Sum liabilities
    liability_snapshots = (
        db.session.query(AccountSnapshot)
        .join(Account)
        .filter(
            AccountSnapshot.snapshot_date == month_date,
            Account.account_type == 'liability',
            Account.include_in_networth == True
        ).all()
    )
    total_liabilities = sum((s.balance for s in liability_snapshots), Decimal('0'))

    # Real estate assets and tied mortgage liabilities (both excluded from net_worth_non_re)
    re_snapshots = (
        db.session.query(AccountSnapshot)
        .join(Account)
        .filter(
            AccountSnapshot.snapshot_date == month_date,
            Account.category == 'real_estate'
        ).all()
    )
    re_balance = sum((s.balance for s in re_snapshots), Decimal('0'))

    mortgage_snapshots = (
        db.session.query(AccountSnapshot)
        .join(Account)
        .filter(
            AccountSnapshot.snapshot_date == month_date,
            Account.category == 'mortgage'
        ).all()
    )
    mortgage_balance = sum((s.balance for s in mortgage_snapshots), Decimal('0'))

    net_worth = total_assets - total_liabilities
    # Exclude RE assets and their tied mortgage liabilities:
    # net_worth already subtracted mortgage, so add it back when stripping RE
    net_worth_non_re = net_worth - re_balance + mortgage_balance

    # Liquid NW: only assets where account.is_liquid=True, minus all liabilities
    liquid_assets = sum((s.balance for s in asset_snapshots if s.account.is_liquid), Decimal('0'))
    net_worth_liquid = liquid_assets - total_liabilities

    # Previous month net worth for change calculation
    if month_date.month == 1:
        prev_date = date(month_date.year - 1, 12, 1)
    else:
        prev_date = date(month_date.year, month_date.month - 1, 1)

    prev_metric = CalculatedMetric.query.filter_by(metric_date=prev_date).first()
    prev_nw = prev_metric.net_worth if prev_metric and prev_metric.net_worth is not None else None

    monthly_change_amount = (net_worth - prev_nw) if prev_nw is not None else None
    monthly_change_pct = (
        (monthly_change_amount / prev_nw * 100) if prev_nw and prev_nw != 0 else None
    )

    # Income and expenses
    income_entries = SpendingEntry.query.filter_by(entry_date=month_date, entry_type='income').all()
    expense_entries = SpendingEntry.query.filter_by(entry_date=month_date, entry_type='expense').all()
    total_income = sum((e.amount for e in income_entries), Decimal('0'))
    total_expenses = sum((e.amount for e in expense_entries), Decimal('0'))
    save_rate = (
        ((total_income - total_expenses) / total_income * 100)
        if total_income > 0 else None
    )

    # Upsert CalculatedMetric
    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
    if not metric:
        metric = CalculatedMetric(metric_date=month_date)
        db.session.add(metric)

    metric.total_assets = total_assets
    metric.total_liabilities = total_liabilities
    metric.net_worth = net_worth
    metric.net_worth_non_re = net_worth_non_re
    metric.net_worth_liquid = net_worth_liquid
    metric.monthly_change_amount = monthly_change_amount
    metric.monthly_change_pct = monthly_change_pct
    metric.total_income = total_income
    metric.total_expenses = total_expenses
    metric.save_rate = save_rate

    db.session.commit()
    logger.debug('Metrics recalculated for %s', month_date.strftime('%Y-%m'))
    return metric


def _metric_to_dict(metric):
    """Serialize a CalculatedMetric to a JSON-safe dict."""
    def f(v):
        return float(v) if v is not None else None

    return {
        'month': metric.metric_date.strftime('%Y-%m'),
        'total_assets': f(metric.total_assets),
        'total_liabilities': f(metric.total_liabilities),
        'net_worth': f(metric.net_worth),
        'net_worth_non_re': f(metric.net_worth_non_re),
        'monthly_change_amount': f(metric.monthly_change_amount),
        'monthly_change_pct': f(metric.monthly_change_pct),
        'total_income': f(metric.total_income),
        'total_expenses': f(metric.total_expenses),
        'save_rate': f(metric.save_rate),
    }


def _build_month_list():
    """Return a sorted list of month summary dicts from all data sources."""
    snapshot_dates = db.session.query(AccountSnapshot.snapshot_date).distinct().all()
    spending_dates = db.session.query(SpendingEntry.entry_date).distinct().all()
    metric_dates = db.session.query(CalculatedMetric.metric_date).distinct().all()

    all_month_dates = sorted(
        {d[0] for d in snapshot_dates + spending_dates + metric_dates},
        reverse=True
    )

    months = []
    for md in all_month_dates:
        metric = CalculatedMetric.query.filter_by(metric_date=md).first()
        snap_count = AccountSnapshot.query.filter_by(snapshot_date=md).count()
        spend_count = SpendingEntry.query.filter_by(entry_date=md).count()
        months.append({
            'month': md.strftime('%Y-%m'),
            'label': md.strftime('%B %Y'),
            'net_worth': float(metric.net_worth) if metric and metric.net_worth is not None else None,
            'save_rate': float(metric.save_rate) if metric and metric.save_rate is not None else None,
            'monthly_change_pct': float(metric.monthly_change_pct) if metric and metric.monthly_change_pct is not None else None,
            'snapshot_count': snap_count,
            'spending_count': spend_count,
        })
    return months


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@main_bp.route('/')
def index():
    """Main dashboard view"""
    no_accounts = Account.query.filter_by(is_active=True).count() == 0
    no_snapshots = AccountSnapshot.query.count() == 0
    latest_metric = CalculatedMetric.query.order_by(
        CalculatedMetric.metric_date.desc()
    ).first()
    recent_months = _build_month_list()[:5]

    # YTD save rate: sum income/expenses across all months in the current year
    current_year = date.today().year
    ytd_metrics = CalculatedMetric.query.filter(
        CalculatedMetric.metric_date >= date(current_year, 1, 1),
        CalculatedMetric.metric_date <= date(current_year, 12, 31),
    ).all()
    ytd_income = sum(float(m.total_income) for m in ytd_metrics if m.total_income)
    ytd_expenses = sum(float(m.total_expenses) for m in ytd_metrics if m.total_expenses)
    ytd_save_rate = ((ytd_income - ytd_expenses) / ytd_income * 100) if ytd_income > 0 else None

    target_setting = AppSetting.query.filter_by(key='save_rate_target').first()
    save_rate_target = float(target_setting.value) if target_setting and target_setting.value else 35.0

    return render_template('index.html',
                           latest_metric=latest_metric,
                           recent_months=recent_months,
                           no_accounts=no_accounts,
                           no_snapshots=no_snapshots,
                           ytd_save_rate=ytd_save_rate,
                           ytd_year=current_year,
                           save_rate_target=save_rate_target)


ACCOUNT_CATEGORIES = [
    'cash', 'checking', 'savings', 'brokerage', 'retirement',
    '401k', 'ira', 'roth_ira', 'hsa', '529', 'investment',
    'real_estate', 'vehicle', 'mortgage', 'loan', 'credit_card',
]

# Categories that hold investable assets and need allocation splits
INVESTMENT_CATS = {'brokerage', 'retirement', '401k', 'ira', 'roth_ira', 'hsa', '529', 'investment'}

# Categories that are automatically 100% cash in allocation math
CASH_CATS = {'savings', 'checking', 'cash'}

# Fixed asset classes for allocation tracking
ALLOCATION_CLASSES = ['domestic', 'international', 'bonds', 'cash']


def _validate_account_form(data, existing_id=None):
    errors = []
    if not data.get('name'):
        errors.append('Name is required.')
    elif Account.query.filter(
        func.lower(Account.name) == data['name'].lower(),
        Account.id != existing_id
    ).first():
        errors.append('An account with that name already exists.')
    if data.get('account_type') not in ('asset', 'liability'):
        errors.append('Account type must be asset or liability.')
    if not data.get('category'):
        errors.append('Category is required.')
    color = data.get('display_color', '').strip()
    if color and not re.match(r'^#[0-9a-fA-F]{6}$', color):
        errors.append('Display color must be a valid hex color (e.g. #4a90e2).')
    return errors


def _account_from_form(data, account=None):
    """Apply form data to an Account instance (new or existing)."""
    if account is None:
        account = Account()
    account.name = data['name'].strip()
    account.account_type = data['account_type']
    account.category = data['category']
    account.tax_status = data.get('tax_status') or None
    account.is_liquid = 'is_liquid' in data
    account.include_in_networth = 'include_in_networth' in data
    account.institution = data.get('institution', '').strip() or None
    account.account_number = data.get('account_number', '').strip() or None
    account.notes = data.get('notes', '').strip() or None
    plid = str(data.get('paired_liability_id', '')).strip()
    account.paired_liability_id = int(plid) if plid.isdigit() else None
    # New accounts are always active; existing accounts read the checkbox
    if account.id is None:
        account.is_active = True
    else:
        account.is_active = 'is_active' in data
    color = data.get('display_color', '').strip()
    account.display_color = color if color else None
    return account


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


def _form_values_from_account(account):
    """Build a values dict from an Account model object."""
    return {
        'name': account.name or '',
        'institution': account.institution or '',
        'category': account.category or '',
        'account_type': account.account_type or 'asset',
        'tax_status': account.tax_status or '',
        'is_liquid': account.is_liquid,
        'include_in_networth': account.include_in_networth,
        'is_active': account.is_active,
        'account_number': account.account_number or '',
        'display_color': account.display_color or '#6c757d',
        'notes': account.notes or '',
        'paired_liability_id': account.paired_liability_id or '',
    }


def _form_values_from_post():
    """Build a values dict from request.form POST data."""
    return {
        'name': request.form.get('name', ''),
        'institution': request.form.get('institution', ''),
        'category': request.form.get('category', ''),
        'account_type': request.form.get('account_type', 'asset'),
        'tax_status': request.form.get('tax_status', ''),
        'is_liquid': 'is_liquid' in request.form,
        'include_in_networth': 'include_in_networth' in request.form,
        'is_active': 'is_active' in request.form,
        'account_number': request.form.get('account_number', ''),
        'display_color': request.form.get('display_color', '#6c757d'),
        'notes': request.form.get('notes', ''),
        'paired_liability_id': request.form.get('paired_liability_id', ''),
    }


_FORM_DEFAULTS = {
    'name': '', 'institution': '', 'category': '', 'account_type': 'asset',
    'tax_status': '', 'is_liquid': True, 'include_in_networth': True,
    'is_active': True, 'account_number': '', 'display_color': '#6c757d', 'notes': '',
    'paired_liability_id': '',
}


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
            except (ValueError, Exception):
                pass
        flash(f'Account "{account.name}" created successfully.', 'success')
        return redirect(url_for('main.accounts'))
    return render_template('account_form.html', errors=[], values=_FORM_DEFAULTS,
                           account=None, categories=ACCOUNT_CATEGORIES, current_month=current_month,
                           mortgage_accounts=mortgage_accounts,
                           investment_cats=INVESTMENT_CATS,
                           allocation_classes=ALLOCATION_CLASSES)


def _get_account_allocations(account_id):
    """Return {asset_class: percentage} for the latest allocation splits on an account."""
    rows = (AssetAllocation.query
            .filter_by(account_id=account_id)
            .order_by(AssetAllocation.effective_date.desc())
            .all())
    # Take most-recent effective_date only
    if not rows:
        return {}
    latest_date = rows[0].effective_date
    return {r.asset_class: float(r.percentage) for r in rows if r.effective_date == latest_date}


def _save_account_allocations(account_id, form):
    """Parse allocation split fields from form and upsert AssetAllocation rows."""
    splits = {}
    for cls in ALLOCATION_CLASSES:
        raw = form.get(f'alloc_{cls}', '').strip()
        try:
            val = float(raw)
        except ValueError:
            val = 0.0
        if val > 0:
            splits[cls] = val

    if not splits:
        return  # leave existing records untouched if nothing submitted

    # Replace all existing records for this account with today's date
    AssetAllocation.query.filter_by(account_id=account_id).delete()
    today = date.today()
    for cls, pct in splits.items():
        db.session.add(AssetAllocation(
            account_id=account_id,
            effective_date=today,
            asset_class=cls,
            percentage=pct,
        ))


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


@main_bp.route('/monthly-update')
def monthly_update():
    """Month list — browse and manage monthly data records"""
    months = _build_month_list()
    all_accounts = Account.query.filter_by(is_active=True).order_by(Account.display_order).all()
    return render_template('monthly_update.html', months=months, accounts=all_accounts)


@main_bp.route('/monthly-update/<month_str>')
def month_detail(month_str):
    """Per-month detail and edit view"""
    month_date = _parse_month_str(month_str)
    if month_date is None:
        return "Invalid month format. Use YYYY-MM.", 400

    snapshots = (
        AccountSnapshot.query
        .filter_by(snapshot_date=month_date)
        .join(Account)
        .order_by(Account.display_order)
        .all()
    )
    spending_entries = (
        SpendingEntry.query
        .filter_by(entry_date=month_date)
        .order_by(SpendingEntry.entry_type, SpendingEntry.account_name)
        .all()
    )
    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
    all_accounts = Account.query.filter_by(is_active=True).order_by(Account.display_order).all()

    # Most recent balance per account before this month (for pre-population)
    last_balances = {}
    for acct in all_accounts:
        prior = (
            AccountSnapshot.query
            .filter_by(account_id=acct.id)
            .filter(AccountSnapshot.snapshot_date < month_date)
            .order_by(AccountSnapshot.snapshot_date.desc())
            .first()
        )
        if prior:
            last_balances[acct.id] = float(prior.balance)

    # Compute adjacent months for navigation
    all_months = _build_month_list()  # newest-first
    month_strs = [m['month'] for m in all_months]

    prev_month = None  # older (higher index in desc-sorted list)
    next_month = None  # newer (lower index)

    if month_str in month_strs:
        idx = month_strs.index(month_str)
        if idx + 1 < len(month_strs):
            prev_month = month_strs[idx + 1]
        if idx - 1 >= 0:
            next_month = month_strs[idx - 1]

    # For "copy from previous" button in empty state
    copy_source = None
    copy_source_label = None
    if not snapshots:
        prior = (
            AccountSnapshot.query
            .filter(AccountSnapshot.snapshot_date < month_date)
            .order_by(AccountSnapshot.snapshot_date.desc())
            .first()
        )
        if prior:
            copy_source = prior.snapshot_date.strftime('%Y-%m')
            copy_source_label = prior.snapshot_date.strftime('%B %Y')

    # Group snapshots into the 5 display sections
    _SNAPSHOT_GROUPS = [
        ('Cash',        {'cash', 'checking', 'savings'},                         False),
        ('Retirement',  {'retirement', '401k', 'ira', 'roth_ira', 'hsa', '529'}, False),
        ('Investments', {'brokerage', 'investment'},                              False),
        ('Real Estate', {'real_estate', 'vehicle'},                               False),
        ('Liabilities', {'mortgage', 'loan', 'credit_card'},                      True),
    ]
    _known_cats = {cat for _, cats, _ in _SNAPSHOT_GROUPS for cat in cats}
    grouped_snapshots = []
    for label, cats, is_liability in _SNAPSHOT_GROUPS:
        grp = [s for s in snapshots if s.account.category in cats]
        if grp:
            grouped_snapshots.append({
                'label': label,
                'snapshots': grp,
                'is_liability': is_liability,
                'total': sum(float(s.balance) for s in grp),
            })
    # Catch-all: any category not covered by the groups above
    other = [s for s in snapshots if s.account.category not in _known_cats]
    if other:
        grouped_snapshots.insert(-1, {
            'label': 'Other Assets',
            'snapshots': other,
            'is_liability': False,
            'total': sum(float(s.balance) for s in other),
        })

    # Group ALL active accounts by display section (for the account grid selector)
    snapshotted_ids = {s.account_id for s in snapshots}
    grouped_accounts = []
    for label, cats, is_liability in _SNAPSHOT_GROUPS:
        grp = [a for a in all_accounts if a.category in cats]
        if grp:
            grouped_accounts.append({'label': label, 'accounts': grp})
    other_accts = [a for a in all_accounts if a.category not in _known_cats]
    if other_accts:
        grouped_accounts.insert(-1, {'label': 'Other', 'accounts': other_accts})

    holdings_values = {
        s.account_id: _compute_holdings_value(s.account_id)
        for s in snapshots if s.account.category in INVESTMENT_CATS
    }
    return render_template(
        'month_detail.html',
        month_date=month_date,
        month_str=month_str,
        label=month_date.strftime('%B %Y'),
        snapshots=snapshots,
        grouped_snapshots=grouped_snapshots,
        spending_entries=spending_entries,
        metric=metric,
        accounts=all_accounts,
        last_balances=last_balances,
        prev_month=prev_month,
        next_month=next_month,
        all_months=all_months,
        copy_source=copy_source,
        copy_source_label=copy_source_label,
        grouped_accounts=grouped_accounts,
        snapshotted_ids=snapshotted_ids,
        holdings_values=holdings_values,
        investment_cats=INVESTMENT_CATS,
    )


@main_bp.route('/onboarding')
def onboarding():
    """First-time setup wizard"""
    return render_template('onboarding.html',
                           categories=ACCOUNT_CATEGORIES,
                           current_month=datetime.today().strftime('%Y-%m'))


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


@main_bp.route('/visualizations')
def visualizations():
    """Data visualization dashboard"""
    return render_template('visualizations.html')


# ---------------------------------------------------------------------------
# Existing API endpoints
# ---------------------------------------------------------------------------

@main_bp.route('/api/networth-history')
def networth_history():
    """API endpoint: Get net worth history for charts"""
    metrics = CalculatedMetric.query.order_by(CalculatedMetric.metric_date).all()
    data = {
        'dates': [m.metric_date.strftime('%Y-%m-%d') for m in metrics],
        'net_worth': [float(m.net_worth) if m.net_worth else 0 for m in metrics],
        'net_worth_non_re': [float(m.net_worth_non_re) if m.net_worth_non_re else 0 for m in metrics]
    }
    return jsonify(data)


@main_bp.route('/api/sp500-change')
def sp500_period_change():
    """Return S&P 500 % change for the requested dashboard period."""
    from datetime import timedelta
    period = request.args.get('period', 'YTD')
    today = date.today()

    if period == '1M':
        first_of_current = today.replace(day=1)
        last_of_prev = first_of_current - timedelta(days=1)
        start = last_of_prev.replace(day=1)
        end = last_of_prev
        label = 'S&P 500 (' + start.strftime('%b %Y') + ')'
    elif period == '3M':
        start = today - timedelta(days=90)
        end = today
        label = 'S&P 500 (3M)'
    elif period == 'YTD':
        start = date(today.year, 1, 1)
        end = today
        label = f'S&P 500 ({today.year} YTD)'
    elif period == 'ALL':
        earliest = CalculatedMetric.query.order_by(CalculatedMetric.metric_date).first()
        start = earliest.metric_date if earliest else date(today.year, 1, 1)
        end = today
        label = 'S&P 500 (All)'
    else:
        return jsonify({'error': 'Invalid period'}), 400

    from app.price_service import get_sp500_range_change
    pct = get_sp500_range_change(start, end)
    return jsonify({'pct': pct, 'label': label, 'period': period})


@main_bp.route('/api/save-rate-history')
def save_rate_history():
    """Rolling 12-month average save rate for the visualizations chart."""
    metrics = CalculatedMetric.query.filter(
        CalculatedMetric.save_rate.isnot(None)
    ).order_by(CalculatedMetric.metric_date).all()

    months = [m.metric_date.strftime('%Y-%m') for m in metrics]
    rates  = [float(m.save_rate) for m in metrics]

    rolling = []
    for i in range(len(rates)):
        window = rates[max(0, i - 11): i + 1]
        rolling.append(round(sum(window) / len(window), 2))

    target_setting = AppSetting.query.filter_by(key='save_rate_target').first()
    target = float(target_setting.value) if target_setting and target_setting.value else 35.0

    return jsonify({'months': months, 'save_rate': rates, 'rolling_12': rolling, 'target': target})


@main_bp.route('/api/cashflow-history')
def cashflow_history():
    """Monthly income, expenses, and save rate for cash flow charts."""
    metrics = CalculatedMetric.query.filter(
        CalculatedMetric.total_income.isnot(None)
    ).order_by(CalculatedMetric.metric_date).all()
    return jsonify({
        'months':    [m.metric_date.strftime('%Y-%m') for m in metrics],
        'income':    [float(m.total_income)   if m.total_income   else 0 for m in metrics],
        'expenses':  [float(m.total_expenses) if m.total_expenses else 0 for m in metrics],
        'save_rate': [float(m.save_rate)      if m.save_rate      else None for m in metrics],
    })


@main_bp.route('/api/allocation-history')
def allocation_history():
    """Per-month category-level balances for the allocation drift chart."""
    BUCKET_MAP = {
        'cash': 'Cash', 'checking': 'Cash', 'savings': 'Cash',
        'retirement': 'Retirement', '401k': 'Retirement', 'ira': 'Retirement',
        'roth_ira': 'Retirement', 'hsa': 'Retirement', '529': 'Retirement',
        'brokerage': 'Investments', 'investment': 'Investments',
        'real_estate': 'Real Estate', 'vehicle': 'Real Estate',
        'mortgage': 'Liabilities', 'loan': 'Liabilities', 'credit_card': 'Liabilities',
    }
    rows = (
        db.session.query(
            AccountSnapshot.snapshot_date,
            Account.category,
            Account.account_type,
            func.sum(AccountSnapshot.balance).label('total'),
        )
        .join(Account, Account.id == AccountSnapshot.account_id)
        .filter(Account.is_active == True, Account.include_in_networth == True)
        .group_by(AccountSnapshot.snapshot_date, Account.category, Account.account_type)
        .order_by(AccountSnapshot.snapshot_date)
        .all()
    )
    if not rows:
        return jsonify({'months': [], 'by_category': {}})

    from collections import defaultdict
    month_buckets: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for snap_date, cat, acct_type, total in rows:
        month_str = snap_date.strftime('%Y-%m')
        bucket = BUCKET_MAP.get(cat, cat.replace('_', ' ').title())
        val = float(total) if total else 0.0
        # Liabilities stored as positive; negate for chart
        if acct_type == 'liability':
            val = -val
        month_buckets[month_str][bucket] += val

    months = sorted(month_buckets.keys())
    all_buckets = sorted({b for m in month_buckets.values() for b in m})
    by_category = {b: [round(month_buckets[m].get(b, 0.0), 2) for m in months] for b in all_buckets}

    return jsonify({'months': months, 'by_category': by_category})


@main_bp.route('/api/asset-distribution')
def asset_distribution():
    """API endpoint: current asset balances by category (assets only, excludes liabilities)."""
    LIABILITY_CATS = {'mortgage', 'loan', 'credit_card'}
    latest_date = db.session.query(func.max(AccountSnapshot.snapshot_date)).scalar()
    if not latest_date:
        return jsonify({'categories': [], 'values': []})

    rows = (
        db.session.query(Account.category, func.sum(AccountSnapshot.balance))
        .join(AccountSnapshot, AccountSnapshot.account_id == Account.id)
        .filter(Account.is_active == True)
        .filter(Account.account_type == 'asset')
        .filter(AccountSnapshot.snapshot_date == latest_date)
        .group_by(Account.category)
        .all()
    )

    BUCKET_MAP = {
        'cash': 'Cash',
        'checking': 'Cash',
        'savings': 'Cash',
        'retirement': 'Retirement',
        '401k': 'Retirement',
        'ira': 'Retirement',
        'roth_ira': 'Retirement',
        'hsa': 'Retirement',
        '529': 'Retirement',
        'brokerage': 'Investments',
        'investment': 'Investments',
        'real_estate': 'Real Estate',
        'vehicle': 'Real Estate',
    }
    BUCKET_ORDER = ['Cash', 'Investments', 'Retirement', 'Real Estate']
    bucket_totals: dict[str, float] = {}
    for cat, total in rows:
        if cat in LIABILITY_CATS or not total:
            continue
        bucket = BUCKET_MAP.get(cat, 'Other')
        bucket_totals[bucket] = bucket_totals.get(bucket, 0.0) + float(total)

    ordered = sorted(
        bucket_totals.items(),
        key=lambda x: BUCKET_ORDER.index(x[0]) if x[0] in BUCKET_ORDER else len(BUCKET_ORDER)
    )
    return jsonify({
        'categories': [b for b, _ in ordered],
        'values': [round(v, 2) for _, v in ordered],
    })


@main_bp.route('/api/allocation/holdings-summary')
def allocation_holdings_summary():
    """API endpoint: holdings-derived asset-class totals {labels, values}."""
    all_cats = INVESTMENT_CATS | CASH_CATS
    inv_accounts = (
        Account.query
        .filter(Account.category.in_(all_cats))
        .filter_by(is_active=True, account_type='asset', include_in_networth=True)
        .all()
    )

    latest_dates = dict(
        db.session.query(AccountSnapshot.account_id, func.max(AccountSnapshot.snapshot_date))
        .filter(AccountSnapshot.account_id.in_([a.id for a in inv_accounts]))
        .group_by(AccountSnapshot.account_id)
        .all()
    )
    latest_balances = {}
    for acct_id, max_date in latest_dates.items():
        snap = AccountSnapshot.query.filter_by(account_id=acct_id, snapshot_date=max_date).first()
        if snap:
            latest_balances[acct_id] = float(snap.balance)

    totals = {cls: 0.0 for cls in ALLOCATION_CLASSES}
    for acct in inv_accounts:
        snap_bal = latest_balances.get(acct.id, 0.0)
        if acct.category in CASH_CATS:
            totals['cash'] += snap_bal
            continue
        h_result = _account_holding_splits(acct.id)
        if h_result is not None:
            splits, effective_bal = h_result
        else:
            splits = _get_account_allocations(acct.id)
            effective_bal = snap_bal
        if splits:
            for cls, pct in splits.items():
                if cls in totals:
                    totals[cls] += effective_bal * pct / 100.0

    CLASS_LABELS = {
        'domestic': 'Domestic',
        'international': 'International',
        'bonds': 'Bonds',
        'cash': 'Cash',
    }
    labels = [CLASS_LABELS.get(c, c.title()) for c in ALLOCATION_CLASSES if totals[c] > 0]
    values = [round(totals[c], 2) for c in ALLOCATION_CLASSES if totals[c] > 0]
    return jsonify({'labels': labels, 'values': values})


@main_bp.route('/api/account-balances/<int:account_id>')
def account_balances(account_id):
    """API endpoint: Get balance history for a specific account"""
    account = Account.query.get_or_404(account_id)
    snapshots = AccountSnapshot.query.filter_by(
        account_id=account_id
    ).order_by(AccountSnapshot.snapshot_date).all()
    data = {
        'account_id': account_id,
        'account_name': account.name,
        'account_color': account.display_color,
        'account_type': account.account_type,
        'category': account.category,
        'dates': [s.snapshot_date.strftime('%Y-%m-%d') for s in snapshots],
        'balances': [float(s.balance) for s in snapshots],
    }
    return jsonify(data)


@main_bp.route('/api/accounts/<int:account_id>/history')
def account_history_api(account_id):
    """JSON: last 24 months of balance history for sparkline rendering."""
    snapshots = (
        AccountSnapshot.query
        .filter_by(account_id=account_id)
        .order_by(AccountSnapshot.snapshot_date.desc())
        .limit(24)
        .all()
    )
    snapshots = list(reversed(snapshots))
    return jsonify({
        'history': [
            {'date': s.snapshot_date.strftime('%Y-%m-%d'), 'balance': float(s.balance)}
            for s in snapshots
        ]
    })


@main_bp.route('/accounts/<int:account_id>/history')
def account_history(account_id):
    """Balance history chart for a single account."""
    account = Account.query.get_or_404(account_id)
    snapshots = (
        AccountSnapshot.query
        .filter_by(account_id=account_id)
        .order_by(AccountSnapshot.snapshot_date)
        .all()
    )

    cagr = None
    if len(snapshots) >= 2:
        first, last = snapshots[0], snapshots[-1]
        years = (last.snapshot_date - first.snapshot_date).days / 365.25
        first_bal = float(first.balance)
        last_bal = float(last.balance)
        if years > 0 and first_bal > 0:
            cagr = ((last_bal / first_bal) ** (1 / years) - 1) * 100

    chart_dates = [s.snapshot_date.strftime('%Y-%m-%d') for s in snapshots]
    chart_balances = [float(s.balance) for s in snapshots]

    # Precompute MoM changes for the table: index matches snapshots list
    changes = [None] + [
        float(snapshots[i].balance - snapshots[i - 1].balance)
        for i in range(1, len(snapshots))
    ]

    return render_template(
        'account_history.html',
        account=account,
        snapshots=snapshots,
        changes=changes,
        cagr=cagr,
        chart_dates=chart_dates,
        chart_balances=chart_balances,
    )


@main_bp.route('/accounts/<int:account_id>/history/export')
def account_history_export(account_id):
    """Download balance history for a single account as CSV."""
    account = Account.query.get_or_404(account_id)
    snapshots = (
        AccountSnapshot.query
        .filter_by(account_id=account_id)
        .order_by(AccountSnapshot.snapshot_date)
        .all()
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['Month', 'Balance'])
    for s in snapshots:
        writer.writerow([s.snapshot_date.strftime('%b %Y'), float(s.balance)])
    safe_name = account.name.replace(' ', '_').replace('/', '_')
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename={safe_name}_history.csv'
    return response


@main_bp.route('/import', methods=['GET', 'POST'])
def import_data():
    """Data import interface — GET shows the form, POST processes the uploaded CSV."""
    if request.method == 'GET':
        from_onboarding = request.args.get('from') == 'onboarding'
        recent_logs = ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all()
        return render_template('import.html', recent_logs=recent_logs,
                               from_onboarding=from_onboarding)

    if 'csv_file' not in request.files or request.files['csv_file'].filename == '':
        return render_template('import.html',
                               error="No file selected.",
                               recent_logs=ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all())

    file = request.files['csv_file']
    filename = secure_filename(file.filename)

    if not filename.lower().endswith('.csv'):
        return render_template('import.html',
                               error="Only .csv files are supported.",
                               recent_logs=ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all())

    models = {
        'Account': Account,
        'AccountSnapshot': AccountSnapshot,
        'SpendingEntry': SpendingEntry,
        'CalculatedMetric': CalculatedMetric,
        'ImportLog': ImportLog,
    }

    file_stream = io.StringIO(file.read().decode('utf-8-sig'))
    logger.info('CSV import started: file=%s', filename)
    results = process_csv(file_stream, filename, db, models)

    # Recalculate metrics for every imported month (oldest first so each month's
    # change can reference the previous month's freshly-computed net worth).
    for month_date_str in results.get('month_dates', []):
        _recalculate_metrics(date.fromisoformat(month_date_str))

    if results.get('success'):
        logger.info(
            'CSV import complete: file=%s, accounts=%d, snapshots=%d, spending=%d, warnings=%d',
            filename, results['accounts_created'], results['snapshots_imported'],
            results['spending_imported'], len(results['warnings'])
        )
        if not results['warnings']:
            flash('Data imported successfully. Your dashboard is ready!', 'success')
            return redirect(url_for('main.index'))
    else:
        logger.error('CSV import failed: file=%s, errors=%s', filename, results['errors'])

    recent_logs = ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all()
    return render_template('import.html', results=results, recent_logs=recent_logs)


@main_bp.route('/import/template')
def import_template():
    """
    Download a blank CSV template in the Ledger export format so it can be
    filled in and re-imported.  Columns match export_csv exactly:
        Account | Type | Category | Tax Status | Institution | Jan 'YY ... Dec 'YY
    Account rows are seeded from active accounts (or two example rows when
    there are none). All balance cells are blank. Income and Expenses rows
    are always included.
    """
    year = datetime.today().year
    month_labels = [
        datetime(year, m, 1).strftime("%b '") + str(year)[-2:]
        for m in range(1, 13)
    ]
    META_COLS = ['Account', 'Type', 'Category', 'Tax Status', 'Institution']
    blank = [''] * 12

    rows = [META_COLS + month_labels]

    accounts = Account.query.filter_by(is_active=True).order_by(Account.display_order, Account.name).all()
    if accounts:
        for acct in accounts:
            rows.append([
                acct.name,
                acct.account_type,
                acct.category,
                acct.tax_status or '',
                acct.institution or '',
            ] + blank)
    else:
        rows.append(['Example Checking', 'asset', 'checking', 'taxable', ''] + blank)
        rows.append(['Example 401k', 'asset', '401k', 'tax_deferred', ''] + blank)

    rows.append(['Income', '', '', '', ''] + blank)
    rows.append(['Expenses', '', '', '', ''] + blank)

    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    filename = f'ledger_template_{year}.csv'
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


@main_bp.route('/export/csv')
def export_csv():
    """
    Export all account data as a Ledger-format CSV that can be re-imported
    into a fresh instance to restore full history.

    Query params:
        from  YYYY-MM  earliest month to include (default: all)
        to    YYYY-MM  latest month to include   (default: all)
    """
    from_str = request.args.get('from')
    to_str = request.args.get('to')

    # Collect all months that have snapshot or spending data
    snapshot_dates = {d for (d,) in db.session.query(AccountSnapshot.snapshot_date).distinct()}
    spending_dates = {d for (d,) in db.session.query(SpendingEntry.entry_date).distinct()}
    all_dates = sorted(snapshot_dates | spending_dates)

    if from_str:
        try:
            from_date = datetime.strptime(from_str, '%Y-%m').date().replace(day=1)
            all_dates = [d for d in all_dates if d >= from_date]
        except ValueError:
            pass
    if to_str:
        try:
            to_date = datetime.strptime(to_str, '%Y-%m').date().replace(day=1)
            all_dates = [d for d in all_dates if d <= to_date]
        except ValueError:
            pass

    if not all_dates:
        return make_response('No data to export.', 404)

    # Month header labels: "Jan '24"
    month_labels = [d.strftime("%b '") + d.strftime('%y') for d in all_dates]

    # Metadata column names
    META_COLS = ['Account', 'Type', 'Category', 'Tax Status', 'Institution']

    def fmt_currency(v):
        if v is None:
            return ''
        return '${:,.0f}'.format(float(v))

    def fmt_pct(v):
        if v is None:
            return ''
        return '{:.2f}%'.format(float(v))

    rows = []

    # Header row
    rows.append(META_COLS + month_labels)

    # One row per active account, ordered by display_order
    accounts = (
        Account.query
        .filter_by(is_active=True)
        .order_by(Account.display_order, Account.name)
        .all()
    )
    for acct in accounts:
        # Build balance lookup for this account
        snaps = {
            s.snapshot_date: s.balance
            for s in AccountSnapshot.query.filter_by(account_id=acct.id).all()
        }
        balances = [fmt_currency(snaps.get(d)) for d in all_dates]
        rows.append([
            acct.name,
            acct.account_type,
            acct.category,
            acct.tax_status or '',
            acct.institution or '',
        ] + balances)

    # Income row — aggregate all income entries per month
    income_by_month = {}
    for entry in SpendingEntry.query.filter_by(entry_type='income').all():
        if entry.entry_date in income_by_month:
            income_by_month[entry.entry_date] += float(entry.amount)
        else:
            income_by_month[entry.entry_date] = float(entry.amount)
    if income_by_month:
        rows.append(
            ['Income', '', '', '', ''] +
            [fmt_currency(income_by_month.get(d)) for d in all_dates]
        )

    # Expenses row — aggregate all expense entries per month
    expenses_by_month = {}
    for entry in SpendingEntry.query.filter_by(entry_type='expense').all():
        if entry.entry_date in expenses_by_month:
            expenses_by_month[entry.entry_date] += float(entry.amount)
        else:
            expenses_by_month[entry.entry_date] = float(entry.amount)
    if expenses_by_month:
        rows.append(
            ['Expenses', '', '', '', ''] +
            [fmt_currency(expenses_by_month.get(d)) for d in all_dates]
        )

    # Calculated metric rows
    metrics = {
        m.metric_date: m
        for m in CalculatedMetric.query.filter(
            CalculatedMetric.metric_date.in_(all_dates)
        ).all()
    }
    if any(m.net_worth is not None for m in metrics.values()):
        rows.append(
            ['Net Worth', '', '', '', ''] +
            [fmt_currency(metrics[d].net_worth if d in metrics else None) for d in all_dates]
        )
    if any(m.net_worth_non_re is not None for m in metrics.values()):
        rows.append(
            ['Net Worth Non-RE', '', '', '', ''] +
            [fmt_currency(metrics[d].net_worth_non_re if d in metrics else None) for d in all_dates]
        )
    if any(m.monthly_change_pct is not None for m in metrics.values()):
        rows.append(
            ['% Change', '', '', '', ''] +
            [fmt_pct(metrics[d].monthly_change_pct if d in metrics else None) for d in all_dates]
        )
    if any(m.save_rate is not None for m in metrics.values()):
        rows.append(
            ['Save Rate', '', '', '', ''] +
            [fmt_pct(metrics[d].save_rate if d in metrics else None) for d in all_dates]
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)

    if all_dates:
        start_str = all_dates[0].strftime('%Y-%m')
        end_str = all_dates[-1].strftime('%Y-%m')
        filename = f'ledger_{start_str}_{end_str}.csv'
    else:
        filename = 'ledger_export.csv'

    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    logger.info('CSV export: months=%d, accounts=%d', len(all_dates), len(accounts))
    return response


@main_bp.route('/api/import/preview', methods=['POST'])
def api_import_preview():
    """Parse an uploaded CSV and return a preview without writing to the DB."""
    if 'csv_file' not in request.files or request.files['csv_file'].filename == '':
        return jsonify({'error': 'No file provided.'}), 400

    file = request.files['csv_file']
    if not file.filename.lower().endswith('.csv'):
        return jsonify({'error': 'Only .csv files are supported.'}), 400

    models = {
        'Account': Account,
        'AccountSnapshot': AccountSnapshot,
        'SpendingEntry': SpendingEntry,
        'CalculatedMetric': CalculatedMetric,
        'ImportLog': ImportLog,
    }
    file_stream = io.StringIO(file.read().decode('utf-8-sig'))
    results = preview_csv(file_stream, models)
    return jsonify(results)


# ---------------------------------------------------------------------------
# CRUD API: Months
# ---------------------------------------------------------------------------

@main_bp.route('/api/months')
def api_months():
    """List all months with summary stats."""
    return jsonify(_build_month_list())


@main_bp.route('/api/months/init', methods=['POST'])
def api_month_init():
    """Initialize a new month by creating a CalculatedMetric stub."""
    data = request.get_json()
    if not data or 'month' not in data:
        return jsonify({'error': 'Missing month field'}), 400

    month_date = _parse_month_str(data['month'])
    if month_date is None:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
    if not metric:
        metric = CalculatedMetric(metric_date=month_date)
        db.session.add(metric)
        db.session.commit()
        logger.info('Month initialized: %s', data['month'])

    # Auto-populate active recurring entries (idempotent: skip if already present)
    templates = RecurringEntry.query.filter_by(is_active=True).order_by(
        RecurringEntry.display_order, RecurringEntry.id
    ).all()
    applied = skipped = 0
    for tmpl in templates:
        existing = SpendingEntry.query.filter_by(
            entry_date=month_date,
            account_name=tmpl.account_name,
            entry_type=tmpl.entry_type,
        ).first()
        if existing:
            skipped += 1
        else:
            db.session.add(SpendingEntry(
                entry_date=month_date,
                account_name=tmpl.account_name,
                amount=tmpl.amount,
                entry_type=tmpl.entry_type,
                notes=tmpl.notes or '',
            ))
            applied += 1
    if applied:
        db.session.commit()
        _recalculate_metrics(month_date)
        logger.info('Recurring entries applied for %s: applied=%d skipped=%d',
                    data['month'], applied, skipped)

    return jsonify({
        'month': month_date.strftime('%Y-%m'),
        'redirect': f'/monthly-update/{month_date.strftime("%Y-%m")}',
        'recurring_applied': applied,
        'recurring_skipped': skipped,
    })


@main_bp.route('/api/months/<month_str>/copy-from-previous', methods=['POST'])
def api_month_copy_from_previous(month_str):
    """Copy account snapshots from the most recent prior month into this month."""
    month_date = _parse_month_str(month_str)
    if month_date is None:
        return jsonify({'error': 'Invalid month format.'}), 400

    prior = (
        AccountSnapshot.query
        .filter(AccountSnapshot.snapshot_date < month_date)
        .order_by(AccountSnapshot.snapshot_date.desc())
        .first()
    )
    if prior is None:
        return jsonify({'error': 'No prior month with snapshots found.'}), 404

    source_date = prior.snapshot_date
    source_snapshots = AccountSnapshot.query.filter_by(snapshot_date=source_date).all()

    copied = skipped = 0
    for snap in source_snapshots:
        existing = AccountSnapshot.query.filter_by(
            account_id=snap.account_id, snapshot_date=month_date
        ).first()
        if existing:
            skipped += 1
        else:
            db.session.add(AccountSnapshot(
                account_id=snap.account_id,
                snapshot_date=month_date,
                balance=snap.balance
            ))
            copied += 1

    db.session.commit()
    _recalculate_metrics(month_date)
    logger.info('Month %s copied from %s: copied=%d, skipped=%d',
                month_str, source_date.strftime('%Y-%m'), copied, skipped)

    return jsonify({
        'copied': copied,
        'skipped': skipped,
        'source_month': source_date.strftime('%Y-%m'),
        'source_label': source_date.strftime('%B %Y'),
    })


@main_bp.route('/api/months/<month_str>', methods=['DELETE'])
def api_month_delete(month_str):
    """Delete all data (snapshots, spending, metrics) for a given month."""
    month_date = _parse_month_str(month_str)
    if month_date is None:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    snap_count = AccountSnapshot.query.filter_by(snapshot_date=month_date).count()
    spend_count = SpendingEntry.query.filter_by(entry_date=month_date).count()
    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()

    if snap_count == 0 and spend_count == 0 and metric is None:
        return jsonify({'error': f'No data found for month {month_str}'}), 404

    AccountSnapshot.query.filter_by(snapshot_date=month_date).delete()
    SpendingEntry.query.filter_by(entry_date=month_date).delete()
    if metric:
        db.session.delete(metric)
    db.session.commit()
    logger.info('Month deleted: %s (snapshots=%d, spending=%d)', month_str, snap_count, spend_count)

    return jsonify({'success': True, 'month': month_str})


# ---------------------------------------------------------------------------
# CRUD API: AccountSnapshot
# ---------------------------------------------------------------------------

@main_bp.route('/api/snapshots', methods=['POST'])
def api_snapshot_create():
    """Create or update an account snapshot for a given month."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    account_id = data.get('account_id')
    month_str = data.get('month')
    balance = data.get('balance')

    if account_id is None or month_str is None or balance is None:
        return jsonify({'error': 'account_id, month, and balance are required'}), 400

    month_date = _parse_month_str(month_str)
    if month_date is None:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    account = db.session.get(Account, account_id)
    if account is None:
        return jsonify({'error': 'Account not found'}), 404

    snapshot = AccountSnapshot.query.filter_by(
        account_id=account_id, snapshot_date=month_date
    ).first()

    if snapshot:
        snapshot.balance = balance
    else:
        snapshot = AccountSnapshot(
            account_id=account_id,
            snapshot_date=month_date,
            balance=balance
        )
        db.session.add(snapshot)

    db.session.commit()
    _recalculate_metrics(month_date)
    logger.info('Snapshot upserted: account_id=%d, month=%s', account_id, month_str)

    return jsonify({
        'id': snapshot.id,
        'account_id': snapshot.account_id,
        'account_name': account.name,
        'month': month_str,
        'balance': float(snapshot.balance),
    })


@main_bp.route('/api/snapshots/<int:snapshot_id>', methods=['PUT'])
def api_snapshot_update(snapshot_id):
    """Update an account snapshot's balance."""
    snapshot = db.session.get(AccountSnapshot, snapshot_id)
    if snapshot is None:
        return jsonify({'error': 'Snapshot not found'}), 404

    data = request.get_json()
    if not data or 'balance' not in data:
        return jsonify({'error': 'balance is required'}), 400

    snapshot.balance = data['balance']
    db.session.commit()
    _recalculate_metrics(snapshot.snapshot_date)
    logger.info('Snapshot updated: id=%d, account_id=%d, month=%s',
                snapshot_id, snapshot.account_id, snapshot.snapshot_date.strftime('%Y-%m'))

    return jsonify({
        'id': snapshot.id,
        'account_id': snapshot.account_id,
        'month': snapshot.snapshot_date.strftime('%Y-%m'),
        'balance': float(snapshot.balance),
    })


@main_bp.route('/api/snapshots/<int:snapshot_id>', methods=['DELETE'])
def api_snapshot_delete(snapshot_id):
    """Delete an account snapshot."""
    snapshot = db.session.get(AccountSnapshot, snapshot_id)
    if snapshot is None:
        return jsonify({'error': 'Snapshot not found'}), 404

    month_date = snapshot.snapshot_date
    db.session.delete(snapshot)
    db.session.commit()
    _recalculate_metrics(month_date)
    logger.info('Snapshot deleted: id=%d, month=%s', snapshot_id, month_date.strftime('%Y-%m'))

    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# CRUD API: SpendingEntry
# ---------------------------------------------------------------------------

@main_bp.route('/api/spending', methods=['POST'])
def api_spending_create():
    """Create a new spending entry."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    month_str = data.get('month')
    account_name = (data.get('account_name') or '').strip()
    amount = data.get('amount')
    entry_type = data.get('entry_type')

    if not month_str or not account_name or amount is None or entry_type not in ('income', 'expense'):
        return jsonify({'error': 'month, account_name, amount, and entry_type (income/expense) are required'}), 400

    month_date = _parse_month_str(month_str)
    if month_date is None:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    entry = SpendingEntry(
        entry_date=month_date,
        account_name=account_name,
        amount=amount,
        entry_type=entry_type,
        notes=data.get('notes', ''),
    )
    db.session.add(entry)
    db.session.commit()
    _recalculate_metrics(month_date)
    logger.info('Spending entry created: id=%d, month=%s, type=%s', entry.id, month_str, entry_type)

    return jsonify({
        'id': entry.id,
        'month': month_str,
        'account_name': entry.account_name,
        'amount': float(entry.amount),
        'entry_type': entry.entry_type,
        'notes': entry.notes or '',
    }), 201


@main_bp.route('/api/spending/<int:entry_id>', methods=['PUT'])
def api_spending_update(entry_id):
    """Update a spending entry."""
    entry = db.session.get(SpendingEntry, entry_id)
    if entry is None:
        return jsonify({'error': 'Spending entry not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    if 'account_name' in data:
        entry.account_name = data['account_name'].strip()
    if 'amount' in data:
        entry.amount = data['amount']
    if 'entry_type' in data:
        if data['entry_type'] not in ('income', 'expense'):
            return jsonify({'error': 'entry_type must be income or expense'}), 400
        entry.entry_type = data['entry_type']
    if 'notes' in data:
        entry.notes = data['notes']

    db.session.commit()
    _recalculate_metrics(entry.entry_date)
    logger.info('Spending entry updated: id=%d', entry_id)

    return jsonify({
        'id': entry.id,
        'month': entry.entry_date.strftime('%Y-%m'),
        'account_name': entry.account_name,
        'amount': float(entry.amount),
        'entry_type': entry.entry_type,
        'notes': entry.notes or '',
    })


@main_bp.route('/api/spending/<int:entry_id>', methods=['DELETE'])
def api_spending_delete(entry_id):
    """Delete a spending entry."""
    entry = db.session.get(SpendingEntry, entry_id)
    if entry is None:
        return jsonify({'error': 'Spending entry not found'}), 404

    month_date = entry.entry_date
    db.session.delete(entry)
    db.session.commit()
    _recalculate_metrics(month_date)
    logger.info('Spending entry deleted: id=%d', entry_id)

    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# CRUD API: RecurringEntry
# ---------------------------------------------------------------------------

def _recurring_to_dict(r):
    return {
        'id':            r.id,
        'account_name':  r.account_name,
        'amount':        float(r.amount),
        'entry_type':    r.entry_type,
        'notes':         r.notes or '',
        'is_active':     r.is_active,
        'display_order': r.display_order,
    }


@main_bp.route('/api/recurring-entries', methods=['GET'])
def api_recurring_entries_list():
    """List all recurring entry templates ordered by display_order, id."""
    rows = RecurringEntry.query.order_by(
        RecurringEntry.display_order, RecurringEntry.id
    ).all()
    return jsonify([_recurring_to_dict(r) for r in rows])


@main_bp.route('/api/recurring-entries', methods=['POST'])
def api_recurring_entry_create():
    """Create a new recurring entry template."""
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Missing request body'}), 400

    account_name = (data.get('account_name') or '').strip()
    if not account_name:
        return jsonify({'error': 'account_name is required'}), 400

    entry_type = data.get('entry_type', '')
    if entry_type not in ('income', 'expense'):
        return jsonify({'error': 'entry_type must be income or expense'}), 400

    try:
        amount = Decimal(str(data['amount']))
        if amount <= 0:
            raise ValueError
    except (KeyError, ValueError, Exception):
        return jsonify({'error': 'amount must be a positive number'}), 400

    entry = RecurringEntry(
        account_name=account_name,
        amount=amount,
        entry_type=entry_type,
        notes=(data.get('notes') or '').strip(),
        is_active=bool(data.get('is_active', True)),
        display_order=int(data.get('display_order', 0)),
    )
    db.session.add(entry)
    db.session.commit()
    logger.info('Recurring entry created: id=%d name=%s', entry.id, entry.account_name)
    return jsonify(_recurring_to_dict(entry)), 201


@main_bp.route('/api/recurring-entries/<int:entry_id>', methods=['PUT'])
def api_recurring_entry_update(entry_id):
    """Update a recurring entry template."""
    entry = db.session.get(RecurringEntry, entry_id)
    if entry is None:
        return jsonify({'error': 'Recurring entry not found'}), 404

    data = request.get_json() or {}

    if 'account_name' in data:
        account_name = data['account_name'].strip()
        if not account_name:
            return jsonify({'error': 'account_name cannot be empty'}), 400
        entry.account_name = account_name

    if 'entry_type' in data:
        if data['entry_type'] not in ('income', 'expense'):
            return jsonify({'error': 'entry_type must be income or expense'}), 400
        entry.entry_type = data['entry_type']

    if 'amount' in data:
        try:
            amount = Decimal(str(data['amount']))
            if amount <= 0:
                raise ValueError
            entry.amount = amount
        except (ValueError, Exception):
            return jsonify({'error': 'amount must be a positive number'}), 400

    if 'notes' in data:
        entry.notes = (data['notes'] or '').strip()
    if 'is_active' in data:
        entry.is_active = bool(data['is_active'])
    if 'display_order' in data:
        entry.display_order = int(data['display_order'])

    db.session.commit()
    logger.info('Recurring entry updated: id=%d', entry_id)
    return jsonify(_recurring_to_dict(entry))


@main_bp.route('/api/recurring-entries/<int:entry_id>', methods=['DELETE'])
def api_recurring_entry_delete(entry_id):
    """Delete a recurring entry template."""
    entry = db.session.get(RecurringEntry, entry_id)
    if entry is None:
        return jsonify({'error': 'Recurring entry not found'}), 404

    db.session.delete(entry)
    db.session.commit()
    logger.info('Recurring entry deleted: id=%d', entry_id)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# CRUD API: CalculatedMetric
# ---------------------------------------------------------------------------

@main_bp.route('/api/metrics/calculate/<month_str>', methods=['POST'])
def api_metrics_calculate(month_str):
    """Trigger auto-recalculation of metrics from snapshots and spending."""
    month_date = _parse_month_str(month_str)
    if month_date is None:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    metric = _recalculate_metrics(month_date)
    return jsonify(_metric_to_dict(metric))


@main_bp.route('/api/metrics/<month_str>', methods=['PUT'])
def api_metrics_update(month_str):
    """Manually override calculated metric fields for a month."""
    month_date = _parse_month_str(month_str)
    if month_date is None:
        return jsonify({'error': 'Invalid month format. Use YYYY-MM.'}), 400

    data = request.get_json()
    if not data:
        return jsonify({'error': 'Request body required'}), 400

    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
    if not metric:
        metric = CalculatedMetric(metric_date=month_date)
        db.session.add(metric)

    for field in ('total_assets', 'total_liabilities', 'net_worth', 'net_worth_non_re',
                  'monthly_change_amount', 'monthly_change_pct',
                  'total_income', 'total_expenses', 'save_rate'):
        if field in data:
            setattr(metric, field, data[field])

    db.session.commit()
    return jsonify(_metric_to_dict(metric))


# ---------------------------------------------------------------------------
# Projections page (items 15 & 19)
# ---------------------------------------------------------------------------

def _build_projections_context():
    """
    Gather historical data and default rates needed by the /projections page.
    Returns a dict ready to pass to render_template (all values JSON-safe).
    """
    # Historical net worth from CalculatedMetric
    metrics = (
        CalculatedMetric.query
        .filter(CalculatedMetric.net_worth.isnot(None))
        .order_by(CalculatedMetric.metric_date)
        .all()
    )

    historical_labels = [m.metric_date.strftime('%Y-%m') for m in metrics]
    historical_nw = [float(m.net_worth) for m in metrics]
    historical_nw_nonre = [
        float(m.net_worth_non_re) if m.net_worth_non_re is not None else None
        for m in metrics
    ]

    # Current balance per category (latest snapshot for each active account)
    accounts = Account.query.filter_by(is_active=True).all()
    current_by_category: dict[str, float] = {}
    for account in accounts:
        latest = (
            AccountSnapshot.query
            .filter_by(account_id=account.id)
            .order_by(AccountSnapshot.snapshot_date.desc())
            .first()
        )
        if latest:
            bal = float(latest.balance)
            current_by_category[account.category] = (
                current_by_category.get(account.category, 0.0) + bal
            )

    # Default CAGR per category from snapshot history
    snap_rows = (
        db.session.query(
            Account.category,
            AccountSnapshot.snapshot_date,
            AccountSnapshot.balance,
        )
        .join(Account, Account.id == AccountSnapshot.account_id)
        .filter(Account.is_active == True)
        .all()
    )
    snap_dicts = [
        {'category': r.category, 'snapshot_date': r.snapshot_date, 'balance': float(r.balance)}
        for r in snap_rows
    ]
    default_rates = proj.calculate_category_cagr(snap_dicts)
    # Convert to percentages for the UI; allow negatives for liabilities (declining balance is correct)
    default_rates_pct = {cat: round(r * 100, 2) for cat, r in default_rates.items()}

    # Portfolio-level CAGR from net_worth_non_re history
    portfolio_cagr = 0.0
    nonre_with_values = [(m.metric_date, float(m.net_worth_non_re))
                         for m in metrics if m.net_worth_non_re and float(m.net_worth_non_re) > 0]
    if len(nonre_with_values) >= 2:
        first_date, first_val = nonre_with_values[0]
        last_date, last_val = nonre_with_values[-1]
        years = (last_date - first_date).days / 365.25
        portfolio_cagr = proj.calculate_cagr(first_val, last_val, years)

    # Latest investable NW and average save rate for FI tab defaults
    current_investable_nw = float(metrics[-1].net_worth_non_re) if (
        metrics and metrics[-1].net_worth_non_re is not None
    ) else 0.0

    save_rates = [float(m.save_rate) for m in metrics if m.save_rate is not None]
    avg_save_rate = sum(save_rates) / len(save_rates) if save_rates else 0.0

    # Latest total income for estimating monthly savings default
    latest_income = float(metrics[-1].total_income) if (
        metrics and metrics[-1].total_income
    ) else 0.0
    latest_expenses = float(metrics[-1].total_expenses) if (
        metrics and metrics[-1].total_expenses
    ) else 0.0
    default_monthly_savings = max(0.0, latest_income - latest_expenses)

    today = date.today()

    return {
        'historical_labels': historical_labels,
        'historical_nw': historical_nw,
        'historical_nw_nonre': historical_nw_nonre,
        'current_by_category': current_by_category,
        'default_rates_pct': default_rates_pct,
        'portfolio_cagr_pct': round(portfolio_cagr * 100, 2),
        'current_investable_nw': current_investable_nw,
        'avg_save_rate': round(avg_save_rate, 1),
        'default_monthly_savings': default_monthly_savings,
        'today_str': today.strftime('%Y-%m'),
        'has_data': len(metrics) > 0,
    }


@main_bp.route('/projections')
def projections():
    ctx = _build_projections_context()
    return render_template('projections.html', **ctx)


# ---------------------------------------------------------------------------
# Allocation targets (app_settings key='allocation_targets')
# ---------------------------------------------------------------------------

_ALLOC_TARGET_KEY = 'allocation_targets'
_ALLOC_TARGET_DEFAULTS = {'domestic': 70.0, 'international': 15.0, 'bonds': 10.0, 'cash': 5.0}


def _get_allocation_targets():
    """Return {class: float} from app_settings, falling back to defaults."""
    import json
    row = AppSetting.query.filter_by(key=_ALLOC_TARGET_KEY).first()
    if not row or not row.value:
        return dict(_ALLOC_TARGET_DEFAULTS)
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return dict(_ALLOC_TARGET_DEFAULTS)


def _set_allocation_targets(targets: dict):
    """Upsert allocation targets into app_settings."""
    import json
    row = AppSetting.query.filter_by(key=_ALLOC_TARGET_KEY).first()
    if not row:
        row = AppSetting(key=_ALLOC_TARGET_KEY, description='Portfolio asset class target percentages')
        db.session.add(row)
    row.value = json.dumps(targets)
    db.session.commit()


def _get_app_setting(key: str, default: str = '') -> str:
    """Read a single key from AppSetting, returning default if absent or empty."""
    row = AppSetting.query.filter_by(key=key).first()
    return row.value if row and row.value else default


def _set_app_setting(key: str, value: str, description: str = '') -> None:
    """Upsert a single key in AppSetting."""
    row = AppSetting.query.filter_by(key=key).first()
    if not row:
        row = AppSetting(key=key, description=description)
        db.session.add(row)
    row.value = value
    db.session.commit()


def _account_holding_splits(account_id: int) -> tuple[dict[str, float], float] | None:
    """
    If an account has active holdings with allocation splits, compute effective
    allocation percentages from holdings (Phase 2 data source).

    Returns (splits_pct, total_market_value) if holdings exist and have splits,
    or None to fall back to AssetAllocation × AccountSnapshot (Phase 1).

    The percentages are weighted by each holding's market value so the result
    represents the account's true allocation, not an unweighted average.
    The total_market_value should be used as the effective balance instead of
    the snapshot balance so that allocation amounts reflect live prices.
    """
    holdings = Holding.query.filter_by(account_id=account_id, is_active=True).all()
    if not holdings:
        return None

    # Only use Phase 2 if at least one holding has a price and splits
    class_values: dict[str, float] = {cls: 0.0 for cls in ALLOCATION_CLASSES}
    total_value = 0.0
    any_split = False

    for h in holdings:
        if h.last_price is None or h.shares is None:
            continue
        value = float(h.shares) * float(h.last_price)
        splits = {ha.asset_class: float(ha.percentage) for ha in h.allocations.all()}
        if not splits:
            continue
        any_split = True
        total_value += value
        for cls, pct in splits.items():
            if cls in class_values:
                class_values[cls] += value * pct / 100.0

    if not any_split or total_value == 0:
        return None

    splits_pct = {cls: class_values[cls] / total_value * 100.0 for cls in ALLOCATION_CLASSES}
    return splits_pct, total_value


def _compute_holdings_value(account_id: int) -> float | None:
    """Return total market value of active holdings, or None if no priced holdings exist."""
    holdings = Holding.query.filter_by(account_id=account_id, is_active=True).all()
    total, found = 0.0, False
    for h in holdings:
        if h.shares is not None and h.last_price is not None:
            total += float(h.shares) * float(h.last_price)
            found = True
    return total if found else None


def _holdings_discrepancy(holdings_value: float | None, snapshot_balance: float | None) -> bool:
    """Return True if holdings value differs from snapshot by more than 1%."""
    if holdings_value is None or snapshot_balance is None or snapshot_balance == 0:
        return False
    return abs(holdings_value - snapshot_balance) / abs(snapshot_balance) > 0.01


@main_bp.route('/allocation')
def allocation():
    """Portfolio-wide asset allocation dashboard."""
    from app.price_service import is_stale
    targets = _get_allocation_targets()

    # Investment + cash accounts with snapshots
    all_cats = INVESTMENT_CATS | CASH_CATS
    inv_accounts = (
        Account.query
        .filter(Account.category.in_(all_cats))
        .filter_by(is_active=True, account_type='asset', include_in_networth=True)
        .order_by(Account.name)
        .all()
    )

    # Latest snapshot balance per account
    latest_dates = dict(
        db.session.query(AccountSnapshot.account_id, func.max(AccountSnapshot.snapshot_date))
        .filter(AccountSnapshot.account_id.in_([a.id for a in inv_accounts]))
        .group_by(AccountSnapshot.account_id)
        .all()
    )
    latest_balances = {}
    for acct_id, max_date in latest_dates.items():
        snap = AccountSnapshot.query.filter_by(account_id=acct_id, snapshot_date=max_date).first()
        if snap:
            latest_balances[acct_id] = float(snap.balance)

    # Per-account allocation splits:
    # CASH_CATS → automatic 100% cash
    # Phase 2 (holdings) takes precedence over Phase 1 (AssetAllocation × snapshot)
    account_splits = {}         # {account_id: {class: pct}}
    account_source = {}         # {account_id: 'cash'|'holdings'|'allocation'|None}
    account_eff_balances = {}   # {account_id: effective balance for allocation math}
    for acct in inv_accounts:
        bal = latest_balances.get(acct.id, 0.0)
        if acct.category in CASH_CATS:
            account_splits[acct.id] = {'cash': 100.0}
            account_source[acct.id] = 'cash'
            account_eff_balances[acct.id] = bal
            continue
        h_result = _account_holding_splits(acct.id)
        if h_result is not None:
            h_splits, h_value = h_result
            account_splits[acct.id] = h_splits
            account_source[acct.id] = 'holdings'
            account_eff_balances[acct.id] = h_value  # use live market value
        else:
            a_splits = _get_account_allocations(acct.id)
            account_splits[acct.id] = a_splits
            account_source[acct.id] = 'allocation' if a_splits else None
            account_eff_balances[acct.id] = bal

    # Aggregate totals per asset class
    totals = {cls: 0.0 for cls in ALLOCATION_CLASSES}
    unclassified = 0.0
    by_account = []

    for acct in inv_accounts:
        bal = account_eff_balances.get(acct.id, 0.0)
        splits = account_splits.get(acct.id, {})
        contributions = {}
        if splits:
            for cls, pct in splits.items():
                amt = bal * pct / 100.0
                contributions[cls] = amt
                if cls in totals:
                    totals[cls] += amt
        else:
            unclassified += bal
        by_account.append({
            'account':      acct,
            'balance':      bal,
            'splits':       splits,
            'contributions': contributions,
            'source':       account_source.get(acct.id),
        })

    total_invested = sum(totals.values()) + unclassified
    actuals = {
        cls: (totals[cls] / total_invested * 100.0) if total_invested > 0 else 0.0
        for cls in ALLOCATION_CLASSES
    }
    drifts = {cls: actuals[cls] - targets.get(cls, 0.0) for cls in ALLOCATION_CLASSES}
    gaps   = {cls: (targets.get(cls, 0.0) / 100.0 * total_invested) - totals[cls]
              for cls in ALLOCATION_CLASSES}

    max_abs_drift = max(abs(d) for d in drifts.values()) if drifts else 0
    if max_abs_drift <= 2:
        status = 'on_target'
    elif max_abs_drift <= 5:
        status = 'review'
    else:
        status = 'rebalance'

    # Staleness: is any holding price stale (>24 h old)?
    stale_holdings = (
        Holding.query
        .filter_by(is_active=True)
        .filter(Holding.account_id.in_([a.id for a in inv_accounts]))
        .all()
    )
    prices_stale = any(is_stale(h.last_fetched) for h in stale_holdings) if stale_holdings else False
    holdings_count = len(stale_holdings)

    # Build flat holdings list for the "All Holdings" table
    acct_map = {a.id: a for a in inv_accounts}

    # Batch-load TickerClassification for all tickers in this set
    tickers_in_holdings = {h.ticker for h in stale_holdings}
    classifications = {
        tc.ticker: tc.market_cap_tilt
        for tc in TickerClassification.query.filter(
            TickerClassification.ticker.in_(tickers_in_holdings)
        ).all()
    } if tickers_in_holdings else {}

    all_holdings_rows = []
    for h in sorted(stale_holdings, key=lambda x: (
            float(x.shares or 0) * float(x.last_price or 0)), reverse=True):
        value = (float(h.shares) * float(h.last_price)
                 if h.shares is not None and h.last_price is not None else None)
        alloc_splits = {ha.asset_class: float(ha.percentage) for ha in h.allocations.all()}
        acct = acct_map.get(h.account_id)
        all_holdings_rows.append({
            'holding': h,
            'account_name': acct.name if acct else '—',
            'account_institution': acct.institution if acct else '',
            'value': value,
            'alloc_splits': alloc_splits,
            'is_stale': is_stale(h.last_fetched),
            'cap_class': h.cap_class or classifications.get(h.ticker),
        })

    # Market cap distribution across all priced holdings
    cap_classes_ordered = ['large', 'mid', 'small', 'other']
    cap_totals = {c: 0.0 for c in cap_classes_ordered}
    cap_invested_total = 0.0
    for row in all_holdings_rows:
        if row['value'] is None:
            continue
        bucket = row['cap_class'] if row['cap_class'] in ('large', 'mid', 'small') else 'other'
        cap_totals[bucket] += row['value']
        cap_invested_total += row['value']
    cap_pcts = {
        k: (v / cap_invested_total * 100.0) if cap_invested_total > 0 else 0.0
        for k, v in cap_totals.items()
    }

    # YTD cash flow summary for Cash Flow tab
    current_year = date.today().year
    ytd_metrics = CalculatedMetric.query.filter(
        CalculatedMetric.metric_date >= date(current_year, 1, 1),
        CalculatedMetric.metric_date <= date(current_year, 12, 31),
    ).all()
    ytd_income   = sum(float(m.total_income)   for m in ytd_metrics if m.total_income)
    ytd_expenses = sum(float(m.total_expenses) for m in ytd_metrics if m.total_expenses)
    ytd_save_rate = ((ytd_income - ytd_expenses) / ytd_income * 100) if ytd_income > 0 else None

    return render_template(
        'allocation.html',
        targets=targets,
        actuals=actuals,
        totals=totals,
        drifts=drifts,
        gaps=gaps,
        total_invested=total_invested,
        unclassified=unclassified,
        by_account=by_account,
        status=status,
        allocation_classes=ALLOCATION_CLASSES,
        actuals_list=[actuals.get(c, 0) for c in ALLOCATION_CLASSES],
        targets_list=[targets.get(c, 0) for c in ALLOCATION_CLASSES],
        prices_stale=prices_stale,
        holdings_count=holdings_count,
        all_holdings_rows=all_holdings_rows,
        cap_classes=cap_classes_ordered,
        cap_totals=cap_totals,
        cap_pcts=cap_pcts,
        cap_invested_total=cap_invested_total,
        ytd_income=ytd_income,
        ytd_expenses=ytd_expenses,
        ytd_save_rate=ytd_save_rate,
        ytd_year=current_year,
    )


@main_bp.route('/api/allocation/targets', methods=['POST'])
def api_allocation_targets_save():
    """Save target allocation percentages."""
    data = request.get_json(silent=True) or {}
    targets = {}
    for cls in ALLOCATION_CLASSES:
        try:
            targets[cls] = float(data.get(cls, 0))
        except (ValueError, TypeError):
            targets[cls] = 0.0
    total = sum(targets.values())
    if abs(total - 100.0) > 0.5:
        return jsonify({'error': f'Percentages must sum to 100 (got {total:.1f})'}), 400
    _set_allocation_targets(targets)
    return jsonify({'ok': True, 'targets': targets})


_INVEST_CATS = {'retirement', 'brokerage', '401k', 'ira', 'roth_ira', 'hsa', '529', 'investment'}
_CASH_CATS   = {'cash', 'checking', 'savings'}
_LIAB_CATS   = {'mortgage', 'credit_card', 'loan'}


def _compute_income_contributions(
    income: dict,
    current_by_category: dict[str, float],
) -> tuple[dict[str, float], float]:
    """
    Convert income/savings params into a per-category monthly contribution dict.

    Returns (monthly_contributions, monthly_total) where monthly_total is the
    total monthly savings amount (for display in callouts).

    income keys:
        mode          'gross' | 'net'
        gross_annual  float (gross mode)
        tax_rate_pct  float (gross mode, e.g. 22.0)
        net_annual    float (net mode)
        save_rate_pct float  0–100
        distribution  'split' | 'pro_rata' | 'all_invested' | 'all_cash'
        invest_pct    float  0–100 (split mode only)
    """
    mode = income.get('mode', 'gross')
    save_rate = max(0.0, min(100.0, float(income.get('save_rate_pct', 0)))) / 100.0

    if mode == 'net':
        net_annual = float(income.get('net_annual', 0) or 0)
    else:
        gross = float(income.get('gross_annual', 0) or 0)
        tax_rate = max(0.0, min(60.0, float(income.get('tax_rate_pct', 0) or 0))) / 100.0
        net_annual = gross * (1.0 - tax_rate)

    monthly_total = (net_annual * save_rate) / 12.0
    if monthly_total <= 0:
        return {}, 0.0

    distribution = income.get('distribution', 'split')
    invest_pct = max(0.0, min(100.0, float(income.get('invest_pct', 80) or 80))) / 100.0

    # Determine per-category weights based on distribution mode
    asset_cats = {c: v for c, v in current_by_category.items() if c not in _LIAB_CATS}

    if distribution == 'all_invested':
        invest_pct = 1.0
        cash_pct = 0.0
    elif distribution == 'all_cash':
        invest_pct = 0.0
        cash_pct = 1.0
    elif distribution == 'pro_rata':
        # Distribute proportional to all asset balances (no invest/cash split)
        total_assets = sum(max(0.0, v) for v in asset_cats.values())
        if total_assets <= 0:
            return {}, 0.0
        return (
            {cat: monthly_total * max(0.0, bal) / total_assets
             for cat, bal in asset_cats.items()},
            monthly_total,
        )
    else:  # split
        cash_pct = 1.0 - invest_pct

    contributions: dict[str, float] = {}

    # Invested portion → investment categories weighted by current balance
    if invest_pct > 0:
        inv_cats = {c: max(0.0, v) for c, v in asset_cats.items() if c in _INVEST_CATS}
        inv_total = sum(inv_cats.values())
        if inv_total > 0:
            inv_amount = monthly_total * invest_pct
            for cat, bal in inv_cats.items():
                contributions[cat] = contributions.get(cat, 0.0) + inv_amount * bal / inv_total
        else:
            # Fallback: put invest portion into cash if no invest categories exist
            cash_pct += invest_pct

    # Saved portion → cash/savings categories weighted by current balance
    if cash_pct > 0:
        csh_cats = {c: max(0.0, v) for c, v in asset_cats.items() if c in _CASH_CATS}
        csh_total = sum(csh_cats.values())
        if csh_total > 0:
            csh_amount = monthly_total * cash_pct
            for cat, bal in csh_cats.items():
                contributions[cat] = contributions.get(cat, 0.0) + csh_amount * bal / csh_total
        else:
            # Fallback: put cash portion into first invest category
            if invest_pct > 0 and contributions:
                first_cat = next(iter(contributions))
                contributions[first_cat] = contributions.get(first_cat, 0.0) + monthly_total * cash_pct

    return contributions, monthly_total


@main_bp.route('/api/projections/growth', methods=['POST'])
def api_projections_growth():
    """
    Compute a growth projection and return chart data + callouts.

    Request JSON:
        rates         {category: annual_pct_rate}  e.g. {"retirement": 7.0}
        horizon_years int  (1–30)
        mortgage      optional {annual_rate: float (%), remaining_months: int}
        current_age   optional int
        income        optional {mode, gross_annual, tax_rate_pct, net_annual,
                                save_rate_pct, distribution, invest_pct}
        expenses      optional [{name, amount, year}]

    Response JSON:
        labels                list[str]  'YYYY-MM'
        total_projected       list[float]
        by_category           {category: list[float]}
        callouts              {yr5, yr10, yr20, yr30, doubles_date,
                               mortgage_payoff_date, retirement_nw, retirement_label}
        monthly_contribution  float  total monthly savings amount
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    rates_pct: dict = data.get('rates', {})
    horizon_years = int(data.get('horizon_years', 20))
    horizon_years = max(1, min(30, horizon_years))
    horizon_months = horizon_years * 12

    mortgage_raw = data.get('mortgage')
    mortgage_params = None
    if mortgage_raw:
        mortgage_params = {
            'annual_rate': float(mortgage_raw.get('annual_rate', 6.5)) / 100,
            'remaining_months': int(mortgage_raw.get('remaining_months', 360)),
        }

    # Build current balances
    accounts = Account.query.filter_by(is_active=True).all()
    current_by_category: dict[str, float] = {}
    for account in accounts:
        latest = (
            AccountSnapshot.query
            .filter_by(account_id=account.id)
            .order_by(AccountSnapshot.snapshot_date.desc())
            .first()
        )
        if latest:
            bal = float(latest.balance)
            current_by_category[account.category] = (
                current_by_category.get(account.category, 0.0) + bal
            )

    # Convert pct rates to decimals
    annual_rates = {cat: float(pct) / 100 for cat, pct in rates_pct.items()}

    # Income → monthly contributions per category
    income_raw = data.get('income')
    monthly_contributions: dict[str, float] = {}
    monthly_contribution_total = 0.0
    if income_raw:
        monthly_contributions, monthly_contribution_total = _compute_income_contributions(
            income_raw, current_by_category
        )

    # Planned expenses → convert year to month offset from today
    today = date.today().replace(day=1)
    planned_expenses: list[dict] = []
    for exp in (data.get('expenses') or []):
        try:
            exp_year = int(exp.get('year', 0))
            if exp_year <= 0:
                continue
            month_offset = (exp_year - today.year) * 12 - today.month + 1
            if 0 < month_offset <= horizon_months:
                planned_expenses.append({
                    'month_offset': month_offset,
                    'amount': max(0.0, float(exp.get('amount', 0) or 0)),
                    'name': str(exp.get('name', 'Expense'))[:50],
                })
        except (TypeError, ValueError):
            continue

    result = proj.project_growth(
        current_by_category, annual_rates, horizon_months,
        mortgage_params=mortgage_params,
        monthly_contributions=monthly_contributions or None,
        planned_expenses=planned_expenses or None,
    )

    proj_labels = proj.build_month_labels(today, horizon_months)

    current_age = data.get('current_age')
    if current_age is not None:
        current_age = int(current_age)

    mort_series = result['by_category'].get('mortgage')
    callouts = proj.growth_callouts(
        result['total'], today,
        mortgage_series=mort_series,
        current_age=current_age,
    )
    for key in ('yr5', 'yr10', 'yr20', 'yr30', 'retirement_nw'):
        if callouts.get(key) is not None:
            callouts[key] = round(callouts[key])

    return jsonify({
        'labels': proj_labels,
        'total_projected': [round(v) for v in result['total']],
        'by_category': {cat: [round(v) for v in vals]
                        for cat, vals in result['by_category'].items()},
        'callouts': callouts,
        'monthly_contribution': round(monthly_contribution_total),
        'planned_expenses': [
            {'month_offset': e['month_offset'], 'name': e['name'], 'amount': e['amount']}
            for e in planned_expenses
        ],
    })


@main_bp.route('/api/projections/fi', methods=['POST'])
def api_projections_fi():
    """
    Compute FI projections for up to 3 scenarios.

    Request JSON:
        scenarios  list of {name, spending, swr, growth_rate, monthly_savings}
                   swr and growth_rate are percentages (e.g. 4.0 = 4%)
        current_age  optional int

    Response JSON:
        current_investable_nw  float
        scenarios  list of {name, fi_number, fi_date, years_to_fi, age_at_fi, projection}
        sensitivity {swr_labels, spending_labels, data}
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    scenarios_raw = data.get('scenarios')
    if not scenarios_raw or not isinstance(scenarios_raw, list):
        return jsonify({'error': 'scenarios list required'}), 400

    current_age = data.get('current_age')

    # Latest investable NW
    latest_metric = (
        CalculatedMetric.query
        .filter(CalculatedMetric.net_worth_non_re.isnot(None))
        .order_by(CalculatedMetric.metric_date.desc())
        .first()
    )
    current_nw = float(latest_metric.net_worth_non_re) if latest_metric else 0.0

    today = date.today().replace(day=1)
    projection_months = 50 * 12  # 50 years max display

    scenario_results = []
    for raw in scenarios_raw[:3]:
        spending = float(raw.get('spending', 80_000))
        swr = float(raw.get('swr', 4.0)) / 100
        growth_rate = float(raw.get('growth_rate', 7.0)) / 100
        monthly_savings = float(raw.get('monthly_savings', 0))
        name = str(raw.get('name', 'Scenario'))[:50]

        fi_target = proj.fi_number(spending, swr)
        m = proj.months_to_fi(current_nw, fi_target, monthly_savings, growth_rate)
        fi_date = None
        years_to_fi = None
        age_at_fi = None
        if m is not None:
            fi_date = (today + relativedelta(months=m)).strftime('%Y-%m')
            years_to_fi = round(m / 12, 1)
            if current_age:
                age_at_fi = round(current_age + m / 12, 1)

        series = proj.project_fi_series(current_nw, monthly_savings, growth_rate, projection_months)

        scenario_results.append({
            'name': name,
            'fi_number': round(fi_target) if fi_target != float('inf') else None,
            'fi_date': fi_date,
            'years_to_fi': years_to_fi,
            'age_at_fi': age_at_fi,
            'projection': [round(v) for v in series],
        })

    # Sensitivity table uses the active scenario (sent as active_index, position in submitted list)
    active_idx = max(0, min(int(data.get('active_index', 0)), len(scenarios_raw) - 1))
    active_raw = scenarios_raw[active_idx]
    sens_growth = float(active_raw.get('growth_rate', 7.0)) / 100
    sens_savings = float(active_raw.get('monthly_savings', 0))
    sens_spending = float(active_raw.get('spending', 80_000))
    swr_range = [0.025, 0.030, 0.035, 0.040, 0.045, 0.050]
    sensitivity = proj.fi_sensitivity_table_growth(
        current_nw, sens_savings, sens_spending, sens_growth, swr_range
    )

    proj_labels = proj.build_month_labels(today, projection_months)

    return jsonify({
        'current_investable_nw': round(current_nw),
        'labels': proj_labels,
        'scenarios': scenario_results,
        'sensitivity': sensitivity,
    })


# ---------------------------------------------------------------------------
# Holdings CRUD API  (#7)
# ---------------------------------------------------------------------------

def _holding_to_dict(h: Holding) -> dict:
    """Serialize a Holding row + its HoldingAllocation splits."""
    value = None
    if h.shares is not None and h.last_price is not None:
        value = round(float(h.shares) * float(h.last_price), 2)
    allocs = {ha.asset_class: float(ha.percentage) for ha in h.allocations.all()}
    return {
        'id':           h.id,
        'account_id':   h.account_id,
        'ticker':       h.ticker,
        'name':         h.name or h.ticker,
        'shares':       float(h.shares),
        'last_price':   float(h.last_price) if h.last_price is not None else None,
        'last_fetched': h.last_fetched.isoformat() if h.last_fetched else None,
        'is_active':    h.is_active,
        'cap_class':    h.cap_class,
        'value':        value,
        'allocations':  allocs,
    }


@main_bp.route('/api/accounts/<int:account_id>/holdings')
def api_holdings_list(account_id):
    """List active holdings for an account."""
    account = db.session.get(Account, account_id)
    if account is None:
        return jsonify({'error': 'Account not found'}), 404
    holdings = (
        Holding.query
        .filter_by(account_id=account_id, is_active=True)
        .order_by(Holding.ticker)
        .all()
    )
    return jsonify([_holding_to_dict(h) for h in holdings])


@main_bp.route('/api/accounts/<int:account_id>/holdings', methods=['POST'])
def api_holding_create(account_id):
    """Create a new holding (+ allocation splits) for an account."""
    account = db.session.get(Account, account_id)
    if account is None:
        return jsonify({'error': 'Account not found'}), 404

    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'error': 'ticker is required'}), 400

    try:
        shares = float(data.get('shares', 0))
        if shares < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'shares must be a non-negative number'}), 400

    allocs_raw: dict = data.get('allocations') or {}
    allocs = {}
    for cls in ALLOCATION_CLASSES:
        try:
            allocs[cls] = float(allocs_raw.get(cls, 0))
        except (ValueError, TypeError):
            allocs[cls] = 0.0
    alloc_sum = sum(allocs.values())
    if alloc_sum > 0 and abs(alloc_sum - 100.0) > 0.5:
        return jsonify({'error': f'Allocation percentages must sum to 100 (got {alloc_sum:.1f})'}), 400

    holding = Holding(
        account_id=account_id,
        ticker=ticker,
        name=data.get('name') or ticker,
        shares=shares,
        cap_class=data.get('cap_class') or None,
    )
    db.session.add(holding)
    db.session.flush()  # populate holding.id

    # Fetch price immediately so the holding has a live value right away
    try:
        from app.price_service import get_price
        from datetime import datetime, timezone
        price, fetched_name = get_price(ticker)
        holding.last_price = price
        holding.name = fetched_name
        holding.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception as e:
        logger.warning('Price fetch on holding create failed: ticker=%s error=%s', ticker, e)

    for cls, pct in allocs.items():
        if pct > 0:
            db.session.add(HoldingAllocation(holding_id=holding.id, asset_class=cls, percentage=pct))

    db.session.commit()
    logger.info('Holding created: account_id=%d ticker=%s shares=%s price=%s',
                account_id, ticker, shares, holding.last_price)
    return jsonify(_holding_to_dict(holding)), 201


@main_bp.route('/api/holdings/<int:holding_id>', methods=['PUT'])
def api_holding_update(holding_id):
    """Update shares and/or allocation splits for a holding."""
    holding = db.session.get(Holding, holding_id)
    if holding is None:
        return jsonify({'error': 'Holding not found'}), 404

    data = request.get_json(silent=True) or {}

    if 'shares' in data:
        try:
            holding.shares = float(data['shares'])
        except (ValueError, TypeError):
            return jsonify({'error': 'shares must be a number'}), 400

    if 'name' in data:
        holding.name = (data['name'] or '').strip() or holding.ticker

    if 'cap_class' in data:
        holding.cap_class = data['cap_class'] or None

    if 'allocations' in data:
        allocs_raw = data['allocations'] or {}
        allocs = {}
        for cls in ALLOCATION_CLASSES:
            try:
                allocs[cls] = float(allocs_raw.get(cls, 0))
            except (ValueError, TypeError):
                allocs[cls] = 0.0
        alloc_sum = sum(allocs.values())
        if alloc_sum > 0 and abs(alloc_sum - 100.0) > 0.5:
            return jsonify({'error': f'Allocation percentages must sum to 100 (got {alloc_sum:.1f})'}), 400
        HoldingAllocation.query.filter_by(holding_id=holding.id).delete()
        for cls, pct in allocs.items():
            if pct > 0:
                db.session.add(HoldingAllocation(holding_id=holding.id, asset_class=cls, percentage=pct))

    db.session.commit()
    logger.info('Holding updated: id=%d', holding_id)
    return jsonify(_holding_to_dict(holding))


@main_bp.route('/api/holdings/<int:holding_id>', methods=['DELETE'])
def api_holding_archive(holding_id):
    """Archive (soft-delete) a holding."""
    holding = db.session.get(Holding, holding_id)
    if holding is None:
        return jsonify({'error': 'Holding not found'}), 404
    holding.is_active = False
    db.session.commit()
    logger.info('Holding archived: id=%d ticker=%s', holding_id, holding.ticker)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Price service API  (#8)
# ---------------------------------------------------------------------------

@main_bp.route('/api/prices/<ticker>')
def api_price_lookup(ticker):
    """
    Fetch current price + name for a single ticker.
    Used by the holdings add-row UI to auto-populate price and name on blur.
    """
    from app.price_service import get_price
    ticker = ticker.strip().upper()
    if not ticker:
        return jsonify({'error': 'ticker required'}), 400
    try:
        price, name = get_price(ticker)
        return jsonify({'ticker': ticker, 'price': price, 'name': name})
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        logger.warning('Price fetch failed: ticker=%s error=%s', ticker, e)
        return jsonify({'error': f'Price fetch failed: {e}'}), 502


@main_bp.route('/api/prices/refresh', methods=['POST'])
def api_prices_refresh():
    """
    Refresh prices for all active holdings.
    Skips holdings whose last_fetched is within the last 24 hours.
    Returns {updated, skipped, failed, errors}.
    """
    from app.price_service import get_price, is_stale
    from datetime import datetime, timezone

    holdings = Holding.query.filter_by(is_active=True).all()
    updated = skipped = failed = 0
    errors = []

    for h in holdings:
        if not is_stale(h.last_fetched):
            skipped += 1
            continue
        try:
            price, name = get_price(h.ticker)
            h.last_price = price
            h.name = name
            h.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
            updated += 1
        except Exception as e:
            failed += 1
            errors.append({'ticker': h.ticker, 'error': str(e)})
            logger.warning('Price refresh failed: ticker=%s error=%s', h.ticker, e)

    db.session.commit()
    logger.info('Price refresh: updated=%d skipped=%d failed=%d', updated, skipped, failed)
    return jsonify({'updated': updated, 'skipped': skipped, 'failed': failed, 'errors': errors})


# ---------------------------------------------------------------------------
# Ticker Classification API  (#claude-classification)
# ---------------------------------------------------------------------------

@main_bp.route('/api/classify/<ticker>')
def api_classify_ticker(ticker):
    """
    Classify a ticker's asset class, market cap tilt, and allocation weights.
    Results are cached in ticker_classifications table.

    Query params:
        web_search=1   Enable Claude web search for obscure tickers
        force=1        Bypass DB cache and re-classify

    Response 200: {ticker, asset_class, market_cap_tilt, sector_weights, source, from_cache}
    Response 503: {error, manual_required: true}  — feature disabled or API unavailable
    """
    from app.classification_service import get_or_classify

    if _get_app_setting('claude_classification_enabled', 'false') != 'true':
        return jsonify({'error': 'AI classification is disabled', 'manual_required': True}), 503

    api_key = _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY is not configured', 'manual_required': True}), 503

    ticker = ticker.strip().upper()
    if not ticker:
        return jsonify({'error': 'ticker required'}), 400

    use_web_search = request.args.get('web_search', '').lower() in ('1', 'true')

    if request.args.get('force', '').lower() in ('1', 'true'):
        existing = TickerClassification.query.filter_by(ticker=ticker).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

    try:
        result, from_cache = get_or_classify(ticker, api_key, use_web_search=use_web_search)
        result['from_cache'] = from_cache
        return jsonify(result)
    except RuntimeError as e:
        logger.warning('Classification unavailable: ticker=%s error=%s', ticker, e)
        return jsonify({'error': str(e), 'manual_required': True}), 503
    except ValueError as e:
        logger.error('Classification parse error: ticker=%s error=%s', ticker, e)
        return jsonify({'error': str(e), 'manual_required': True}), 503
    except Exception as e:
        logger.error('Classification API error: ticker=%s error=%s', ticker, e)
        return jsonify({'error': f'Classification service error: {e}', 'manual_required': True}), 503


# ---------------------------------------------------------------------------
# Settings page
# ---------------------------------------------------------------------------

@main_bp.route('/settings')
def settings_page():
    """App settings: AI classification toggle and API key management."""
    enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    api_key_set = bool(_get_app_setting('anthropic_api_key'))
    return render_template('settings.html', classification_enabled=enabled, api_key_set=api_key_set)


@main_bp.route('/api/classifications')
def api_classifications_list():
    """List all cached ticker classifications (for the settings page)."""
    rows = TickerClassification.query.order_by(TickerClassification.ticker).all()
    return jsonify([{
        'ticker':          r.ticker,
        'asset_class':     r.asset_class,
        'market_cap_tilt': r.market_cap_tilt,
        'sector_weights':  r.weights_dict(),
        'source':          r.source,
        'classified_at':   r.classified_at.isoformat() if r.classified_at else None,
    } for r in rows])


@main_bp.route('/api/settings', methods=['POST'])
def api_settings_save():
    """
    Save application settings. Accepts JSON body with optional fields:
        classification_enabled  bool
        anthropic_api_key       str  (blank = keep existing key unchanged)
    """
    data = request.get_json(silent=True) or {}

    if 'classification_enabled' in data:
        val = 'true' if data['classification_enabled'] else 'false'
        _set_app_setting('claude_classification_enabled', val,
                         'Enable AI ticker classification via Claude API')

    if 'anthropic_api_key' in data:
        key = (data['anthropic_api_key'] or '').strip()
        if key:  # blank means "keep existing"
            _set_app_setting('anthropic_api_key', key,
                             'Anthropic API key for ticker classification')

    logger.info('Settings saved: classification_enabled=%s',
                _get_app_setting('claude_classification_enabled', 'false'))
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Dividend / Passive Income API
# ---------------------------------------------------------------------------

def _get_api_key_and_check_enabled():
    """Return (api_key, error_response) — error_response is None if all good."""
    if _get_app_setting('claude_classification_enabled', 'false') != 'true':
        return None, (jsonify({'error': 'AI features are disabled', 'manual_required': True}), 503)
    api_key = _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return None, (jsonify({'error': 'ANTHROPIC_API_KEY is not configured', 'manual_required': True}), 503)
    return api_key, None


@main_bp.route('/api/dividend-data/<ticker>')
def api_dividend_data_get(ticker):
    """
    Fetch (or return cached) dividend data for a single ticker.
    Query params:
        force=1   bypass 30-day cache and re-fetch from Claude
    Response 200: dividend data dict + from_cache bool
    Response 503: AI disabled or API key missing
    """
    from app.dividend_service import get_or_fetch

    api_key, err = _get_api_key_and_check_enabled()
    if err:
        return err

    ticker = ticker.strip().upper()
    force  = request.args.get('force', '').lower() in ('1', 'true')

    try:
        data, from_cache = get_or_fetch(ticker, api_key, force=force)
        data['from_cache'] = from_cache
        return jsonify(data)
    except RuntimeError as e:
        return jsonify({'error': str(e), 'manual_required': True}), 503
    except Exception as e:
        logger.error('Dividend data fetch error: ticker=%s error=%s', ticker, e)
        return jsonify({'error': f'Dividend service error: {e}'}), 500


@main_bp.route('/api/dividend-data/<ticker>', methods=['POST'])
def api_dividend_data_upsert(ticker):
    """
    Manually upsert dividend data for a ticker (bypasses Claude).
    Useful for overriding AI-fetched values.
    Request JSON: {annual_yield, dividend_per_share, frequency, payer_type,
                   is_dividend_payer, tax_treatment}
    Response 200: persisted row as dict
    Response 400: validation error
    """
    ticker = ticker.strip().upper()
    body   = request.get_json(silent=True) or {}

    annual_yield = body.get('annual_yield')
    if annual_yield is not None:
        try:
            annual_yield = float(annual_yield)
        except (TypeError, ValueError):
            return jsonify({'error': 'annual_yield must be a number'}), 400
        if annual_yield < 0 or annual_yield > 1:
            return jsonify({'error': 'annual_yield must be a decimal between 0 and 1 (e.g. 0.035 for 3.5%)'}), 400

    row = DividendData.query.filter_by(ticker=ticker).first()
    if not row:
        row = DividendData(ticker=ticker)
        db.session.add(row)

    if annual_yield is not None:
        row.annual_yield = annual_yield
    if 'dividend_per_share' in body:
        row.dividend_per_share = body['dividend_per_share']
    if 'frequency' in body:
        row.frequency = body['frequency'] or None
    if 'payer_type' in body:
        row.payer_type = body['payer_type'] or None
    if 'is_dividend_payer' in body:
        row.is_dividend_payer = bool(body['is_dividend_payer'])
    if 'tax_treatment' in body:
        row.tax_treatment = body['tax_treatment'] or None

    row.last_fetched_at = datetime.utcnow()
    db.session.commit()

    from app.dividend_service import _row_to_dict
    return jsonify(_row_to_dict(row))


@main_bp.route('/api/passive-income')
def api_passive_income():
    """
    Compute current annual/monthly passive income across all active holdings.
    Auto-fetches missing/stale dividend data via Claude Haiku.
    Query params:
        account_id=<int>   filter to a single account (optional)
    Response 200: {by_holding, by_account, total_annual_income, total_monthly_income,
                   est_after_tax_annual, tax_rate_used, missing_data}
    """
    from app.dividend_service import get_or_fetch, is_dividend_stale
    from app.dividend_calc import calculate_current_income, DEFAULT_TAX_RATE

    account_id_filter = request.args.get('account_id', type=int)
    tax_rate = float(_get_app_setting('drip_tax_rate_pct', str(DEFAULT_TAX_RATE * 100))) / 100

    q = Holding.query.filter_by(is_active=True)
    if account_id_filter:
        q = q.filter_by(account_id=account_id_filter)
    holdings = q.all()

    if not holdings:
        return jsonify({
            'by_holding': [], 'by_account': [],
            'total_annual_income': 0.0, 'total_monthly_income': 0.0,
            'est_after_tax_annual': 0.0, 'tax_rate_used': tax_rate,
            'missing_data': [],
        })

    ai_enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    api_key    = _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '') if ai_enabled else ''

    # Build account lookup for tax_status
    account_ids = {h.account_id for h in holdings}
    accounts    = {a.id: a for a in Account.query.filter(Account.id.in_(account_ids)).all()}

    missing_data = []
    holdings_data = []

    for h in holdings:
        acct = accounts.get(h.account_id)

        # Get or fetch dividend data
        cached = DividendData.query.filter_by(ticker=h.ticker).first()
        if ai_enabled and api_key and (not cached or is_dividend_stale(cached.last_fetched_at)):
            try:
                div_dict, _ = get_or_fetch(h.ticker, api_key)
            except Exception as e:
                logger.warning('Could not fetch dividend data for %s: %s', h.ticker, e)
                div_dict = None
        elif cached:
            from app.dividend_service import _row_to_dict
            div_dict = _row_to_dict(cached)
        else:
            div_dict = None

        if div_dict is None or div_dict.get('fetch_error'):
            missing_data.append(h.ticker)
            annual_yield   = 0.0
            is_payer       = False
            frequency      = None
            payer_type     = None
            tax_treatment  = None
        else:
            annual_yield   = float(div_dict.get('annual_yield') or 0)
            is_payer       = bool(div_dict.get('is_dividend_payer', True))
            frequency      = div_dict.get('frequency')
            payer_type     = div_dict.get('payer_type')
            tax_treatment  = div_dict.get('tax_treatment')

        holdings_data.append({
            'ticker':             h.ticker,
            'shares':             float(h.shares or 0),
            'last_price':         float(h.last_price or 0),
            'annual_yield':       annual_yield,
            'is_dividend_payer':  is_payer,
            'frequency':          frequency,
            'payer_type':         payer_type,
            'tax_treatment':      tax_treatment,
            'account_id':         h.account_id,
            'account_name':       acct.name if acct else 'Unknown',
            'account_tax_status': acct.tax_status if acct else 'taxable',
        })

    result = calculate_current_income(holdings_data, tax_rate=tax_rate)
    result['missing_data'] = missing_data
    return jsonify(result)


@main_bp.route('/api/passive-income/projection')
def api_passive_income_projection():
    """
    Run a DRIP simulation for all active holdings.
    Query params (all optional; fall back to app_settings then defaults):
        price_appreciation_rate  float  e.g. 7.0 (percent)
        dividend_growth_rate     float  e.g. 3.0 (percent)
        monthly_contribution     float  e.g. 500 (dollars)
        horizon_years            int    e.g. 20 (max 30)
    Response 200: {labels, drip_on, drip_off, no_action, callouts, assumptions}
    """
    from app.dividend_service import _row_to_dict
    from app.dividend_calc import simulate_drip

    def _param(name, setting_key, default):
        raw = request.args.get(name)
        if raw is not None:
            try:
                val = float(raw)
                _set_app_setting(setting_key, str(val), f'DRIP projection: {name}')
                return val
            except (TypeError, ValueError):
                pass
        stored = _get_app_setting(setting_key)
        if stored:
            try:
                return float(stored)
            except ValueError:
                pass
        return default

    price_appreciation = _param('price_appreciation_rate', 'drip_price_appreciation_pct', 7.0) / 100
    dividend_growth    = _param('dividend_growth_rate',    'drip_dividend_growth_pct',    3.0) / 100
    monthly_contrib    = _param('monthly_contribution',    'drip_monthly_contribution',   0.0)
    horizon_years      = int(min(request.args.get('horizon_years', 20, type=int), 30))

    holdings = Holding.query.filter_by(is_active=True).all()
    holdings_data = []
    for h in holdings:
        cached = DividendData.query.filter_by(ticker=h.ticker).first()
        div_dict = _row_to_dict(cached) if cached else {}
        holdings_data.append({
            'ticker':            h.ticker,
            'shares':            float(h.shares or 0),
            'last_price':        float(h.last_price or 0),
            'annual_yield':      float(div_dict.get('annual_yield') or 0),
            'is_dividend_payer': bool(div_dict.get('is_dividend_payer', False)) if cached else False,
            'frequency':         div_dict.get('frequency'),
        })

    result = simulate_drip(
        holdings_data,
        horizon_years=horizon_years,
        price_appreciation_rate=price_appreciation,
        dividend_growth_rate=dividend_growth,
        monthly_contribution=monthly_contrib,
    )
    result['assumptions'] = {
        'price_appreciation_rate': price_appreciation * 100,
        'dividend_growth_rate':    dividend_growth * 100,
        'monthly_contribution':    monthly_contrib,
        'horizon_years':           horizon_years,
    }
    return jsonify(result)
