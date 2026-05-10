"""
Tests for dividend data service, calculation logic, and API endpoints.

Covers:
  - dividend_service: _validate_dividend_data(), fetch_dividend_data(), get_or_fetch()
  - dividend_calc: calculate_current_income(), simulate_drip()
  - Routes: GET/POST /api/dividend-data/<ticker>
  - Routes: GET /api/passive-income, GET /api/passive-income/projection
"""
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest

from app import db as _db
from app.models import Account, AppSetting, Holding, DividendData


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID_DIV_RESPONSE = {
    "is_dividend_payer": True,
    "annual_yield": 0.031,
    "dividend_per_share": 3.52,
    "frequency": "quarterly",
    "payer_type": "etf",
    "tax_treatment": "qualified",
    "payout_ratio": None,
    "cut_risk": "low",
    "ttm_yield": True,
}

_NON_PAYER_RESPONSE = {
    "is_dividend_payer": False,
    "annual_yield": 0,
    "dividend_per_share": 0,
    "frequency": None,
    "payer_type": "non_payer",
    "tax_treatment": None,
    "payout_ratio": None,
    "cut_risk": None,
    "ttm_yield": True,
}


def _make_mock_client(response_text=None):
    if response_text is None:
        response_text = json.dumps(_VALID_DIV_RESPONSE)
    block = MagicMock()
    block.text = response_text
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    return client


def _enable_ai(db, api_key="sk-test"):
    db.session.add(AppSetting(key='claude_classification_enabled', value='true', description='test'))
    db.session.add(AppSetting(key='anthropic_api_key', value=api_key, description='test'))
    db.session.commit()


def _make_investment_account(db, name="Brokerage", tax_status="taxable"):
    acct = Account(
        name=name,
        account_type="asset",
        category="brokerage",
        is_liquid=True,
        include_in_networth=True,
        is_active=True,
        tax_status=tax_status,
    )
    db.session.add(acct)
    db.session.commit()
    return acct


def _make_holding(db, account_id, ticker="VYM", shares=100, price=50.0):
    h = Holding(
        account_id=account_id,
        ticker=ticker,
        shares=shares,
        last_price=price,
        is_active=True,
    )
    db.session.add(h)
    db.session.commit()
    return h


# ---------------------------------------------------------------------------
# 1. Validation unit tests
# ---------------------------------------------------------------------------

class TestValidateDividendData:
    def test_valid_payer_passes(self):
        from app.dividend_service import _validate_dividend_data
        result = _validate_dividend_data(_VALID_DIV_RESPONSE.copy())
        assert result['is_dividend_payer'] is True
        assert result['annual_yield'] == pytest.approx(0.031)
        assert result['frequency'] == 'quarterly'
        assert result['tax_treatment'] == 'qualified'

    def test_annual_yield_percentage_auto_corrected(self):
        from app.dividend_service import _validate_dividend_data
        data = {**_VALID_DIV_RESPONSE, 'annual_yield': 3.1}  # looks like 3.1%, not 310%
        result = _validate_dividend_data(data)
        assert result['annual_yield'] == pytest.approx(0.031)

    def test_missing_is_dividend_payer_raises(self):
        from app.dividend_service import _validate_dividend_data
        data = {k: v for k, v in _VALID_DIV_RESPONSE.items() if k != 'is_dividend_payer'}
        with pytest.raises(ValueError, match='is_dividend_payer'):
            _validate_dividend_data(data)

    def test_non_payer_zero_yield_passes(self):
        from app.dividend_service import _validate_dividend_data
        result = _validate_dividend_data(_NON_PAYER_RESPONSE.copy())
        assert result['is_dividend_payer'] is False
        assert result['annual_yield'] == 0.0
        assert result['frequency'] is None

    def test_unknown_payer_type_falls_back(self):
        from app.dividend_service import _validate_dividend_data
        data = {**_VALID_DIV_RESPONSE, 'payer_type': 'unknown_fund_type'}
        result = _validate_dividend_data(data)
        assert result['payer_type'] == 'etf'

    def test_null_string_frequency_normalized(self):
        from app.dividend_service import _validate_dividend_data
        data = {**_VALID_DIV_RESPONSE, 'frequency': 'null'}
        result = _validate_dividend_data(data)
        assert result['frequency'] is None

    def test_source_notes_contains_cut_risk(self):
        from app.dividend_service import _validate_dividend_data
        result = _validate_dividend_data(_VALID_DIV_RESPONSE.copy())
        notes = json.loads(result['source_notes'])
        assert notes['cut_risk'] == 'low'
        assert notes['ttm_yield'] is True


