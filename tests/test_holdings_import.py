"""
Tests for AI-based holdings import:
  - app/holdings_import_service.py  (unit tests for image and text extraction)
  - POST /api/accounts/<id>/holdings/import-screenshot  (legacy route tests)
  - POST /api/accounts/<id>/holdings/import-ai          (new route tests)
"""
import io
from unittest.mock import patch

import pytest

from tests.conftest import (
    make_anthropic_mock_client,
    make_image_upload,
    make_investment_account,
    seed_ai_settings,
)


# ---------------------------------------------------------------------------
# Service unit tests
# ---------------------------------------------------------------------------

class TestExtractHoldingsFromImage:
    """Tests for holdings_import_service.extract_holdings_from_image."""

    def _call(self, response_json=None, response_text=None, image_bytes=b'fakeimage', mime_type='image/jpeg'):
        from app.holdings_import_service import extract_holdings_from_image
        mock_client = make_anthropic_mock_client(response_json=response_json, response_text=response_text)
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            return extract_holdings_from_image(image_bytes, mime_type, api_key='sk-test')

    def test_extract_success_list(self):
        result = self._call([{'ticker': 'VTI', 'shares': 42.5}, {'ticker': 'VXUS', 'shares': 18}])
        assert result == [{'ticker': 'VTI', 'shares': 42.5}, {'ticker': 'VXUS', 'shares': 18.0}]

    def test_extract_wrapped_in_holdings_key(self):
        result = self._call({'holdings': [{'ticker': 'AAPL', 'shares': 10.0}]})
        assert result == [{'ticker': 'AAPL', 'shares': 10.0}]

    def test_extract_wrapped_empty_array(self):
        result = self._call({'holdings': []})
        assert result == []

    def test_extract_empty_array(self):
        result = self._call([])
        assert result == []

    def test_extract_normalises_ticker_to_uppercase(self):
        result = self._call([{'ticker': 'vti', 'shares': 5}])
        assert result[0]['ticker'] == 'VTI'

    def test_extract_filters_missing_shares(self):
        result = self._call([{'ticker': 'VTI'}, {'ticker': 'VXUS', 'shares': 10}])
        assert len(result) == 1
        assert result[0]['ticker'] == 'VXUS'

    def test_extract_filters_missing_ticker(self):
        result = self._call([{'shares': 10}, {'ticker': 'BND', 'shares': 5}])
        assert len(result) == 1
        assert result[0]['ticker'] == 'BND'

    def test_extract_filters_zero_shares(self):
        result = self._call([{'ticker': 'VTI', 'shares': 0}, {'ticker': 'BND', 'shares': 1}])
        assert len(result) == 1

    def test_extract_filters_negative_shares(self):
        result = self._call([{'ticker': 'VTI', 'shares': -5}])
        assert result == []

    def test_extract_filters_non_dict_items(self):
        result = self._call([None, 'bad', {'ticker': 'VTI', 'shares': 5}])
        assert len(result) == 1

    def test_extract_ticker_too_long_filtered(self):
        result = self._call([{'ticker': 'TOOLONGTICKER', 'shares': 5}])
        assert result == []

    def test_extract_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            self._call(response_text='this is not json')

    def test_validate_holdings_mixed_valid_and_invalid(self):
        from app.holdings_import_service import _validate_holdings
        raw = [
            {'ticker': 'AAPL', 'shares': 10},       # valid
            {'shares': 5},                            # missing ticker
            {'ticker': 'VTI'},                        # missing shares
            {'ticker': 'TOOLONGTICKER', 'shares': 5}, # ticker too long
            {'ticker': 'BND', 'shares': 'bad'},       # shares not a number
            {'ticker': 'VOO', 'shares': 0},           # zero shares
            {'ticker': 'VEA', 'shares': 15.5},        # valid, fractional
        ]
        result = _validate_holdings(raw)
        assert len(result) == 2
        assert result[0] == {'ticker': 'AAPL', 'shares': 10.0}
        assert result[1] == {'ticker': 'VEA', 'shares': 15.5}

    def test_make_anthropic_client_raises_runtime_error(self):
        from app.holdings_import_service import extract_holdings_from_image
        with patch('app.holdings_import_service.make_anthropic_client',
                   side_effect=RuntimeError('anthropic package not installed')):
            with pytest.raises(RuntimeError, match='not installed'):
                extract_holdings_from_image(b'img', 'image/jpeg', api_key='sk-test')


