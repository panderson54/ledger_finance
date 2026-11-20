"""
Allocation routes:
  /allocation, /api/allocation/*, /api/accounts/performance, /api/accounts/overview-history
"""
import logging
from collections import defaultdict

from flask import render_template, jsonify, request

from app.routes import main_bp
from app.routes.helpers import _set_allocation_targets, _get_app_setting
from app.models import Account, AccountSnapshot
from app import portfolio_service
from app.account_categories import ALLOCATION_CLASSES

logger = logging.getLogger(__name__)


@main_bp.route('/allocation')
def allocation():
    """Portfolio-wide asset allocation dashboard."""
    overview = portfolio_service.allocation_overview()
    ytd = portfolio_service.ytd_cash_flow()
    return render_template(
        'allocation.html',
        **overview,
        allocation_classes=ALLOCATION_CLASSES,
        actuals_list=[overview['actuals'].get(c, 0) for c in ALLOCATION_CLASSES],
        targets_list=[overview['targets'].get(c, 0) for c in ALLOCATION_CLASSES],
        cap_classes=portfolio_service.CAP_CLASSES,
        ytd_income=ytd['income'],
        ytd_expenses=ytd['expenses'],
        ytd_save_rate=ytd['save_rate'],
        ytd_year=ytd['year'],
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
