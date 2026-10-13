"""Bearer tokens for agent (MCP client) access. Configured only via the
`LEDGER_MCP_TOKENS` server env var as `name:<sha256 hex>` entries — only hashes
are stored, and there is deliberately no database table or UI for them.
These tokens are for read-only agent tools and never unlock the website."""
import hashlib
import hmac
import re
import secrets

TOKEN_PREFIX = 'ldmcp_'
_CLIENT_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')
_SHA256_HEX_RE = re.compile(r'^[0-9a-f]{64}$')


class McpTokenConfigError(ValueError):
    pass


def is_valid_client_name(name):
    return bool(_CLIENT_NAME_RE.match(name or ''))


def generate_token():
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token):
    # Plain SHA-256 is enough: tokens are 256-bit random, so there's nothing to brute-force.
    return hashlib.sha256(token.encode()).hexdigest()


def parse_tokens(raw):
    """Parse `name:<sha256 hex>,...` into {name: hash}; raises McpTokenConfigError on a bad entry."""
    tokens = {}
    for entry in (raw or '').split(','):
        if not entry.strip():
            continue
        name, sep, token_hash = (part.strip().lower() for part in entry.partition(':'))
        # The value is left out of the error on purpose: a raw token pasted by
        # mistake would otherwise end up in the deploy logs.
        if not sep or not is_valid_client_name(name) or not _SHA256_HEX_RE.match(token_hash):
            raise McpTokenConfigError(
                f'LEDGER_MCP_TOKENS entry for {name!r} is malformed; expected name:<64-char sha256 hex>.'
            )
        if name in tokens:
            raise McpTokenConfigError(f'LEDGER_MCP_TOKENS lists {name!r} more than once.')
        tokens[name] = token_hash
    return tokens


def bearer_token(authorization_header):
    """Extract the token from an `Authorization: Bearer <token>` header value, or None."""
    scheme, _, token = (authorization_header or '').partition(' ')
    if scheme.lower() != 'bearer':
        return None
    return token.strip() or None


def find_client(token, tokens):
    """Return the name of the client this token belongs to, or None."""
    if not token:
        return None
    presented = hash_token(token)
    match = None
    for name, expected in tokens.items():
        # No early exit, so response time doesn't reveal which entry matched.
        if hmac.compare_digest(presented, expected):
            match = name
    return match
