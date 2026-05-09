# Ledger — Multi-Agent Product Review
**Date:** 2026-05-05
**Codebase:** panderson54/ledger_finance
**Method:** Parallel subagent review — Data Integrity, Code Architect, UX, Security, Product Strategy, Roadmap Synthesis

---

## Executive Summary

Ledger is a mature, well-structured self-hosted personal finance dashboard that substantially exceeds its stated feature set: alongside the core "<5 min/month net worth tracking" promise, the codebase ships a full holdings tracking system, live price fetching, AI-powered ticker classification, dividend income modeling, DRIP projections, a multi-scenario Path to FI calculator, and a growth projection engine. The architecture is clean (app factory, proper test fixtures, Numeric fields throughout) and the UX is genuinely well-thought-out for a solo project. Two issues need immediate attention before any production deployment: `debug=True` is hardcoded in `run.py` (Werkzeug RCE on LAN), and `_recalculate_metrics` does not cascade to subsequent months when a historical snapshot is edited (silent stale data). Three to five hours of targeted fixes close all critical gaps. The app is otherwise close to a coherent v1 release, pending a documentation catch-up and a few confirmation-dialog additions.

---

## Agent A — Data Integrity Findings

### Strengths
- All financial amounts correctly use `Numeric(12,2)` / `Numeric(10,2)` rather than Float — no floating-point precision errors (models.py:50, 72, 115-128)
- `AssetAllocation` and `HoldingAllocation` have database-level CHECK constraints: `percentage >= 0 AND percentage <= 100` (models.py:97, 195)
- `save_rate` correctly stores `NULL` for zero-income months rather than `0` (routes.py:115-118)
- Every AccountSnapshot and SpendingEntry write triggers `_recalculate_metrics()` — CalculatedMetric stays in sync with source data (routes.py:1549, 1574, 1597, 1634, 1670, 1693)
- `CalculatedMetric.metric_date` is unique; AccountSnapshot has a composite unique constraint on `(account_id, snapshot_date)` — no duplicate months possible (models.py:56, 112)
- `fi_number()` returns `float("inf")` when SWR ≤ 0; `months_to_fi()` returns `None` gracefully after 600 months — no crash path (projections.py:331-332, 348)
- Division-by-zero guarded in `_recalculate_metrics`: `monthly_change_pct` uses `if prev_nw and prev_nw != 0 else None` (routes.py:107)
- `amortize_mortgage()` handles `r == 0` with straight-line payoff (projections.py:107-110)
- `asset_allocations` table is actively populated via `_save_account_allocations()` — not a dead table (routes.py:432-456)

### Issues Found

- **Severity:** High
- **Location:** `app/routes.py`, `_recalculate_metrics()` (lines 37-139)
- **Description:** Editing a historical month's snapshot updates only that month's `CalculatedMetric`. The *next* month's `monthly_change_amount` and `monthly_change_pct` depend on the current month's `net_worth` as `prev_nw` (routes.py:103-108). Correcting January causes February's MoM change to silently stale. This compounds over multi-month corrections.
- **Recommendation:** After computing `metric` for `month_date`, queue a recalculation of `month_date + relativedelta(months=1)`. A simple approach: add one recursive call capped by a `recurse=False` flag so it propagates one level only.

---

- **Severity:** High
- **Location:** `app/models.py`, `AccountSnapshot` (line 50)
- **Description:** `balance = Numeric(12,2)` has no CHECK constraint preventing negative values. A user can enter –$500,000 for a Cash asset account and corrupt net worth calculations. Liabilities are stored as positive numbers by convention — that convention is undocumented and unenforced.
- **Recommendation:** Add `db.CheckConstraint('balance >= 0', name='non_negative_balance')` to `AccountSnapshot.__table_args__` and document the positive-liability convention in a code comment.

---

- **Severity:** Medium
- **Location:** `app/models.py`, `SpendingEntry` (line 71)
- **Description:** `account_name = String(100)` is free text with no FK to `accounts`. "Chase Visa" and "chase visa" are treated as distinct payers; renaming an account leaves historical entries orphaned. Aggregation in `_recalculate_metrics` works correctly but income attribution is fragmented.
- **Recommendation:** Add an optional `account_id FK` with backfill migration (fuzzy-match by name). Keep `account_name` for backward compatibility. Low urgency since the design intentionally tracks by card name, not account.

---

- **Severity:** Medium
- **Location:** `app/routes.py`, `api_snapshot_create()` and `api_snapshot_update()` (lines 1512-1583)
- **Description:** No validation prevents future-dated snapshots. A June 2026 snapshot can be created in May 2026, confusing projections (which use "latest snapshot" as the current position) and the month list UI.
- **Recommendation:** Add `if month_date > date.today().replace(day=1): return error('Snapshot date cannot be more than one month in the future')` in both endpoints.

---

