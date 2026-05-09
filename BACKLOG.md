# Ledger — Product Backlog

**Last updated:** 2026-05-05 (multi-agent product review)
**Product promise:** Self-hosted, privacy-first net worth tracking with optional investment analytics. Core flow ≤5 min/month.

---

## ✅ Completed

Features confirmed shipped by code and template review.

### Core Tracking
- **Monthly data entry** — snapshot wizard with per-account balance entry, inline edit, AJAX saves, keyboard navigation (Enter, Tab, Arrow keys), progress indicator, copy-from-previous (`month_detail.html`, `api_snapshot_create`)
- **Dashboard** — net worth hero card, save rate YTD, recent months table, empty states with CTAs (`index.html`)
- **Account CRUD** — create, edit, archive/restore, display ordering, color coding, institution/tax metadata (`accounts.html`, `account_form.html`)
- **Spending entry CRUD** — full create/read/update/delete for income and expenses by account name (`api_spending_*` routes)
- **Monthly metrics** — auto-calculated net worth, save rate, MoM change, liquid NW, non-RE NW; recalculated on every snapshot/spending write (`_recalculate_metrics`)
- **Recurring entries** — template income/expense entries auto-applied when a new month is initialized; full CRUD in Settings (`RecurringEntry` model, `settings.html`)

### Import / Export
- **CSV import** — two formats: generic category-based and Ledger-export roundtrip; flexible month headers; idempotent upsert; import audit log (`import_processor.py`, `import.html`)
- **CSV export** — full account + spending + metric export in Ledger format; date range filtering (`/export/csv`)
- **CSV import template** — downloadable blank template pre-seeded with active accounts (`/import/template`)

### Visualizations and Reporting
- **Net worth chart** — Plotly time series with 1M/3M/YTD/ALL periods; S&P 500 benchmark comparison (`/api/networth-history`, `/api/sp500-change`)
- **Allocation history** — stacked area chart of asset category balances over time (`/api/allocation-history`)
- **Asset distribution** — pie chart of current asset mix by category (`/api/asset-distribution`)
- **Save rate history** — monthly save rate with 12-month rolling average and target line (`/api/save-rate-history`)
- **Cash flow history** — monthly income, expenses, and save rate bars (`/api/cashflow-history`)
- **Account history** — per-account balance chart with CAGR and MoM change table (`/accounts/<id>/history`)

### Investment Analytics (Phase 1)
- **Asset allocation (Phase 1)** — per-account manual allocation splits (domestic/international/bonds/cash); stored in `AssetAllocation` table
- **Asset allocation (Phase 2)** — holdings-derived weighted allocation; Phase 2 automatically supersedes Phase 1 when holdings with prices exist (`_account_holding_splits`)
- **Allocation drift detection** — actual vs. target with on-target / review / rebalance status; rebalance amounts per asset class (`allocation.html`)
- **Holdings CRUD** — ticker + shares + live price; create/update/archive; allocation splits per holding (`Holding`, `HoldingAllocation` models, holdings API)
- **Live price fetching** — Yahoo Finance via `price_service.py`; staleness check (24-hour TTL); batch refresh API
- **AI ticker classification** — Claude API classifies tickers into asset classes, market cap tilt, sector weights; cached in `TickerClassification`; optional (behind feature flag)
- **Dividend data** — Claude AI fetches yield, frequency, payer type, tax treatment; 30-day cache in `DividendData`; manual override API
- **Passive income calculation** — annual/monthly income across all holdings; after-tax estimate by account tax status; missing-data list (`/api/passive-income`)
- **DRIP projection** — simulation with price appreciation + dividend growth + reinvestment; 3-scenario display (DRIP on/off/no action) (`dividend_calc.py`, `/api/passive-income/projection`)
- **Market cap distribution** — large/mid/small breakdown across all priced holdings

