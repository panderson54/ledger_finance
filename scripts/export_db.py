"""
export_db.py — Write the full-database JSON export to a file from the command line.

Produces the same file as Settings → Export Database, without going through the
web UI (and so without needing Google sign-in or HTTPS). Import the file on the
new instance via Settings → Import & Replace.

Usage (on the machine hosting the database, e.g. the Raspberry Pi):
    source venv/bin/activate
    python scripts/export_db.py                 # writes ./ledger_backup_<timestamp>.json
    python scripts/export_db.py -o backup.json
"""
import argparse
import json
import os
import sys

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.db_transfer_service import export_database, export_filename  # noqa: E402


def write_export(path: str) -> dict:
    """Must run inside an app context."""
    payload = export_database()
    # Owner-only: the export includes the Anthropic API key if one is set.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(payload, f)
    return {name: len(rows) for name, rows in payload['tables'].items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Export the entire Ledger database to a JSON file.')
    parser.add_argument('-o', '--output', help='output file (default: ./ledger_backup_<timestamp>.json)')
    args = parser.parse_args(argv)
    path = args.output or export_filename()

    with create_app().app_context():
        counts = write_export(path)

    for name, count in counts.items():
        print(f'  {name:<24} {count:>6}')
    print(f'Exported {sum(counts.values())} rows to {os.path.abspath(path)}')


if __name__ == '__main__':
    main()