- **Severity:** Low
- **Location:** `app/models.py`, `CalculatedMetric` (line 123)
- **Description:** `monthly_change_pct = Numeric(6,2)` supports max ±9999.99%. For an account growing from $10 to $1,000 in one month (9,900% change), the value silently truncates.
- **Recommendation:** Widen to `Numeric(8,2)` (max ±999,999.99%) or clamp at application layer in `_recalculate_metrics`.

---

- **Severity:** Low
- **Location:** `app/models.py`, `Holding` (line 169); `app/routes.py`, `api_holding_archive()` (line 2723)
- **Description:** Soft-deleting a holding (`is_active=False`) leaves `HoldingAllocation` rows intact. The `cascade='all, delete-orphan'` only fires on hard deletes. Over time, stale allocation rows accumulate.
- **Recommendation:** On soft-delete, explicitly delete related `HoldingAllocation` rows: `HoldingAllocation.query.filter_by(holding_id=holding.id).delete()` before `holding.is_active = False`.

---

- **Severity:** Low
- **Location:** `app/models.py`, `Holding` (lines 166-167)
- **Description:** `shares` and `last_price` have no CHECK constraints. Negative shares or prices are accepted and produce incorrect portfolio values in `_compute_holdings_value()`.
- **Recommendation:** Add CHECK constraints: `shares >= 0`, `last_price >= 0`.

### Schema Observations

- The two-phase allocation architecture (Phase 1: `AssetAllocation` × snapshot balance; Phase 2: `HoldingAllocation` × market value) is elegant and well-implemented in `_account_holding_splits()` (routes.py:2011-2050). Phase 2 automatically takes precedence when holdings with prices exist.
- `CalculatedMetric` is a pre-computed denormalized view upserted on every write. It's the right pattern for read performance but requires the cascade fix above to stay correct.
- Account `is_active` is a soft-delete flag, but `AccountSnapshot` rows cascade on hard delete — archiving does not delete history, which is correct.
- No explicit index defined on `CalculatedMetric.metric_date` despite being the primary sort/filter key for all chart endpoints. On 120+ rows this is fine; worth adding for long-running instances.

---

## Agent B — Code Architecture Findings

### Strengths
- Clean app factory pattern with sensible defaults and rotating log handlers (`app/__init__.py:63-113`)
- Test suite uses session-scoped `app`, function-scoped `db` with full teardown, in-memory SQLite + `StaticPool` — proper isolation (`tests/conftest.py:14-50`)
- 10 single-responsibility models with thoughtful relationships and Decimal types (`app/models.py`)
- Consistent JSON API patterns: uniform response shapes, correct HTTP status codes (`app/routes.py:1512-1812`)
- Batch account creation is transactional: validates all before writing, uses `flush()` to populate IDs, rolls back on error (`app/routes.py:683-751`)
- Phase 2 holdings fallback logic is well-architected: Holdings × live price takes precedence over manual AssetAllocation splits (`app/routes.py:2011-2050`)
- `app/static/style.css` is pure Bootstrap 5 CSS-variable overrides — no JS mixed into styles

### Issues Found

- **Severity:** Critical
- **Location:** `run.py`, line 25
- **Description:** `app.run(debug=True, host='0.0.0.0', port=5001)` hardcodes debug mode. When invoked directly via `python run.py`, the Werkzeug interactive debugger is accessible to any LAN device. This is a separate issue from the security audit; from an architecture perspective, the run configuration is not environment-aware.
- **Recommendation:** `app.run(debug=os.getenv('FLASK_DEBUG', '').lower() in ('1','true'), host='0.0.0.0', port=int(os.getenv('PORT', 5001)))`

---

- **Severity:** Critical
- **Location:** `app/__init__.py`, line 76
- **Description:** `SECRET_KEY` silently falls back to `'dev-secret-key-change-this'` with no warning log. Any feature using Flask sessions or CSRF tokens added in future would be immediately insecure if SECRET_KEY isn't set.
- **Recommendation:** Log a warning at startup when the fallback is used. In production, raise `RuntimeError` if `FLASK_ENV == 'production'` and key is default.

---

- **Severity:** High
- **Location:** `app/__init__.py`, lines 110-111
- **Description:** `db.create_all()` runs inside the factory alongside Flask-Migrate. A developer who adds a model but forgets `flask db migrate` gets the table auto-created in dev but not in production. Schema drift is silent.
- **Recommendation:** Remove `db.create_all()` from the factory. `conftest.py` already calls it for tests (correct for ephemeral DBs). Add a note in the README dev setup section.

---

- **Severity:** High
- **Location:** `requirements.txt` (absent); all POST routes in `app/routes.py`
- **Description:** No CSRF protection. Flask-WTF is not listed. State-mutating routes accept POST/PUT/DELETE without token validation.
- **Recommendation:** Add `Flask-WTF>=1.2.1` to `requirements.txt`. Initialize `CSRFProtect` in the factory. Add `{{ csrf_token() }}` to all HTML forms. JSON API endpoints should validate `X-CSRFToken` header.

---

