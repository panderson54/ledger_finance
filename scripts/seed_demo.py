"""
Seed the database with 24 months of realistic demo data (Jan 2024 – Dec 2025).

Net worth grows from ~$122k to ~$212k across five accounts with natural market
fluctuations. Income is ~$7,500/month, expenses ~$5,000-5,800/month (~27% save rate).

Usage:
    source venv/bin/activate
    python scripts/seed_demo.py

The script refuses to run if any accounts already exist, so it is safe to run
against a fresh database without risking data loss.
"""

import sys
import os
from datetime import date

# Allow running from the repo root without installing the package.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models import Account, AccountSnapshot, SpendingEntry
from app.routes import _recalculate_metrics


# ---------------------------------------------------------------------------
# Demo dataset
# ---------------------------------------------------------------------------

ACCOUNTS = [
    dict(name="Checking",           account_type="asset",     category="cash",       is_liquid=True,  tax_status="taxable",      institution="Local Bank",   display_order=1),
    dict(name="High-Yield Savings", account_type="asset",     category="cash",       is_liquid=True,  tax_status="taxable",      institution="Online Bank",  display_order=2),
    dict(name="401(k)",             account_type="asset",     category="retirement", is_liquid=False, tax_status="tax_deferred", institution="Fidelity",     display_order=3),
    dict(name="Roth IRA",           account_type="asset",     category="retirement", is_liquid=False, tax_status="tax_free",     institution="Fidelity",     display_order=4),
    dict(name="Brokerage",          account_type="asset",     category="investment", is_liquid=True,  tax_status="taxable",      institution="Fidelity",     display_order=5),
]

# (year, month): {account_name: balance}
# Net worth grows from ~$121k (Jan 2024) to ~$212k (Dec 2025) with realistic volatility.
SNAPSHOTS = {
    (2024,  1): {"Checking": 8500,  "High-Yield Savings": 18000, "401(k)": 52000, "Roth IRA": 19000, "Brokerage": 24000},
    (2024,  2): {"Checking": 8200,  "High-Yield Savings": 18400, "401(k)": 53500, "Roth IRA": 19600, "Brokerage": 25200},
    (2024,  3): {"Checking": 9100,  "High-Yield Savings": 18900, "401(k)": 55200, "Roth IRA": 20300, "Brokerage": 26800},
    (2024,  4): {"Checking": 7800,  "High-Yield Savings": 19400, "401(k)": 54800, "Roth IRA": 20000, "Brokerage": 26400},  # mild dip
    (2024,  5): {"Checking": 8600,  "High-Yield Savings": 20100, "401(k)": 57000, "Roth IRA": 21000, "Brokerage": 28100},
    (2024,  6): {"Checking": 9200,  "High-Yield Savings": 20800, "401(k)": 58500, "Roth IRA": 21700, "Brokerage": 29500},
    (2024,  7): {"Checking": 8400,  "High-Yield Savings": 21300, "401(k)": 60200, "Roth IRA": 22400, "Brokerage": 31000},
    (2024,  8): {"Checking": 8900,  "High-Yield Savings": 21900, "401(k)": 59800, "Roth IRA": 22100, "Brokerage": 30400},  # pullback
    (2024,  9): {"Checking": 9500,  "High-Yield Savings": 22500, "401(k)": 61500, "Roth IRA": 22900, "Brokerage": 32000},
    (2024, 10): {"Checking": 8300,  "High-Yield Savings": 23100, "401(k)": 63000, "Roth IRA": 23500, "Brokerage": 33500},
    (2024, 11): {"Checking": 9100,  "High-Yield Savings": 23800, "401(k)": 65500, "Roth IRA": 24400, "Brokerage": 35200},
    (2024, 12): {"Checking": 10200, "High-Yield Savings": 24500, "401(k)": 67000, "Roth IRA": 25000, "Brokerage": 36800},
    (2025,  1): {"Checking": 8600,  "High-Yield Savings": 25100, "401(k)": 68500, "Roth IRA": 25600, "Brokerage": 38200},
    (2025,  2): {"Checking": 9300,  "High-Yield Savings": 25700, "401(k)": 70200, "Roth IRA": 26300, "Brokerage": 39800},
    (2025,  3): {"Checking": 8800,  "High-Yield Savings": 26300, "401(k)": 72000, "Roth IRA": 27100, "Brokerage": 41500},
    (2025,  4): {"Checking": 9400,  "High-Yield Savings": 26900, "401(k)": 70500, "Roth IRA": 26500, "Brokerage": 40200},  # correction
    (2025,  5): {"Checking": 10100, "High-Yield Savings": 27500, "401(k)": 73000, "Roth IRA": 27500, "Brokerage": 42800},
    (2025,  6): {"Checking": 9200,  "High-Yield Savings": 28100, "401(k)": 75500, "Roth IRA": 28400, "Brokerage": 44500},
    (2025,  7): {"Checking": 9800,  "High-Yield Savings": 28600, "401(k)": 77200, "Roth IRA": 29100, "Brokerage": 46000},
    (2025,  8): {"Checking": 10300, "High-Yield Savings": 29200, "401(k)": 79000, "Roth IRA": 29900, "Brokerage": 47500},
    (2025,  9): {"Checking": 9100,  "High-Yield Savings": 29700, "401(k)": 80500, "Roth IRA": 30600, "Brokerage": 48800},
    (2025, 10): {"Checking": 9700,  "High-Yield Savings": 30300, "401(k)": 82000, "Roth IRA": 31300, "Brokerage": 50200},
    (2025, 11): {"Checking": 10500, "High-Yield Savings": 30900, "401(k)": 83500, "Roth IRA": 32000, "Brokerage": 51800},
    (2025, 12): {"Checking": 9800,  "High-Yield Savings": 31500, "401(k)": 85000, "Roth IRA": 32800, "Brokerage": 53200},
}

