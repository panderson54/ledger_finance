"""AUTH_MODE=proxy: only requests that come through the Google-SSO gateway get in."""
import pytest

from app import create_app
from app import db as _db
from app.proxy_auth import EMAIL_HEADER, SECRET_HEADER, parse_email_list

SECRET = 'gateway-secret'


@pytest.fixture
def proxy_client(monkeypatch):
    monkeypatch.setenv('AUTH_MODE', 'proxy')
    monkeypatch.setenv('AUTH_PROXY_SECRET', SECRET)
    monkeypatch.setenv('ALLOWED_EMAILS', 'Owner@Gmail.com, partner@gmail.com')
    application = create_app()
    application.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
    with application.app_context():
        _db.create_all()
        yield application.test_client()
        _db.session.remove()
        _db.drop_all()


def _headers(email='owner@gmail.com', secret=SECRET):
    return {EMAIL_HEADER: email, SECRET_HEADER: secret}


class TestParseEmailList:
    def test_normalises_case_whitespace_and_blanks(self):
        assert parse_email_list(' A@x.com, ,b@X.com ') == {'a@x.com', 'b@x.com'}

    def test_empty(self):
        assert parse_email_list(None) == set()


class TestStartupConfig:
    def test_default_mode_is_none_and_needs_no_headers(self, client):
        assert client.application.config['AUTH_MODE'] == 'none'
        assert client.get('/settings').status_code == 200

    def test_session_cookie_is_app_specific(self, client):
        assert client.application.config['SESSION_COOKIE_NAME'] == 'ledger_session'

    def test_unknown_mode_raises(self, monkeypatch):
        monkeypatch.setenv('AUTH_MODE', 'google')
        with pytest.raises(RuntimeError, match='AUTH_MODE'):
            create_app()

    def test_proxy_mode_without_secret_raises(self, monkeypatch):
        monkeypatch.setenv('AUTH_MODE', 'proxy')
        monkeypatch.delenv('AUTH_PROXY_SECRET', raising=False)
        monkeypatch.setenv('ALLOWED_EMAILS', 'owner@gmail.com')
        with pytest.raises(RuntimeError, match='AUTH_PROXY_SECRET'):
            create_app()

    def test_proxy_mode_without_allowlist_raises(self, monkeypatch):
        monkeypatch.setenv('AUTH_MODE', 'proxy')
        monkeypatch.setenv('AUTH_PROXY_SECRET', SECRET)
        monkeypatch.delenv('ALLOWED_EMAILS', raising=False)
        with pytest.raises(RuntimeError, match='ALLOWED_EMAILS'):
            create_app()


class TestGatewayCheck:
    def test_allowlisted_email_through_gateway_succeeds(self, proxy_client):
        resp = proxy_client.get('/settings', headers=_headers())
        assert resp.status_code == 200
        assert b'owner@gmail.com' in resp.data
        assert b'/oauth2/sign_out' in resp.data

    def test_email_match_is_case_insensitive(self, proxy_client):
        assert proxy_client.get('/settings', headers=_headers('PARTNER@gmail.com')).status_code == 200

    def test_missing_secret_is_401_even_with_valid_email(self, proxy_client):
        assert proxy_client.get('/settings', headers={EMAIL_HEADER: 'owner@gmail.com'}).status_code == 401

    def test_wrong_secret_is_401(self, proxy_client):
        assert proxy_client.get('/settings', headers=_headers(secret='guess')).status_code == 401

    def test_email_not_on_allowlist_is_403(self, proxy_client):
        assert proxy_client.get('/settings', headers=_headers('stranger@gmail.com')).status_code == 403

    def test_missing_email_is_403(self, proxy_client):
        assert proxy_client.get('/settings', headers={SECRET_HEADER: SECRET}).status_code == 403

    def test_api_routes_are_gated_too(self, proxy_client):
        assert proxy_client.get('/api/months').status_code == 401