- **Severity:** High
- **Location:** `app/routes.py` (entire file, 3147 lines)
- **Description:** Single Blueprint with 15+ route groups: accounts, monthly data, charts API, import/export, snapshots, spending, recurring entries, metrics, projections, allocation, holdings, prices, classification, dividend, settings. Navigation requires scrolling 1,500+ lines to find related code.
- **Recommendation:** Split into 5-6 blueprints: `accounts_bp`, `monthly_bp`, `allocation_bp`, `holdings_bp`, `projections_bp`, `settings_bp`. Estimated effort: 4-6 hours. Highest-value refactor for long-term maintainability.

---

- **Severity:** Medium
- **Location:** `requirements.txt`
- **Description:** Mixed pinning: `Flask==3.0.0` (exact), `plotly==5.18.0` (exact), but `Flask-SQLAlchemy>=3.1.1`, `anthropic>=0.50.0` (minimum). Transitive dependency drift possible. `pytest` is not listed at all.
- **Recommendation:** Add `pytest>=7.0` and `pytest-cov>=4.0` (or split into `requirements-dev.txt`). Pin all dependencies to exact versions or use `pip-tools` to generate a lock file.

---

- **Severity:** Medium
- **Location:** `app/__init__.py`, lines 17-18
- **Description:** `db` and `migrate` are module-level globals. Safe for this app's usage but brittle if the factory is ever called multiple times in a process.
- **Recommendation:** Document the pattern with a comment; acceptable for a solo dev self-hosted app.

---

- **Severity:** Medium
- **Location:** `app/routes.py`, various validation functions (lines 242-259, etc.)
- **Description:** Manual `if`-chain validation throughout. Inconsistent error messages, no type coercion.
- **Recommendation:** Adopt Marshmallow or Pydantic for API endpoint validation. Medium-term improvement, not blocking.

---

- **Severity:** Low
- **Location:** `app/routes.py`, `_get_app_setting()` (line 1995)
- **Description:** Every route that checks a setting makes a fresh DB query. With frequent page loads, these compound.
- **Recommendation:** Cache settings dict at app startup; invalidate on `/api/settings` POST.

---

- **Severity:** Low
- **Location:** `app/routes.py`, `api_holding_create()` (line 2620)
- **Description:** `shares=0` is accepted without error. A holding with 0 shares pollutes the portfolio display.
- **Recommendation:** Validate `shares > 0` in create and update endpoints.

---

- **Severity:** Low
- **Location:** `app/models.py`, query patterns throughout
- **Description:** No explicit DB indexes on `CalculatedMetric.metric_date`, `AccountSnapshot(account_id, snapshot_date)`, `SpendingEntry.entry_date`, or `Holding(account_id, is_active)`.
- **Recommendation:** Add indexes via `__table_args__` + migration. Low impact at current data sizes.

### Refactoring Opportunities

1. **Split routes.py into blueprints** — 4-6 hours, highest long-term value. Split by feature domain.
2. **Add CSRF protection** — 2-3 hours, critical if any internet exposure is planned.
3. **Remove `db.create_all()` from factory** — 30 minutes, prevents migration drift.
4. **Environment-conditional debug mode** — 15 minutes, eliminates security footgun.
5. **Pin all dependencies + add pytest** — 1 hour, reproducible builds.
6. **Settings caching** — 1 hour, marginal performance gain.
7. **DB indexes for query patterns** — 1 hour, future-proofing.

---

## Agent C — UX Findings

### Strengths
- Responsive nav with correct `request.endpoint`-based active state across nested pages (`base.html`)
- Actionable empty states with direct CTAs on dashboard, monthly update, and accounts pages
- Flash messages use Bootstrap alert classes with dismiss buttons — consistent for success paths (`index.html`, `accounts.html`)
- 4-step onboarding wizard with progress indicator and category-based auto-fill reduces cognitive friction (`onboarding.html`)
- Sticky metrics banner (NW, MoM change, Save Rate, Surplus) stays visible while scrolling in month detail view
- Account "pill" selector with inline balance editing streamlines snapshot entry — keyboard navigation (Arrow keys for months, Enter to save) supports power users
- Delta warnings: large balance changes are flagged with color-coded badges and dismissible alerts
- Collapsible snapshot table reduces visual clutter on months with many accounts
- Settings page covers recurring entries, snapshot timing, AI toggle, and cache management with clear explanations

### Issues Found

- **Severity:** Critical
- **Location:** `month_detail.html`, `monthly_update.html`, `accounts.html` (delete/archive JS functions)
- **Description:** Destructive actions (delete month, delete snapshot, delete spending entry) use the browser's native `confirm()` dialog — small, unstyled, easy to dismiss accidentally. No custom modal with warning context.
- **Recommendation:** Replace `confirm()` with a Bootstrap modal that names the item being deleted, shows a red "Delete" CTA, and requires a second click. Example: "Delete all data for March 2025? This cannot be undone."

---