# (year, month): [(account_name, amount, entry_type)]
SPENDING = {
    (2024,  1): [("Paycheck", 7500, "income"), ("Chase Sapphire", 4950, "expense"), ("Utilities", 280, "expense")],
    (2024,  2): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5100, "expense"), ("Utilities", 310, "expense")],
    (2024,  3): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5250, "expense"), ("Utilities", 260, "expense")],
    (2024,  4): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5400, "expense"), ("Utilities", 275, "expense")],
    (2024,  5): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5050, "expense"), ("Utilities", 240, "expense")],
    (2024,  6): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5600, "expense"), ("Utilities", 220, "expense")],  # summer travel
    (2024,  7): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5700, "expense"), ("Utilities", 310, "expense")],  # AC bill
    (2024,  8): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5150, "expense"), ("Utilities", 295, "expense")],
    (2024,  9): [("Paycheck", 7500, "income"), ("Chase Sapphire", 4900, "expense"), ("Utilities", 265, "expense")],
    (2024, 10): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5200, "expense"), ("Utilities", 280, "expense")],
    (2024, 11): [("Paycheck", 7500, "income"), ("Chase Sapphire", 5800, "expense"), ("Utilities", 290, "expense")],  # holidays
    (2024, 12): [("Paycheck", 7500, "income"), ("Annual Bonus",   4000, "income"),  ("Chase Sapphire", 5950, "expense"), ("Utilities", 320, "expense")],
    (2025,  1): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5000, "expense"), ("Utilities", 305, "expense")],  # raise
    (2025,  2): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5100, "expense"), ("Utilities", 315, "expense")],
    (2025,  3): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5200, "expense"), ("Utilities", 270, "expense")],
    (2025,  4): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5350, "expense"), ("Utilities", 255, "expense")],
    (2025,  5): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5050, "expense"), ("Utilities", 235, "expense")],
    (2025,  6): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5700, "expense"), ("Utilities", 215, "expense")],
    (2025,  7): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5550, "expense"), ("Utilities", 325, "expense")],
    (2025,  8): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5100, "expense"), ("Utilities", 310, "expense")],
    (2025,  9): [("Paycheck", 7700, "income"), ("Chase Sapphire", 4950, "expense"), ("Utilities", 270, "expense")],
    (2025, 10): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5250, "expense"), ("Utilities", 285, "expense")],
    (2025, 11): [("Paycheck", 7700, "income"), ("Chase Sapphire", 5750, "expense"), ("Utilities", 295, "expense")],
    (2025, 12): [("Paycheck", 7700, "income"), ("Annual Bonus",   4500, "income"),  ("Chase Sapphire", 5900, "expense"), ("Utilities", 330, "expense")],
}


# ---------------------------------------------------------------------------
# Seed logic
# ---------------------------------------------------------------------------

def seed():
    app = create_app()
    with app.app_context():
        existing = Account.query.count()
        if existing > 0:
            print(f"Database already contains {existing} account(s). Aborting to avoid overwriting data.")
            print("To start fresh, delete data/finance.db and run: flask db upgrade")
            sys.exit(1)

        print("Creating accounts...")
        account_map = {}
        for a in ACCOUNTS:
            acct = Account(
                name=a["name"],
                account_type=a["account_type"],
                category=a["category"],
                is_liquid=a["is_liquid"],
                include_in_networth=True,
                is_active=True,
                tax_status=a.get("tax_status"),
                institution=a.get("institution"),
                display_order=a["display_order"],
            )
            db.session.add(acct)
        db.session.commit()

        for acct in Account.query.all():
            account_map[acct.name] = acct

        print("Creating snapshots and spending entries...")
        months = sorted(SNAPSHOTS.keys())
        for (year, month) in months:
            month_date = date(year, month, 1)
            balances = SNAPSHOTS[(year, month)]

            for acct_name, balance in balances.items():
                snapshot = AccountSnapshot(
                    account_id=account_map[acct_name].id,
                    snapshot_date=month_date,
                    balance=balance,
                )
                db.session.add(snapshot)

            for (acct_name, amount, entry_type) in SPENDING.get((year, month), []):
                entry = SpendingEntry(
                    entry_date=month_date,
                    account_name=acct_name,
                    amount=amount,
                    entry_type=entry_type,
                )
                db.session.add(entry)

            db.session.commit()
            _recalculate_metrics(month_date)
            print(f"  {month_date.strftime('%b %Y')}: NW ${sum(balances.values()):,.0f}")

        print("\nDone. Start the app with: python run.py")
        print("Then open http://localhost:5001")


if __name__ == "__main__":
    seed()
