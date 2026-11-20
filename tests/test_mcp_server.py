import json
from datetime import date, datetime, timedelta
from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from sqlalchemy import update

from app import mcp_server, read_only_guard
from app.models import (
    Account, AccountSnapshot, AppSetting, AssetAllocation, CalculatedMetric, DividendData, Holding,
    HoldingAllocation, RecurringEntry, RentalProperty, SpendingEntry,
)
from tests.conftest import make_holding, make_investment_account, seed_ai_settings

CANARY = 'CANARY-7f3a'
FINANCE_TABLES = (
    Account, AccountSnapshot, AppSetting, AssetAllocation, CalculatedMetric, DividendData, Holding,
    HoldingAllocation, RecurringEntry, RentalProperty, SpendingEntry,
)
FRESH = datetime.utcnow()


def _call(tool, **kwargs):
    with read_only_guard.read_only():
        return tool(**kwargs)


def _metric(month, net_worth, income=None, expenses=None, non_re=None):
    return CalculatedMetric(
        metric_date=month, net_worth=net_worth, net_worth_non_re=non_re if non_re is not None else net_worth,
        net_worth_liquid=net_worth / 2, total_assets=net_worth + 100_000, total_liabilities=100_000,
        monthly_change_amount=1000, monthly_change_pct=1.0,
        total_income=income, total_expenses=expenses,
        save_rate=round((income - expenses) / income * 100, 2) if income else None,
    )


@pytest.fixture
def seeded(db):
    """A small but complete household, with a canary in every field that must never be returned."""
    brokerage = make_investment_account(db.session)
    brokerage.institution = f'{CANARY} Bank'
    brokerage.account_number = '9999'
    brokerage.notes = f'{CANARY} note'
    savings = Account(name='Savings', account_type='asset', category='savings', is_active=True,
                      include_in_networth=True, notes=CANARY)
    mortgage = Account(name='Mortgage', account_type='liability', category='mortgage', is_active=True,
                       include_in_networth=True, is_liquid=False)
    closed = Account(name='Old 401k', account_type='asset', category='401k', is_active=False)
    db.session.add_all([savings, mortgage, closed])
    db.session.flush()
    for month, scale in ((date(2025, 1, 1), 1.0), (date(2025, 6, 1), 1.1), (date(2026, 1, 1), 1.2)):
        db.session.add_all([
            AccountSnapshot(account_id=brokerage.id, snapshot_date=month, balance=100_000 * scale),
            AccountSnapshot(account_id=savings.id, snapshot_date=month, balance=20_000 * scale),
            AccountSnapshot(account_id=mortgage.id, snapshot_date=month, balance=200_000 / scale),
        ])
    db.session.add_all([
        _metric(date(2025, 1, 1), 300_000, 10_000, 6_000),
        _metric(date(2025, 6, 1), 330_000, 10_000, 5_000),
        _metric(date(2026, 1, 1), 360_000, 12_000, 6_000),
        SpendingEntry(entry_date=date(2026, 1, 1), account_name='Employer', amount=12_000, entry_type='income',
                      notes=CANARY),
        SpendingEntry(entry_date=date(2026, 1, 1), account_name='Visa', amount=4_000, entry_type='expense'),
        SpendingEntry(entry_date=date(2026, 1, 1), account_name='Visa', amount=2_000, entry_type='expense'),
        RecurringEntry(account_name='Employer', amount=12_000, entry_type='income', notes=CANARY),
        RentalProperty(name='Lake house', address=f'1 {CANARY} Way', monthly_rent=2000, vacancy_rate=0.05,
                       notes=CANARY),
        DividendData(ticker='VTI', annual_yield=0.015, is_dividend_payer=True, last_fetched_at=FRESH),
    ])
    vti = make_holding(db.session, brokerage.id, ticker='VTI', shares=300, price=250.0)
    bnd = make_holding(db.session, brokerage.id, ticker='BND', shares=100, price=72.0)
    vti.last_fetched = FRESH
    bnd.last_fetched = FRESH - timedelta(days=3)
    db.session.add_all([
        HoldingAllocation(holding_id=vti.id, asset_class='domestic', percentage=100),
        HoldingAllocation(holding_id=bnd.id, asset_class='bonds', percentage=100),
    ])
    seed_ai_settings(db.session, api_key=f'sk-{CANARY}')
    return {'brokerage': brokerage, 'savings': savings, 'mortgage': mortgage, 'closed': closed}


