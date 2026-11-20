"""Starting a month and recording its balances and cash flow — the monthly update.

Shared by the website's month routes and the agent's record_monthly_update MCP tool.
"""
import logging
from datetime import date
from decimal import Decimal

from app import db
from app.metrics_service import recalculate_metrics
from app.models import Account, AccountSnapshot, CalculatedMetric, RecurringEntry, SpendingEntry

logger = logging.getLogger(__name__)

ENTRY_TYPES = ('income', 'expense')
MAX_SOURCE_LENGTH = 100


def _cents(value):
    return Decimal(str(round(float(value), 2)))


def _amount(value):
    return round(float(value), 2) if value is not None else None


def _entries_by_key(month_date):
    grouped: dict[tuple[str, str], list[SpendingEntry]] = {}
    for e in SpendingEntry.query.filter_by(entry_date=month_date).order_by(SpendingEntry.id).all():
        grouped.setdefault((e.account_name, e.entry_type), []).append(e)
    return grouped


def _pending_templates(month_date, existing_keys):
    templates = RecurringEntry.query.filter_by(is_active=True).order_by(
        RecurringEntry.display_order, RecurringEntry.id
    ).all()
    return [t for t in templates if (t.account_name, t.entry_type) not in existing_keys]


def _add_from_templates(month_date, templates):
    for t in templates:
        db.session.add(SpendingEntry(
            entry_date=month_date, account_name=t.account_name, amount=t.amount,
            entry_type=t.entry_type, notes=t.notes or '',
        ))


def _ensure_metric_stub(month_date):
    if not CalculatedMetric.query.filter_by(metric_date=month_date).first():
        db.session.add(CalculatedMetric(metric_date=month_date))


def start_month(month_date) -> tuple[int, int]:
    """Create the month's metric row and add any active recurring entries it lacks; returns (applied, skipped)."""
    _ensure_metric_stub(month_date)
    existing = _entries_by_key(month_date)
    pending = _pending_templates(month_date, existing)
    _add_from_templates(month_date, pending)
    db.session.commit()
    if pending:
        recalculate_metrics(month_date)
    active_templates = RecurringEntry.query.filter_by(is_active=True).count()
    return len(pending), active_templates - len(pending)


def month_status(month_date) -> dict:
    """Each active account's balance this month and its previous balance, plus the month's entries."""
    accounts = Account.query.filter_by(is_active=True).order_by(Account.display_order, Account.name).all()
    this_month = {
        s.account_id: s for s in AccountSnapshot.query.filter_by(snapshot_date=month_date).all()
    }
    accounts_out = []
    for a in accounts:
        previous = (
            AccountSnapshot.query.filter(
                AccountSnapshot.account_id == a.id, AccountSnapshot.snapshot_date < month_date,
            ).order_by(AccountSnapshot.snapshot_date.desc()).first()
        )
        accounts_out.append({
            'account_id': a.id,
            'account_name': a.name,
            'category': a.category,
            'account_type': a.account_type,
            'balance': _amount(this_month[a.id].balance) if a.id in this_month else None,
            'previous_balance': _amount(previous.balance) if previous else None,
            'previous_month': previous.snapshot_date.strftime('%Y-%m') if previous else None,
        })
    entries = _entries_by_key(month_date)
    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
    return {
        'month': month_date.strftime('%Y-%m'),
        'started': metric is not None,
        'accounts': accounts_out,
        'missing_balances': [a['account_name'] for a in accounts_out if a['balance'] is None],
        'cash_flow': [
            {'source': source, 'type': entry_type, 'amount': _amount(sum(e.amount for e in group))}
            for (source, entry_type), group in sorted(entries.items())
        ],
        'recurring_not_yet_applied': [
            {'source': t.account_name, 'type': t.entry_type, 'amount': _amount(t.amount)}
            for t in _pending_templates(month_date, entries)
        ],
    }


