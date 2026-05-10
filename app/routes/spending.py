"""
Spending and recurring entry routes:
  /api/spending, /api/recurring-entries
"""
import logging
from decimal import Decimal

from flask import jsonify, request

from app.routes import main_bp
from app.routes.helpers import _parse_month_str, _recurring_to_dict
from app.models import SpendingEntry, RecurringEntry
from app import db
from app.metrics_service import recalculate_metrics as _recalculate_metrics

logger = logging.getLogger(__name__)


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
