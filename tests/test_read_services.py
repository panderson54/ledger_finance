from datetime import date
from unittest.mock import patch

import pytest

from app import income_service, portfolio_service, projections_service, settings_service
from app.models import Account, AccountSnapshot, AppSetting
from tests.conftest import make_anthropic_mock_client, make_holding, make_investment_account


def _account(db, name, category, account_type='asset', balances=()):
    acct = Account(name=name, category=category, account_type=account_type, is_active=True, include_in_networth=True)
    db.session.add(acct)
    db.session.flush()
    for month, balance in balances:
        db.session.add(AccountSnapshot(account_id=acct.id, snapshot_date=month, balance=balance))
    db.session.commit()
    return acct


class TestSettingsService:
    def test_float_setting(self, db):
        db.session.add_all([AppSetting(key='good', value='4.5'), AppSetting(key='bad', value='abc')])
        db.session.commit()
        assert settings_service.get_float_setting('good', 1.0) == 4.5
        assert settings_service.get_float_setting('bad', 1.0) == 1.0
        assert settings_service.get_float_setting('missing', 1.0) == 1.0

    def test_allocation_targets_fall_back_on_bad_json(self, db):
        assert settings_service.get_allocation_targets() == settings_service.ALLOCATION_TARGET_DEFAULTS
        db.session.add(AppSetting(key='allocation_targets', value='{not json'))
        db.session.commit()
        assert settings_service.get_allocation_targets() == settings_service.ALLOCATION_TARGET_DEFAULTS


class TestPortfolioService:
    def test_latest_snapshots_and_buckets(self, db):
        cash = _account(db, 'Checking', 'checking', balances=[(date(2025, 1, 1), 100), (date(2025, 2, 1), 150)])
        _account(db, 'Card', 'credit_card', 'liability', balances=[(date(2025, 2, 1), 40)])
        _account(db, 'Empty', 'brokerage')
        latest = portfolio_service.latest_snapshots()
        assert float(latest[cash.id].balance) == 150
        assert portfolio_service.bucket_totals() == {'Cash': 150.0, 'Liabilities': 40.0}
        assert portfolio_service.current_balances_by_category() == {'checking': 150.0, 'credit_card': 40.0}

    def test_empty(self, db):
        assert portfolio_service.latest_snapshots() == {}
        assert portfolio_service.bucket_totals() == {}
        assert portfolio_service.allocation_overview()['total_invested'] == 0

    def test_holding_splits_need_priced_classified_holdings(self, db):
        acct = make_investment_account(db.session)
        make_holding(db.session, acct.id)
        assert portfolio_service.account_holding_splits(acct.id) is None


class TestIncomeService:
    def test_fetches_missing_data_only_with_an_api_key(self, db):
        acct = make_investment_account(db.session)
        make_holding(db.session, acct.id, ticker='VYM', shares=10, price=100.0)
        response = {'is_dividend_payer': True, 'annual_yield': 0.03, 'frequency': 'quarterly'}
        with patch('anthropic.Anthropic', return_value=make_anthropic_mock_client(response)):
            assert income_service.passive_income()['missing_data'] == ['VYM']
            assert income_service.passive_income(api_key='sk-test')['total_annual_income'] == pytest.approx(30.0)

    def test_fetch_failure_marks_ticker_missing(self, db):
        acct = make_investment_account(db.session)
        make_holding(db.session, acct.id, ticker='VYM')
        with patch('anthropic.Anthropic', side_effect=Exception('network down')):
            assert income_service.passive_income(api_key='sk-test')['missing_data'] == ['VYM']


class TestProjectionsService:
    def test_growth_projection_clamps_horizon(self, db):
        _account(db, 'Brokerage', 'brokerage', balances=[(date(2025, 1, 1), 1000)])
        result = projections_service.growth_projection({'brokerage': 10}, 99)
        assert len(result['labels']) == projections_service.MAX_GROWTH_YEARS * 12 + 1
        assert result['total_projected'][-1] > 1000

    def test_unreachable_fi(self, db):
        result = projections_service.fi_scenario(0, 50_000, 4.0, 0.0, 0)
        assert result['fi_number'] == 1_250_000
        assert result['fi_date'] is None

    def test_projection_context_without_data(self, db):
        ctx = projections_service.projection_context()
        assert ctx['has_data'] is False and ctx['current_investable_nw'] == 0.0