class TestFinancialSnapshot:
    def test_reports_latest_position(self, seeded):
        result = _call(mcp_server.get_financial_snapshot)
        assert result['as_of'] == '2026-01'
        assert result['net_worth'] == 360_000
        assert result['balances_by_bucket'] == {'Cash': 24_000.0, 'Investments': 120_000.0, 'Liabilities': 166_666.67}
        assert result['ytd']['save_rate_target_pct'] == 35.0

    def test_empty_database(self, db):
        result = _call(mcp_server.get_financial_snapshot)
        assert result['as_of'] is None and result['balances_by_bucket'] == {}
        assert result['warnings']


class TestNetWorthHistory:
    def test_range_and_cagr(self, seeded):
        result = _call(mcp_server.get_net_worth_history, start_month='2025-06', end_month='2026-01')
        assert [m['month'] for m in result['months']] == ['2025-06', '2026-01']
        assert result['net_worth_cagr_pct'] > 0

    def test_empty_range_warns(self, seeded):
        result = _call(mcp_server.get_net_worth_history, start_month='2030-01')
        assert result['months'] == [] and result['warnings']

    @pytest.mark.parametrize('kwargs', [{'start_month': '2025-13'}, {'end_month': 'jan'},
                                        {'start_month': '2026-01', 'end_month': '2025-01'}])
    def test_invalid_months_rejected(self, db, kwargs):
        with pytest.raises(ValueError):
            _call(mcp_server.get_net_worth_history, **kwargs)


class TestCashFlow:
    def test_groups_entries_by_source(self, seeded):
        month = _call(mcp_server.get_cash_flow, start_month='2026-01')['months'][0]
        assert month['savings'] == 6_000
        assert month['by_source'] == {'income': {'Employer': 12_000.0}, 'expense': {'Visa': 6_000.0}}

    def test_empty_database(self, db):
        result = _call(mcp_server.get_cash_flow)
        assert result['months'] == [] and result['recurring'] == []


class TestAccounts:
    def test_active_accounts_with_latest_balance(self, seeded):
        accounts = {a['name']: a for a in _call(mcp_server.list_accounts)}
        assert 'Old 401k' not in accounts
        assert accounts['Brokerage']['latest_balance'] == 120_000
        assert accounts['Brokerage']['latest_month'] == '2026-01'

    def test_include_inactive(self, seeded):
        names = [a['name'] for a in _call(mcp_server.list_accounts, include_inactive=True)]
        assert 'Old 401k' in names

    def test_account_history(self, seeded):
        result = _call(mcp_server.get_account_history, account_id=seeded['brokerage'].id)
        assert [b['balance'] for b in result['balances']] == [100_000, 110_000, 120_000]
        assert result['cagr_pct'] > 0

    def test_account_history_without_balances(self, seeded):
        assert _call(mcp_server.get_account_history, account_id=seeded['closed'].id)['warnings']

    def test_unknown_account(self, db):
        with pytest.raises(ValueError, match='No account'):
            _call(mcp_server.get_account_history, account_id=999)


class TestHoldings:
    def test_weights_concentration_and_stale_prices(self, seeded):
        result = _call(mcp_server.get_holdings)
        assert [h['ticker'] for h in result['holdings']] == ['VTI', 'BND']
        assert result['total_value'] == 82_200
        assert result['concentration']['positions_over_10_pct'] == ['VTI']  # BND is 8.8%
        assert 'BND' in result['warnings'][0]

    def test_filter_by_account_and_unknown_account(self, seeded):
        assert _call(mcp_server.get_holdings, account_id=seeded['savings'].id)['holdings'] == []
        with pytest.raises(ValueError):
            _call(mcp_server.get_holdings, account_id=999)


class TestAssetAllocation:
    def test_actual_vs_target(self, seeded):
        result = _call(mcp_server.get_asset_allocation)
        # brokerage uses holdings' market value (82,200); savings is all cash (24,000)
        assert result['total_invested'] == 106_200
        assert result['classes']['bonds']['amount'] == 7_200
        assert result['status'] == 'rebalance'
        assert {a['source'] for a in result['by_account']} == {'holdings', 'cash'}

    def test_nothing_invested(self, db):
        result = _call(mcp_server.get_asset_allocation)
        assert result['status'] is None and result['warnings']


class TestPassiveIncome:
    def test_uses_cached_data_only(self, seeded):
        # AI is enabled with a key, yet the tool must not fetch BND's missing data.
        with patch('anthropic.Anthropic', side_effect=AssertionError('Claude must not be called')):
            result = _call(mcp_server.get_passive_income)
        assert result['total_annual_income'] == pytest.approx(1125.0)
        assert result['missing_data'] == ['BND']
        assert result['rental_properties'][0]['net_annual_income'] == 22_800

    def test_no_holdings(self, db):
        result = _call(mcp_server.get_passive_income)
        assert result['total_annual_income'] == 0 and result['by_holding'] == []

    def test_unknown_account(self, db):
        with pytest.raises(ValueError):
            _call(mcp_server.get_passive_income, account_id=999)


