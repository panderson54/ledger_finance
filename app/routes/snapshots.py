"""
Snapshot and month API routes:
  /api/snapshots, /api/months/*, /api/metrics/*
"""
import logging

from flask import jsonify, request

from app.routes import main_bp
from app.routes.helpers import (
    _parse_month_str,
    _build_month_list,
    _metric_to_dict,
)
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric, RecurringEntry
from app import db
from app.metrics_service import recalculate_metrics as _recalculate_metrics

logger = logging.getLogger(__name__)


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