### Projections
- **Growth projections** — per-category CAGR from history (with fallback defaults); mortgage amortization; planned expense timeline markers; income contribution modeling; callouts (yr5/yr10/yr20/yr30, doubles date, mortgage payoff, retirement NW) (`projections.py`, `projections.html`)
- **Path to FI calculator** — FI number, months/years to FI, age at FI; up to 3 scenarios; sensitivity table (SWR × growth rate); Coast FI; historical NW overlay (`/api/projections/fi`)

### Setup and Configuration
- **Onboarding wizard** — 4-step account setup with batch creation, category auto-fill, opening balance entry; redirects to CSV import (`onboarding.html`)
- **Settings page** — recurring entries management, AI classification toggle/API key, snapshot timing (start/end of month), ticker classification cache management (`settings.html`)
- **Database migrations** — 9 versioned Flask-Migrate migrations
- **Docker deployment** — `docker-compose.yml` with `restart: unless-stopped`, `SECRET_KEY` from env, persistent volume mounts
- **Raspberry Pi deployment** — Gunicorn + Nginx + systemd setup (documented in README)
- **Demo seed script** — `scripts/seed_demo.py` for realistic demo data; `scripts/backup_db.py` for DB backup

---

## 🔴 Immediate — Fix This Week

Items 1-5 from roadmap prioritization. Estimated total: ~4 hours.

### PR-01 — Fix debug=True in run.py (**Security / Critical**)
**Rationale:** `app.run(debug=True, host='0.0.0.0', port=5001)` exposes the Werkzeug interactive debugger to all LAN devices when running via `python run.py`. This is RCE-level exposure. The Docker/Gunicorn path is safe but `python run.py` is not.
**Fix:** `app.run(debug=os.getenv('FLASK_DEBUG', '').lower() in ('1','true'), ...)`. Update README Quick Start to warn.
**Effort:** 15 min

### PR-02 — Add startup warning for dev SECRET_KEY (**Security / Critical**)
**Rationale:** `SECRET_KEY` silently falls back to `'dev-secret-key-change-this'` with no log warning. Any future sessions/CSRF feature added without setting the key is silently insecure.
**Fix:** `logger.warning("SECRET_KEY not set — using dev fallback")` at startup. In `FLASK_ENV=production`, raise `RuntimeError`.
**Effort:** 30 min

### PR-03 — Fix cascade recalculation for subsequent months (**Data Integrity / High**)
**Rationale:** When a historical month's snapshot is edited, only that month's `CalculatedMetric` is recalculated. The *next* month's `monthly_change_amount` and `monthly_change_pct` become stale (they compare against the old net worth). This is silent incorrect data.
**Fix:** In `_recalculate_metrics()`, after committing the current month, check if a metric exists for `month_date + 1 month` and recalculate it (one level, non-recursive).
**Effort:** 30 min

### PR-04 — Replace confirm() with Bootstrap modals for destructive actions (**UX / Critical**)
**Rationale:** Month delete, snapshot delete, and spending entry delete use the browser's native `confirm()` — no context, no styling, easy to dismiss by reflex. These are permanent operations.
**Fix:** Add a shared Bootstrap modal in `base.html` with a title, body, and red "Delete" CTA. Wire all destructive JS functions to show the modal before proceeding.
**Effort:** 2 hours

### PR-05 — Update README Features section (**Documentation / High**)
**Rationale:** 8 significant features are shipped but not listed in README: Projections, Asset Allocation Reports, Holdings tracking, Recurring entries, CSV Export, Onboarding wizard, Account history charts, S&P 500 comparison.
**Fix:** Expand the Features section with one bullet per item. ~8 bullets.
**Effort:** 1 hour

---

## 🟡 Short-Term — Next Sprint

Estimated total: ~8-10 hours.

