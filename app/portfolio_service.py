"""Read-only portfolio queries: latest balances, allocation splits, and the allocation overview.

Shared by the web routes and the MCP tools, so nothing here may write to the database
or call an external API.
"""
from datetime import date

from sqlalchemy import func

from app import db, settings_service
from app.account_categories import ALLOCATION_CLASSES, BUCKET_MAP, BUCKET_ORDER, CASH_CATS, INVESTMENT_CATS
from app.models import Account, AccountSnapshot, AssetAllocation, CalculatedMetric, Holding, TickerClassification
from app.price_service import is_stale

CAP_CLASSES = ['large', 'mid', 'small', 'other']


def latest_snapshots(account_ids=None) -> dict:
    """Return {account_id: latest AccountSnapshot}, optionally limited to account_ids."""
    latest = db.session.query(
        AccountSnapshot.account_id, func.max(AccountSnapshot.snapshot_date).label('max_date'),
    ).group_by(AccountSnapshot.account_id)
    if account_ids is not None:
        latest = latest.filter(AccountSnapshot.account_id.in_(list(account_ids)))
    latest = latest.subquery()
    rows = AccountSnapshot.query.join(
        latest,
        (AccountSnapshot.account_id == latest.c.account_id)
        & (AccountSnapshot.snapshot_date == latest.c.max_date),
    ).all()
    return {s.account_id: s for s in rows}


def current_balances_by_category() -> dict[str, float]:
    """Sum each active account's latest balance by category (liabilities stay positive)."""
    accounts = Account.query.filter_by(is_active=True).all()
    snapshots = latest_snapshots([a.id for a in accounts])
    totals: dict[str, float] = {}
    for account in accounts:
        snap = snapshots.get(account.id)
        if snap:
            totals[account.category] = totals.get(account.category, 0.0) + float(snap.balance)
    return totals


def bucket_totals() -> dict[str, float]:
    """Latest balances of net-worth accounts grouped into display buckets, in BUCKET_ORDER."""
    accounts = Account.query.filter_by(is_active=True, include_in_networth=True).all()
    snapshots = latest_snapshots([a.id for a in accounts])
    totals: dict[str, float] = {}
    for account in accounts:
        snap = snapshots.get(account.id)
        if snap:
            bucket = 'Liabilities' if account.account_type == 'liability' else BUCKET_MAP.get(account.category, 'Other')
            totals[bucket] = totals.get(bucket, 0.0) + float(snap.balance)
    order = {b: i for i, b in enumerate(BUCKET_ORDER)}
    return {b: round(totals[b], 2) for b in sorted(totals, key=lambda b: order.get(b, len(order)))}


def account_allocations(account_id) -> dict[str, float]:
    """Return {asset_class: percentage} from the account's most recent AssetAllocation rows."""
    rows = (AssetAllocation.query
            .filter_by(account_id=account_id)
            .order_by(AssetAllocation.effective_date.desc())
            .all())
    if not rows:
        return {}
    latest_date = rows[0].effective_date
    return {r.asset_class: float(r.percentage) for r in rows if r.effective_date == latest_date}


def account_holding_splits(account_id: int) -> tuple[dict[str, float], float] | None:
    """Return (splits_pct, market_value) derived from priced, classified holdings, or None.

    None means the caller should fall back to the account's AssetAllocation rows.
    """
    holdings = Holding.query.filter_by(account_id=account_id, is_active=True).all()
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
    return {cls: class_values[cls] / total_value * 100.0 for cls in ALLOCATION_CLASSES}, total_value


def _account_split(account, balance):
    """Return (splits, source, effective_balance); holdings beat AssetAllocation rows."""
    if account.category in CASH_CATS:
        return {'cash': 100.0}, 'cash', balance
    from_holdings = account_holding_splits(account.id)
    if from_holdings is not None:
        splits, market_value = from_holdings
        return splits, 'holdings', market_value
    splits = account_allocations(account.id)
    return splits, ('allocation' if splits else None), balance


def _drift_status(drifts):
    max_abs_drift = max((abs(d) for d in drifts.values()), default=0)
    if max_abs_drift <= 2:
        return 'on_target'
    return 'review' if max_abs_drift <= 5 else 'rebalance'


