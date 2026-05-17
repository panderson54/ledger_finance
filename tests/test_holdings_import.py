"""
Tests for screenshot-based holdings import:
  - app/holdings_import_service.py  (unit tests)
  - POST /api/accounts/<id>/holdings/import-screenshot  (route tests)
"""
import io
import json
from unittest.mock import patch

import pytest

from tests.conftest import (
    make_anthropic_mock_client,
    make_investment_account,
    seed_ai_settings,
)


# ---------------------------------------------------------------------------
# Service unit tests
# ---------------------------------------------------------------------------

class TestExtractHoldingsFromImage:
    """Tests for holdings_import_service.extract_holdings_from_image."""

    def _call(self, response_json, image_bytes=b'fakeimage', mime_type='image/jpeg'):
        from app.holdings_import_service import extract_holdings_from_image
        mock_client = make_anthropic_mock_client(response_json=response_json)
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            return extract_holdings_from_image(image_bytes, mime_type, api_key='sk-test')

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

    def test_extract_invalid_json_raises_value_error(self, db):
        from app.holdings_import_service import extract_holdings_from_image
        mock_client = make_anthropic_mock_client(response_text='this is not json')
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(ValueError):
                extract_holdings_from_image(b'img', 'image/jpeg', api_key='sk-test')

    def test_extract_filters_non_dict_items(self):
        result = self._call([None, 'bad', {'ticker': 'VTI', 'shares': 5}])
        assert len(result) == 1

    def test_extract_ticker_too_long_filtered(self):
        result = self._call([{'ticker': 'TOOLONGTICKER', 'shares': 5}])
        assert result == []


# ---------------------------------------------------------------------------
# Route integration tests
# ---------------------------------------------------------------------------

def _make_image_upload(content=b'fakeimage', filename='screenshot.jpg', content_type='image/jpeg'):
    return (io.BytesIO(content), filename, content_type)


class TestImportScreenshotRoute:
    """Tests for POST /api/accounts/<id>/holdings/import-screenshot."""

    def _post(self, client, account_id, image=None, content_type='image/jpeg'):
        if image is None:
            image = _make_image_upload()
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
        bad_file = (io.BytesIO(b'data'), 'file.txt', 'text/plain')
        resp = self._post(client, account_id=acct.id, image=bad_file)
        assert resp.status_code == 400
        assert 'unsupported image type' in resp.get_json()['error']

    def test_image_too_large_returns_400(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        big_image = _make_image_upload(content=b'x' * (10 * 1024 * 1024 + 1))
        resp = self._post(client, account_id=acct.id, image=big_image)
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

    def test_claude_returns_empty_array(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_json=[])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 200
        assert resp.get_json()['holdings'] == []

    def test_claude_returns_invalid_json_gives_500(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        mock_client = make_anthropic_mock_client(response_text='not valid json at all')
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id)
        assert resp.status_code == 500
        assert 'error' in resp.get_json()

    def test_png_mime_type_accepted(self, client, db):
        acct = make_investment_account(db.session)
        seed_ai_settings(db.session)
        png_image = (io.BytesIO(b'pngdata'), 'screen.png', 'image/png')
        mock_client = make_anthropic_mock_client(response_json=[{'ticker': 'VTI', 'shares': 1.0}])
        with patch('app.holdings_import_service.make_anthropic_client', return_value=mock_client):
            resp = self._post(client, account_id=acct.id, image=png_image)
        assert resp.status_code == 200
