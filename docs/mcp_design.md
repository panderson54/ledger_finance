# Ledger MCP — Design

**Status:** Accepted · **Date:** 2026-10-16 (decisions recorded under *Decisions*)

## Goal

Let Claude (web, mobile, Desktop, Code) and Muse AI answer questions about
your current financial position and help you analyse it: asset worth and
distribution, net worth trajectory, cash flow and save rate, dividend income,
and FI/growth projections. Recommendations come from the model reasoning over
the data. The MCP only supplies facts and deterministic projections.

Two constraints override everything else:

1. **Strictly read-only.** No tool may create, update or delete data. That
   includes indirect writes: refreshing prices, fetching dividend data into the
   cache, or recalculating metrics.
2. **Protect private data.** Return the minimum needed to answer the question.
   Never return secrets or free text the user typed. Only you can grant
   access, and you can take it back.

## What already exists

The auth groundwork is merged (PR #26):

- `mcp_token_service.py`: config-only bearer tokens (`LEDGER_MCP_TOKENS`,
  stored as SHA-256 hashes, compared in constant time, revoked by removing the
  entry and restarting).
- `access_control.agent_token_required`: endpoints with this mark accept only
  a bearer token. A Google session isn't enough, and a token can't unlock any
  other endpoint.
- `/agent/whoami`: the only agent endpoint so far.

## Architecture

```
 claude.ai web / mobile / Desktop ──OAuth access token──┐
 Claude Code ───────────── static bearer token ─────────┤        Ledger server (Flask, HTTPS)
 Muse AI ───────────────── static bearer token ─────────┤  ┌────────────────────────────────────────────┐
                                                        └─▶│ POST /mcp  (MCP Streamable HTTP, stateless) │
                                                           │ GET  /agent/v1/<tool>  (REST mirror)        │
                                                           │   access_control: token → agent, scope      │
                                                           │   agent_tools registry (read-only tools)    │
                                                           │     → agent_view (privacy serializers)      │
                                                           │     → read services → models (read-only)    │
 Browser (you) ── Google sign-in ─▶ /oauth/authorize ─────▶│ OAuth authorization server (Authlib)        │
                                    (consent screen)       │   /oauth/token, /.well-known/*              │
                                                           └────────────────────────────────────────────┘
```

**Decision: a remote MCP endpoint inside Flask, with Ledger as its own OAuth
authorization server. You sign in with your existing Google login and the
allowlist.**

- **`/mcp`** speaks MCP over Streamable HTTP in its stateless form. Each
  JSON-RPC `POST` gets one JSON response, with no SSE and no session. That is
  all a read-only, request/response tool server needs.
  - Supported methods: `initialize`, `notifications/initialized`, `ping`,
    `tools/list`, `tools/call`, `prompts/list` and `prompts/get`. Anything else
    gets JSON-RPC `-32601`.
  - `GET` and `DELETE` on `/mcp` get `405`.
  - The handler is a thin dispatcher (about 150 lines) over the tool registry.
    Writing it in Flask keeps one process, one auth guard and the existing
    gunicorn setup. Protocol conformance is checked with the MCP Inspector and
    with tests.
- **`app/agent_tools.py`** is one registry of tools: name, description, JSON
  input schema, required scope, and a handler. `/mcp` and the REST mirror
  `GET /agent/v1/<tool>` (for Muse, if it doesn't speak MCP) both dispatch to
  it. There's no second copy of any tool.
- **Two kinds of credential, one guard.** `access_control` accepts either a
  static `LEDGER_MCP_TOKENS` token (Claude Code, Muse) or a Ledger-issued OAuth
  access token (the hosted Claude apps). Either way the guard sets
  `g.agent_name` and `g.agent_scope`, and everything downstream is identical.

Alternatives considered:

| Option | Why not |
|---|---|
| stdio bridge on your machine (the previous plan) | claude.ai web and mobile can't run local processes. A remote endpoint serves every Claude surface, so the bridge isn't needed. |
| Official `mcp` SDK (ASGI) mounted next to Flask | Needs ASGI workers and an ASGI↔WSGI adapter for a protocol subset this small. Worth revisiting if Ledger ever needs server-initiated messages. |
| Static bearer token entered in claude.ai (`static_headers`) | Beta, limited to some organizations. It's also one shared credential with no expiry or consent step. |
| Dynamic Client Registration / CIMD so any Claude account can connect | Opens the door to consent phishing (see *OAuth*). A pre-registered client fits a single-owner app better. |
| An external IdP (Auth0, Google as the AS) | Google can't issue tokens for Ledger's resource. A third party adds another place your access is controlled, for a single user. |

## OAuth for the hosted Claude apps

Claude's connector auth requirements
(<https://claude.com/docs/connectors/building/authentication>) mapped onto
Ledger:

### Discovery

| Endpoint | Content |
|---|---|
| `401` from `/mcp` without a valid token | `WWW-Authenticate: Bearer resource_metadata="https://<host>/.well-known/oauth-protected-resource", scope="ledger.read"`. It must be a `401`, because Claude ignores the header on a `200`. |
| `/.well-known/oauth-protected-resource` (RFC 9728) | `resource` is exactly `https://<host>/mcp`, the URL you paste into Claude. `authorization_servers` is `["https://<host>"]` (a single entry, since Claude only reads the first). `scopes_supported` is `["ledger.read"]`. |
| `/.well-known/oauth-authorization-server` (RFC 8414) | `issuer`, `authorization_endpoint`, `token_endpoint`, `revocation_endpoint`, `code_challenge_methods_supported: ["S256"]`, `grant_types_supported: ["authorization_code","refresh_token"]`, `token_endpoint_auth_methods_supported: ["client_secret_post","client_secret_basic"]`, `scopes_supported: ["ledger.read","offline_access"]`. There's no `registration_endpoint` and no CIMD flag. |

`<host>` comes from a new `LEDGER_PUBLIC_URL` setting, not from the request,
so a spoofed `Host` header can't change the issuer. `ProxyFix` is configured
for the reverse proxy in front of Ledger.

### Client registration: one pre-registered, confidential client

`LEDGER_OAUTH_CLIENT=claude:<client_id>:<sha256 of client_secret>`. This is
config-only, like the other credentials, and `flask oauth-client claude`
prints a new ID and secret plus the entry. You paste the ID and secret into
claude.ai → *Settings → Connectors → Add custom connector → Advanced*. The
connector then syncs to Desktop and mobile.

**Why confidential, not DCR or CIMD.** Every Claude user's code arrives at the
same callback, `https://claude.ai/api/mcp/auth_callback`, so the redirect URI
can't tell your Claude account apart from an attacker's. With DCR or CIMD,
anyone could add a Ledger connector to *their* Claude, then send you its
authorize link. If you signed in and approved, their Claude could read your
finances. With a confidential client, the token exchange needs the secret,
and only your connector has it.

Allowed redirect URIs (exact match):
- `https://claude.ai/api/mcp/auth_callback`
- `https://claude.com/api/mcp/auth_callback` (Anthropic has said the callback
  may move here)

### Authorization (`/oauth/authorize`)

- The route is *not* public. The existing guard sends you through Google
  sign-in and back (`next=`), and checks the email against
  `LEDGER_ALLOWED_EMAILS`.
- Requests are validated before anything is shown: a known `client_id`, an
  allowed `redirect_uri`, `response_type=code`, an S256 `code_challenge`, a
  `scope` within `ledger.read offline_access`, and `resource` (RFC 8707) equal
  to the `/mcp` URL if present. Any failure renders an error page and never
  redirects, so the redirect can't be abused.
- The consent screen names the client ("Claude"), the redirect host
  (`claude.ai`), and the access in plain words: *"Read-only access to all
  balances, accounts, holdings, income and projections. Cannot change
  anything."* It has Approve and Deny buttons. The form uses Flask-WTF CSRF
  protection and is never auto-approved, even for a repeat grant.

### Tokens

| Item | Lifetime | Storage |
|---|---|---|
| Authorization code | 60 s, single use | SHA-256 hash, bound to client, redirect URI, PKCE challenge, scope, resource and your email |
| Access token | 1 hour | SHA-256 hash. Opaque `ldoat_` + 256-bit random value. The audience is `/mcp`. |
| Refresh token | 30 days idle, 90 days absolute (then you approve again) | SHA-256 hash, **rotated on every use**. Reusing an old refresh token revokes the whole grant and logs a `WARNING`. |

- **New tables:** `oauth_authorization_codes` and `oauth_grants`. A grant
  holds one consent plus its current access and refresh token hashes, scope,
  email, expiry and `revoked_at`. These are the first auth tables in Ledger.
  They are needed because rotation and revocation require state. The
  allowlist and client config stay config-only. The migration follows the
  CLAUDE.md backup procedure.
- **Token endpoint:** `/oauth/token` accepts `application/x-www-form-urlencoded`
  and returns RFC 6749 errors (`invalid_grant` for a dead refresh token). It
  answers quickly: Claude allows 10 seconds for exchanges and 30 seconds for
  refreshes.
- **Every request re-checks the token owner's email against the allowlist**,
  just like web sessions. Removing your email (and restarting) kills every
  OAuth grant immediately.
- **Revocation:** there are three ways.
  - `POST /oauth/revoke` (RFC 7009), for Claude disconnecting.
  - `flask oauth-revoke --all`.
  - Removing `LEDGER_OAUTH_CLIENT`, which disables the hosted-app path
    entirely.

  A read-only "Connected agents" list with a Revoke button on Settings is a
  later nicety. It lists grants and doesn't manage the allowlist.
- **Library:** use Authlib's Flask `AuthorizationServer`. Authlib is already a
  dependency (for Google sign-in), and it provides the authorization-code
  grant, `CodeChallenge` (PKCE) and the refresh-token grant with rotation, so
  Ledger doesn't hand-roll OAuth. Ledger supplies only the client, code and
  token storage callbacks.

### Public endpoints (an explicit exception)

CLAUDE.md says to add nothing to `PUBLIC_ENDPOINTS` beyond what the login page
needs. OAuth makes three exceptions unavoidable, because Claude's servers call
them without any session:

- `/.well-known/oauth-protected-resource`
- `/.well-known/oauth-authorization-server`
- `/oauth/token` (authenticated by client secret and code/refresh token
  instead; CSRF-exempt because no cookies are involved)

`/oauth/revoke` also authenticates with the client secret. CLAUDE.md is
updated to name these endpoints and the reason for the exception.

### Claude Code

Claude Code uses its own OAuth client and a loopback redirect, not the
claude.ai connector. Rather than open a second OAuth path, it keeps using a
static token:

`claude mcp add --transport http ledger https://<host>/mcp --header "Authorization: Bearer ldmcp_…"`

## Read-only enforcement (defence in depth)

Any one of these layers is meant to be enough on its own:

1. **Only read operations are reachable.**
   - `/agent/v1/*` accepts only `GET`/`HEAD`. `/mcp` accepts `POST` (required
     by the transport), but only dispatches the read-only method allowlist
     above, and `tools/call` only reaches the registry.
   - The registry contains no write tools, and a test asserts that each tool's
     handler is wrapped in the read-only context.
   - A test walks `app.url_map` and asserts that every `agent_token_required`
     view is GET-only, except `/mcp`.
2. **Writes fail during agent requests.** A SQLAlchemy `before_flush` listener
   raises when `g.agent_name` is set, and teardown always calls
   `session.rollback()`. On SQLite the agent request also sets
   `PRAGMA query_only = ON` on its connection and clears it at teardown.
   Validating an OAuth access token is a pure lookup and writes nothing. Token
   issuance happens only on `/oauth/*`, which never serves data.
3. **Cache-only services.** The read paths take `fetch_missing=False` and
   never call Claude or yfinance. This matters because some web endpoints have
   side effects today:
   - `/api/passive-income` fetches missing or stale dividend data through
     Claude and writes `DividendData`.
   - The price refresh endpoints write `Holding.last_price`.
   - `/api/sp500-change` calls yfinance.

   The agent tools report stale or missing data in `warnings` instead.
   Tests patch the Anthropic client and yfinance to raise if called.
4. **No write imports.** `agent_tools.py` and `routes/agent.py` must not
   import write helpers such as `_set_app_setting`, `recalculate_metrics` or
   `db.session.commit`. A test checks this.

## Privacy model

### Scopes

Two internal scopes, `summary` and `detail`:

| Scope | Can see |
|---|---|
| `summary` | Totals, categories, allocation percentages, net worth / cash-flow history, projections, and accounts **aliased** by category (e.g. `retirement-2`) |
| `detail` | Everything in `summary`, plus real account names, per-account balances and history, individual holdings, and per-holding income |

How each credential maps to a scope:

- **Static tokens:** `LEDGER_MCP_TOKENS=claude-code:<sha256>:detail,muse:<sha256>:detail`.
  `name:hash` with no scope means `summary`.
- **OAuth:** the `ledger.read` scope grants `detail`, as decided. A
  `ledger.read.summary` scope can be added later if a less trusted client ever
  needs it.

A summary-scoped credential calling a detail-only tool gets `403
insufficient_scope` (REST) or a tool error (MCP).

### Never returned, at any scope

Every tool serializes through its own field allowlist in `app/agent_view.py`.
Tools don't reuse the web `*_to_dict` helpers, so a column added to a model
can't leak out by accident.

| Data | Reason |
|---|---|
| `AppSetting.anthropic_api_key`, and any setting not on the allowlist | Secret. Only `allocation_targets`, `save_rate_target`, `drip_tax_rate_pct` and `snapshot_timing` are readable. |
| `Account.account_number`, `Account.notes` | Identifier / free text |
| `Account.institution` | Identifying, and not needed for analysis (decided: excluded at `detail` too) |
| `SpendingEntry.notes`, `RecurringEntry.notes`, `RentalProperty.notes` | Free text that can hold anything, and also a prompt-injection channel |
| `RentalProperty.address` | Location. Properties are returned as `property-1`, `property-2`, … |
| `ImportLog` (file names, error text), raw `DividendData.source_notes` JSON | Not needed for analysis |
| OAuth grant rows, token hashes, your email | Auth state, not finance data |

### Other protections

- **Transport:** HTTPS only. Tokens go in the `Authorization` header and are
  never accepted in a query string. Responses from `/mcp`, `/agent/*` and
  `/oauth/token` send `Cache-Control: no-store`. `/mcp` rejects a request
  whose `Origin` header is present and isn't allowed (the MCP spec's
  DNS-rebinding guard).
- **Optional network allowlist:** `LEDGER_MCP_OAUTH_CIDRS=160.79.104.0/21`
  (Anthropic's published egress range) restricts *OAuth-token* calls to
  `/mcp` to requests that come from Claude's servers. A stolen access token is
  then useless from anywhere else. This needs `ProxyFix` set up correctly.
  Static tokens (Claude Code, Muse) aren't affected.
- **Audit log:** at `INFO` (with `%s` formatting), every tool call, token
  issuance, refresh and revocation. Each entry records the agent name, scope,
  tool, arguments and response size. Response bodies and amounts are never
  logged. Refresh-token reuse and allowlist failures log at `WARNING`.
- **Bounded responses:** history defaults to the last 36 months, capped at 240
  rows. Holdings are capped at 200 rows. Inputs are validated against each
  tool's JSON schema.
- **Rate limit:** 120 tool calls per minute per agent, plus 10 per minute per
  IP on `/oauth/token`. Both are held in an `app.extensions` instance, not a
  module global.
- **Data leaves your server:** whatever a tool returns goes to Anthropic (or
  Muse's provider) as part of the conversation.
- **Exfiltration through other tools:** a finance connector in the same
  conversation as a tool that can send data out (email, web fetch, …) creates
  an exfiltration path. Keep those tools on "ask before use" when Ledger is
  enabled. Leaving out free-text fields shrinks the prompt-injection surface.

## Tools

Conventions shared by every response:

- Currency is USD rounded to cents.
- Months are `YYYY-MM`.
- Percentages are numbers with a `_pct` suffix (`12.5` means 12.5%).
- Every response includes `as_of` (the latest snapshot month), `scope`, and
  `warnings`, a list such as `"prices for 3 holdings are >7 days old"` or
  `"no snapshot for 2026-09"`.
- MCP `tools/call` returns the JSON as `structuredContent`, with the same
  JSON as text content for older clients. Every tool carries the
  `readOnlyHint: true` annotation.

Tool descriptions spell out metric definitions so the model reads them
correctly. For example: *"net_worth_non_re excludes real estate and the
mortgages tied to it"*, *"liquid net worth = liquid assets − all
liabilities"*, *"save_rate_pct = (income − expenses) / income"*.

| # | Tool | Scope | Arguments | Returns | Backed by |
|---|---|---|---|---|---|
| 1 | `get_financial_snapshot` | summary | — | Net worth, total assets and liabilities, liquid NW, NW excluding real estate, month-over-month change, YTD save rate vs target, totals by bucket (Cash / Investments / Retirement / Real Estate / Liabilities), tax-status split (taxable / deferred / free), data freshness | `CalculatedMetric`, latest snapshots |
| 2 | `get_net_worth_history` | summary | `start`, `end` | Monthly `net_worth`, `net_worth_non_re`, `net_worth_liquid`, assets and liabilities; CAGR over the range; trailing 12-month change; best and worst months; balances by bucket per month | `CalculatedMetric`, `projections.calculate_cagr` |
| 3 | `get_cash_flow` | summary | `start`, `end` | Monthly income, expenses, savings, save rate; trailing 3/12-month averages; recurring income and expense totals. At `detail`, per-card/source breakdown (names only, no notes) | `CalculatedMetric`, `SpendingEntry`, `RecurringEntry` |
| 4 | `get_asset_allocation` | summary | — | Current vs target % for domestic / international / bonds / cash, drift in % and $, the amount to move to rebalance per class, market-cap tilt, allocation by tax status | Extracted allocation service |
| 5 | `list_accounts` | summary* | `include_inactive` | id, label, category, type, tax status, liquid flag, latest balance and its date, 12-month change. *Labels are aliases at `summary` and real names at `detail`. | `Account`, latest snapshots |
| 6 | `get_account_history` | detail | `account_id`, `start`, `end` | Monthly balances, CAGR, allocation splits | `AccountSnapshot` |
| 7 | `get_holdings` | detail | `account_id?` | Ticker, name, shares, price and price age, value, portfolio weight, asset-class splits; concentration (top-10 weight, any position >10%) | `Holding`, `HoldingAllocation`, `TickerClassification` |
| 8 | `get_passive_income` | summary | — | Annual and monthly dividend income, portfolio yield, after-tax estimate, breakdown by payer type and frequency, net rental income (aliased properties), account-level yield estimates, list of tickers missing data. At `detail`, per-holding rows. Cached data only. | Extracted income service, `dividend_calc.calculate_current_income` |
| 9 | `project_net_worth` | summary | `years`, `monthly_contribution?`, `rates?` | Month-by-month projected net worth and categories. Defaults (category CAGRs, current balances, mortgage amortisation) come from history; any can be overridden. Pure computation. | `projections.project_growth`, extracted projection defaults |
| 10 | `project_financial_independence` | summary | `annual_spending?`, `swr?`, `growth?`, `monthly_savings?` | FI number, months/date to FI, projected series, sensitivity table (SWR × growth). Spending defaults to trailing-12 expenses. | `projections.fi_number`, `months_to_fi`, `fi_sensitivity_table*` |
| 11 | `project_dividend_income` | summary | `years`, `drip?`, `contribution?` | DRIP projection of yearly income and portfolio value | `dividend_calc.simulate_drip` |

### Example: `get_financial_snapshot`

```json
{
  "as_of": "2026-09",
  "scope": "detail",
  "net_worth": 1234567.89,
  "net_worth_non_re": 845000.00,
  "net_worth_liquid": 410250.00,
  "total_assets": 1580000.00,
  "total_liabilities": 345432.11,
  "monthly_change": {"amount": 18250.40, "pct": 1.5},
  "ytd_save_rate_pct": 38.2,
  "save_rate_target_pct": 35.0,
  "by_bucket": {"Cash": 60250.00, "Investments": 350000.00, "Retirement": 434750.00,
                "Real Estate": 735000.00, "Liabilities": -345432.11},
  "by_tax_status": {"taxable": 410250.00, "tax_deferred": 300000.00, "tax_free": 134750.00},
  "freshness": {"latest_snapshot": "2026-09", "months_missing_last_12": [],
                "stale_price_holdings": 3},
  "warnings": ["prices for 3 holdings are more than 7 days old"]
}
```

### MCP prompts

These prompt templates are served by `/mcp` (`prompts/list` and
`prompts/get`), and clients that support prompts show them as
slash-commands:

- `monthly_review`: snapshot, then history (12 months), then cash flow; what
  changed and why.
- `rebalance_check`: allocation plus holdings; drift, tax-aware placement
  ideas, concentration risk.
- `fi_checkup`: FI projection under current, conservative and optimistic
  assumptions.

Each prompt tells the model to cite `as_of`, flag stale data, and frame its
output as analysis rather than professional financial advice.

## Code changes

Some logic the tools need lives inside route handlers today: passive income,
allocation, holdings summary, projections context. It's also duplicated, for
example "latest balance per account" appears in four places and `BUCKET_MAP`
in two. CLAUDE.md requires that logic to live in services, so the first step
is extracting it. The web routes then call the same services.

| Module | Change |
|---|---|
| `app/portfolio_service.py` (new) | `latest_balances(account_ids)`, `bucket_totals()`, `allocation_breakdown()`, `holdings_summary()`. Read-only; used by web routes and tools. |
| `app/income_service.py` (new) | `passive_income(account_id=None, fetch_missing=True)`, moved out of `routes/income.py`. Tools pass `fetch_missing=False`. |
| `app/projections_service.py` (new) | `projection_defaults()`, moved out of `helpers._build_projections_context`. |
| `app/account_categories.py` | Add a shared `BUCKET_MAP` and `BUCKET_ORDER` (removes the two copies in `visualizations.py`). |
| `app/agent_view.py` (new) | Scope-aware serializers, account and property aliasing, settings allowlist, `warnings` / freshness builder. |
| `app/agent_tools.py` (new) | Tool and prompt registry (name, description, input schema, scope, handler). |
| `app/mcp_protocol.py` (new) | Stateless JSON-RPC dispatch for the MCP method allowlist. Pure: no Flask or DB imports, so it's unit-testable. |
| `app/oauth_service.py` (new) | Authlib `AuthorizationServer` setup: config-backed client, code and grant storage, PKCE, refresh rotation and reuse detection, revocation, token validation (including the allowlist re-check). |
| `app/models.py` + migration | `OAuthAuthorizationCode`, `OAuthGrant`. |
| `app/mcp_token_service.py` | Optional `:scope` on each token entry; validate `{summary, detail}`. |
| `app/access_control.py` | Accept static or OAuth bearer tokens; `g.agent_scope`; the protected-resource-metadata `WWW-Authenticate`; the GET-only rule (except `/mcp`); `no-store`; Origin check; read-only session guard; rate limiter; optional OAuth CIDR allowlist; the three new public endpoints. |
| `app/routes/agent.py` | `POST /mcp`, `GET /agent/v1/<tool>`. Each parses input, calls the registry and returns. |
| `app/routes/oauth.py` (new) | `/.well-known/*`, `/oauth/authorize` (consent page), `/oauth/token`, `/oauth/revoke`. |
| `app/templates/auth/consent.html` (new) | Consent screen. |
| `app/__init__.py` | `LEDGER_PUBLIC_URL`, `LEDGER_OAUTH_CLIENT`, `LEDGER_MCP_OAUTH_CIDRS` config. A malformed value stops startup, as with the existing credentials. `ProxyFix`. |
| `app/cli.py` | `flask mcp-token NAME --scope`, `flask oauth-client NAME`, `flask oauth-revoke --all`. |
| `CLAUDE.md`, `README.md`, `SECURITY.md` | The public-endpoint exception, OAuth module notes, connector setup for claude.ai / Claude Code / Muse. |

### Tests

Following the CLAUDE.md coverage rules:

- **MCP protocol:**
  - `initialize` negotiates the protocol version.
  - `tools/list` returns all 11 tools with schemas and `readOnlyHint`.
  - `tools/call` succeeds; an unknown tool, invalid arguments, or an unknown
    method each return the right error.
  - Notifications get `202` with no body.
  - `GET` or `DELETE` on `/mcp` gets `405`.
- **Each tool (REST and MCP):** `401` without a token, `401` with a Google
  session only, scope denial for summary credentials, invalid input, unknown
  account, empty data, seeded data.
- **OAuth:**
  - Discovery documents are correct.
  - A `401` from `/mcp` carries `resource_metadata`.
  - The full authorize → consent → code → token → `/mcp` flow, with PKCE.
  - Each of these is rejected: wrong or missing PKCE verifier, wrong redirect
    URI, a reused or expired code, a wrong client secret, and a `resource`
    mismatch.
  - Refresh rotates the token, and reusing a refresh token revokes the grant.
  - An expired access token gets `401`, and so does a token after its email
    leaves the allowlist.
  - A token after `/oauth/revoke` gets `401`.
  - An OAuth access token can't open any web page, and a web session can't
    call `/mcp`.
- **Read-only:**
  - A url_map sweep checks every agent route is GET-only, except `/mcp`.
  - A flush during an agent request raises.
  - Patched Anthropic and yfinance clients assert they are never called.
  - A row-count fingerprint of every finance table is identical before and
    after calling every tool.
- **Privacy:** call every tool, at both scopes, with every sensitive field
  seeded with a canary string (`notes`, `account_number`, `institution`,
  `address`, the API key, …). The canary must never appear in any response.
  At summary scope, real account names must not appear either.
- **Services:** the extracted services keep the existing web route tests
  green. New services get success, empty, invalid-input and external-failure
  tests.

## Phased delivery

| Phase | Scope | Exit criteria |
|---|---|---|
| 0. Hardening | Token scopes, GET-only rule, read-only session guard, `no-store`, rate limit | Read-only and scope tests pass; `whoami` reports scope |
| 1. Data tools | Service extraction, `agent_view`, `agent_tools`, tools 1–8, REST mirror | Web route tests unchanged and green; canary sweep passes |
| 2. Remote MCP | `/mcp` endpoint, prompts, tools 9–11 | Claude Code (static token) and the MCP Inspector can list and call all tools; Muse is connected |
| 3. OAuth | Authorization server, consent page, tables and migration, revocation, optional CIDR allowlist | You can add the connector on claude.ai, approve it, and ask "how did my net worth change this year?" on web and mobile |
| 4. Polish | Read-only "Connected agents" list on Settings, docs | — |

## Decisions

| Question | Decision |
|---|---|
| Clients | **Claude** (web, mobile, Desktop, Code) and **Muse AI**. The hosted Claude apps use OAuth. Claude Code and Muse use static tokens. Muse calls `/mcp` if it speaks MCP, otherwise the `/agent/v1/<tool>` REST mirror. |
| Scope | Everything is **`detail`**: `ledger.read` for OAuth, and `:detail` on the static tokens. `summary` stays the default for a static entry without a scope. |
| Institutions | **Excluded at every scope**, `detail` included. |
| Stale prices / dividend data | **Warn only.** Agent tools never refresh. Refreshing stays a website action. |
| Remote access | **Required**, for claude.ai web and mobile: a remote MCP endpoint with Ledger as the OAuth server and a pre-registered confidential client. The stdio bridge is dropped. |
