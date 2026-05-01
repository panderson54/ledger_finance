# Contributing to Ledger

Thanks for your interest in contributing. Ledger is a small personal finance dashboard — contributions that keep it simple, focused, and reliable are most welcome.

---

## Development Setup

**Prerequisites:** Python 3.10+, Git

```bash
git clone https://github.com/your-username/ledger.git
cd ledger
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env            # then open .env and set SECRET_KEY
python run.py
```

The app starts at **http://localhost:5001** and creates `data/finance.db` automatically.

---

## Running Tests

Tests use an in-memory SQLite database and do not require a `.env` file:

```bash
pytest
```

Run tests before submitting a PR. All tests must pass.

---

## Database Migrations

If you change any SQLAlchemy models, **back up the database before running migrations**:

```bash
python scripts/backup_db.py
flask db migrate -m "describe the change"
flask db upgrade
```

`backup_db.py` copies `data/finance.db` to `data/archive/finance_<timestamp>.db`. The archive directory is gitignored. Never run `flask db upgrade` without backing up first — SQLite migrations are not easily reversible.

---

## Submitting Changes

1. Fork the repo and create a feature branch off `main`
2. Keep PRs focused — one logical change per PR
3. Run `pytest` and confirm all tests pass
4. Open a pull request against `main` with a clear description of what changed and why

---

## Code Style

No linter is enforced. Match the style of the surrounding code. A few conventions used throughout:

- No comments unless the *why* is non-obvious — well-named identifiers should speak for themselves
- No defensive error handling for internal code paths that can't fail
- Currency values use `Decimal` (via `Numeric(12,2)` columns) — never `float`
- All dates stored as the first day of the month (`date(year, month, 1)`)
