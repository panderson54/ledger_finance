"""
Unit tests for shared route helper functions.

Covers:
  - _validate_allocation_splits  (pure, no DB)
  - _get_anthropic_api_key       (reads AppSetting + env fallback)
  - _get_api_key_and_check_enabled (full enabled + key gate)
  - Holdings CRUD allocation validation via the API endpoints
  - Silent opening-balance error no longer swallowed silently
"""
import os
from unittest.mock import patch

import pytest

from app import db as _db
from app.models import Account, AppSetting, Holding, HoldingAllocation, AccountSnapshot


# ---------------------------------------------------------------------------
# _validate_allocation_splits — pure function tests
# ---------------------------------------------------------------------------

class TestValidateAllocationSplits:
    def test_valid_splits_sum_to_100(self):
        from app.routes.helpers import _validate_allocation_splits
        allocs, err = _validate_allocation_splits(
            {'domestic': 70, 'international': 15, 'bonds': 10, 'cash': 5}
        )
        assert err is None
        assert allocs['domestic'] == 70.0

    def test_empty_splits_allowed(self):
        from app.routes.helpers import _validate_allocation_splits
        allocs, err = _validate_allocation_splits({})
        assert err is None
        assert all(v == 0.0 for v in allocs.values())

    def test_zero_splits_allowed(self):
        from app.routes.helpers import _validate_allocation_splits
        allocs, err = _validate_allocation_splits(
            {'domestic': 0, 'international': 0, 'bonds': 0, 'cash': 0}
        )
        assert err is None

    def test_sum_within_tolerance_allowed(self):
        # 99.6 is within the 0.5 tolerance
        from app.routes.helpers import _validate_allocation_splits
        _, err = _validate_allocation_splits(
            {'domestic': 70, 'international': 14.6, 'bonds': 10, 'cash': 5}
        )
        assert err is None

    def test_sum_outside_tolerance_returns_error(self):
        from app.routes.helpers import _validate_allocation_splits
        _, err = _validate_allocation_splits(
            {'domestic': 70, 'international': 20, 'bonds': 10, 'cash': 5}
        )
        assert err is not None
        assert '105.0' in err

    def test_non_numeric_values_default_to_zero(self):
        from app.routes.helpers import _validate_allocation_splits
        allocs, err = _validate_allocation_splits({'domestic': 'abc', 'bonds': None})
        assert allocs['domestic'] == 0.0
        assert allocs['bonds'] == 0.0
        assert err is None

    def test_partial_keys_remaining_default_to_zero(self):
        from app.routes.helpers import _validate_allocation_splits
        allocs, err = _validate_allocation_splits({'domestic': 100})
        assert err is None
        assert allocs['international'] == 0.0


# ---------------------------------------------------------------------------
# _get_anthropic_api_key — env + DB sources
# ---------------------------------------------------------------------------

class TestGetAnthropicApiKey:
    def test_returns_key_from_app_settings(self, db):
        _db.session.add(AppSetting(key='anthropic_api_key', value='sk-from-db', description='test'))
        _db.session.commit()
        from app.routes.helpers import _get_anthropic_api_key
        assert _get_anthropic_api_key() == 'sk-from-db'

    def test_falls_back_to_env_when_no_db_key(self, db):
        from app.routes.helpers import _get_anthropic_api_key
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-from-env'}):
            assert _get_anthropic_api_key() == 'sk-from-env'

    def test_db_key_takes_precedence_over_env(self, db):
        _db.session.add(AppSetting(key='anthropic_api_key', value='sk-db-wins', description='test'))
        _db.session.commit()
        from app.routes.helpers import _get_anthropic_api_key
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'sk-env-loses'}):
            assert _get_anthropic_api_key() == 'sk-db-wins'

    def test_returns_empty_string_when_neither_configured(self, db):
        from app.routes.helpers import _get_anthropic_api_key
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop('ANTHROPIC_API_KEY', None)
            assert _get_anthropic_api_key() == ''


# ---------------------------------------------------------------------------
# _get_api_key_and_check_enabled
# ---------------------------------------------------------------------------