def allocation_overview() -> dict:
    """Portfolio-wide allocation vs target, per-account splits, holdings and market-cap mix.

    by_account rows carry the Account and all_holdings_rows the Holding ORM objects,
    for the template; callers that serialize must pick fields themselves.
    """
    targets = settings_service.get_allocation_targets()
    inv_accounts = (
        Account.query
        .filter(Account.category.in_(INVESTMENT_CATS | CASH_CATS))
        .filter_by(is_active=True, account_type='asset', include_in_networth=True)
        .order_by(Account.name)
        .all()
    )
    snapshots = latest_snapshots([a.id for a in inv_accounts])

    totals = {cls: 0.0 for cls in ALLOCATION_CLASSES}
    unclassified = 0.0
    by_account = []
    for acct in inv_accounts:
        snap = snapshots.get(acct.id)
        splits, source, bal = _account_split(acct, float(snap.balance) if snap else 0.0)
        contributions = {cls: bal * pct / 100.0 for cls, pct in splits.items()}
        for cls, amt in contributions.items():
            if cls in totals:
                totals[cls] += amt
        if not splits:
            unclassified += bal
        by_account.append({
            'account': acct, 'balance': bal, 'splits': splits,
            'contributions': contributions, 'source': source,
        })

    total_invested = sum(totals.values()) + unclassified
    actuals = {
        cls: (totals[cls] / total_invested * 100.0) if total_invested > 0 else 0.0
        for cls in ALLOCATION_CLASSES
    }
    drifts = {cls: actuals[cls] - targets.get(cls, 0.0) for cls in ALLOCATION_CLASSES}
    gaps = {cls: (targets.get(cls, 0.0) / 100.0 * total_invested) - totals[cls] for cls in ALLOCATION_CLASSES}

    holdings = (
        Holding.query.filter_by(is_active=True)
        .filter(Holding.account_id.in_([a.id for a in inv_accounts]))
        .all()
    )
    acct_map = {a.id: a for a in inv_accounts}
    tickers = {h.ticker for h in holdings}
    cap_tilts = {
        tc.ticker: tc.market_cap_tilt
        for tc in TickerClassification.query.filter(TickerClassification.ticker.in_(tickers)).all()
    } if tickers else {}

    all_holdings_rows = []
    for h in sorted(holdings, key=lambda x: float(x.shares or 0) * float(x.last_price or 0), reverse=True):
        acct = acct_map.get(h.account_id)
        all_holdings_rows.append({
            'holding': h,
            'account_name': acct.name if acct else '—',
            'account_institution': acct.institution if acct else '',
            'value': (float(h.shares) * float(h.last_price)
                      if h.shares is not None and h.last_price is not None else None),
            'alloc_splits': {ha.asset_class: float(ha.percentage) for ha in h.allocations.all()},
            'is_stale': is_stale(h.last_fetched),
            'cap_class': h.cap_class or cap_tilts.get(h.ticker),
        })

    cap_totals = {c: 0.0 for c in CAP_CLASSES}
    for row in all_holdings_rows:
        if row['value'] is not None:
            bucket = row['cap_class'] if row['cap_class'] in ('large', 'mid', 'small') else 'other'
            cap_totals[bucket] += row['value']
    cap_invested_total = sum(cap_totals.values())
    cap_pcts = {k: (v / cap_invested_total * 100.0) if cap_invested_total > 0 else 0.0 for k, v in cap_totals.items()}

    return {
        'targets': targets, 'actuals': actuals, 'totals': totals, 'drifts': drifts, 'gaps': gaps,
        'total_invested': total_invested, 'unclassified': unclassified, 'by_account': by_account,
        'status': _drift_status(drifts),
        'prices_stale': any(is_stale(h.last_fetched) for h in holdings),
        'holdings_count': len(holdings),
        'all_holdings_rows': all_holdings_rows,
        'cap_totals': cap_totals, 'cap_pcts': cap_pcts, 'cap_invested_total': cap_invested_total,
    }


def ytd_cash_flow(year: int | None = None) -> dict:
    """Year-to-date income, expenses and save rate from CalculatedMetric."""
    year = year or date.today().year
    metrics = CalculatedMetric.query.filter(
        CalculatedMetric.metric_date >= date(year, 1, 1),
        CalculatedMetric.metric_date <= date(year, 12, 31),
    ).all()
    income = sum(float(m.total_income) for m in metrics if m.total_income)
    expenses = sum(float(m.total_expenses) for m in metrics if m.total_expenses)
    return {
        'year': year, 'income': income, 'expenses': expenses,
        'save_rate': ((income - expenses) / income * 100) if income > 0 else None,
    }