### PR-06 — Add CSRF protection via Flask-WTF (**Security / High**)
**Rationale:** No CSRF protection on any state-mutating route. On a LAN with a predictable address (e.g., `ledger.local`), a malicious page visited by the user can trigger data modifications. Flask-WTF is not in `requirements.txt`.
**Fix:** Add `Flask-WTF>=1.2.1`. Initialize `CSRFProtect` in factory. Add `{{ csrf_token() }}` to HTML forms. JSON API calls set `X-CSRFToken` header.
**Effort:** 3 hours

### PR-07 — Remove db.create_all() from app factory (**Architecture / High**)
**Rationale:** `db.create_all()` in the factory auto-creates tables in dev, masking missing migrations. In production, schema changes arrive only via `flask db upgrade`. If a developer adds a model without a migration, dev works but prod breaks silently.
**Fix:** Remove lines 110-111 from `app/__init__.py`. `conftest.py` already calls `db.create_all()` for tests (correct). Add `flask db upgrade` to README Quick Start.
**Effort:** 30 min

### PR-08 — Add security headers and rate limiting to Nginx config in README (**Security / High**)
**Rationale:** The example Nginx config has no `X-Frame-Options`, `X-Content-Type-Options`, or `limit_req` directives. Clickjacking and request flooding are unaddressed.
**Fix:** Add 4 security headers and `limit_req_zone` / `limit_req` to the Nginx server block in README.
**Effort:** 30 min

### PR-09 — Enable SQLite WAL mode (**Security / Medium**)
**Rationale:** With 2 Gunicorn workers (as documented), concurrent writes to SQLite can produce `database is locked` errors. WAL mode increases write concurrency significantly.
**Fix:** Execute `PRAGMA journal_mode=WAL` after DB creation in factory (SQLAlchemy `event.listen` on engine connect).
**Effort:** 1 hour

### PR-10 — Add CHECK(balance >= 0) to AccountSnapshot (**Data Integrity / High**)
**Rationale:** No constraint prevents negative balances on asset accounts. Users can enter –$500,000 for a Cash account and corrupt net worth calculations. The positive-liability convention is also unenforced.
**Fix:** Add `db.CheckConstraint('balance >= 0', name='non_negative_balance')` to `AccountSnapshot.__table_args__` + migration.
**Effort:** 1 hour (including migration)

### PR-11 — Add pytest and pin all dependencies (**Architecture / Medium**)
**Rationale:** `pytest` is not in `requirements.txt` — a clone of the repo cannot run tests without a separate `pip install pytest`. Other dependencies use `>=` minimum versions, allowing undetected drift.
**Fix:** Add `pytest>=7.0`, `pytest-cov>=4.0`. Consider `requirements-dev.txt` split. Pin `Flask-SQLAlchemy`, `Flask-Migrate`, `SQLAlchemy` to their current tested versions.
**Effort:** 1 hour

### PR-12 — Add Import page to main nav (**UX / High**)
**Rationale:** The `/import` page is accessible only via empty-state CTAs or direct URL. Users who want to re-import updated historical data cannot discover the page through normal navigation.
**Fix:** Add "Import" nav item between Monthly Update and Projections in `base.html`.
**Effort:** 15 min

### PR-13 — Add account archive confirmation modal (**UX / High**)
**Rationale:** Archiving an account removes it from projections, quick actions, and new month suggestions. No confirmation is shown before this change.
**Fix:** Show a Bootstrap modal with context: "Archive [Account Name]? All data is preserved but it will be hidden from active views."
**Effort:** 30 min

### PR-14 — Document API key plaintext storage and add rotation guidance (**Security / Medium**)
**Rationale:** The Anthropic API key is stored plaintext in SQLite. Users should know to rotate it if the data directory is exposed.
**Fix:** Add a Settings page help text and a README note: "The API key is stored in the local database. Rotate it if your data directory is accessed by unauthorized parties."
**Effort:** 30 min

