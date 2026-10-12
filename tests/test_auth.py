from urllib.parse import parse_qs, urlsplit

import pytest
from authlib.integrations.base_client import OAuthError

from app import auth_service, create_app
from app.routes.helpers import safe_next_path

GOOGLE_METADATA = {
    'issuer': 'https://accounts.google.com',
    'authorization_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
    'token_endpoint': 'https://oauth2.googleapis.com/token',
    'jwks_uri': 'https://www.googleapis.com/oauth2/v3/certs',
}
ALLOWED = frozenset({'owner@example.com', 'pmanderson@gmail.com'})


def _claims(email='owner@example.com', sub='google-sub-owner', verified=True, name='Owner'):
    return {'sub': sub, 'email': email, 'email_verified': verified, 'name': name}


@pytest.fixture
def google(app, monkeypatch):
    """The app's real Authlib client, with discovery stubbed so nothing hits the network."""
    client = app.extensions['google_oauth']
    monkeypatch.setattr(client, 'load_server_metadata', lambda: GOOGLE_METADATA)
    return client


@pytest.fixture
def google_returns(google, monkeypatch):
    def _set(claims):
        monkeypatch.setattr(google, 'authorize_access_token', lambda: {'userinfo': claims})
    return _set


class TestSafeNextPath:
    @pytest.mark.parametrize('target', ['/accounts', '/month/2024-01?tab=spending'])
    def test_same_site_paths_pass(self, target):
        assert safe_next_path(target) == target

    @pytest.mark.parametrize('target', [
        None, '', 'accounts', 'https://evil.example/', '//evil.example/',
        '/\\evil.example', '/\t/evil.example', '/\n/evil.example',
    ])
    def test_off_site_or_ambiguous_targets_rejected(self, target):
        assert safe_next_path(target) is None


class TestEmailAllowlist:
    @pytest.mark.parametrize('raw, expected', [
        (' Bob@Example.com ', 'bob@example.com'),
        ('P.M.Anderson@gmail.com', 'pmanderson@gmail.com'),
        ('pm.anderson@googlemail.com', 'pmanderson@gmail.com'),
        ('first.last@example.com', 'first.last@example.com'),  # dots only ignored by Gmail
        ('not-an-email', 'not-an-email'),
        (None, ''),
    ])
    def test_normalize_email(self, raw, expected):
        assert auth_service.normalize_email(raw) == expected

    def test_parse_allowed_emails_skips_blanks(self):
        assert auth_service.parse_allowed_emails(' a@x.com, ,B@X.com,') == frozenset({'a@x.com', 'b@x.com'})

    def test_parse_allowed_emails_empty(self):
        assert auth_service.parse_allowed_emails(None) == frozenset()

    def test_authorized_when_verified_and_allowlisted(self):
        assert auth_service.is_authorized(_claims(email='P.M.Anderson@gmail.com'), ALLOWED)

    @pytest.mark.parametrize('claims', [
        _claims(email='stranger@gmail.com'),
        _claims(verified=False),
        _claims(verified='true'),
        {'sub': 'x', 'email': 'owner@example.com'},
        {'sub': 'x', 'email_verified': True},
    ])
    def test_not_authorized(self, claims):
        assert not auth_service.is_authorized(claims, ALLOWED)


class TestLoginPage:
    def test_renders_google_sign_in_without_app_nav(self, anon_client):
        resp = anon_client.get('/login')
        assert resp.status_code == 200
        assert b'Sign in with Google' in resp.data
        assert b'Sign out' not in resp.data
        assert b'href="/accounts"' not in resp.data

    def test_carries_safe_next_into_sign_in_link(self, anon_client):
        resp = anon_client.get('/login?next=/accounts')
        assert b'/auth/google?next=/accounts' in resp.data

    def test_drops_off_site_next(self, anon_client):
        resp = anon_client.get('/login?next=https://evil.example/')
        assert b'evil.example' not in resp.data

    def test_signed_in_user_redirected_to_dashboard(self, client):
        resp = client.get('/login')
        assert resp.status_code == 302
        assert urlsplit(resp.headers['Location']).path == '/'


class TestGoogleLogin:
    def test_redirects_to_google_with_pkce_and_account_picker(self, anon_client, google):
        resp = anon_client.get('/auth/google')
        assert resp.status_code == 302
        url = urlsplit(resp.headers['Location'])
        params = parse_qs(url.query)
        assert f'{url.scheme}://{url.netloc}{url.path}' == GOOGLE_METADATA['authorization_endpoint']
        assert params['client_id'] == ['test-client-id']
        assert params['scope'] == ['openid email profile']
        assert params['prompt'] == ['select_account']
        assert params['code_challenge_method'] == ['S256']
        assert params['redirect_uri'] == ['http://localhost/auth/google/callback']
        assert params['state'] and params['nonce']

    def test_configured_redirect_uri_wins(self, app, anon_client, google, monkeypatch):
        monkeypatch.setitem(app.config, 'GOOGLE_REDIRECT_URI', 'https://ledger.example.com/auth/google/callback')
        resp = anon_client.get('/auth/google')
        params = parse_qs(urlsplit(resp.headers['Location']).query)
        assert params['redirect_uri'] == ['https://ledger.example.com/auth/google/callback']


