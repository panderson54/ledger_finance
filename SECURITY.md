# Security Policy

## Intended Deployment Model

Ledger is a **single-user (or single-household) application** protected by
Google sign-in. Every request is denied by default unless it comes from a
signed-in Google account whose verified email is listed in the
`LEDGER_ALLOWED_EMAILS` environment variable. There is no sign-up flow, user
table, or UI for granting access — the server config is the only source of
truth, and it is re-checked on every request.

- Sign-in uses OpenID Connect (Authlib) with PKCE, `state` and `nonce`; the ID
  token's signature, issuer, audience and expiry are validated
- Anonymous `GET`s redirect to `/login`; other methods get `401`; unknown URLs
  are denied the same way, so routes can't be probed
- Only the login page, the OAuth endpoints and `static/style.css` are public
- Session cookies are `HttpOnly`, `SameSite=Lax`, `Secure` in production, and
  last 14 days
- Production refuses to start without the OAuth client ID/secret, redirect URI
  and a non-empty allowlist

**Not supported:** multi-tenant deployments — every allowed account sees the
same data.

## Recommended Hardening

- Serve the app over HTTPS (required for Google sign-in on anything but
  `localhost`)
- For a home-hosted install, a VPN (WireGuard, Tailscale, etc.) remains a
  useful extra layer
- Remove an address from `LEDGER_ALLOWED_EMAILS` and restart to revoke access
  immediately

## Sensitive Data

- Your `.env` file contains `SECRET_KEY` and `GOOGLE_CLIENT_SECRET` — keep it out of version control (it is already in `.gitignore`)
- The SQLite database at `data/finance.db` contains all your financial records — back it up regularly and keep it off public storage
- Log files in `logs/` may contain request details — treat them as sensitive

## Reporting a Vulnerability

If you find a security issue, please open a [GitHub Issue](../../issues) marked with the `security` label, or contact the maintainer directly via the email listed on their GitHub profile. Please do not include sensitive exploit details in a public issue — describe the class of vulnerability and we will coordinate privately.