### PR-15 — Add Quick Start → Production callout in README (**Documentation / Medium**)
**Rationale:** A user following Quick Start will run `python run.py` (debug=True) on their Raspberry Pi indefinitely.
**Fix:** One-line callout after the Quick Start section: "For always-on deployment, see Docker or Raspberry Pi + Nginx sections below."
**Effort:** 15 min

---

## 🟢 Medium-Term — Next 1-2 Months

Estimated total: ~15-18 hours.

### PR-16 — Split routes.py into multiple blueprints (**Architecture / High**)
**Rationale:** `routes.py` is 3,147 lines. Finding a specific route requires scrolling 1,500+ lines. Testing and code review are slow.
**Suggested split:** `accounts_bp`, `monthly_bp`, `allocation_bp` (holdings + prices + classification), `projections_bp`, `settings_bp`, `api_data_bp` (chart data endpoints).
**Effort:** 4-6 hours

### PR-17 — Eliminate page reload in snapshot wizard (**UX / High**)
**Rationale:** Each snapshot save triggers a full page reload. Entering 10+ accounts across a month involves 10+ page loads, which is jarring and slow.
**Fix:** After successful `POST /api/snapshots`, inject the new row into the DOM, update the progress counter, and re-enable the account pill — no page reload.
**Effort:** 3 hours

### PR-18 — Add loading state and toast on Refresh Prices (**UX / Medium**)
**Rationale:** The "Refresh Prices" button shows no spinner and no success/failure feedback. Users don't know if the refresh worked.
**Fix:** Disable button and show spinner during fetch; display a toast with updated/failed counts on completion.
**Effort:** 1 hour

### PR-19 — Add future-dated snapshot validation (**Data Integrity / Medium**)
**Rationale:** Snapshots can be created for future months, which corrupts the "current position" used by projections.
**Fix:** In `api_snapshot_create()` and `api_snapshot_update()`, reject `month_date > date.today().replace(day=1)` with a clear error.
**Effort:** 30 min

### PR-20 — Widen monthly_change_pct field (**Data Integrity / Low**)
**Rationale:** `Numeric(6,2)` maxes at ±9999.99%. Small-balance accounts with large contributions can exceed this and truncate silently.
**Fix:** Change to `Numeric(8,2)` (max ±999,999.99%) + migration.
**Effort:** 30 min (including migration)

### PR-21 — Clean up HoldingAllocation rows on soft-delete (**Data Integrity / Low**)
**Rationale:** When a holding is archived (`is_active=False`), its `HoldingAllocation` rows remain in the DB. Over time, stale allocation data accumulates.
**Fix:** In `api_holding_archive()`, add `HoldingAllocation.query.filter_by(holding_id=holding.id).delete()` before soft-delete.
**Effort:** 30 min

### PR-22 — Add API cost hint to Settings page (**Product / Medium**)
**Rationale:** Users enabling Claude AI classification don't know it incurs per-ticker API costs.
**Fix:** Add help text: "Each new ticker incurs a small API cost (~$0.001–$0.01); lookups are cached per ticker."
**Effort:** 15 min

### PR-23 — Add dividend data last-updated label (**Product / Medium**)
**Rationale:** Dividend data has a 30-day TTL but users see no indication of when data was last fetched. Stale dividends silently affect passive income calculations.
**Fix:** Show "Last updated: X days ago" on holdings rows with dividend data. Add a per-ticker "Refresh" button.
**Effort:** 1 hour

### PR-24 — Revise product pitch for core vs. advanced feature tiers (**Product / Medium**)
**Rationale:** README describes a simple net worth tracker; the app ships a full investment analytics platform. New users may be confused or overwhelmed.
**Fix:** Update README intro: "Self-hosted net worth tracker with optional investment analytics (holdings, projections, dividends)." Make it clear the core is 5-account snapshot entry — advanced features are opt-in.
**Effort:** 1 hour

---

## ⚪ Deferred — Post-v1

Low urgency or architectural changes requiring careful planning.