- **Severity:** High
- **Location:** `base.html`, navigation (lines 44-69)
- **Description:** Visualizations (`/visualizations`) and Import (`/import`) pages exist but are not in the main nav. Users cannot discover them except via buttons buried in empty states or direct URL knowledge.
- **Recommendation:** Either add "Import" to the nav (between Monthly Update and Projections) or ensure every entry point is clearly signposted. Visualizations may be intentionally subordinate to Reports — if so, add a "Charts" tab within the Reports page.

---

- **Severity:** High
- **Location:** `month_detail.html`, snapshot wizard save handler (approx line 967)
- **Description:** Saving a snapshot in the wizard triggers a full page reload. This loses scroll position and creates a jarring experience when entering 10+ accounts across a month.
- **Recommendation:** Replace with an AJAX update: insert the new snapshot row into the DOM, update the progress counter, and re-enable the account pill without reloading.

---

- **Severity:** High
- **Location:** `accounts.html`, `toggleArchive()` function
- **Description:** Archiving an account has no confirmation dialog. Archive is reversible but removes the account from projections, quick actions, and new month suggestions — a significant change that users may make accidentally.
- **Recommendation:** Add a modal: "Archive [Account Name]? It will be hidden from active views but all data is preserved."

---

- **Severity:** Medium
- **Location:** `index.html`, empty state alerts (lines 18-32)
- **Description:** Two separate alert blocks for "no accounts" vs "no snapshots." The second alert can appear even after accounts are created, which is confusing.
- **Recommendation:** Consolidate: one alert with context-sensitive text ("Add your first account" → "Now record your first month's balances").

---

- **Severity:** Medium
- **Location:** `month_detail.html`, progress bar
- **Description:** Progress bar shows completion visually but the counter label is small. With 15+ accounts, users can lose track of how many remain.
- **Recommendation:** Make the counter more prominent: "3 of 12 accounts entered" in a visible heading rather than just a progress bar.

---

- **Severity:** Medium
- **Location:** `allocation.html`, Refresh Prices button
- **Description:** No loading state when the refresh button is clicked. No success/error notification after completion.
- **Recommendation:** Disable button and show spinner during fetch; display a toast on success ("12 prices updated") or failure.

---

- **Severity:** Medium
- **Location:** `projections.html`, empty state message
- **Description:** Generic "No financial data found" does not indicate whether accounts, snapshots, or both are missing.
- **Recommendation:** Check which condition is missing and provide direct links: "Add your first account" or "Enter your first month's balances."

---

- **Severity:** Medium
- **Location:** `onboarding.html`, `commitAccounts()` function
- **Description:** Duplicate name validation is client-side only. Server-side duplicate key violations show generic errors without pointing to which account name conflicts.
- **Recommendation:** Improve error handling in `commitAccounts()` to parse server errors and highlight the conflicting account row.

---

- **Severity:** Low
- **Location:** `month_detail.html`, snapshot collapse state
- **Description:** Snapshot table collapse state resets on every page load. Users who prefer expanded view must click every time they navigate to a month.
- **Recommendation:** Persist collapse preference in `sessionStorage`.

---

- **Severity:** Low
- **Location:** `settings.html`, auto-hiding alerts
- **Description:** Success/error alerts auto-hide after 4 seconds. Users looking away may miss confirmation.
- **Recommendation:** Extend auto-hide to 6-8 seconds or require explicit dismiss.

### Flow Observations

1. **Monthly update flow friction:** The biggest friction point is the full page reload after each snapshot save (in the wizard). Eliminating this one reload would make the monthly entry feel significantly faster. The rest of the flow (month init → recurring entries auto-applied → wizard → spending entries) is lean and well-designed.

2. **Onboarding is strong but exits awkwardly:** After completing the wizard, users land on the CSV import page with `from=onboarding`. For users who manually entered accounts (not via CSV), this redirect feels like a non-sequitur. Consider redirecting to the first month's detail page instead.

3. **Destructive action audit:** Month delete and snapshot delete are permanent and protected only by `confirm()`. Account archive is reversible and lacks any confirmation. These protections are inverted — the reversible action gets no dialog while the permanent ones get only a browser popup.

4. **Settings discoverability:** Recurring entries, snapshot timing, AI classification, and cache management are all on one long settings page. Tab-based layout (Recurring | Preferences | Integrations | Cache) would improve scanability.

5. **Mobile experience:** The sticky metrics banner may overlap content on screens narrower than 360px. The accounts table shows too many columns on mobile. Both are minor for a self-hosted desktop-first app.

---

## Agent D — Security & Privacy Findings

### Strengths
- `.gitignore` properly excludes `.env`, `.env.*`, `data/`, `logs/` — no accidental credential commits
- All database queries use SQLAlchemy's parameterized API (`filter_by`, `query.filter`) — no SQL injection vectors
- File upload uses `secure_filename()` to strip path traversal characters (`routes.py:1115`)
- Anthropic API key is not logged or returned in API responses — settings endpoint returns only a boolean `api_key_set` flag (`routes.py:2854`)
- CSV import uses pandas/regex parsing — no raw injection vectors
- `app/__init__.py` 500 handler logs exceptions without exposing stack traces to users (line 106)
- Docker image runs as non-root user (`appuser`) per Dockerfile
- Docker Compose sets `FLASK_ENV=production` explicitly — debug mode disabled in container deployments
- README prominently warns users the app is unauthenticated and unsuitable for public internet exposure, with VPN recommendations

