from datetime import date

import pytest

from app import mcp_server, monthly_update_service
from app.models import Account, AccountSnapshot, CalculatedMetric, RecurringEntry, SpendingEntry

LAST_MONTH = date(2026, 1, 1)
MONTH = date(2026, 2, 1)


@pytest.fixture
def household(db):
    checking = Account(name='Checking', account_type='asset', category='checking', is_active=True,
                       include_in_networth=True)
    card = Account(name='Visa', account_type='liability', category='credit_card', is_active=True,
                   include_in_networth=True)
    closed = Account(name='Closed', account_type='asset', category='savings', is_active=False)
    db.session.add_all([checking, card, closed])
    db.session.flush()
    db.session.add_all([
        AccountSnapshot(account_id=checking.id, snapshot_date=LAST_MONTH, balance=5000),
        AccountSnapshot(account_id=checking.id, snapshot_date=MONTH, balance=5200),
        SpendingEntry(entry_date=MONTH, account_name='Amex', amount=300, entry_type='expense', notes='keep me'),
        SpendingEntry(entry_date=MONTH, account_name='Amex', amount=200, entry_type='expense'),
        RecurringEntry(account_name='Employer', amount=8000, entry_type='income', is_active=True),
        RecurringEntry(account_name='Gym', amount=50, entry_type='expense', is_active=True),
    ])
    db.session.commit()
    return {'checking': checking, 'card': card, 'closed': closed}


def _record(**kwargs):
    return monthly_update_service.record_monthly_update(MONTH, **kwargs)


class TestStartMonth:
    def test_creates_metric_and_applies_missing_templates(self, db, household):
        assert monthly_update_service.start_month(MONTH) == (2, 0)
        assert CalculatedMetric.query.filter_by(metric_date=MONTH).first().total_income == 8000
        assert monthly_update_service.start_month(MONTH) == (0, 2)


class TestMonthStatus:
    def test_shows_balances_missing_accounts_and_pending_recurring(self, db, household):
        status = monthly_update_service.month_status(MONTH)
        checking = next(a for a in status['accounts'] if a['account_name'] == 'Checking')
        assert (checking['balance'], checking['previous_balance'], checking['previous_month']) == (5200, 5000, '2026-01')
        assert status['missing_balances'] == ['Visa']
        assert status['cash_flow'] == [{'source': 'Amex', 'type': 'expense', 'amount': 500}]
        assert [r['source'] for r in status['recurring_not_yet_applied']] == ['Employer', 'Gym']
        assert status['started'] is False

    def test_empty_database(self, db):
        status = monthly_update_service.month_status(MONTH)
        assert status['accounts'] == [] and status['cash_flow'] == []


class TestRecordMonthlyUpdate:
    def test_upserts_and_reports_previous_values(self, db, household):
        result = _record(
            balances=[{'account_id': household['checking'].id, 'balance': 5300.5},
                      {'account_id': household['card'].id, 'balance': 1200}],
            cash_flow=[{'source': 'Amex', 'type': 'expense', 'amount': 650},
                       {'source': 'Employer', 'type': 'income', 'amount': 8100}],
        )
        assert [(b['previous'], b['new'], b['status']) for b in result['balances']] == [
            (5200, 5300.5, 'updated'), (None, 1200, 'created')]
        assert [(c['previous'], c['status']) for c in result['cash_flow']] == [(500, 'updated'), (None, 'created')]
        # Employer was given explicitly, so only Gym comes from the templates.
        assert [r['source'] for r in result['recurring_applied']] == ['Gym']

        amex = SpendingEntry.query.filter_by(entry_date=MONTH, account_name='Amex').all()
        assert [(float(e.amount), e.notes) for e in amex] == [(650, 'keep me')]
        employer = SpendingEntry.query.filter_by(entry_date=MONTH, account_name='Employer').one()
        assert float(employer.amount) == 8100
        # net worth = 5300.50 checking - 1200 card; save rate = (8100 - 700) / 8100
        assert result['net_worth'] == 4100.5
        assert result['save_rate_pct'] == pytest.approx(91.36, abs=0.01)

    def test_repeating_the_same_update_changes_nothing(self, db, household):
        update = {'balances': [{'account_id': household['checking'].id, 'balance': 5300}]}
        _record(**update)
        result = _record(**update)
        assert result['balances'][0]['status'] == 'unchanged'
        assert result['recurring_applied'] == []

    def test_dry_run_writes_nothing(self, db, household):
        result = _record(balances=[{'account_id': household['card'].id, 'balance': 900}], dry_run=True)
        assert result['dry_run'] and result['balances'][0]['status'] == 'created'
        assert [r['source'] for r in result['recurring_applied']] == ['Employer', 'Gym']
        assert AccountSnapshot.query.filter_by(account_id=household['card'].id).count() == 0
        assert CalculatedMetric.query.filter_by(metric_date=MONTH).first() is None

    def test_empty_update_just_starts_the_month(self, db, household):
        result = _record()
        assert result['balances'] == [] and len(result['recurring_applied']) == 2
        assert CalculatedMetric.query.filter_by(metric_date=MONTH).first() is not None

    @pytest.mark.parametrize('kwargs, message', [
        ({'balances': [{'account_id': 999, 'balance': 1}]}, 'no active account'),
        ({'balances': [{'account_id': 'closed', 'balance': 1}]}, 'no active account'),
        ({'balances': [{'account_id': 'checking', 'balance': -5}]}, 'balance must be'),
        ({'balances': [{'account_id': 'checking', 'balance': 1}, {'account_id': 'checking', 'balance': 2}]},
         'more than once'),
        ({'cash_flow': [{'source': ' ', 'type': 'income', 'amount': 1}]}, 'source must be'),
        ({'cash_flow': [{'source': 'Pay', 'type': 'gift', 'amount': 1}]}, 'type must be'),
        ({'cash_flow': [{'source': 'Pay', 'type': 'income', 'amount': 'lots'}]}, 'amount must be'),
    ])
    def test_invalid_input_writes_nothing(self, db, household, kwargs, message):
        for item in kwargs.get('balances', []):
            if isinstance(item['account_id'], str):
                item['account_id'] = household[item['account_id']].id
        before = AccountSnapshot.query.count(), SpendingEntry.query.count()
        with pytest.raises(ValueError, match=message):
            _record(**kwargs)
        assert (AccountSnapshot.query.count(), SpendingEntry.query.count()) == before

    def test_future_month_rejected(self, db):
        with pytest.raises(ValueError, match='future'):
            monthly_update_service.record_monthly_update(date(2999, 1, 1))


class TestMcpTools:
    def test_write_tool_records_without_the_read_only_guard(self, app, db, household):
        record = mcp_server._in_app_context(app, mcp_server.record_monthly_update, read_only=False)
        result = record(month='2026-02', balances=[{'account_id': household['card'].id, 'balance': 750}])
        assert result['balances'][0]['status'] == 'created'
        assert AccountSnapshot.query.filter_by(account_id=household['card'].id).count() == 1

    def test_month_status_tool_rejects_bad_month(self, app, db):
        from mcp.server.mcpserver.exceptions import ToolError
        with pytest.raises(ToolError, match='YYYY-MM'):
            mcp_server._in_app_context(app, mcp_server.get_month_status)(month='Feb')

    def test_write_tool_only_on_write_servers(self, app):
        def names(server):
            return {t.name for t in server._tool_manager.list_tools()}

        assert 'record_monthly_update' not in names(mcp_server.create_mcp_server(app))
        assert 'record_monthly_update' in names(mcp_server.create_mcp_server(app, allow_writes=True))