### PR-25 — Add Marshmallow/Pydantic schema validation (**Architecture / Medium**)
**Rationale:** Manual `if`-chain validation throughout routes is error-prone and inconsistent. A schema library provides type coercion, reusable validation, and self-documenting APIs.
**Effort:** 3-4 hours

### PR-26 — Add DB indexes for chart query patterns (**Architecture / Low**)
**Rationale:** No explicit indexes on `CalculatedMetric.metric_date`, `AccountSnapshot(account_id, snapshot_date)`, `SpendingEntry.entry_date`. No impact at current data scales (<120 rows) but worth adding for long-running instances.
**Effort:** 1 hour + migration

### PR-27 — Add SpendingEntry.account_id FK (**Data Integrity / Medium**)
**Rationale:** `account_name` is free text — renaming an account orphans historical entries. An optional FK would enable proper account-level spending history.
**Note:** Breaking design change. Spending is intentionally tracked by card/source name, not account FK. Evaluate whether this conflicts with the product model.
**Effort:** 2 hours + migration

### PR-28 — Add settings caching (**Architecture / Low**)
**Rationale:** `_get_app_setting()` makes a DB query on every call. Low impact at current scale.
**Effort:** 1 hour

### PR-29 — Persist snapshot collapse state in sessionStorage (**UX / Low**)
**Rationale:** Users who prefer the expanded snapshot view must click to expand every time they navigate to a month.
**Effort:** 30 min

### PR-30 — Consolidate empty-state alerts on dashboard (**UX / Low**)
**Rationale:** Two separate alert blocks for "no accounts" and "no snapshots" can coexist awkwardly. One contextual alert is cleaner.
**Effort:** 30 min

### PR-31 — Add CHECK(shares >= 0), CHECK(last_price >= 0) to Holding (**Data Integrity / Low**)
**Rationale:** Negative values accepted without error, corrupting portfolio calculations.
**Effort:** 30 min + migration

---

## Prioritization Notes

**Solo developer calibration:** All Immediate items are under 2 hours individually. The full Immediate tier (PR-01 through PR-05) is ~4 hours. Short-Term (PR-06 through PR-15) is ~8-10 hours. Together, completing both tiers (~12-14 hours) defines v1 shippable.

**v1 Release Definition:** v1 is shippable when PR-01 through PR-15 are complete. The codebase already exceeds the feature set of most personal finance tools — what remains is documentation parity and security hardening. No features need to be built for v1.

**Holdings system scope:** The Holdings/DRIP/Dividend system (Phase 2) is production-quality and already shipped. It is NOT scope creep — it is an intentional product expansion that serves the same privacy-first, self-hosted user. The product pitch needs to be updated to reflect this (PR-24), not the features removed.

**CSRF and debug=True are the only issues that affect any LAN deployment.** All other findings are polish or architectural debt with no immediate risk.

---

## Decision Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-05-05 | Holdings/projections/DRIP included in v1 scope | These are production-quality, shipped features — removing them would be a regression. v1 = current feature set + documentation + security hardening. |
| 2026-05-05 | SpendingEntry.account_name remains free text | Spending tracked by card/source name (Chase, Amex, Employer) is a core product design decision. Adding an accounts FK would require all spending to reference a formal account — conflicts with the "quick entry" model. Deferred. |
| 2026-05-05 | No authentication in v1 | By design. Self-hosted LAN-only. README warns clearly. Future auth (Basic Auth in Nginx, Authelia, VPN) is documented as optional. |
| 2026-05-05 | SQLite retained (not PostgreSQL) for v1 | Zero-config is a core design constraint. SQLite + WAL mode is sufficient for single-user self-hosted use. PostgreSQL migration deferred to post-v1. |
| 2026-05-05 | DRIP projection is exploratory (read-only) | No mechanism to apply DRIP recommendations back to holdings. This is a visualization tool, not a trade recommendation system. Document as such. |
