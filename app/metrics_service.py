"""
Metrics calculation service.

Extracted from routes.py to separate business logic from request handling.
"""
import logging
from datetime import date
from decimal import Decimal

from dateutil.relativedelta import relativedelta

from app.account_categories import INVESTMENT_CATS, CASH_CATS, LIABILITY_CATS

logger = logging.getLogger(__name__)

_LIAB_CATS = LIABILITY_CATS


def recalculate_metrics(month_date, _cascade=True):
    """
    Recalculate and upsert CalculatedMetric for the given month_date.
    Called automatically after any AccountSnapshot or SpendingEntry write.
    _cascade=True causes one additional recalculation of the next month so
    its monthly_change fields stay accurate after a historical edit.
    """
    from app import db
    from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric

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

    # Cascade one level: next month's monthly_change depends on this month's net_worth
    if _cascade:
        next_month = month_date + relativedelta(months=1)
        if CalculatedMetric.query.filter_by(metric_date=next_month).first() is not None:
            recalculate_metrics(next_month, _cascade=False)

    return metric


def compute_income_contributions(
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
        save_rate_pct float  0-100
        distribution  'split' | 'pro_rata' | 'all_invested' | 'all_cash'
        invest_pct    float  0-100 (split mode only)
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
        inv_cats = {c: max(0.0, v) for c, v in asset_cats.items() if c in INVESTMENT_CATS}
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
        csh_cats = {c: max(0.0, v) for c, v in asset_cats.items() if c in CASH_CATS}
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