class TestProjections:
    def test_net_worth_defaults_and_overrides(self, seeded):
        result = _call(mcp_server.project_net_worth, years=5, rates_pct={'brokerage': 10.0}, monthly_savings=1000)
        assert len(result['total_projected']) == 6  # today plus one point per year
        assert result['rates_used_pct']['brokerage'] == 10.0
        assert result['default_rates_pct']['brokerage'] != 10.0
        assert result['monthly_savings'] == 1000

    @pytest.mark.parametrize('kwargs', [{'years': 0}, {'years': 31}, {'monthly_savings': -1}])
    def test_net_worth_invalid_input(self, db, kwargs):
        with pytest.raises(ValueError):
            _call(mcp_server.project_net_worth, **kwargs)

    def test_fi_defaults_spending_from_history(self, seeded):
        result = _call(mcp_server.project_financial_independence)
        assert result['annual_spending'] == 17_000
        assert result['fi_number'] == 425_000
        assert result['current_investable_net_worth'] == 360_000

    def test_fi_without_history_needs_spending(self, db):
        with pytest.raises(ValueError, match='annual_spending'):
            _call(mcp_server.project_financial_independence)
        assert _call(mcp_server.project_financial_independence, annual_spending=40_000)['fi_number'] == 1_000_000

    @pytest.mark.parametrize('kwargs', [{'swr_pct': 0}, {'growth_pct': 99}, {'annual_spending': -5}])
    def test_fi_invalid_input(self, db, kwargs):
        with pytest.raises(ValueError):
            _call(mcp_server.project_financial_independence, **kwargs)

    def test_dividend_projection_uses_saved_assumptions(self, db, seeded):
        db.session.add(AppSetting(key='drip_price_appreciation_pct', value='5.0'))
        db.session.commit()
        result = _call(mcp_server.project_dividend_income, years=10)
        assert result['assumptions']['price_appreciation_pct'] == 5.0
        assert len(result['labels']) == 11

    def test_dividend_projection_invalid_years(self, db):
        with pytest.raises(ValueError):
            _call(mcp_server.project_dividend_income, years=0)


def _call_every_tool(seeded):
    needs_args = (mcp_server.get_account_history, mcp_server.get_month_status)
    calls = [(tool, {}) for tool in mcp_server.TOOLS if tool not in needs_args]
    calls += [(mcp_server.get_account_history, {'account_id': seeded['brokerage'].id}),
              (mcp_server.get_month_status, {'month': '2026-01'}),
              (mcp_server.get_holdings, {'account_id': seeded['brokerage'].id}),
              (mcp_server.get_passive_income, {'account_id': seeded['brokerage'].id}),
              (mcp_server.list_accounts, {'include_inactive': True})]
    return [_call(tool, **kwargs) for tool, kwargs in calls]


class TestReadOnlyAndPrivacy:
    def test_no_tool_returns_private_fields(self, seeded):
        dumped = json.dumps(_call_every_tool(seeded), default=str)
        assert CANARY not in dumped
        assert '9999' not in dumped

    def test_tools_change_nothing(self, db, seeded):
        def fingerprint():
            return {m.__tablename__: m.query.count() for m in FINANCE_TABLES}

        before = fingerprint()
        with patch('yfinance.Ticker', side_effect=AssertionError('prices must not be fetched')):
            _call_every_tool(seeded)
        assert not (db.session.new or db.session.dirty or db.session.deleted)
        assert fingerprint() == before

    def test_guard_rejects_orm_writes(self, db):
        with read_only_guard.read_only():
            db.session.add(AppSetting(key='x', value='y'))
            with pytest.raises(read_only_guard.ReadOnlyViolation):
                db.session.flush()
        db.session.rollback()

    def test_guard_rejects_write_statements(self, db):
        with read_only_guard.read_only(), pytest.raises(read_only_guard.ReadOnlyViolation):
            db.session.execute(update(AppSetting).values(value='z'))
        db.session.rollback()

    def test_guard_lifts_after_the_call(self, db):
        with read_only_guard.read_only():
            pass
        db.session.add(AppSetting(key='after', value='ok'))
        db.session.commit()

    def test_wrapper_runs_read_only_and_surfaces_bad_input(self, app, db):
        def writes():
            db.session.add(AppSetting(key='sneaky', value='1'))
            db.session.flush()

        with pytest.raises(read_only_guard.ReadOnlyViolation):
            mcp_server._in_app_context(app, writes)()
        with pytest.raises(ToolError, match='No account'):
            mcp_server._in_app_context(app, mcp_server.get_account_history)(account_id=999)
