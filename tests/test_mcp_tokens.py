import pytest

from app import create_app, mcp_token_service
from app.mcp_token_service import McpTokenConfigError

VALID_HASH = 'a' * 64


class TestParseTokens:
    def test_parses_entries_case_insensitively(self):
        raw = f' Muse:{VALID_HASH.upper()} , claude-desktop:{"b" * 64},'
        assert mcp_token_service.parse_tokens(raw) == {'muse': VALID_HASH, 'claude-desktop': 'b' * 64}

    @pytest.mark.parametrize('raw', [None, '', ' , '])
    def test_empty_config_means_no_tokens(self, raw):
        assert mcp_token_service.parse_tokens(raw) == {}

    @pytest.mark.parametrize('entry', ['muse', f'Bad Name:{VALID_HASH}', 'muse:not-a-sha256'])
    def test_malformed_entry_raises(self, entry):
        with pytest.raises(McpTokenConfigError, match='malformed'):
            mcp_token_service.parse_tokens(entry)

    def test_raw_token_pasted_by_mistake_is_not_echoed(self):
        with pytest.raises(McpTokenConfigError) as excinfo:
            mcp_token_service.parse_tokens('muse:ldmcp_secretvalue')
        assert 'secretvalue' not in str(excinfo.value)

    def test_duplicate_name_raises(self):
        with pytest.raises(McpTokenConfigError, match='more than once'):
            mcp_token_service.parse_tokens(f'muse:{VALID_HASH},muse:{"b" * 64}')


class TestBearerToken:
    @pytest.mark.parametrize('header, expected', [
        ('Bearer ldmcp_abc', 'ldmcp_abc'),
        ('bearer  ldmcp_abc ', 'ldmcp_abc'),
        ('Basic dXNlcjpwYXNz', None),
        ('Bearer', None),
        ('Bearer ', None),
        ('', None),
        (None, None),
    ])
    def test_extracts_only_bearer_tokens(self, header, expected):
        assert mcp_token_service.bearer_token(header) == expected


class TestFindClient:
    def test_generated_token_matches_its_hashed_entry(self):
        token = mcp_token_service.generate_token()
        assert token.startswith(mcp_token_service.TOKEN_PREFIX)
        tokens = {'muse': mcp_token_service.hash_token(token), 'other': VALID_HASH}
        assert mcp_token_service.find_client(token, tokens) == 'muse'

    @pytest.mark.parametrize('token', [None, '', 'ldmcp_wrong'])
    def test_missing_or_unknown_token_rejected(self, token):
        assert mcp_token_service.find_client(token, {'muse': VALID_HASH}) is None

    def test_no_tokens_configured_rejects_everything(self):
        assert mcp_token_service.find_client(mcp_token_service.generate_token(), {}) is None


class TestMcpTokenCommand:
    def test_prints_token_and_matching_config_entry(self, app):
        result = app.test_cli_runner().invoke(args=['mcp-token', 'Muse'])
        assert result.exit_code == 0, result.output
        lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        token = next(line for line in lines if line.startswith(mcp_token_service.TOKEN_PREFIX))
        entry = next(line for line in lines if line.startswith('muse:'))
        assert mcp_token_service.find_client(token, mcp_token_service.parse_tokens(entry)) == 'muse'

    def test_invalid_name_rejected(self, app):
        result = app.test_cli_runner().invoke(args=['mcp-token', 'not valid'])
        assert result.exit_code != 0
        assert mcp_token_service.TOKEN_PREFIX not in result.output


class TestConfig:
    def test_tokens_loaded_from_env(self, monkeypatch):
        monkeypatch.setenv('LEDGER_MCP_TOKENS', f'muse:{VALID_HASH}')
        assert create_app().config['MCP_TOKENS'] == {'muse': VALID_HASH}

    def test_malformed_env_stops_startup(self, monkeypatch):
        monkeypatch.setenv('LEDGER_MCP_TOKENS', 'muse:oops')
        with pytest.raises(McpTokenConfigError):
            create_app()


def test_agent_token_does_not_unlock_website(app, anon_client, monkeypatch):
    token = mcp_token_service.generate_token()
    monkeypatch.setitem(app.config, 'MCP_TOKENS', {'muse': mcp_token_service.hash_token(token)})
    headers = {'Authorization': f'Bearer {token}'}
    assert anon_client.get('/accounts', headers=headers).status_code == 302
    assert anon_client.post('/api/accounts/batch', json={}, headers=headers).status_code == 401