class TestExtractHoldingsFromText:
    """Tests for holdings_import_service.extract_holdings_from_text."""

    def _call(self, response_json=None, response_text=None, text='Ticker Shares\nVTI 42'):
        from app.holdings_import_service import extract_holdings_from_text
        mock_client = make_anthropic_mock_client(response_json=response_json, response_text=response_text)
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            return extract_holdings_from_text(text, api_key='sk-test')

    def test_extract_success_list(self):
        result = self._call([{'ticker': 'VTI', 'shares': 42.5}, {'ticker': 'VXUS', 'shares': 18}])
        assert result == [{'ticker': 'VTI', 'shares': 42.5}, {'ticker': 'VXUS', 'shares': 18.0}]

    def test_extract_wrapped_in_holdings_key(self):
        result = self._call({'holdings': [{'ticker': 'AAPL', 'shares': 10.0}]})
        assert result == [{'ticker': 'AAPL', 'shares': 10.0}]

    def test_extract_empty_array(self):
        result = self._call([])
        assert result == []

    def test_extract_normalises_ticker_to_uppercase(self):
        result = self._call([{'ticker': 'vti', 'shares': 5}])
        assert result[0]['ticker'] == 'VTI'

    def test_extract_filters_invalid_items(self):
        result = self._call([{'ticker': 'VTI'}, {'shares': 10}, {'ticker': 'BND', 'shares': 5}])
        assert len(result) == 1
        assert result[0]['ticker'] == 'BND'

    def test_extract_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError):
            self._call(response_text='not valid json')

    def test_make_anthropic_client_raises_runtime_error(self):
        from app.holdings_import_service import extract_holdings_from_text
        with patch('app.holdings_import_service.make_anthropic_client',
                   side_effect=RuntimeError('anthropic package not installed')):
            with pytest.raises(RuntimeError, match='not installed'):
                extract_holdings_from_text('some text', api_key='sk-test')


# ---------------------------------------------------------------------------
# Route integration tests
# ---------------------------------------------------------------------------

class TestImportScreenshotRoute:
    """Tests for POST /api/accounts/<id>/holdings/import-screenshot."""

    def _post(self, client, account_id, image=None):
        if image is None:
            image = make_image_upload()
        return client.post(
            f'/api/accounts/{account_id}/holdings/import-screenshot',
            data={'image': image},
            content_type='multipart/form-data',
        )

    def test_account_not_found(self, client, db):
        resp = self._post(client, account_id=9999)
        assert resp.status_code == 404
        assert 'not found' in resp.get_json()['error'].lower()

    def test_no_api_key_returns_503(self, client, db):
        acct = make_investment_account(db.session)
        resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 503
        body = resp.get_json()
        assert 'error' in body
        assert 'not configured' in body['error'].lower()

    def test_missing_file_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = client.post(
            f'/api/accounts/{acct.id}/holdings/import-screenshot',
            data={},
            content_type='multipart/form-data',
        )
        assert resp.status_code == 400
        assert 'image file is required' in resp.get_json()['error']

    def test_unsupported_mime_type_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = self._post(client, account_id=acct.id, image=make_image_upload(content_type='text/plain'))
        assert resp.status_code == 400
        assert 'unsupported image type' in resp.get_json()['error']

    def test_image_too_large_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = self._post(client, account_id=acct.id,
                          image=make_image_upload(content=b'x' * (10 * 1024 * 1024 + 1)))
        assert resp.status_code == 400
        assert '10 MB' in resp.get_json()['error']

    def test_successful_extraction(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        extracted = [{'ticker': 'VTI', 'shares': 10.0}, {'ticker': 'SCHD', 'shares': 5.5}]
        mock_client = make_anthropic_mock_client(response_json=extracted)
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['holdings'] == extracted

    def test_success_response_always_has_holdings_key(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_json=[])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'holdings' in body
        assert isinstance(body['holdings'], list)

    def test_claude_returns_invalid_json_gives_500(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_text='not valid json at all')
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 500
        assert 'error' in resp.get_json()

    def test_runtime_error_from_client_gives_500(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        with patch('app.holdings_import_service.make_anthropic_client',
                   side_effect=RuntimeError('anthropic not installed')):
            resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 500
        assert 'error' in resp.get_json()

    def test_png_mime_type_accepted(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_json=[{'ticker': 'VTI', 'shares': 1.0}])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id,
                              image=make_image_upload(filename='screen.png', content_type='image/png'))
        assert resp.status_code == 200

    def test_webp_mime_type_accepted(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_json=[{'ticker': 'VTI', 'shares': 1.0}])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id,
                              image=make_image_upload(filename='screen.webp', content_type='image/webp'))
        assert resp.status_code == 200

    def test_gif_mime_type_accepted(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_json=[{'ticker': 'VTI', 'shares': 1.0}])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id,
                              image=make_image_upload(filename='screen.gif', content_type='image/gif'))
        assert resp.status_code == 200


