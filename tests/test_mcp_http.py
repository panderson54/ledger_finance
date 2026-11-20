import pytest
from starlette.testclient import TestClient

from app import mcp_server, mcp_token_service
from app.mcp_http import create_asgi_app

TOKEN = 'ldmcp_test-token'
WRITE_TOKEN = 'ldmcp_write-token'
MCP_HEADERS = {
    'Authorization': f'Bearer {TOKEN}',
    'Accept': 'application/json, text/event-stream',
    'MCP-Protocol-Version': '2025-06-18',
}


def _rpc(method, params=None):
    return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}}


@pytest.fixture
def asgi_client(app, db):
    original = app.config['MCP_TOKENS'], app.config['MCP_WRITE_CLIENTS']
    app.config['MCP_TOKENS'] = {
        'muse': mcp_token_service.hash_token(TOKEN), 'writer': mcp_token_service.hash_token(WRITE_TOKEN),
    }
    app.config['MCP_WRITE_CLIENTS'] = frozenset({'writer'})
    with TestClient(create_asgi_app(app), follow_redirects=False) as client:
        yield client
    app.config['MCP_TOKENS'], app.config['MCP_WRITE_CLIENTS'] = original


class TestMcpAuth:
    @pytest.mark.parametrize('headers', [
        {},
        {'Authorization': 'Bearer ldmcp_wrong'},
        {'Authorization': f'Basic {TOKEN}'},
    ])
    def test_rejects_missing_or_invalid_token(self, asgi_client, headers):
        resp = asgi_client.post('/mcp', json=_rpc('tools/list'), headers=headers)
        assert resp.status_code == 401
        assert resp.json() == {'error': 'invalid_token'}
        assert resp.headers['www-authenticate'] == 'Bearer realm="ledger-mcp"'

    def test_token_does_not_unlock_the_website(self, asgi_client):
        resp = asgi_client.get('/', headers={'Authorization': f'Bearer {TOKEN}'})
        assert resp.status_code == 302
        assert '/login' in resp.headers['location']


class TestMcpEndpoint:
    def test_lists_every_tool_as_read_only(self, asgi_client):
        resp = asgi_client.post('/mcp', json=_rpc('tools/list'), headers=MCP_HEADERS)
        assert resp.status_code == 200
        tools = resp.json()['result']['tools']
        assert {t['name'] for t in tools} == {t.__name__ for t in mcp_server.TOOLS}
        assert all(t['annotations']['readOnlyHint'] for t in tools)

    def test_read_only_token_cannot_call_the_write_tool(self, asgi_client):
        resp = asgi_client.post('/mcp', headers=MCP_HEADERS, json=_rpc(
            'tools/call', {'name': 'record_monthly_update', 'arguments': {'month': '2026-01'}}))
        result = resp.json()['result']
        assert result['isError']
        assert 'Unknown tool' in result['content'][0]['text']

    def test_write_token_gets_and_can_call_the_write_tool(self, asgi_client):
        headers = {**MCP_HEADERS, 'Authorization': f'Bearer {WRITE_TOKEN}'}
        tools = asgi_client.post('/mcp', json=_rpc('tools/list'), headers=headers).json()['result']['tools']
        write_tool = next(t for t in tools if t['name'] == 'record_monthly_update')
        assert write_tool['annotations']['readOnlyHint'] is False
        result = asgi_client.post('/mcp', headers=headers, json=_rpc(
            'tools/call', {'name': 'record_monthly_update', 'arguments': {'month': '2026-01', 'dry_run': True}},
        )).json()['result']
        assert not result['isError']
        assert '"dry_run": true' in result['content'][0]['text']

    def test_calls_a_tool(self, asgi_client):
        resp = asgi_client.post('/mcp', headers=MCP_HEADERS, json=_rpc(
            'tools/call', {'name': 'get_net_worth_history', 'arguments': {}}))
        result = resp.json()['result']
        assert not result['isError']
        assert 'net_worth_cagr_pct' in result['content'][0]['text']

    def test_bad_input_comes_back_as_tool_error(self, asgi_client):
        resp = asgi_client.post('/mcp', headers=MCP_HEADERS, json=_rpc(
            'tools/call', {'name': 'get_account_history', 'arguments': {'account_id': 999}}))
        result = resp.json()['result']
        assert result['isError']
        assert 'No account 999' in result['content'][0]['text']