class TestGoogleCallback:
    def test_allowed_account_signs_in(self, anon_client, google_returns):
        google_returns(_claims(email='Owner@Example.com'))
        resp = anon_client.get('/auth/google/callback')
        assert resp.status_code == 302
        assert urlsplit(resp.headers['Location']).path == '/'
        with anon_client.session_transaction() as sess:
            assert sess['user_email'] == 'owner@example.com'
        assert anon_client.get('/accounts').status_code == 200

    def test_returns_to_stored_next(self, anon_client, google, google_returns):
        anon_client.get('/auth/google?next=/accounts')
        google_returns(_claims())
        resp = anon_client.get('/auth/google/callback')
        assert resp.headers['Location'].endswith('/accounts')

    def test_session_is_permanent(self, anon_client, google_returns):
        google_returns(_claims())
        resp = anon_client.get('/auth/google/callback')
        assert 'Expires=' in resp.headers['Set-Cookie']

    @pytest.mark.parametrize('claims', [_claims(email='stranger@gmail.com'), _claims(verified=False)])
    def test_unauthorized_account_denied_without_session(self, anon_client, google_returns, claims):
        google_returns(claims)
        resp = anon_client.get('/auth/google/callback')
        assert resp.status_code == 403
        assert b'Access denied' in resp.data
        assert anon_client.get('/accounts').status_code == 302

    def test_google_error_returns_to_login(self, anon_client, google, monkeypatch):
        def _raise():
            raise OAuthError(error='access_denied')
        monkeypatch.setattr(google, 'authorize_access_token', _raise)
        resp = anon_client.get('/auth/google/callback?error=access_denied')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']

    def test_forged_state_rejected(self, anon_client, google):
        resp = anon_client.get('/auth/google/callback?code=abc&state=forged')
        assert resp.status_code == 302
        assert '/login' in resp.headers['Location']
        with anon_client.session_transaction() as sess:
            assert 'user_email' not in sess


class TestAccessGuard:
    def test_anonymous_get_redirects_to_login_with_next(self, anon_client):
        resp = anon_client.get('/accounts?show=archived')
        assert resp.status_code == 302
        assert urlsplit(resp.headers['Location']).path == '/login'
        assert parse_qs(urlsplit(resp.headers['Location']).query)['next'] == ['/accounts?show=archived']

    def test_anonymous_unknown_url_does_not_reveal_404(self, anon_client):
        assert anon_client.get('/no-such-page').status_code == 302

    def test_anonymous_api_post_is_401(self, anon_client):
        assert anon_client.post('/api/accounts/batch', json={}).status_code == 401

    def test_auth_check_runs_before_csrf(self, app, anon_client, monkeypatch):
        monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)
        assert anon_client.post('/api/accounts/batch', json={}).status_code == 401

    def test_login_page_stylesheet_is_public(self, anon_client):
        assert anon_client.get('/static/style.css').status_code == 200

    def test_other_static_files_need_sign_in(self, anon_client):
        assert anon_client.get('/static/anything-else.js').status_code == 302

    def test_removing_email_from_config_revokes_live_session(self, app, client, monkeypatch):
        monkeypatch.setitem(app.config, 'ALLOWED_EMAILS', frozenset({'someone-else@example.com'}))
        assert client.get('/accounts').status_code == 403
        monkeypatch.setitem(app.config, 'ALLOWED_EMAILS', ALLOWED)
        assert client.get('/accounts').status_code == 302

    def test_signed_in_pages_show_sign_out(self, client):
        assert b'Sign out' in client.get('/accounts').data

    def test_logout_ends_session(self, client):
        resp = client.post('/logout')
        assert resp.status_code == 302
        assert client.get('/accounts').status_code == 302


class TestConfig:
    @pytest.fixture
    def production_env(self, monkeypatch):
        monkeypatch.setenv('FLASK_ENV', 'production')
        monkeypatch.setenv('SECRET_KEY', 'a-real-secret-key-for-testing-only')
        monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'https://ledger.example.com/auth/google/callback')

    @pytest.mark.parametrize('missing', [
        'GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REDIRECT_URI', 'LEDGER_ALLOWED_EMAILS',
    ])
    def test_production_refuses_to_start_without_auth_config(self, production_env, monkeypatch, missing):
        monkeypatch.setenv(missing, '')
        with pytest.raises(RuntimeError, match=missing):
            create_app()

    def test_development_warns_but_starts_without_auth_config(self, monkeypatch, caplog):
        monkeypatch.delenv('FLASK_ENV', raising=False)
        monkeypatch.setenv('LEDGER_ALLOWED_EMAILS', '')
        create_app()
        assert any('LEDGER_ALLOWED_EMAILS' in r.getMessage() for r in caplog.records)

    def test_production_session_cookie_hardening(self, production_env):
        config = create_app().config
        assert config['SESSION_COOKIE_SECURE'] is True
        assert config['SESSION_COOKIE_HTTPONLY'] is True
        assert config['SESSION_COOKIE_SAMESITE'] == 'Lax'
        assert config['PERMANENT_SESSION_LIFETIME'].days == 14
