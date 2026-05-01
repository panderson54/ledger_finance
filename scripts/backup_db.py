"""
backup_db.py — Copy data/finance.db to data/archive/ with a timestamp.
Run this before applying any DB migration.
"""
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "finance.db"
ARCHIVE_DIR = ROOT / "data" / "archive"


def backup():
    if not DB_PATH.exists():
        print(f"No database found at {DB_PATH}, skipping backup.")
        return

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = ARCHIVE_DIR / f"finance_{timestamp}.db"
    shutil.copy2(DB_PATH, dest)
    print(f"DB backed up to {dest}")


if __name__ == "__main__":
    backup()