# ---------------------------------------------------------------------------
# 2. Service-level tests (mocked Anthropic client)
# ---------------------------------------------------------------------------

class TestFetchDividendData:
    def test_happy_path_returns_validated_dict(self):
        from app.dividend_service import fetch_dividend_data
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            result = fetch_dividend_data('VYM', 'sk-test')
        assert result['annual_yield'] == pytest.approx(0.031)
        assert result['is_dividend_payer'] is True

    def test_non_payer_handled(self):
        from app.dividend_service import fetch_dividend_data
        with patch('anthropic.Anthropic', return_value=_make_mock_client(json.dumps(_NON_PAYER_RESPONSE))):
            result = fetch_dividend_data('GOOG', 'sk-test')
        assert result['is_dividend_payer'] is False
        assert result['annual_yield'] == 0.0

    def test_bad_json_raises_value_error(self):
        from app.dividend_service import fetch_dividend_data
        with patch('anthropic.Anthropic', return_value=_make_mock_client('not json')):
            with pytest.raises(ValueError, match='non-JSON'):
                fetch_dividend_data('XYZ', 'sk-test')

    def test_no_api_key_raises_runtime_error(self):
        from app.dividend_service import fetch_dividend_data
        with pytest.raises(RuntimeError, match='API key'):
            fetch_dividend_data('VYM', '')

    def test_markdown_fences_stripped(self):
        from app.dividend_service import fetch_dividend_data
        raw = '```json\n' + json.dumps(_VALID_DIV_RESPONSE) + '\n```'
        with patch('anthropic.Anthropic', return_value=_make_mock_client(raw)):
            result = fetch_dividend_data('VYM', 'sk-test')
        assert result['annual_yield'] == pytest.approx(0.031)


