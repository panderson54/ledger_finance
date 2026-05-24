"""
Dashboard and monthly update routes:
  /, /accounts, /monthly-update, /monthly-update/<month_str>, /onboarding
"""
import logging
from datetime import datetime, date

from flask import render_template, request

from app.routes import main_bp
from app.routes.helpers import (
    _build_month_list,
    _parse_month_str,
    _compute_holdings_value,
    _get_app_setting,
)
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric, AppSetting
from app import db
from app.account_categories import (
    ALL_CATEGORIES as ACCOUNT_CATEGORIES,
    INVESTMENT_CATS,
)
from sqlalchemy import func

logger = logging.getLogger(__name__)


@main_bp.route('/marketing')
def marketing():
    return render_template('marketing.html')


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
