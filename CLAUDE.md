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
