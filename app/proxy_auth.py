"""Gateway auth for the shared Google-SSO deployment in deploy/. AUTH_MODE=none (default)
keeps the LAN-only behaviour; AUTH_MODE=proxy trusts oauth2-proxy's email header only on
requests that also carry the per-app secret Caddy injects, so a request that bypasses the
gateway is refused even if it forges the email header."""
import hmac
import logging
import os

from flask import abort, current_app, g, request
from werkzeug.middleware.proxy_fix import ProxyFix

EMAIL_HEADER = 'X-Forwarded-Email'
SECRET_HEADER = 'X-Proxy-Secret'
AUTH_MODES = ('none', 'proxy')

logger = logging.getLogger(__name__)


def parse_email_list(raw):
    return {email.strip().lower() for email in (raw or '').split(',') if email.strip()}


def init_app(app):
    mode = os.getenv('AUTH_MODE', 'none').strip().lower()
    if mode not in AUTH_MODES:
        raise RuntimeError(f'AUTH_MODE must be one of {AUTH_MODES}, got {mode!r}')
    app.config['AUTH_MODE'] = mode
    # Distinct from other apps on the same domain so their sessions never collide.
    app.config['SESSION_COOKIE_NAME'] = 'ledger_session'
    if mode != 'proxy':
        return

    secret = os.getenv('AUTH_PROXY_SECRET', '')
    allowed = parse_email_list(os.getenv('ALLOWED_EMAILS'))
    if not secret:
        raise RuntimeError('AUTH_MODE=proxy requires AUTH_PROXY_SECRET (must match the gateway).')
    if not allowed:
        raise RuntimeError('AUTH_MODE=proxy requires ALLOWED_EMAILS (comma-separated Google accounts).')

    app.config.update(
        AUTH_PROXY_SECRET=secret,
        ALLOWED_EMAILS=allowed,
        SESSION_COOKIE_SECURE=True,
    )
    # Exactly one trusted hop (Caddy): lets redirects use the public https host.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.before_request(_require_gateway_identity)


def _require_gateway_identity():
    supplied = request.headers.get(SECRET_HEADER, '')
    if not hmac.compare_digest(supplied.encode(), current_app.config['AUTH_PROXY_SECRET'].encode()):
        logger.warning('Rejected %s %s: missing or wrong gateway secret', request.method, request.path)
        abort(401)

    email = request.headers.get(EMAIL_HEADER, '').strip().lower()
    if email not in current_app.config['ALLOWED_EMAILS']:
        logger.warning('Rejected %s %s for %s: not in ALLOWED_EMAILS',
                       request.method, request.path, email or '<no email>')
        abort(403)
    g.user_email = email