class TestGetOrFetch:
    def test_cache_miss_fetches_and_stores(self, db):
        from app.dividend_service import get_or_fetch
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            data, from_cache = get_or_fetch('VYM', 'sk-test')
        assert from_cache is False
        assert data['annual_yield'] == pytest.approx(0.031)
        stored = DividendData.query.filter_by(ticker='VYM').first()
        assert stored is not None
        assert float(stored.annual_yield) == pytest.approx(0.031)

    def test_cache_hit_within_30_days(self, db):
        from app.dividend_service import get_or_fetch
        row = DividendData(
            ticker='VYM', annual_yield=0.031, is_dividend_payer=True,
            last_fetched_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.commit()
        data, from_cache = get_or_fetch('VYM', 'sk-test')
        assert from_cache is True

    def test_stale_cache_triggers_refetch(self, db):
        from app.dividend_service import get_or_fetch
        stale_time = datetime.utcnow() - timedelta(days=31)
        row = DividendData(
            ticker='VYM', annual_yield=0.025, is_dividend_payer=True,
            last_fetched_at=stale_time,
        )
        db.session.add(row)
        db.session.commit()
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            data, from_cache = get_or_fetch('VYM', 'sk-test')
        assert from_cache is False
        assert data['annual_yield'] == pytest.approx(0.031)

    def test_force_bypasses_fresh_cache(self, db):
        from app.dividend_service import get_or_fetch
        row = DividendData(
            ticker='VYM', annual_yield=0.025, is_dividend_payer=True,
            last_fetched_at=datetime.utcnow(),
        )
        db.session.add(row)
        db.session.commit()
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            data, from_cache = get_or_fetch('VYM', 'sk-test', force=True)
        assert from_cache is False
        assert data['annual_yield'] == pytest.approx(0.031)

    def test_fetch_error_no_cache_raises(self, db):
        from app.dividend_service import get_or_fetch
        with patch('anthropic.Anthropic', side_effect=Exception('network failure')):
            with pytest.raises(Exception, match='network failure'):
                get_or_fetch('FAIL', 'sk-test')


# ---------------------------------------------------------------------------
# 3. Calculation unit tests
# ---------------------------------------------------------------------------

class TestCalculateCurrentIncome:
    def test_basic_income_calculation(self):
        from app.dividend_calc import calculate_current_income
        holdings = [{
            'ticker': 'VYM', 'shares': 100, 'last_price': 100.0,
            'annual_yield': 0.03, 'is_dividend_payer': True,
            'tax_treatment': 'qualified', 'account_tax_status': 'taxable',
            'account_id': 1, 'account_name': 'Brokerage',
        }]
        result = calculate_current_income(holdings)
        assert result['total_annual_income'] == pytest.approx(300.0)
        assert result['total_monthly_income'] == pytest.approx(25.0)

    def test_non_payer_shows_zero_not_excluded(self):
        from app.dividend_calc import calculate_current_income
        holdings = [{
            'ticker': 'GOOG', 'shares': 10, 'last_price': 200.0,
            'annual_yield': 0.0, 'is_dividend_payer': False,
            'tax_treatment': None, 'account_tax_status': 'taxable',
            'account_id': 1, 'account_name': 'Brokerage',
        }]
        result = calculate_current_income(holdings)
        assert len(result['by_holding']) == 1
        assert result['by_holding'][0]['annual_income'] == 0.0
        assert result['total_annual_income'] == 0.0

    def test_after_tax_only_on_taxable(self):
        from app.dividend_calc import calculate_current_income
        holdings = [
            {'ticker': 'VYM', 'shares': 100, 'last_price': 100.0,
             'annual_yield': 0.03, 'is_dividend_payer': True,
             'tax_treatment': 'qualified', 'account_tax_status': 'taxable',
             'account_id': 1, 'account_name': 'Brokerage'},
            {'ticker': 'BND', 'shares': 50, 'last_price': 80.0,
             'annual_yield': 0.04, 'is_dividend_payer': True,
             'tax_treatment': 'ordinary', 'account_tax_status': 'tax_free',
             'account_id': 2, 'account_name': 'Roth IRA'},
        ]
        result = calculate_current_income(holdings, tax_rate=0.27)
        taxable_income = 300.0
        roth_income = 160.0
        expected_after_tax = taxable_income * (1 - 0.27) + roth_income
        assert result['est_after_tax_annual'] == pytest.approx(expected_after_tax)

    def test_by_account_rollup(self):
        from app.dividend_calc import calculate_current_income
        holdings = [
            {'ticker': 'VYM', 'shares': 100, 'last_price': 100.0,
             'annual_yield': 0.03, 'is_dividend_payer': True,
             'tax_treatment': 'qualified', 'account_tax_status': 'taxable',
             'account_id': 1, 'account_name': 'Brokerage'},
            {'ticker': 'VXUS', 'shares': 50, 'last_price': 60.0,
             'annual_yield': 0.02, 'is_dividend_payer': True,
             'tax_treatment': 'qualified', 'account_tax_status': 'taxable',
             'account_id': 1, 'account_name': 'Brokerage'},
        ]
        result = calculate_current_income(holdings)
        assert len(result['by_account']) == 1
        assert result['by_account'][0]['annual_income'] == pytest.approx(360.0)


class TestSimulateDrip:
    def _base_holding(self):
        return [{
            'ticker': 'VYM', 'shares': 100, 'last_price': 100.0,
            'annual_yield': 0.03, 'is_dividend_payer': True, 'frequency': 'quarterly',
        }]

    def test_series_length_matches_horizon(self):
        from app.dividend_calc import simulate_drip
        result = simulate_drip(self._base_holding(), horizon_years=10)
        assert len(result['labels']) == 10 * 12 + 1
        assert len(result['drip_on']) == 10 * 12 + 1

    def test_drip_on_ge_drip_off_ge_no_action(self):
        from app.dividend_calc import simulate_drip
        result = simulate_drip(self._base_holding(), horizon_years=5)
        for i in range(1, len(result['drip_on'])):
            assert result['drip_on'][i] >= result['drip_off'][i], f"DRIP ON < DRIP OFF at index {i}"
            assert result['drip_off'][i] >= result['no_action'][i], f"DRIP OFF < No Action at index {i}"

    def test_callouts_present_for_20yr_horizon(self):
        from app.dividend_calc import simulate_drip
        result = simulate_drip(self._base_holding(), horizon_years=20)
        assert 'yr5_drip_on' in result['callouts']
        assert 'yr10_drip_on' in result['callouts']
        assert 'yr20_drip_on' in result['callouts']
        assert 'drip_advantage_20yr' in result['callouts']

    def test_horizon_capped_at_30(self):
        from app.dividend_calc import simulate_drip
        result = simulate_drip(self._base_holding(), horizon_years=50)
        assert len(result['labels']) == 30 * 12 + 1

    def test_empty_holdings_returns_zeros(self):
        from app.dividend_calc import simulate_drip
        result = simulate_drip([], horizon_years=5)
        assert all(v == 0.0 for v in result['drip_on'])


# ---------------------------------------------------------------------------
# 4. Route integration tests
# ---------------------------------------------------------------------------

class TestDividendDataRoute:
    def test_get_returns_503_when_ai_disabled(self, client):
        resp = client.get('/api/dividend-data/VYM')
        assert resp.status_code == 503
        assert resp.get_json()['manual_required'] is True

    def test_get_fetches_and_caches(self, client, db):
        _enable_ai(db)
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            resp = client.get('/api/dividend-data/VYM')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['annual_yield'] == pytest.approx(0.031)
        assert data['from_cache'] is False

    def test_get_returns_cached_on_second_call(self, client, db):
        _enable_ai(db)
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            client.get('/api/dividend-data/VYM')
        # second call — should hit cache, no API call
        resp = client.get('/api/dividend-data/VYM')
        assert resp.status_code == 200
        assert resp.get_json()['from_cache'] is True

    def test_get_force_bypasses_cache(self, client, db):
        _enable_ai(db)
        row = DividendData(ticker='VYM', annual_yield=0.025, is_dividend_payer=True,
                           last_fetched_at=datetime.utcnow())
        db.session.add(row)
        db.session.commit()
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            resp = client.get('/api/dividend-data/VYM?force=1')
        assert resp.status_code == 200
        assert resp.get_json()['from_cache'] is False


class TestDividendDataUpsertRoute:
    def test_post_creates_row(self, client, db):
        resp = client.post('/api/dividend-data/VYM', json={
            'annual_yield': 0.031, 'is_dividend_payer': True,
            'frequency': 'quarterly', 'payer_type': 'etf',
        })
        assert resp.status_code == 200
        stored = DividendData.query.filter_by(ticker='VYM').first()
        assert stored is not None
        assert float(stored.annual_yield) == pytest.approx(0.031)

    def test_post_updates_existing_row(self, client, db):
        row = DividendData(ticker='VYM', annual_yield=0.025, is_dividend_payer=True)
        db.session.add(row)
        db.session.commit()
        resp = client.post('/api/dividend-data/VYM', json={'annual_yield': 0.035})
        assert resp.status_code == 200
        db.session.refresh(row)
        assert float(row.annual_yield) == pytest.approx(0.035)

    def test_post_rejects_yield_greater_than_one(self, client, db):
        resp = client.post('/api/dividend-data/VYM', json={'annual_yield': 3.1})
        assert resp.status_code == 400

    def test_post_rejects_negative_yield(self, client, db):
        resp = client.post('/api/dividend-data/VYM', json={'annual_yield': -0.5})
        assert resp.status_code == 400


class TestPassiveIncomeRoute:
    def test_no_holdings_returns_zero(self, client, db):
        resp = client.get('/api/passive-income')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total_annual_income'] == 0.0
        assert data['by_holding'] == []

    def test_correct_income_for_seeded_holding(self, client, db):
        acct = _make_investment_account(db)
        _make_holding(db, acct.id, ticker='VYM', shares=100, price=100.0)
        row = DividendData(ticker='VYM', annual_yield=0.03, is_dividend_payer=True,
                           last_fetched_at=datetime.utcnow())
        db.session.add(row)
        db.session.commit()
        resp = client.get('/api/passive-income')
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['total_annual_income'] == pytest.approx(300.0)

    def test_non_payer_in_results_not_hidden(self, client, db):
        acct = _make_investment_account(db)
        _make_holding(db, acct.id, ticker='GOOG', shares=10, price=200.0)
        row = DividendData(ticker='GOOG', annual_yield=0.0, is_dividend_payer=False,
                           last_fetched_at=datetime.utcnow())
        db.session.add(row)
        db.session.commit()
        resp = client.get('/api/passive-income')
        data = resp.get_json()
        assert len(data['by_holding']) == 1
        assert data['by_holding'][0]['is_dividend_payer'] is False
        assert data['total_annual_income'] == 0.0

    def test_taxable_excluded_from_after_tax(self, client, db):
        taxable = _make_investment_account(db, 'Brokerage', 'taxable')
        roth    = _make_investment_account(db, 'Roth IRA',  'tax_free')
        _make_holding(db, taxable.id, ticker='VYM',  shares=100, price=100.0)
        _make_holding(db, roth.id,    ticker='SCHD', shares=100, price=80.0)
        db.session.add(DividendData(ticker='VYM',  annual_yield=0.03, is_dividend_payer=True,
                                    last_fetched_at=datetime.utcnow()))
        db.session.add(DividendData(ticker='SCHD', annual_yield=0.03, is_dividend_payer=True,
                                    last_fetched_at=datetime.utcnow()))
        db.session.commit()
        resp = client.get('/api/passive-income')
        data = resp.get_json()
        # taxable: 100*100*0.03 = 300 → after tax = 300*(1-0.27) = 219
        # roth:    100*80*0.03  = 240 → full = 240
        assert data['est_after_tax_annual'] == pytest.approx(219.0 + 240.0, rel=0.01)

    def test_missing_data_list_populated_when_ai_disabled(self, client, db):
        acct = _make_investment_account(db)
        _make_holding(db, acct.id, ticker='VYM', shares=100, price=100.0)
        # no DividendData row, AI disabled → VYM goes to missing_data
        resp = client.get('/api/passive-income')
        data = resp.get_json()
        assert 'VYM' in data['missing_data']

    def test_account_id_filter(self, client, db):
        acct1 = _make_investment_account(db, 'Brokerage 1')
        acct2 = _make_investment_account(db, 'Brokerage 2')
        _make_holding(db, acct1.id, ticker='VYM',  shares=100, price=100.0)
        _make_holding(db, acct2.id, ticker='SCHD', shares=50,  price=80.0)
        db.session.add(DividendData(ticker='VYM',  annual_yield=0.03, is_dividend_payer=True,
                                    last_fetched_at=datetime.utcnow()))
        db.session.add(DividendData(ticker='SCHD', annual_yield=0.03, is_dividend_payer=True,
                                    last_fetched_at=datetime.utcnow()))
        db.session.commit()
        resp = client.get(f'/api/passive-income?account_id={acct1.id}')
        data = resp.get_json()
        tickers = [h['ticker'] for h in data['by_holding']]
        assert 'VYM' in tickers
        assert 'SCHD' not in tickers


class TestDripProjectionRoute:
    def test_returns_three_series(self, client, db):
        acct = _make_investment_account(db)
        _make_holding(db, acct.id, ticker='VYM', shares=100, price=100.0)
        db.session.add(DividendData(ticker='VYM', annual_yield=0.03, is_dividend_payer=True,
                                    last_fetched_at=datetime.utcnow()))
        db.session.commit()
        resp = client.get('/api/passive-income/projection?horizon_years=5')
        assert resp.status_code == 200
        data = resp.get_json()
        assert 'drip_on' in data
        assert 'drip_off' in data
        assert 'no_action' in data
        assert len(data['labels']) == 5 * 12 + 1

    def test_horizon_capped_at_30(self, client, db):
        resp = client.get('/api/passive-income/projection?horizon_years=99')
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data['labels']) == 30 * 12 + 1

    def test_assumptions_persisted_to_app_settings(self, client, db):
        client.get('/api/passive-income/projection?price_appreciation_rate=8.0')
        stored = AppSetting.query.filter_by(key='drip_price_appreciation_pct').first()
        assert stored is not None
        assert float(stored.value) == pytest.approx(8.0)
