# Ledger MCP — Design

**Status:** Accepted · **Date:** 2026-10-16 (decisions recorded under *Decisions*)

## Goal

Let an AI agent (Claude Desktop, Claude Code, …) answer questions about your
current financial position and help you analyse it: asset worth and
distribution, net worth trajectory, cash flow and save rate, dividend income,
and FI/growth projections. Recommendations come from the model reasoning over
the data. The MCP only supplies facts and deterministic projections.

Two constraints override everything else:

1. **Strictly read-only.** No tool may create, update or delete data. That
   includes indirect writes: refreshing prices, fetching dividend data into the
   cache, or recalculating metrics.
2. **Protect private data.** Return the minimum needed to answer the question.
   Never return secrets or free text the user typed, and keep identifying
   details behind an explicit, per-token opt-in.

## What already exists

The auth groundwork is merged (PR #26):

- `mcp_token_service.py`: config-only bearer tokens (`LEDGER_MCP_TOKENS`,
  stored as SHA-256 hashes, compared in constant time, revoked by removing the
  entry and restarting).
- `access_control.agent_token_required`: endpoints with this mark accept only
  a bearer token. A Google session isn't enough, and a token can't unlock any
  other endpoint.
- `/agent/whoami`: the only agent endpoint so far.

This design builds the data surface and the MCP adapter on top of that.

## Architecture

```
 Claude Desktop / Code                     Ledger server (Flask, HTTPS)
┌──────────────────────┐   stdio   ┌────────────────────┐  HTTPS GET + Bearer  ┌──────────────────────────────┐
│ MCP client (LLM)     │◀────────▶│ ledger-mcp bridge  │─────────────────────▶│ access_control (token+scope) │
└──────────────────────┘           │ (mcp/ in repo)     │                      │ routes/agent.py  /agent/v1/* │
                                   │ holds token only,  │                      │   → agent_view (privacy)     │
                                   │ no DB access       │                      │   → read services            │
                                   └────────────────────┘                      │   → models (read-only session)│
                                                                               └──────────────────────────────┘
```

**Decision: read-only REST endpoints in Flask, plus a thin stdio MCP bridge.**

- `/agent/v1/*` are plain JSON `GET` endpoints, one per tool. They use the
  existing token guard and are tested with the existing pytest setup. Business
  logic lives in services, per CLAUDE.md.
- `mcp/ledger_mcp.py` is a small stdio server built on the official `mcp`
  Python SDK (FastMCP). It maps each tool to one `GET` and returns the JSON. It
  holds only the bearer token: no database file, no write endpoints, and
  nothing to configure besides `LEDGER_URL` and `LEDGER_MCP_TOKEN`.

Alternatives considered:

| Option | Why not (for now) |
|---|---|
| stdio MCP that opens `finance.db` directly | Skips auth and the privacy layer, needs a copy of the whole DB on the client machine, and one bug could write to it. |
| Streamable-HTTP MCP endpoint (`/mcp`) inside Flask | The SDK is ASGI, Flask is WSGI. It's only worth the extra transport code once a client needs a remote URL (see Phase 5). |
| Remote MCP for claude.ai web/mobile | Custom connectors there need OAuth 2.1, not static bearer tokens. That's a separate project (Phase 5). |

The REST layer can later sit behind a remote MCP transport unchanged, so the
bridge isn't throwaway work.

## Read-only enforcement (defence in depth)

Any one of these layers is meant to be enough on its own:

1. **GET only.** In `access_control`, an agent-marked endpoint called with any
   method other than `GET`/`HEAD` gets `405`. A test walks `app.url_map` and
   asserts that every `agent_token_required` view allows only safe methods.
2. **Writes fail during agent requests.** A SQLAlchemy `before_flush` listener
   raises when `g.agent_name` is set, and teardown always calls
   `session.rollback()`. On SQLite the agent request also sets
   `PRAGMA query_only = ON` on its connection and clears it at teardown, so the
   database itself refuses writes.
3. **Cache-only services.** The read paths take `fetch_missing=False` and
   never call Claude or yfinance. This matters because some web endpoints have
   side effects today:
   - `/api/passive-income` fetches missing or stale dividend data through
     Claude and writes `DividendData`.
   - The price refresh endpoints write `Holding.last_price`.
   - `/api/sp500-change` calls yfinance.

   The agent versions report stale or missing data in `warnings` instead.
   Tests patch the Anthropic client and yfinance to raise if called.
4. **No write imports.** `routes/agent.py` must not import write helpers such
   as `_set_app_setting`, `recalculate_metrics` or `db.session.commit`. A test
   checks this.

## Privacy model

### Token scopes

Each token entry gets an optional scope, and the default is the least
privileged:

```
LEDGER_MCP_TOKENS=claude:<sha256>:detail,muse:<sha256>:detail
```

`name:hash` with no scope means `summary`, so existing entries keep working.
`parse_tokens` returns `{name: (hash, scope)}`. The guard sets `g.agent_scope`,
and detail-only endpoints return `403 {'error': 'insufficient_scope'}` to a
summary token.

| Scope | Can see |
|---|---|
| `summary` | Totals, categories, allocation percentages, net worth / cash-flow history, projections, and accounts **aliased** by category (e.g. `retirement-2`) |
| `detail` | Everything in `summary`, plus real account names, per-account balances and history, individual holdings, and per-holding income |

### Never returned, at any scope

Every agent endpoint uses its own explicit serializer in `app/agent_view.py`
(an allowlist of fields). Agent endpoints don't reuse the web `*_to_dict`
helpers, so a column added to a model can't leak out by accident.

| Data | Reason |
|---|---|
| `AppSetting.anthropic_api_key`, and any setting not on the allowlist | Secret. Only `allocation_targets`, `save_rate_target`, `drip_tax_rate_pct` and `snapshot_timing` are readable. |
| `Account.account_number`, `Account.notes` | Identifier / free text |
| `Account.institution` | Identifying, and not needed for analysis (decided: excluded at `detail` too) |
| `SpendingEntry.notes`, `RecurringEntry.notes`, `RentalProperty.notes` | Free text that can hold anything, and also a prompt-injection channel |
| `RentalProperty.address` | Location. Properties are returned as `property-1`, `property-2`, … |
| `ImportLog` (file names, error text), raw `DividendData.source_notes` JSON | Not needed for analysis |

### Other protections

- **Transport:** HTTPS only. Tokens go in the header and never in a query
  string. Agent responses send `Cache-Control: no-store`.
- **Audit log:** each request logs agent name, scope, endpoint, parameters and
  response size at `INFO`, using `%s` formatting. Response bodies and amounts
  are never logged.
- **Bounded responses:** history defaults to the last 36 months, capped at 240
  rows. Holdings are capped at 200 rows. Inputs are validated with
  `_bad_request`.
- **Rate limit:** 120 requests per minute per token, held in an
  `app.extensions` instance (not a module global). A leaked token is then slow
  to scrape, and it still can't change anything.
- **Bridge secrets:** the bridge reads `LEDGER_MCP_TOKEN` from its environment,
  never from CLI arguments, which show up in `ps`. Claude Desktop stores
  that env in plain JSON, and both planned tokens are `detail`, so the README
  says to keep that config file user-only (`chmod 600`) and to rotate the
  token (`flask mcp-token`, swap the entry, restart) if the machine is shared
  or lost.
- **Data leaves your server:** whatever a tool returns goes to the model
  provider as part of the conversation. Scopes and aliasing are how you limit
  that.
- **Exfiltration through other tools:** a read-only finance MCP in the same
  session as a tool that can send data out (email, web fetch, …) creates an
  exfiltration path. Keep send tools on "ask before use" in sessions that load
  Ledger. Leaving out free-text fields shrinks the prompt-injection surface
  that could try to trigger that.

## Tools

Conventions shared by every response:

- Currency is USD rounded to cents.
- Months are `YYYY-MM`.
- Percentages are numbers with a `_pct` suffix (`12.5` means 12.5%).
- Every response includes `as_of` (the latest snapshot month), `scope`, and
  `warnings`, a list such as `"prices for 3 holdings are >7 days old"` or
  `"no snapshot for 2026-09"`.

Tool descriptions spell out metric definitions so the model reads them
correctly. For example: *"net_worth_non_re excludes real estate and the
mortgages tied to it"*, *"liquid net worth = liquid assets − all
liabilities"*, *"save_rate_pct = (income − expenses) / income"*.

| # | Tool | Scope | Endpoint | Returns | Backed by |
|---|---|---|---|---|---|
| 1 | `get_financial_snapshot` | summary | `/agent/v1/snapshot` | Net worth, total assets and liabilities, liquid NW, NW excluding real estate, month-over-month change, YTD save rate vs target, totals by bucket (Cash / Investments / Retirement / Real Estate / Liabilities), tax-status split (taxable / deferred / free), data freshness | `CalculatedMetric`, latest snapshots |
| 2 | `get_net_worth_history` | summary | `/agent/v1/net-worth-history?start=&end=` | Monthly `net_worth`, `net_worth_non_re`, `net_worth_liquid`, assets and liabilities; CAGR over the range; trailing 12-month change; best and worst months; balances by bucket per month (the allocation-history data) | `CalculatedMetric`, `projections.calculate_cagr` |
| 3 | `get_cash_flow` | summary | `/agent/v1/cash-flow?start=&end=` | Monthly income, expenses, savings, save rate; trailing 3/12-month averages; recurring income and expense totals. At `detail`, per-card/source breakdown (names only, no notes) | `CalculatedMetric`, `SpendingEntry`, `RecurringEntry` |
| 4 | `get_asset_allocation` | summary | `/agent/v1/allocation` | Current vs target % for domestic / international / bonds / cash, drift in % and $, the amount to move to rebalance per class, market-cap tilt, allocation by tax status | Extracted allocation service (today in `routes/allocation.py`) |
| 5 | `list_accounts` | summary* | `/agent/v1/accounts` | id, label, category, type, tax status, liquid flag, latest balance and its date, 12-month change. *Labels are aliases at `summary` and real names at `detail`. | `Account`, latest snapshots |
| 6 | `get_account_history` | detail | `/agent/v1/accounts/<id>/history?start=&end=` | Monthly balances, CAGR, allocation splits | `AccountSnapshot` |
| 7 | `get_holdings` | detail | `/agent/v1/holdings?account_id=` | Ticker, name, shares, price and price age, value, portfolio weight, asset-class splits; concentration (top-10 weight, any position >10%) | `Holding`, `HoldingAllocation`, `TickerClassification` |
| 8 | `get_passive_income` | summary | `/agent/v1/income` | Annual and monthly dividend income, portfolio yield, after-tax estimate, breakdown by payer type and frequency, net rental income (aliased properties), account-level yield estimates, list of tickers missing data. At `detail`, per-holding rows. Cached data only. | Extracted income service, `dividend_calc.calculate_current_income` |
| 9 | `project_net_worth` | summary | `/agent/v1/projections/growth?years=&monthly_contribution=&rates=` | Month-by-month projected net worth and categories. Defaults (category CAGRs, current balances, mortgage amortisation) come from history; any can be overridden. Pure computation. | `projections.project_growth`, `_build_projections_context` (extracted) |
| 10 | `project_financial_independence` | summary | `/agent/v1/projections/fi?annual_spending=&swr=&growth=&monthly_savings=` | FI number, months/date to FI, projected series, sensitivity table (SWR × growth). Spending defaults to trailing-12 expenses. | `projections.fi_number`, `months_to_fi`, `fi_sensitivity_table*` |
| 11 | `project_dividend_income` | summary | `/agent/v1/projections/income?years=&drip=&contribution=` | DRIP projection of yearly income and portfolio value | `dividend_calc.simulate_drip` |

Tools 9–11 are `GET` with query parameters even though they compute: they
read data and write nothing, so they don't need `POST`. The web equivalents
are `POST` only because they take a JSON body.

### Example: `get_financial_snapshot` (summary scope)

```json
{
  "as_of": "2026-09",
  "scope": "summary",
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

### MCP prompts (optional, in the bridge)

These prompt templates show up as slash-commands in clients that support them:

- `monthly_review`: snapshot, then history (12 months), then cash flow; what
  changed and why.
- `rebalance_check`: allocation plus holdings; drift, tax-aware placement
  ideas, concentration risk.
- `fi_checkup`: FI projection under current, conservative and optimistic
  assumptions.

Each prompt tells the model to cite `as_of`, flag stale data, and frame its
output as analysis rather than professional financial advice.

## Code changes

Some logic the agent needs lives inside route handlers today: passive income,
allocation, holdings summary, projections context. It's also duplicated, for
example "latest balance per account" appears in four places and `BUCKET_MAP`
in two. CLAUDE.md requires that logic to live in services, and the agent must
not copy it, so the first step is extracting it. The web routes then call the
same services.

| Module | Change |
|---|---|
| `app/portfolio_service.py` (new) | `latest_balances(account_ids)`, `bucket_totals()`, `allocation_breakdown()`, `holdings_summary()`. Read-only. Used by web and agent routes. |
| `app/income_service.py` (new) | `passive_income(account_id=None, fetch_missing=True)`, moved out of `routes/income.py`. The agent passes `fetch_missing=False`. |
| `app/projections_service.py` (new) | `projection_defaults()`, moved out of `helpers._build_projections_context`. |
| `app/account_categories.py` | Add a shared `BUCKET_MAP` and `BUCKET_ORDER` (removes the two copies in `visualizations.py`). |
| `app/agent_view.py` (new) | Scope-aware serializers, account and property aliasing, settings allowlist, `warnings` / freshness builder. |
| `app/mcp_token_service.py` | Optional `:scope` on each token entry; validate `{summary, detail}`. |
| `app/access_control.py` | GET-only rule, `g.agent_scope`, `agent_scope_required('detail')`, `no-store` header, read-only session guard, rate limiter. |
| `app/routes/agent.py` | The 11 `/agent/v1/*` endpoints. Each parses input, calls a service, and serializes through `agent_view`. |
| `app/cli.py` | `flask mcp-token NAME --scope summary|detail`. |
| `mcp/ledger_mcp.py`, `mcp/requirements.txt` (new) | stdio bridge (FastMCP + `httpx`). Kept out of the server's `requirements.txt`. |
| `README.md`, `SECURITY.md` | Bridge setup and scope guidance. |

### Tests

Following the CLAUDE.md coverage rules:

- **Each endpoint:** `401` without a token, `401` with a Google session only,
  `403` for a summary token on detail endpoints, `400` for bad parameters,
  `404` for an unknown account, `200` with empty data, `200` with seeded data.
- **Read-only:** a url_map sweep checks every agent route is GET-only. A flush
  during an agent request raises. Patched Anthropic and yfinance clients assert
  they are never called. A row-count fingerprint of every table is identical
  before and after calling every agent endpoint.
- **Privacy:** the full `/agent/v1/*` sweep with every sensitive field seeded
  with a canary string (`notes`, `account_number`, `address`, API key, …).
  The canary must never appear in any response, at any scope. At summary
  scope, real account names must not appear either.
- **Services:** the extracted services keep the existing web route tests
  green. New services get success, empty, invalid-input and external-failure
  tests.

## Phased delivery

| Phase | Scope | Exit criteria |
|---|---|---|
| 0. Hardening | Token scopes, GET-only rule, read-only session guard, `no-store`, rate limit | Read-only and scope tests pass, `whoami` reports scope |
| 1. Summary tools | Service extraction + tools 1–4 and 8 at summary scope | Web route tests unchanged and green; canary sweep passes |
| 2. Bridge | `mcp/ledger_mcp.py` + docs; try it from Claude Desktop / Code | You can ask "how did my net worth change this year?" end to end |
| 3. Detail tools | Tools 5–7, detail views of 3 and 8 | Scope tests pass |
| 4. Projections + prompts | Tools 9–11, MCP prompts | Results match the web projections page for the same inputs |
| 5. Remote (optional) | Streamable-HTTP transport + OAuth 2.1 for claude.ai web/mobile | Separate design |

## Decisions

| Question | Decision |
|---|---|
| Clients | **Claude** and **Muse AI**, one token each (`claude`, `muse`). Claude connects through the stdio bridge. Muse uses the bridge if it speaks MCP; otherwise it calls `/agent/v1/*` directly with the same token, and gets the same read-only guarantees. |
| Scope | Both tokens are **`detail`**. `summary` stays the default for any entry without a scope, so a future agent starts least-privileged. |
| Institutions | **Excluded at every scope**, `detail` included. |
| Stale prices / dividend data | **Warn only.** Agent tools never refresh. Refreshing stays a website action. |

## Open question

- **Claude on web/mobile?** If "Claude" includes claude.ai in the browser or
  the mobile app (not just Claude Desktop / Code), Phase 5 (remote transport
  with OAuth) becomes required rather than optional.
