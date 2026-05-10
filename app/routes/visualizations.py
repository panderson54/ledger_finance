"""
Visualization routes:
  /visualizations, /api/networth-history, /api/sp500-change, /api/save-rate-history,
  /api/cashflow-history, /api/allocation-history, /api/asset-distribution,
  /api/allocation/holdings-summary, /api/account-balances/<id>,
  /api/accounts/<id>/history (GET), /accounts/<id>/history, /accounts/<id>/history/export
"""
import csv
import io
import logging
from collections import defaultdict
from datetime import date, timedelta

from flask import render_template, jsonify, request, make_response

from app.routes import main_bp
from app.routes.helpers import (
    _month_display_date,
    _account_holding_splits,
    _get_account_allocations,
    _get_app_setting,
    CLASS_LABELS,
)
from app.models import Account, AccountSnapshot, CalculatedMetric, AppSetting
from app import db
from app.account_categories import INVESTMENT_CATS, CASH_CATS, ALLOCATION_CLASSES
from sqlalchemy import func

logger = logging.getLogger(__name__)


@main_bp.route('/visualizations')
def visualizations():
    """Data visualization dashboard"""
    return render_template('visualizations.html')


@main_bp.route('/api/networth-history')
def networth_history():
    """API endpoint: Get net worth history for charts"""
    metrics = CalculatedMetric.query.order_by(CalculatedMetric.metric_date).all()
    data = {
        'dates': [_month_display_date(m.metric_date).strftime('%Y-%m-%d') for m in metrics],
        'net_worth': [float(m.net_worth) if m.net_worth else 0 for m in metrics],
        'net_worth_non_re': [float(m.net_worth_non_re) if m.net_worth_non_re else 0 for m in metrics]
    }
    return jsonify(data)


@main_bp.route('/api/sp500-change')
def sp500_period_change():
    """Return S&P 500 % change for the requested dashboard period."""
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
    """JSON: balance history for sparkline/chart rendering. Pass ?all=true for full history."""
    q = (
        AccountSnapshot.query
        .filter_by(account_id=account_id)
        .order_by(AccountSnapshot.snapshot_date.desc())
    )
    if request.args.get('all') != 'true':
        q = q.limit(24)
    snapshots = list(reversed(q.all()))
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