class TestGetApiKeyAndCheckEnabled:
    def test_returns_error_when_disabled(self, db):
        from app.routes.helpers import _get_api_key_and_check_enabled
        key, err = _get_api_key_and_check_enabled()
        assert key is None
        assert err is not None

    def test_returns_error_when_enabled_but_no_key(self, db):
        _db.session.add(AppSetting(key='claude_classification_enabled', value='true', description='t'))
        _db.session.commit()
        from app.routes.helpers import _get_api_key_and_check_enabled
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop('ANTHROPIC_API_KEY', None)
            key, err = _get_api_key_and_check_enabled()
        assert key is None
        assert err is not None

    def test_returns_key_when_fully_configured(self, db):
        _db.session.add(AppSetting(key='claude_classification_enabled', value='true', description='t'))
        _db.session.add(AppSetting(key='anthropic_api_key', value='sk-ok', description='t'))
        _db.session.commit()
        from app.routes.helpers import _get_api_key_and_check_enabled
        key, err = _get_api_key_and_check_enabled()
        assert key == 'sk-ok'
        assert err is None


# ---------------------------------------------------------------------------
# Holdings CRUD — allocation validation via API
# ---------------------------------------------------------------------------

def _brokerage(name='Brokerage'):
    a = Account(name=name, account_type='asset', category='brokerage',
                is_liquid=True, include_in_networth=True, is_active=True)
    _db.session.add(a)
    _db.session.commit()
    return a


class TestHoldingAllocationValidation:
    def test_create_holding_rejects_bad_sum(self, client, db):
        acct = _brokerage()
        r = client.post(f'/api/accounts/{acct.id}/holdings', json={
            'ticker': 'VTI', 'shares': 10,
            'allocations': {'domestic': 80, 'international': 30},
        })
        assert r.status_code == 400
        assert 'sum' in r.get_json()['error'].lower()

    def test_create_holding_accepts_valid_sum(self, client, db):
        acct = _brokerage('Brok2')
        with patch('app.price_service.get_price', return_value=(100.0, 'VTI ETF')):
            r = client.post(f'/api/accounts/{acct.id}/holdings', json={
                'ticker': 'VTI', 'shares': 5,
                'allocations': {'domestic': 100},
            })
        assert r.status_code == 201

    def test_create_holding_accepts_zero_allocations(self, client, db):
        acct = _brokerage('Brok3')
        with patch('app.price_service.get_price', return_value=(50.0, 'BOND ETF')):
            r = client.post(f'/api/accounts/{acct.id}/holdings', json={
                'ticker': 'BND', 'shares': 20,
            })
        assert r.status_code == 201

    def test_update_holding_rejects_bad_sum(self, client, db):
        acct = _brokerage('Brok4')
        h = Holding(account_id=acct.id, ticker='SCHD', shares=10, last_price=75.0, is_active=True)
        _db.session.add(h)
        _db.session.commit()
        r = client.put(f'/api/holdings/{h.id}', json={
            'allocations': {'domestic': 50, 'international': 60},
        })
        assert r.status_code == 400
        assert 'sum' in r.get_json()['error'].lower()

    def test_update_holding_accepts_valid_sum(self, client, db):
        acct = _brokerage('Brok5')
        h = Holding(account_id=acct.id, ticker='SCHD', shares=10, last_price=75.0, is_active=True)
        _db.session.add(h)
        _db.session.commit()
        r = client.put(f'/api/holdings/{h.id}', json={
            'allocations': {'domestic': 60, 'international': 40},
        })
        assert r.status_code == 200
        assert r.get_json()['ticker'] == 'SCHD'


# ---------------------------------------------------------------------------
# accounts.py — opening balance error logs rather than silently passes
# ---------------------------------------------------------------------------

def test_account_create_bad_opening_balance_is_logged(client, db):
    """A non-numeric opening balance must log a warning, not raise an exception."""
    import logging
    with patch.object(logging.getLogger('app.routes.accounts'), 'warning') as mock_warn:
        r = client.post('/accounts/new', data={
            'name': 'Balance Test',
            'account_type': 'asset',
            'category': 'checking',
            'is_active': 'on',
            'opening_balance': 'not-a-number',
            'opening_month': '2024-01',
        }, follow_redirects=True)
    assert r.status_code == 200
    acct = Account.query.filter_by(name='Balance Test').first()
    assert acct is not None
    mock_warn.assert_called_once()
    assert 'opening balance' in mock_warn.call_args[0][0].lower()


def test_account_create_bad_opening_month_is_logged(client, db):
    """A bad month string in opening balance must log, not raise."""
    import logging
    with patch.object(logging.getLogger('app.routes.accounts'), 'warning') as mock_warn:
        r = client.post('/accounts/new', data={
            'name': 'Month Test',
            'account_type': 'asset',
            'category': 'savings',
            'is_active': 'on',
            'opening_balance': '5000',
            'opening_month': 'not-a-month',
        }, follow_redirects=True)
    assert r.status_code == 200
    mock_warn.assert_called_once()
