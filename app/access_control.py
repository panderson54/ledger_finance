"""Deny-by-default request guard: every request needs a signed-in user whose email is
still on the server-configured allowlist, except the few endpoints signing in needs.
Endpoints marked @agent_token_required instead need a bearer token from LEDGER_MCP_TOKENS."""
import logging

from flask import abort, current_app, g, jsonify, redirect, render_template, request, session, url_for

from app import csrf, mcp_token_service
from app.auth_service import is_email_allowed

logger = logging.getLogger(__name__)

# Ledger has no user table: the signed (tamper-proof) session cookie carries the
# Google-verified email, and the allowlist below is the only source of access.
SESSION_EMAIL_KEY = 'user_email'
SESSION_NAME_KEY = 'user_name'

PUBLIC_ENDPOINTS = frozenset({'main.login', 'main.google_login', 'main.google_callback'})
# Only what the login page itself loads; every other static file stays private.
PUBLIC_STATIC_FILES = frozenset({'style.css'})


def _is_public_request():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return True
    return request.endpoint == 'static' and (request.view_args or {}).get('filename') in PUBLIC_STATIC_FILES


def signed_in_email():
    return session.get(SESSION_EMAIL_KEY)


def agent_token_required(view):
    """Make an endpoint agent-only: it needs a bearer token, and a Google session alone won't do."""
    view.agent_token_required = True
    # Bearer tokens aren't sent automatically by browsers, so there's no CSRF to guard against.
    return csrf.exempt(view)


def _is_agent_endpoint():
    view = current_app.view_functions.get(request.endpoint)
    return getattr(view, 'agent_token_required', False)


def _authenticate_agent():
    token = mcp_token_service.bearer_token(request.headers.get('Authorization'))
    client = mcp_token_service.find_client(token, current_app.config['MCP_TOKENS'])
    if client is None:
        logger.warning('Rejected agent request to %s: missing or invalid bearer token', request.path)
        response = jsonify({'error': 'invalid_token'})
        response.headers['WWW-Authenticate'] = 'Bearer realm="ledger-agent"'
        return response, 401
    g.agent_name = client
    logger.info('Agent request from %s to %s', client, request.path)
    return None


def require_authorized_user():
    # Runs before routing errors are raised, so unknown URLs (endpoint None) are
    # denied too — an anonymous client can't probe which routes exist.
    if _is_public_request():
        return None
    if _is_agent_endpoint():
        return _authenticate_agent()

    email = signed_in_email()
    if not email:
        if request.method in ('GET', 'HEAD'):
            next_path = request.full_path if request.query_string else request.path
            return redirect(url_for('main.login', next=next_path))
        abort(401)

    # Re-checked on every request so removing an address from the config (and
    # restarting) locks that user out even with a still-valid session cookie.
    if not is_email_allowed(email, current_app.config['ALLOWED_EMAILS']):
        logger.warning('Revoking session for %s: no longer on the allowlist', email)
        session.clear()
        return render_template('auth/forbidden.html'), 403

    return None


def register(app):
    app.before_request(require_authorized_user)
