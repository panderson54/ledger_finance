"""
Tests for ticker classification service and API.

Covers:
  - classification_service: classify_ticker(), get_or_classify()
  - Routes: GET /api/classify/<ticker>, GET /api/classifications
  - Routes: GET /settings, POST /api/settings
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from app import db as _db
from app.models import AppSetting, TickerClassification


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_VALID_RESPONSE = {
    "asset_class": "domestic",
    "market_cap_tilt": "large",
    "sector_weights": {"domestic": 100, "international": 0, "bonds": 0, "cash": 0},
}


def _make_mock_client(response_text=None):
    """Return a mock anthropic client whose messages.create returns response_text."""
    if response_text is None:
        response_text = json.dumps(_VALID_RESPONSE)
    block = MagicMock()
    block.text = response_text
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    return client


def _enable_classification(db, api_key="sk-test-key"):
    """Seed AppSetting rows to enable classification with a test API key."""
    db.session.add(AppSetting(
        key='claude_classification_enabled', value='true',
        description='test'))
    db.session.add(AppSetting(
        key='anthropic_api_key', value=api_key,
        description='test'))
    db.session.commit()


# ---------------------------------------------------------------------------
# Service-level unit tests
# ---------------------------------------------------------------------------

class TestValidateClassification:
    def test_valid_single_class(self):
        from app.classification_service import _validate_classification
        result = _validate_classification(_VALID_RESPONSE.copy())
        assert result['asset_class'] == 'domestic'
        assert result['market_cap_tilt'] == 'large'
        assert result['sector_weights']['domestic'] == 100

    def test_null_string_cap_normalized(self):
        from app.classification_service import _validate_classification
        data = {**_VALID_RESPONSE, 'market_cap_tilt': 'null'}
        result = _validate_classification(data)
        assert result['market_cap_tilt'] is None

    def test_empty_string_cap_normalized(self):
        from app.classification_service import _validate_classification
        data = {**_VALID_RESPONSE, 'market_cap_tilt': ''}
        result = _validate_classification(data)
        assert result['market_cap_tilt'] is None

    def test_invalid_asset_class_raises(self):
        from app.classification_service import _validate_classification
        with pytest.raises(ValueError, match='asset_class'):
            _validate_classification({**_VALID_RESPONSE, 'asset_class': 'equities'})

    def test_weights_not_summing_raises(self):
        from app.classification_service import _validate_classification
        bad = {**_VALID_RESPONSE, 'sector_weights': {'domestic': 80, 'international': 0, 'bonds': 0, 'cash': 0}}
        with pytest.raises(ValueError, match='sum to 100'):
            _validate_classification(bad)

    def test_missing_weight_key_raises(self):
        from app.classification_service import _validate_classification
        bad = {**_VALID_RESPONSE, 'sector_weights': {'domestic': 100}}
        with pytest.raises(ValueError, match='missing key'):
            _validate_classification(bad)


class TestClassifyTicker:
    def test_happy_path(self, app):
        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                result = classify_ticker('VTI', api_key='sk-test')
        assert result['asset_class'] == 'domestic'
        assert result['market_cap_tilt'] == 'large'
        assert abs(sum(result['sector_weights'].values()) - 100.0) < 0.01

    def test_raises_runtime_error_when_api_key_empty(self, app):
        with app.app_context():
            from app.classification_service import classify_ticker
            with pytest.raises(RuntimeError, match='API key'):
                classify_ticker('VTI', api_key='')

    def test_raises_value_error_on_non_json(self, app):
        mock_client = _make_mock_client("Sorry, I cannot classify that.")
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                with pytest.raises(ValueError):
                    classify_ticker('UNKNOWN', api_key='sk-test')

    def test_strips_markdown_fences(self, app):
        fenced = "```json\n" + json.dumps(_VALID_RESPONSE) + "\n```"
        mock_client = _make_mock_client(fenced)
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                result = classify_ticker('VTI', api_key='sk-test')
        assert result['asset_class'] == 'domestic'

    def test_web_search_passes_tools(self, app):
        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                classify_ticker('OBSCURE', api_key='sk-test', use_web_search=True)
        kwargs = mock_client.messages.create.call_args.kwargs
        assert 'tools' in kwargs
        assert kwargs['tools'][0]['type'] == 'web_search_20250305'

    def test_no_web_search_omits_tools(self, app):
        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                classify_ticker('VTI', api_key='sk-test', use_web_search=False)
        kwargs = mock_client.messages.create.call_args.kwargs
        assert not kwargs.get('tools')

    def test_uses_prompt_caching(self, app):
        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                classify_ticker('VTI', api_key='sk-test')
        kwargs = mock_client.messages.create.call_args.kwargs
        system = kwargs['system']
        assert isinstance(system, list)
        assert system[0].get('cache_control') == {'type': 'ephemeral'}

    def test_last_text_block_used(self, app):
        """When web search emits tool_use blocks first, only the last text block is used."""
        tool_block = MagicMock(spec=[])  # no .text attribute
        text_block = MagicMock()
        text_block.text = json.dumps(_VALID_RESPONSE)
        resp = MagicMock()
        resp.content = [tool_block, text_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = resp
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import classify_ticker
                result = classify_ticker('VTI', api_key='sk-test')
        assert result['asset_class'] == 'domestic'


class TestGetOrClassify:
    def test_caches_to_db(self, app, db):
        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import get_or_classify
                result, from_cache = get_or_classify('VTI', api_key='sk-test')
        assert from_cache is False
        with app.app_context():
            row = TickerClassification.query.filter_by(ticker='VTI').first()
        assert row is not None
        assert row.asset_class == 'domestic'
        assert row.source == 'claude'

    def test_returns_cached_without_api_call(self, app, db):
        with app.app_context():
            _db.session.add(TickerClassification(
                ticker='CACHED',
                asset_class='bonds',
                market_cap_tilt=None,
                sector_weights=json.dumps({'domestic': 0, 'international': 0, 'bonds': 100, 'cash': 0}),
                source='claude',
            ))
            _db.session.commit()

        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import get_or_classify
                result, from_cache = get_or_classify('CACHED', api_key='sk-test')

        assert from_cache is True
        assert result['asset_class'] == 'bonds'
        mock_client.messages.create.assert_not_called()

    def test_ticker_uppercased(self, app, db):
        mock_client = _make_mock_client()
        with app.app_context():
            with patch('anthropic.Anthropic', return_value=mock_client):
                from app.classification_service import get_or_classify
                result, _ = get_or_classify('vti', api_key='sk-test')
        assert result['ticker'] == 'VTI'


# ---------------------------------------------------------------------------
# Route-level integration tests
# ---------------------------------------------------------------------------

class TestClassifyRoute:
    def test_503_when_feature_disabled(self, client, db):
        r = client.get('/api/classify/VTI')
        assert r.status_code == 503
        assert r.get_json()['manual_required'] is True
        assert 'disabled' in r.get_json()['error']

    def test_503_when_no_api_key(self, client, db, app):
        with app.app_context():
            _db.session.add(AppSetting(key='claude_classification_enabled', value='true'))
            _db.session.commit()
        r = client.get('/api/classify/VTI')
        assert r.status_code == 503
        assert r.get_json()['manual_required'] is True

    def test_200_happy_path(self, client, db, app):
        _enable_classification(db)
        mock_client = _make_mock_client()
        with patch('anthropic.Anthropic', return_value=mock_client):
            r = client.get('/api/classify/VTI')
        assert r.status_code == 200
        body = r.get_json()
        assert body['ticker'] == 'VTI'
        assert body['asset_class'] == 'domestic'
        assert body['market_cap_tilt'] == 'large'
        assert 'sector_weights' in body
        assert body['from_cache'] is False

    def test_second_call_returns_from_cache(self, client, db, app):
        _enable_classification(db)
        mock_client = _make_mock_client()
        with patch('anthropic.Anthropic', return_value=mock_client):
            client.get('/api/classify/VTI')
            r = client.get('/api/classify/VTI')
        assert r.status_code == 200
        assert r.get_json()['from_cache'] is True
        assert mock_client.messages.create.call_count == 1

    def test_ticker_uppercased_in_response(self, client, db, app):
        _enable_classification(db)
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            r = client.get('/api/classify/vti')
        assert r.get_json()['ticker'] == 'VTI'

    def test_force_drops_cache_and_reclassifies(self, client, db, app):
        _enable_classification(db)
        with app.app_context():
            _db.session.add(TickerClassification(
                ticker='VTI',
                asset_class='bonds',   # stale / wrong
                market_cap_tilt=None,
                sector_weights=json.dumps({'domestic': 0, 'international': 0, 'bonds': 100, 'cash': 0}),
                source='manual',
            ))
            _db.session.commit()
        with patch('anthropic.Anthropic', return_value=_make_mock_client()):
            r = client.get('/api/classify/VTI?force=1')
        assert r.status_code == 200
        assert r.get_json()['asset_class'] == 'domestic'
        assert r.get_json()['from_cache'] is False

    def test_web_search_param_forwarded(self, client, db, app):
        _enable_classification(db)
        mock_client = _make_mock_client()
        with patch('anthropic.Anthropic', return_value=mock_client):
            r = client.get('/api/classify/OBSCURE?web_search=1')
        assert r.status_code == 200
        kwargs = mock_client.messages.create.call_args.kwargs
        assert kwargs['tools'][0]['type'] == 'web_search_20250305'


class TestClassificationsListRoute:
    def test_empty_list(self, client, db):
        r = client.get('/api/classifications')
        assert r.status_code == 200
        assert r.get_json() == []

    def test_returns_cached_rows(self, client, db, app):
        with app.app_context():
            _db.session.add(TickerClassification(
                ticker='BND', asset_class='bonds', market_cap_tilt=None,
                sector_weights=json.dumps({'domestic': 0, 'international': 0, 'bonds': 100, 'cash': 0}),
                source='claude',
            ))
            _db.session.commit()
        r = client.get('/api/classifications')
        rows = r.get_json()
        assert len(rows) == 1
        assert rows[0]['ticker'] == 'BND'
        assert rows[0]['asset_class'] == 'bonds'


class TestSettingsRoutes:
    def test_settings_page_renders(self, client, db):
        r = client.get('/settings')
        assert r.status_code == 200
        assert b'AI Ticker Classification' in r.data

    def test_settings_page_shows_disabled_by_default(self, client, db):
        r = client.get('/settings')
        assert b'Disabled' in r.data

    def test_settings_page_shows_enabled_when_set(self, client, db, app):
        _enable_classification(db)
        r = client.get('/settings')
        assert b'Enabled' in r.data

    def test_api_settings_save_toggle(self, client, db, app):
        r = client.post('/api/settings',
                        data=json.dumps({'classification_enabled': True}),
                        content_type='application/json')
        assert r.status_code == 200
        assert r.get_json()['success'] is True
        with app.app_context():
            row = AppSetting.query.filter_by(key='claude_classification_enabled').first()
        assert row.value == 'true'

    def test_api_settings_disable_toggle(self, client, db, app):
        _enable_classification(db)
        client.post('/api/settings',
                    data=json.dumps({'classification_enabled': False}),
                    content_type='application/json')
        with app.app_context():
            row = AppSetting.query.filter_by(key='claude_classification_enabled').first()
        assert row.value == 'false'

    def test_api_settings_save_api_key(self, client, db, app):
        r = client.post('/api/settings',
                        data=json.dumps({'anthropic_api_key': 'sk-ant-test'}),
                        content_type='application/json')
        assert r.status_code == 200
        with app.app_context():
            row = AppSetting.query.filter_by(key='anthropic_api_key').first()
        assert row.value == 'sk-ant-test'

    def test_api_settings_blank_key_not_overwritten(self, client, db, app):
        with app.app_context():
            _db.session.add(AppSetting(key='anthropic_api_key', value='sk-original'))
            _db.session.commit()
        client.post('/api/settings',
                    data=json.dumps({'anthropic_api_key': ''}),
                    content_type='application/json')
        with app.app_context():
            row = AppSetting.query.filter_by(key='anthropic_api_key').first()
        assert row.value == 'sk-original'

    def test_api_settings_accepts_partial_update(self, client, db):
        r = client.post('/api/settings',
                        data=json.dumps({}),
                        content_type='application/json')
        assert r.status_code == 200

    def test_settings_page_shows_key_configured_badge(self, client, db, app):
        with app.app_context():
            _db.session.add(AppSetting(key='anthropic_api_key', value='sk-secret'))
            _db.session.commit()
        r = client.get('/settings')
        assert b'API key configured' in r.data
