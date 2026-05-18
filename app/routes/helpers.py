"""
Shared helper functions for route handlers.
No import of main_bp here — these are pure utilities imported by sub-modules.
"""
import logging
import os
import re
import calendar
import json
from datetime import datetime, date

from flask import jsonify, request
from sqlalchemy import func

from app import db
from app.models import (
    Account, AccountSnapshot, SpendingEntry, CalculatedMetric,
    AssetAllocation, AppSetting, Holding, HoldingAllocation,
)
from app.account_categories import ALLOCATION_CLASSES, INVESTMENT_CATS, CASH_CATS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Simple error helpers
# ---------------------------------------------------------------------------

def _bad_request(msg: str):
    return jsonify({'error': msg}), 400


def _not_found(resource: str):
    return jsonify({'error': f'{resource} not found'}), 404


# ---------------------------------------------------------------------------
# Month parsing
# ---------------------------------------------------------------------------

def _parse_month_str(month_str):
    """Parse 'YYYY-MM' string to a date(year, month, 1). Returns None on failure."""
    try:
        return datetime.strptime(month_str, '%Y-%m').date().replace(day=1)
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Metric serialization
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Month list builder
# ---------------------------------------------------------------------------

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
# Account form helpers
# ---------------------------------------------------------------------------

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
    raw_apy = data.get('apy', '').strip()
    try:
        account.apy = float(raw_apy) / 100 if raw_apy else None
    except (ValueError, TypeError):
        account.apy = None
    raw_edy = data.get('expected_dividend_yield', '').strip()
    try:
        account.expected_dividend_yield = float(raw_edy) / 100 if raw_edy else None
    except (ValueError, TypeError):
        account.expected_dividend_yield = None
    # New accounts are always active; existing accounts read the checkbox
    if account.id is None:
        account.is_active = True
    else:
        account.is_active = 'is_active' in data
    color = data.get('display_color', '').strip()
    account.display_color = color if color else None
    return account


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
        'apy': '{:.2f}'.format(float(account.apy) * 100) if account.apy else '',
        'expected_dividend_yield': '{:.2f}'.format(float(account.expected_dividend_yield) * 100) if account.expected_dividend_yield else '',
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
        'apy': request.form.get('apy', ''),
        'expected_dividend_yield': request.form.get('expected_dividend_yield', ''),
    }


# ---------------------------------------------------------------------------
# Account allocation helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Display date helper
# ---------------------------------------------------------------------------

def _month_display_date(d):
    """Return the chart display date for a first-of-month date per the snapshot_timing setting."""
    timing = _get_app_setting('snapshot_timing', 'end_of_month')
    if timing == 'end_of_month':
        last_day = calendar.monthrange(d.year, d.month)[1]
        return d.replace(day=last_day)
    return d


# ---------------------------------------------------------------------------
# Recurring entry serialization
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


# ---------------------------------------------------------------------------
# Projections context builder
# ---------------------------------------------------------------------------

def _build_projections_context():
    """
    Gather historical data and default rates needed by the /projections page.
    Returns a dict ready to pass to render_template (all values JSON-safe).
    """
    from app import projections as proj

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


# ---------------------------------------------------------------------------
# Allocation targets
# ---------------------------------------------------------------------------

_ALLOC_TARGET_KEY = 'allocation_targets'
_ALLOC_TARGET_DEFAULTS = {'domestic': 70.0, 'international': 15.0, 'bonds': 10.0, 'cash': 5.0}


def _get_allocation_targets():
    """Return {class: float} from app_settings, falling back to defaults."""
    row = AppSetting.query.filter_by(key=_ALLOC_TARGET_KEY).first()
    if not row or not row.value:
        return dict(_ALLOC_TARGET_DEFAULTS)
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return dict(_ALLOC_TARGET_DEFAULTS)


def _set_allocation_targets(targets: dict):
    """Upsert allocation targets into app_settings."""
    row = AppSetting.query.filter_by(key=_ALLOC_TARGET_KEY).first()
    if not row:
        row = AppSetting(key=_ALLOC_TARGET_KEY, description='Portfolio asset class target percentages')
        db.session.add(row)
    row.value = json.dumps(targets)
    db.session.commit()


# ---------------------------------------------------------------------------
# App settings helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Holdings helpers
# ---------------------------------------------------------------------------

def _validate_allocation_splits(allocs_raw: dict) -> tuple[dict, str | None]:
    """
    Parse allocation percentages from a raw dict and validate they sum to ~100.

    Returns (allocs, error_msg). error_msg is None when the splits are valid.
    A zero sum is allowed (no splits submitted yet); only a non-zero sum that
    differs from 100 by more than 0.5 is rejected.
    """
    allocs = {}
    for cls in ALLOCATION_CLASSES:
        try:
            allocs[cls] = float(allocs_raw.get(cls, 0))
        except (ValueError, TypeError):
            allocs[cls] = 0.0
    alloc_sum = sum(allocs.values())
    if alloc_sum > 0 and abs(alloc_sum - 100.0) > 0.5:
        return allocs, f'Allocation percentages must sum to 100 (got {alloc_sum:.1f})'
    return allocs, None


def _account_holding_splits(account_id: int) -> tuple[dict[str, float], float] | None:
    """
    If an account has active holdings with allocation splits, compute effective
    allocation percentages from holdings (Phase 2 data source).

    Returns (splits_pct, total_market_value) if holdings exist and have splits,
    or None to fall back to AssetAllocation × AccountSnapshot (Phase 1).
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


# ---------------------------------------------------------------------------
# Rental income serialization
# ---------------------------------------------------------------------------

def _rental_to_dict(prop):
    return {
        'id':               prop.id,
        'name':             prop.name,
        'address':          prop.address,
        'monthly_rent':     float(prop.monthly_rent or 0),
        'vacancy_rate':     float(prop.vacancy_rate or 0),
        'is_active':        prop.is_active,
        'notes':            prop.notes,
        'effective_monthly': prop.effective_monthly,
        'annual_income':    prop.annual_income,
    }


# ---------------------------------------------------------------------------
# API key / AI feature check
# ---------------------------------------------------------------------------

def _get_anthropic_api_key() -> str:
    """Return the Anthropic API key from app settings, falling back to the environment."""
    return _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '')


def _get_api_key_and_check_enabled():
    """Return (api_key, error_response) — error_response is None if all good."""
    if _get_app_setting('claude_classification_enabled', 'false') != 'true':
        return None, (jsonify({'error': 'AI features are disabled', 'manual_required': True}), 503)
    api_key = _get_anthropic_api_key()
    if not api_key:
        return None, (jsonify({'error': 'ANTHROPIC_API_KEY is not configured', 'manual_required': True}), 503)
    return api_key, None


# ---------------------------------------------------------------------------
# CLASS_LABELS constant
# ---------------------------------------------------------------------------

CLASS_LABELS = {
    'domestic': 'Domestic',
    'international': 'International',
    'bonds': 'Bonds',
    'cash': 'Cash',
}
