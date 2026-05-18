# Ledger — Claude Instructions

## Architecture & Code Quality Principles

These rules apply to all new code and modifications in this codebase.

### Module Structure (after SOLID review, May 2026)

```
app/
  ai_utils.py           # Shared Claude API utilities (model constants, response parser)
  account_categories.py # Shared account category sets and allocation class list
  metrics_service.py    # Metrics recalculation and income contribution logic
  routes/               # Request handlers split by domain (sub-package)
    __init__.py         # Blueprint + sub-module imports
    helpers.py          # Shared route utilities
    dashboard.py, accounts.py, snapshots.py, spending.py,
    holdings.py, allocation.py, projections.py, income.py,
    import_export.py, settings.py, visualizations.py
  classification_service.py  # Ticker classification via Claude
  dividend_service.py         # Dividend data via Claude
  price_service.py            # yfinance price fetching
  import_processor.py         # CSV import engine
  dividend_calc.py            # Pure income/DRIP math
  projections.py              # Pure FI/growth projection math
  models.py                   # SQLAlchemy ORM models
```

### SOLID & Modularity Rules

**Single Responsibility**: Each module has one job.
- Route handlers only handle HTTP: parse request → call service → return response
- Services contain business logic and external API calls; they do not handle HTTP
- Models are thin: no business logic beyond simple property accessors
- Pure math modules (`dividend_calc.py`, `projections.py`) have zero DB/Flask imports

**DRY**: Before adding code, check for an existing utility:
- Shared Claude response parsing → `app/ai_utils.parse_claude_json_response()`
- Shared model constants → `app/model constants (AI)` → `CLASSIFICATION_MODEL`, `DIVIDEND_MODEL` in `app/ai_utils`
- Account category sets → `app/account_categories.py` (`INVESTMENT_CATS`, `CASH_CATS`, `LIABILITY_CATS`, `ALLOCATION_CLASSES`, `ALL_CATEGORIES`)
- Metrics recalculation → `app/metrics_service.recalculate_metrics()`
- Income contribution logic → `app/metrics_service.compute_income_contributions()`

**Dependency Direction**: Routes → Services → Models. Never import routes from services or models.

**Error Responses**: Always use `_bad_request(msg)` and `_not_found(resource)` helpers from `app/routes/helpers.py`. Error key is always `{'error': '...'}` (singular).

**New Services**: External API integrations belong in dedicated service modules (like `classification_service.py`). They must:
- Import API clients lazily or use `app.ai_utils.make_anthropic_client()`
- Define model name constants, never hard-code model strings inline
- Not swallow exceptions from callers — let them propagate or re-raise with context

**New Route Domains**: Add routes to the appropriate sub-module in `app/routes/`. If none fits, create a new sub-module and register it in `app/routes/__init__.py`.

**Shared Test Utilities**: Factories and mock helpers live in `tests/conftest.py`:
- `make_anthropic_mock_client(response_json)` — mock Claude API client
- `seed_ai_settings(db_session)` — seed AppSetting rows to enable AI features
- `make_investment_account(db_session)` — brokerage account factory
- `make_holding(db_session, account_id)` — holding factory

### What NOT to Do
- Do not add business logic to route handlers — extract to a service
- Do not duplicate category sets — import from `app/account_categories.py`
- Do not hard-code Claude model names — use constants from `app/ai_utils.py`
- Do not add new module-level mutable globals — use class instances or app config
- Do not silently swallow exceptions in service error paths — callers should decide

---

## Pre-Commit Clean Code Pass

Before creating any commit, run a clean code pass over all changed files. Check each of the following:

### SOLID & DRY
- Does any new function duplicate logic that already exists elsewhere? Search for helpers in `app/routes/helpers.py`, `app/ai_utils.py`, `app/account_categories.py`, and adjacent service modules before writing new code.
- Does any new service function share boilerplate with an existing one? Extract a private helper (e.g., `_call_claude(messages, api_key)` in a service) rather than copying the structure.
- Does any route handler contain business logic that belongs in a service?

### Readability & Comments
- Remove any docstring or comment that describes WHAT the code does — well-named identifiers do that already.
- Keep only comments that explain a non-obvious WHY: a hidden constraint, a subtle invariant, a workaround, or a surprising omission (e.g., "does not persist — frontend confirms before saving").
- Prefer one-liner docstrings over multi-line blocks. No multi-paragraph docstrings.

### Code Duplication in JS/Frontend
- Any two JS functions that differ only by a field name or string constant should be unified into a single function with a parameter.
- State management functions (e.g., `setLoading`, `resetModal`) should only control the elements they own — do not reach into other components' state from inside them.

### Logging
- Every service call that hits an external API (Claude, yfinance) should log at `INFO` level with structured fields: what was called, what account/resource it applies to, and what was returned (count, result summary).
- Every caught exception that returns a 4xx/5xx should log at `WARNING` level with `%s` formatting (not f-strings), so the message is only formatted if the log level is active.

### Test Coverage
- Every new public function in a service module needs at least: success case, empty/no-op case, invalid input case, and API/external failure case.
- Every new route needs at least: resource not found (404), missing auth/config (503), invalid input (400), and success (200).
- Do not add tests that exercise the same code path twice under different names.

### Run the test suite
```bash
# Windows
.\venv\Scripts\python -m pytest tests/ -q

# Mac/Linux
python -m pytest tests/ -q
```
All tests must pass before committing.

---

## Database Migrations

**Before running any `flask db upgrade` or `flask db migrate`, always back up the database first:**

```bash
# Activate venv first (source venv/bin/activate  or  venv\Scripts\activate on Windows)
python scripts/backup_db.py
```

This copies `data/finance.db` to `data/archive/finance_<timestamp>.db`. The archive directory is gitignored along with the rest of `data/`.

Then proceed with the migration:

```bash
flask db upgrade
```