def _validate(month_date, balances, cash_flow):
    errors = []
    if month_date > date.today().replace(day=1):
        errors.append('month must not be in the future.')

    account_ids = [b.get('account_id') for b in balances]
    accounts = {
        a.id: a for a in Account.query.filter(Account.id.in_([i for i in account_ids if isinstance(i, int)])).all()
    }
    seen_accounts = set()
    for i, b in enumerate(balances):
        account_id, balance = b.get('account_id'), b.get('balance')
        account = accounts.get(account_id)
        if account is None or not account.is_active:
            errors.append(f'balances[{i}]: no active account {account_id!r} (see list_accounts).')
        elif account_id in seen_accounts:
            errors.append(f'balances[{i}]: account {account_id} is listed more than once.')
        seen_accounts.add(account_id)
        if not isinstance(balance, (int, float)) or isinstance(balance, bool) or balance < 0:
            errors.append(f'balances[{i}]: balance must be a number >= 0 (liabilities are positive amounts owed).')

    seen_entries = set()
    for i, e in enumerate(cash_flow):
        source, entry_type, amount = (e.get('source') or '').strip(), e.get('type'), e.get('amount')
        if not source or len(source) > MAX_SOURCE_LENGTH:
            errors.append(f'cash_flow[{i}]: source must be 1-{MAX_SOURCE_LENGTH} characters.')
        if entry_type not in ENTRY_TYPES:
            errors.append(f'cash_flow[{i}]: type must be one of {list(ENTRY_TYPES)}.')
        if (source, entry_type) in seen_entries:
            errors.append(f'cash_flow[{i}]: {entry_type} from {source!r} is listed more than once.')
        seen_entries.add((source, entry_type))
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or amount < 0:
            errors.append(f'cash_flow[{i}]: amount must be a number >= 0.')
    if errors:
        raise ValueError(' '.join(errors))
    return accounts


def _status(previous, new):
    if previous is None:
        return 'created'
    return 'unchanged' if round(previous, 2) == round(new, 2) else 'updated'


def record_monthly_update(month_date, balances=None, cash_flow=None, dry_run=False) -> dict:
    """Upsert the month's account balances and income/expense totals per source, then recalculate.

    Like starting a month on the website, it also adds any active recurring entries the month
    lacks. Existing values are overwritten and reported with their previous amounts. Input is
    validated as a whole first, so nothing is written unless every item is valid.
    """
    balances, cash_flow = balances or [], cash_flow or []
    accounts = _validate(month_date, balances, cash_flow)

    snapshots = {
        s.account_id: s for s in AccountSnapshot.query.filter(
            AccountSnapshot.snapshot_date == month_date,
            AccountSnapshot.account_id.in_([b['account_id'] for b in balances]),
        ).all()
    } if balances else {}
    entries = _entries_by_key(month_date)
    given_keys = {((e['source'] or '').strip(), e['type']) for e in cash_flow}
    templates = _pending_templates(month_date, set(entries) | given_keys)

    balance_report = [{
        'account_id': b['account_id'],
        'account_name': accounts[b['account_id']].name,
        'previous': _amount(snapshots[b['account_id']].balance) if b['account_id'] in snapshots else None,
        'new': _amount(b['balance']),
    } for b in balances]
    cash_flow_report = [{
        'source': e['source'].strip(),
        'type': e['type'],
        'previous': (
            _amount(sum(x.amount for x in entries[(e['source'].strip(), e['type'])]))
            if (e['source'].strip(), e['type']) in entries else None
        ),
        'new': _amount(e['amount']),
    } for e in cash_flow]
    for row in balance_report + cash_flow_report:
        row['status'] = _status(row['previous'], row['new'])

    result = {
        'month': month_date.strftime('%Y-%m'),
        'dry_run': dry_run,
        'balances': balance_report,
        'cash_flow': cash_flow_report,
        'recurring_applied': [
            {'source': t.account_name, 'type': t.entry_type, 'amount': _amount(t.amount)} for t in templates
        ],
    }
    if dry_run:
        return result

    _ensure_metric_stub(month_date)
    for b in balances:
        snap = snapshots.get(b['account_id'])
        if snap:
            snap.balance = _cents(b['balance'])
        else:
            db.session.add(AccountSnapshot(
                account_id=b['account_id'], snapshot_date=month_date, balance=_cents(b['balance']),
            ))
    for e in cash_flow:
        key = (e['source'].strip(), e['type'])
        existing = entries.get(key)
        if existing:
            # One total per source and type: keep the first row (and its notes), drop the rest.
            existing[0].amount = _cents(e['amount'])
            for extra in existing[1:]:
                db.session.delete(extra)
        else:
            db.session.add(SpendingEntry(
                entry_date=month_date, account_name=key[0], amount=_cents(e['amount']), entry_type=key[1], notes='',
            ))
    _add_from_templates(month_date, templates)
    db.session.commit()
    recalculate_metrics(month_date)

    metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
    result['net_worth'] = _amount(metric.net_worth)
    result['save_rate_pct'] = _amount(metric.save_rate)
    logger.info(
        'Monthly update recorded: month=%s balances=%d cash_flow=%d recurring_applied=%d',
        result['month'], len(balances), len(cash_flow), len(templates),
    )
    return result
