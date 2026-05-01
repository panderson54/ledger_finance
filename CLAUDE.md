# Ledger — Claude Instructions

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
