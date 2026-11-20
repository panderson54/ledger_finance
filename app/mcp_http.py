"""Serves the site and the remote MCP endpoint from one ASGI app: /mcp goes to the
read-only MCP tools (bearer token from LEDGER_MCP_TOKENS), everything else to Flask,
whose own deny-by-default Google sign-in guard still applies.

They share a process because the SQLite database lives on one disk — a separate
MCP service couldn't reach it.
"""
import json
import logging

from a2wsgi import WSGIMiddleware
from mcp.server.transport_security import TransportSecuritySettings

from app import mcp_token_service
from app.mcp_server import create_mcp_server

logger = logging.getLogger(__name__)

MCP_PATH = '/mcp'


def _is_mcp_path(path):
    return path == MCP_PATH or path.startswith(MCP_PATH + '/')


def _authorization_header(scope):
    for name, value in scope.get('headers', []):
        if name == b'authorization':
            return value.decode('latin-1')
    return None


async def _unauthorized(send):
    body = json.dumps({'error': 'invalid_token'}).encode()
    await send({
        'type': 'http.response.start', 'status': 401,
        'headers': [
            (b'content-type', b'application/json'),
            (b'content-length', str(len(body)).encode()),
            (b'www-authenticate', b'Bearer realm="ledger-mcp"'),
        ],
    })
    await send({'type': 'http.response.body', 'body': body})


def create_asgi_app(flask_app):
    mcp_app = create_mcp_server(flask_app).streamable_http_app(
        streamable_http_path=MCP_PATH,
        # Stateless + plain JSON so any worker process can answer any request;
        # these tools are simple request/response calls with nothing to stream.
        stateless_http=True,
        json_response=True,
        # DNS-rebinding protection guards unauthenticated localhost servers; this
        # one is public and every request needs a bearer token, and the default
        # localhost-only Host allowlist would reject the real domain.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    site_app = WSGIMiddleware(flask_app)

    async def app(scope, receive, send):
        if scope['type'] == 'lifespan':
            # Only the MCP side has startup work (its session manager); Flask has none.
            return await mcp_app(scope, receive, send)
        if not _is_mcp_path(scope.get('path', '')):
            return await site_app(scope, receive, send)

        token = mcp_token_service.bearer_token(_authorization_header(scope))
        client = mcp_token_service.find_client(token, flask_app.config['MCP_TOKENS'])
        if client is None:
            logger.warning('Rejected MCP request: missing or invalid bearer token')
            return await _unauthorized(send)
        logger.info('MCP request from %s', client)
        return await mcp_app(scope, receive, send)

    return app