### Issues Found

- **Severity:** Critical
- **Location:** `run.py`, line 25
- **Description:** `app.run(debug=True, host='0.0.0.0', port=5001)` — when invoked directly (not via Docker/Gunicorn), the Werkzeug interactive debugger is accessible to every device on the LAN at `http://<host>:5001`. The debugger provides an interactive Python console that allows **arbitrary code execution** on the host machine. This is not theoretical: any LAN device (another user's laptop, a compromised IoT device, a smartphone on the same WiFi) can trigger and interact with error pages to execute code.
- **Recommendation:** Remove `debug=True` or make it conditional on `FLASK_DEBUG` env var. Update README to warn that `python run.py` should only be used in development, not on a shared LAN.

---

- **Severity:** Critical
- **Location:** `app/__init__.py`, line 76
- **Description:** `SECRET_KEY` silently falls back to `'dev-secret-key-change-this'`. This string is publicly visible in the repository. Any future feature using Flask sessions, CSRF tokens, or signed cookies would be immediately compromised on any deployment where `SECRET_KEY` is not explicitly set.
- **Recommendation:** Log `logger.warning("SECRET_KEY not set — using dev fallback; set SECRET_KEY in .env")` at startup. In `FLASK_ENV=production`, raise `RuntimeError` to prevent silent misconfiguration.

---

- **Severity:** High
- **Location:** All state-mutating routes (`/api/settings`, `/api/accounts/batch`, `/api/snapshots`, `/api/spending`, etc.)
- **Description:** No CSRF protection. A malicious webpage visited by a LAN user can make cross-origin requests to the app's predictable address (e.g., `http://ledger.local:5001`). The browser sends requests without credentials, but the app has no auth anyway — so any tab open in the browser can trigger data modifications. An attacker could craft an email with an embedded `<img src="http://ledger.local/api/months/2025-01" onerror="fetch('...', {method:'DELETE'})" />` style payload.
- **Recommendation:** Add `Flask-WTF>=1.2.1`, initialize `CSRFProtect` in the factory. JSON endpoints should check `X-CSRFToken` header set by all AJAX calls. HTML forms add `{{ csrf_token() }}`.

---

- **Severity:** High
- **Location:** `README.md`, Nginx config (lines 379-395)
- **Description:** The recommended Nginx config includes no security headers (`X-Frame-Options`, `X-Content-Type-Options`, `X-XSS-Protection`, `Referrer-Policy`). Without `X-Frame-Options`, the app could be embedded in an iframe on an attacker-controlled page and used for clickjacking.
- **Recommendation:** Add to Nginx `server` block:
  ```nginx
  add_header X-Frame-Options "SAMEORIGIN" always;
  add_header X-Content-Type-Options "nosniff" always;
  add_header X-XSS-Protection "1; mode=block" always;
  add_header Referrer-Policy "strict-origin-when-cross-origin" always;
  ```

---

- **Severity:** High
- **Location:** `README.md`, Nginx config (lines 379-395)
- **Description:** No rate limiting configured. A buggy or malicious LAN client can flood the API, causing denial of service or exhausting Claude API quota.
- **Recommendation:** Add `limit_req_zone` and `limit_req` directives to the Nginx config for `/api/` routes (e.g., 5 req/s per IP with burst of 20).

---

- **Severity:** Medium
- **Location:** Docker `entrypoint.sh`; `README.md` (2 Gunicorn workers)
- **Description:** SQLite is a single-writer database. Two Gunicorn workers writing concurrently can produce `database is locked` errors under modest load. WAL mode is not configured.
- **Recommendation:** Enable WAL mode after DB creation: `db.engine.execute("PRAGMA journal_mode=WAL")` or equivalent. Alternatively, document that 1 worker is recommended for SQLite deployments and 2 workers are only safe under low write concurrency.

---

- **Severity:** Medium
- **Location:** `app/routes.py`, `api_settings_save()` (lines 2888-2892); `app/models.py`, `AppSetting.value` (line 145)
- **Description:** The Anthropic API key is stored as plaintext in SQLite. Anyone with read access to `data/finance.db` (a database backup, `cp` of the data dir, etc.) can extract the key.
- **Recommendation:** Document this limitation. Advise users to rotate the API key if the data directory is accessed by unauthorized parties. For higher security, encrypt the value before storage using `cryptography.fernet`.

---

- **Severity:** Low
- **Location:** `app/__init__.py`, `create_app()` (lines 63-113)
- **Description:** No startup configuration validation. If `SECRET_KEY` is the dev fallback or the database directory is unwritable, the app starts with no clear error.
- **Recommendation:** Add a `_validate_config(app)` function called before blueprint registration that checks critical settings and logs clear errors.

### Threat Model Notes

The app's security posture is appropriate for its stated self-hosted LAN-only threat model. The README is honest about the lack of authentication. However, two findings break the LAN safety assumption:

1. The Werkzeug debugger RCE is the only finding that allows a *non-user* LAN device to cause harm — it needs to be fixed unconditionally.
2. CSRF is a real attack against the *user's browser* (not the network) — it works even when no other LAN devices are involved, just by tricking the user into visiting a malicious page.

The remaining findings (SECRET_KEY fallback, SQLite WAL, API key in DB) are low-risk for the intended deployment context but should be addressed as hardening before any wider distribution.

---

## Agent E — Product Strategy Findings

### Strengths
- Core monthly entry flow delivers on the "<5 min/month" promise: one-click month init, per-account snapshot wizard, recurring entries auto-applied, AJAX balance saves
- Clean Bootstrap 5 UI with no unnecessary complexity; dark mode works out of the box
- Comprehensive holdings system (ticker tracking, live prices, AI classification, dividend income, DRIP simulation) is production-quality, not a prototype
- Sophisticated projections (multi-scenario growth with mortgage amortization, Path to FI with sensitivity tables, Coast FI modeling) are mathematically sound
- Self-hosted SQLite design delivers the privacy-first promise with zero cloud dependencies
- CSV import and export support two formats (generic and Ledger-export roundtrip) with idempotent behavior
- Recursive migration history (9 migrations) shows steady iterative development

### Backlog Reconciliation

Features shipped but not documented in README:

- **Item:** Projections page (Growth + FI tabs)
  - **Evidence:** `app/templates/projections.html` (1,439 lines), full multi-scenario Growth tab with mortgage amortization and planned expense timeline; FI tab with sensitivity tables, Coast FI, conservative scenario; `app/projections.py` (460 lines)
  - **Recommendation:** Add to README Features: "Financial projections — growth scenarios, path-to-FI analysis, sensitivity modeling"

- **Item:** Asset Allocation / Reports page
  - **Evidence:** `app/templates/allocation.html` (972 lines): Investments tab (allocation drift, market cap distribution, holdings table), Cash Flow tab (YTD summaries, rolling save rate, income/expense trends); DRIP projection tab
  - **Recommendation:** Add to README Features: "Asset allocation reports with drift detection, dividend income tracking, and cash flow analysis"

- **Item:** Holdings tracking system (ticker-level, live prices, dividends, DRIP)
  - **Evidence:** `Holding`, `HoldingAllocation`, `TickerClassification`, `DividendData` models; Holdings CRUD API; `price_service.py`, `classification_service.py`, `dividend_service.py`, `dividend_calc.py`
  - **Recommendation:** Add to README Features: "Holdings tracking with live Yahoo Finance prices, AI-powered ticker classification, dividend income estimation, and DRIP reinvestment projections"

- **Item:** CSV Export
  - **Evidence:** `/export/csv` route (routes.py:1201); "Export CSV" button present in monthly_update.html
  - **Recommendation:** Add to README Features: "CSV export of all accounts and snapshots for backup or migration"

- **Item:** Onboarding wizard
  - **Evidence:** `app/templates/onboarding.html` (330 lines), 4-step wizard with batch account creation API, category auto-fill
  - **Recommendation:** Add to README Features: "Interactive onboarding wizard for first-time setup"

- **Item:** Recurring entries
  - **Evidence:** `RecurringEntry` model, `/api/recurring-entries/*` CRUD, Settings page UI, auto-apply logic in `api_month_init()` (routes.py:1401-1427)
  - **Recommendation:** Add to README Features: "Recurring income and expense templates auto-applied each month"

- **Item:** Account history charts
  - **Evidence:** `/accounts/<id>/history` route and `account_history.html` template with CAGR calculation and MoM change table
  - **Recommendation:** Mention briefly in "Interactive charts" feature bullet

- **Item:** S&P 500 comparison
  - **Evidence:** `/api/sp500-change` endpoint and dashboard hero card with 1M/3M/YTD/ALL period comparison
  - **Recommendation:** Add to README: "Net worth change compared to S&P 500 benchmark"

### Issues Found

- **Priority:** High
- **Description:** The product ships holdings/projections/DRIP as core features, but the README and product pitch describe only "net worth tracking + spending." This creates a gap: new users discovering the app via README expect a simple tracker, then find a full investment analytics platform — either pleasantly surprised or overwhelmed.
- **Recommendation:** Revise the product pitch: "Self-hosted net worth tracker with optional investment analytics (holdings, projections, dividends)." Make the core 5-account, no-holdings setup clearly available without requiring the advanced features.

- **Priority:** High
- **Description:** AI classification silently falls back to "manual entry required" when the API fails or key is missing, but no in-context UI messaging explains this to users who just added a holding.
- **Recommendation:** In the holdings form, show a clear message: "AI classification unavailable — enter allocation splits manually or leave blank." Test the full flow without an API key.

- **Priority:** High
- **Description:** Dividend cache (`DividendData`) has a staleness check via `is_dividend_stale()` (30-day TTL per `dividend_service.py`), but this is only triggered when the passive income API is called. If users never visit the income tab, dividend data ages indefinitely with no refresh prompt.
- **Recommendation:** Add a visible "Last updated: X days ago" label on the dividend data display. Optionally trigger background refresh on holdings page load.

- **Priority:** Medium
- **Description:** Month delete (`DELETE /api/months/<YYYY-MM>`) permanently removes all snapshots and spending entries with no undo. For a self-hosted app where the user is their own DBA, data recovery requires manual SQLite access.
- **Recommendation:** Document the backup procedure prominently near the delete action. The `backup_db.py` script in `scripts/` is the right tool — link to it in the UI or add a "Create backup before deleting" step in the confirmation modal.

- **Priority:** Medium
- **Description:** README's Quick Start section doesn't mention production deployment. A user following the Quick Start will run `python run.py` (with `debug=True`) indefinitely on their Raspberry Pi.
- **Recommendation:** Add a one-line callout in Quick Start: "For always-on deployment, see the Raspberry Pi + Docker sections below."

- **Priority:** Medium
- **Description:** Settings page has no indication that Claude AI ticker classification incurs per-call API costs (~$0.001-$0.01 per ticker). Users enabling it without an API budget may be surprised.
- **Recommendation:** Add help text: "Each new ticker incurs a small API cost; lookups are cached per ticker."

- **Priority:** Low
- **Description:** Projections UI supports up to 3 scenarios but doesn't explain the limit when the "Add Scenario" button disappears after the third.
- **Recommendation:** Show a tooltip or disabled-state message: "Maximum 3 scenarios."

### v1 Release Checklist (Draft)

**Core Functionality**
- [x] Monthly data entry (snapshot wizard + spending entries)
- [x] Dashboard with net worth chart (1M/3M/YTD/ALL periods + S&P 500 comparison)
- [x] Account CRUD and archive
- [x] CSV import (flexible month headers, two formats)
- [x] CSV export (roundtrip fidelity)
- [x] Onboarding wizard
- [x] Recurring entries (auto-apply on month init)
- [x] Monthly metrics calculation (net worth, save rate, MoM change, liquid NW)

**Investment Analytics (Included in v1)**
- [x] Holdings tracking (ticker + shares + live prices via Yahoo Finance)
- [x] Per-account allocation splits (Phase 1: manual; Phase 2: holdings-derived)
- [x] Asset allocation drift detection (actual vs. target)
- [x] Dividend data (Claude AI, 30-day TTL cache)
- [x] DRIP reinvestment projection
- [x] Market cap distribution

**Projections**
- [x] Growth projection (CAGR-based, multi-scenario, mortgage amortization)
- [x] Path to FI calculator (sensitivity tables, Coast FI)

**Polish and Documentation**
- [ ] Update README Features section (projections, allocation, holdings, recurring, onboarding, export)
- [ ] Fix `debug=True` in `run.py`
- [ ] Fix cascade recalculation for subsequent months
- [ ] Add Bootstrap modal confirmations for destructive actions
- [ ] Add security headers to Nginx config in README
- [ ] Add "Quick Start → Production" callout in README
- [ ] Add API cost warning to Settings page
- [ ] Document dividend data refresh behavior
- [ ] Test full CSV export → reimport round-trip on a fresh instance
- [ ] Add Import link to nav or ensure discoverability
- [ ] Verify FLASK_ENV=production behavior in all deployment paths

---

## Roadmap Prioritizer — Synthesized Delivery Plan

### Deduplication and Cross-Agent Analysis

Several issues were flagged by multiple agents:

| Issue | Agents | Combined Severity |
|---|---|---|
| `debug=True` hardcoded in `run.py` | B (Critical), D (Critical) | **Immediate** |
| `SECRET_KEY` fallback, no startup validation | B (Critical), D (Critical) | **Immediate** |
| No CSRF protection | B (High), D (High) | **Short-Term** |
| `db.create_all()` + Flask-Migrate coexistence | B (High) | **Short-Term** |
| Cascade recalculation gap (historical edit) | A (High) | **Immediate** |
| No confirmation dialogs for destructive actions | C (Critical) | **Immediate** |
| Nginx config missing security headers | D (High) | **Short-Term** |
| routes.py 3147-line monolith | B (High) | **Medium-Term** |
| README undocuments shipped features | E (High) | **Short-Term** |
| No negative balance constraint | A (High) | **Short-Term** |
| Visualizations/Import not in nav | C (High) | **Short-Term** |
| Page reload in snapshot wizard | C (High) | **Medium-Term** |
| SQLite WAL mode not enabled | D (Medium) | **Short-Term** |
| Dependency pinning + pytest missing | B (Medium) | **Short-Term** |
| No rate limiting in Nginx | D (High) | **Short-Term** |

### Prioritized Delivery Table

| # | Item | Tier | Effort | Agents |
|---|---|---|---|---|
| 1 | Remove `debug=True` from `run.py` — make conditional on `FLASK_DEBUG` env var | **Immediate** | 15 min | B, D |
| 2 | Add startup warning when using dev `SECRET_KEY`; fail in `FLASK_ENV=production` | **Immediate** | 30 min | B, D |
| 3 | Fix cascade recalculation: after `_recalculate_metrics(month_date)`, trigger recalc for `month_date + 1 month` | **Immediate** | 30 min | A |
| 4 | Replace `confirm()` with Bootstrap modal confirmations on month delete, snapshot delete, spending delete | **Immediate** | 2 hours | C |
| 5 | Update README Features section with all 8 shipped-but-undocumented features | **Immediate** | 1 hour | E |
| 6 | Add CSRF protection via Flask-WTF; add to `requirements.txt` | **Short-Term** | 3 hours | B, D |
| 7 | Remove `db.create_all()` from app factory; rely on Flask-Migrate only | **Short-Term** | 30 min | B |
| 8 | Add security headers to Nginx config in README | **Short-Term** | 30 min | D |
| 9 | Add rate limiting to Nginx config in README | **Short-Term** | 30 min | D |
| 10 | Enable SQLite WAL mode in factory or document 1-worker recommendation | **Short-Term** | 1 hour | D |
| 11 | Add `CHECK(balance >= 0)` constraint on `AccountSnapshot`; add migration | **Short-Term** | 1 hour | A |
| 12 | Add `pytest>=7.0` to `requirements.txt`; pin all dependencies consistently | **Short-Term** | 1 hour | B |
| 13 | Add Import page link to main nav | **Short-Term** | 15 min | C |
| 14 | Add account archive confirmation modal | **Short-Term** | 30 min | C |
| 15 | Document API key plaintext storage; add rotation guidance | **Short-Term** | 30 min | D |
| 16 | Add "Add Quick Start → Production" callout in README | **Short-Term** | 15 min | E |
| 17 | Split `routes.py` into 5-6 blueprints | **Medium-Term** | 4-6 hours | B |
| 18 | Replace page reload in snapshot wizard with AJAX update | **Medium-Term** | 3 hours | C |
| 19 | Add loading state and toast on Refresh Prices button | **Medium-Term** | 1 hour | C |
| 20 | Add future-dated snapshot validation | **Medium-Term** | 30 min | A |
| 21 | Widen `monthly_change_pct` to `Numeric(8,2)` + migration | **Medium-Term** | 30 min | A |
| 22 | Clean up orphaned `HoldingAllocation` rows on holding soft-delete | **Medium-Term** | 30 min | A |
| 23 | Add `CHECK(shares >= 0)` and `CHECK(last_price >= 0)` to `Holding` | **Medium-Term** | 30 min | A |
| 24 | Add API cost hint to Settings page AI section | **Medium-Term** | 15 min | E |
| 25 | Add dividend last-updated label and refresh prompt | **Medium-Term** | 1 hour | E |
| 26 | Revise product pitch to clarify core vs. advanced feature tiers | **Medium-Term** | 1 hour | E |
| 27 | Add Marshmallow/Pydantic schema validation for API endpoints | **Deferred** | 3-4 hours | B |
| 28 | Add DB indexes (`metric_date`, `account_id+snapshot_date`) | **Deferred** | 1 hour | B, A |
| 29 | Add `SpendingEntry.account_id` FK with backfill migration | **Deferred** | 2 hours | A |
| 30 | Add settings caching (dict at app startup, invalidate on POST) | **Deferred** | 1 hour | B |
| 31 | Persist snapshot collapse state in `sessionStorage` | **Deferred** | 30 min | C |
| 32 | Consolidate empty-state alerts on dashboard | **Deferred** | 30 min | C |

### v1 Release Milestone Definition

**v1 is shippable when items 1-16 are complete.** This is approximately 12-15 hours of focused work for a solo developer — achievable in two or three focused sessions.

Items 17-26 (Medium-Term) are quality-of-life improvements that would make v1 a polished release rather than an MVP. Items 27-32 (Deferred) are architectural improvements with no user-visible impact.

The codebase already ships more features than most personal finance tools. The gap between current state and v1 is documentation + five specific code fixes + CSRF protection.

---

## Summary Scorecard

| Dimension | Grade | Key Finding |
|---|---|---|
| Data Integrity | B+ | All Numeric fields ✓; cascade recalculation gap is the one critical bug |
| Code Architecture | B | Clean factory + test suite; 3147-line routes.py is the main debt item |
| UX | B | Strong fundamentals; native `confirm()` dialogs and page reload in wizard are the main friction |
| Security | C+ | `debug=True` is an RCE risk on LAN; CSRF unprotected; Docker path is safe but `python run.py` is not |
| Product Strategy | A- | Feature-complete beyond stated promise; documentation catch-up is the only gap |
| **Overall** | **B+** | A well-built, production-quality self-hosted finance app 10-15 hours of focused work from a clean v1 release |