class TestImportAIRoute:
    """Tests for POST /api/accounts/<id>/holdings/import-ai (image and text modes)."""

    def _post_image(self, client, account_id, image=None):
        if image is None:
            image = make_image_upload()
        return client.post(
            f'/api/accounts/{account_id}/holdings/import-ai',
            data={'image': image},
            content_type='multipart/form-data',
        )

    def _post_text(self, client, account_id, text='Ticker Shares\nVTI 10'):
        return client.post(
            f'/api/accounts/{account_id}/holdings/import-ai',
            data={'text': text},
            content_type='multipart/form-data',
        )

    def test_account_not_found(self, client, db):
        resp = self._post_image(client, account_id=9999)
        assert resp.status_code == 404

    def test_no_api_key_returns_503(self, client, db):
        acct = make_investment_account(db.session)
        resp = self._post_image(client, account_id=acct.id)
        assert resp.status_code == 503

    def test_neither_image_nor_text_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = client.post(
            f'/api/accounts/{acct.id}/holdings/import-ai',
            data={},
            content_type='multipart/form-data',
        )
        assert resp.status_code == 400
        assert 'error' in resp.get_json()

    def test_unsupported_image_mime_type_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = self._post_image(client, account_id=acct.id,
                                image=make_image_upload(content_type='text/plain'))
        assert resp.status_code == 400
        assert 'unsupported image type' in resp.get_json()['error']

    def test_image_too_large_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = self._post_image(client, account_id=acct.id,
                                image=make_image_upload(content=b'x' * (10 * 1024 * 1024 + 1)))
        assert resp.status_code == 400
        assert '10 MB' in resp.get_json()['error']

    def test_image_mode_successful_extraction(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        extracted = [{'ticker': 'VTI', 'shares': 10.0}, {'ticker': 'SCHD', 'shares': 5.5}]
        mock_client = make_anthropic_mock_client(response_json=extracted)
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post_image(client, account_id=acct.id)
        assert resp.status_code == 200
        assert resp.get_json()['holdings'] == extracted

    def test_text_mode_successful_extraction(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        extracted = [{'ticker': 'VTI', 'shares': 42.0}]
        mock_client = make_anthropic_mock_client(response_json=extracted)
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post_text(client, account_id=acct.id, text='VTI\t42\nVXUS\t18')
        assert resp.status_code == 200
        assert resp.get_json()['holdings'] == extracted

    def test_text_mode_whitespace_only_treated_as_missing(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        resp = self._post_text(client, account_id=acct.id, text='   ')
        assert resp.status_code == 400

    def test_image_mode_invalid_json_gives_500(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_text='not valid json')
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post_image(client, account_id=acct.id)
        assert resp.status_code == 500
        assert 'error' in resp.get_json()

    def test_text_mode_invalid_json_gives_500(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_text='not valid json')
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post_text(client, account_id=acct.id)
        assert resp.status_code == 500
        assert 'error' in resp.get_json()

    def test_success_response_always_has_holdings_key(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_json=[])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post_text(client, account_id=acct.id)
        assert resp.status_code == 200
        body = resp.get_json()
        assert 'holdings' in body
        assert isinstance(body['holdings'], list)
