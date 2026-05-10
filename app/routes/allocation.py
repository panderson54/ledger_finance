"""
Allocation routes:
  /allocation, /api/allocation/*, /api/accounts/performance, /api/accounts/overview-history
"""
import logging
from collections import defaultdict
from datetime import date

from flask import render_template, jsonify, request

from app.routes import main_bp
from app.routes.helpers import (
    _get_allocation_targets,
    _set_allocation_targets,
    _get_account_allocations,
    _account_holding_splits,
    _get_app_setting,
    CLASS_LABELS,
)
from app.models import Account, AccountSnapshot, CalculatedMetric, Holding, TickerClassification
from app import db
from app.account_categories import INVESTMENT_CATS, CASH_CATS, ALLOCATION_CLASSES
from sqlalchemy import func

logger = logging.getLogger(__name__)


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
        show_rental_income=_get_app_setting('show_rental_income', 'false') == 'true',
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


@main_bp.route('/api/accounts/performance')
def api_accounts_performance():
    """Ranked account performance metrics. Active asset accounts with >= 2 snapshots."""
    accounts = (
        Account.query
        .filter_by(is_active=True, account_type='asset', include_in_networth=True)
        .order_by(Account.name)
        .all()
    )
    results = []
    for acct in accounts:
        snaps = (
            AccountSnapshot.query
            .filter_by(account_id=acct.id)
            .order_by(AccountSnapshot.snapshot_date)
            .all()
        )
        if len(snaps) < 2:
            continue
        first_bal = float(snaps[0].balance)
        last_bal = float(snaps[-1].balance)
        years = (snaps[-1].snapshot_date - snaps[0].snapshot_date).days / 365.25
        cagr = None
        if years > 0 and first_bal > 0:
            cagr = round(((last_bal / first_bal) ** (1 / years) - 1) * 100, 2)
        three_mo = (
            round(last_bal - float(snaps[-4].balance), 2) if len(snaps) >= 4
            else round(last_bal - float(snaps[-2].balance), 2)
        )
        results.append({
            'id': acct.id,
            'name': acct.name,
            'institution': acct.institution,
            'display_color': acct.display_color,
            'latest_balance': last_bal,
            'cagr': cagr,
            'abs_growth': round(last_bal - first_bal, 2),
            'three_mo_change': three_mo,
            'first_date': snaps[0].snapshot_date.strftime('%Y-%m'),
            'snapshot_count': len(snaps),
            'dates': [s.snapshot_date.strftime('%Y-%m-%d') for s in snaps],
            'balances': [float(s.balance) for s in snaps],
        })
    results.sort(key=lambda r: (r['cagr'] is None, -(r['cagr'] or 0)))
    return jsonify({'accounts': results})


@main_bp.route('/api/accounts/overview-history')
def api_accounts_overview_history():
    """Balance history for all active asset accounts, for the overview stacked chart."""
    GROUP_MAP = {
        'real_estate': 'Real Estate',
        'vehicle': 'Real Estate',
        'brokerage': 'Investments / Brokerage',
        'investment': 'Investments / Brokerage',
        'retirement': 'Retirement',
        '401k': 'Retirement',
        'ira': 'Retirement',
        'roth_ira': 'Retirement',
        'hsa': 'Retirement',
        '529': 'Retirement',
        'savings': 'Savings',
        'checking': 'Savings',
        'cash': 'Savings',
    }

    accounts = (
        Account.query
        .filter_by(is_active=True, account_type='asset', include_in_networth=True)
        .order_by(Account.name)
        .all()
    )
    if not accounts:
        return jsonify({'months': [], 'accounts': []})

    account_ids = [a.id for a in accounts]
    snapshots = (
        AccountSnapshot.query
        .filter(AccountSnapshot.account_id.in_(account_ids))
        .order_by(AccountSnapshot.snapshot_date)
        .all()
    )

    all_months = sorted({s.snapshot_date.strftime('%Y-%m') for s in snapshots})
    if not all_months:
        return jsonify({'months': [], 'accounts': []})

    acct_monthly: dict[int, dict[str, float]] = defaultdict(dict)
    for snap in snapshots:
        month = snap.snapshot_date.strftime('%Y-%m')
        acct_monthly[snap.account_id][month] = float(snap.balance)

    result_accounts = []
    for acct in accounts:
        monthly = acct_monthly.get(acct.id, {})
        # Fill forward: carry last known balance for months with no snapshot
        balances = []
        last_val = 0.0
        for month in all_months:
            if month in monthly:
                last_val = monthly[month]
            balances.append(last_val)
        result_accounts.append({
            'id': acct.id,
            'name': acct.name,
            'category': acct.category,
            'group': GROUP_MAP.get(acct.category, 'Other'),
            'display_color': acct.display_color or '',
            'balances': balances,
        })

    return jsonify({'months': all_months, 'accounts': result_accounts})
