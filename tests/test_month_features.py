"""
Tests for month navigation and copy-from-previous features:
  POST   /api/months/<month_str>/copy-from-previous
  GET    /monthly-update/<month_str>  (navigation context)
  DELETE /api/months/<month_str>
"""
import json
from datetime import date

import pytest

from app import db as _db
from app.models import Account, AccountSnapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_account(db, name="Savings", account_type="asset"):
    acct = Account(
        name=name,
        account_type=account_type,
        category="savings",
        is_liquid=True,
        include_in_networth=True,
        is_active=True,
    )
    _db.session.add(acct)
    _db.session.commit()
    return acct


def make_snapshot(db, account_id, year, month, balance):
    snap = AccountSnapshot(
        account_id=account_id,
        snapshot_date=date(year, month, 1),
        balance=balance,
    )
    _db.session.add(snap)
    _db.session.commit()
    return snap


# ---------------------------------------------------------------------------
# POST /api/months/<month_str>/copy-from-previous
# ---------------------------------------------------------------------------

class TestCopyFromPrevious:
    def test_copies_snapshots_to_empty_month(self, client, db):
        """Copies all prior-month snapshots into target month."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 5000.00)

        r = client.post("/api/months/2024-02/copy-from-previous")
        assert r.status_code == 200
        body = r.get_json()
        assert body["copied"] == 1
        assert body["skipped"] == 0
        assert body["source_month"] == "2024-01"
        assert body["source_label"] == "January 2024"

        # Snapshot should now exist for Feb
        snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 2, 1)
        ).first()
        assert snap is not None
        assert snap.balance == 5000.00

    def test_does_not_overwrite_existing_snapshot(self, client, db):
        """Existing snapshots in target month are skipped."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 5000.00)
        make_snapshot(db, acct.id, 2024, 2, 9999.00)  # already has a value

        r = client.post("/api/months/2024-02/copy-from-previous")
        assert r.status_code == 200
        body = r.get_json()
        assert body["copied"] == 0
        assert body["skipped"] == 1

        # Original value unchanged
        snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 2, 1)
        ).first()
        assert snap.balance == 9999.00

    def test_copies_only_empty_slots_when_partially_filled(self, client, db):
        """Only fills missing accounts, preserves existing ones."""
        acct1 = make_account(db, "Checking")
        acct2 = make_account(db, "Investment")
        make_snapshot(db, acct1.id, 2024, 1, 1000.00)
        make_snapshot(db, acct2.id, 2024, 1, 2000.00)
        # Pre-fill only acct1 in target month
        make_snapshot(db, acct1.id, 2024, 2, 1111.00)

        r = client.post("/api/months/2024-02/copy-from-previous")
        assert r.status_code == 200
        body = r.get_json()
        assert body["copied"] == 1
        assert body["skipped"] == 1

        # acct2 gets copied
        snap2 = AccountSnapshot.query.filter_by(
            account_id=acct2.id, snapshot_date=date(2024, 2, 1)
        ).first()
        assert snap2.balance == 2000.00

        # acct1 unchanged
        snap1 = AccountSnapshot.query.filter_by(
            account_id=acct1.id, snapshot_date=date(2024, 2, 1)
        ).first()
        assert snap1.balance == 1111.00

    def test_no_prior_month_returns_404(self, client, db):
        """Returns 404 when no prior snapshots exist."""
        r = client.post("/api/months/2024-01/copy-from-previous")
        assert r.status_code == 404
        body = r.get_json()
        assert "error" in body

    def test_invalid_month_format_returns_400(self, client, db):
        """Returns 400 for bad month string."""
        r = client.post("/api/months/not-a-month/copy-from-previous")
        assert r.status_code == 400

    def test_uses_most_recent_prior_month(self, client, db):
        """Copies from the most recent prior month, not just any prior month."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2023, 12, 1000.00)
        make_snapshot(db, acct.id, 2024, 1, 2000.00)  # most recent prior

        r = client.post("/api/months/2024-02/copy-from-previous")
        assert r.status_code == 200
        body = r.get_json()
        assert body["source_month"] == "2024-01"

        snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 2, 1)
        ).first()
        assert snap.balance == 2000.00  # from Jan, not Dec


# ---------------------------------------------------------------------------
# GET /monthly-update/<month_str> — navigation context
# ---------------------------------------------------------------------------

class TestMonthDetailNavigation:
    def test_month_detail_page_loads(self, client, db):
        """Month detail page returns 200."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 3, 5000.00)

        r = client.get("/monthly-update/2024-03")
        assert r.status_code == 200

    def test_month_detail_future_month_returns_200(self, client, db):
        """A valid but empty month (no data) still renders a page."""
        r = client.get("/monthly-update/2099-12")
        assert r.status_code == 200

    def test_month_detail_bad_format_returns_400(self, client, db):
        """Bad month format returns 400."""
        r = client.get("/monthly-update/bad-month")
        assert r.status_code == 400

    def test_month_detail_shows_prev_next_links(self, client, db):
        """Page includes prev/next month context when adjacent months exist."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 1000.00)
        make_snapshot(db, acct.id, 2024, 2, 2000.00)
        make_snapshot(db, acct.id, 2024, 3, 3000.00)

        r = client.get("/monthly-update/2024-02")
        assert r.status_code == 200
        html = r.data.decode()
        assert "/monthly-update/2024-01" in html  # prev
        assert "/monthly-update/2024-03" in html  # next

    def test_month_detail_no_prev_for_oldest(self, client, db):
        """Oldest month has no prev link."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 1000.00)
        make_snapshot(db, acct.id, 2024, 2, 2000.00)

        r = client.get("/monthly-update/2024-01")
        assert r.status_code == 200
        html = r.data.decode()
        assert "prevMonthBtn" in html
        assert "disabled" in html  # prev button is disabled

    def test_month_detail_shows_copy_button_when_empty(self, client, db):
        """Empty month with prior data shows copy button."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 5000.00)
        # Init Feb without any snapshots
        client.post("/api/months/init",
                    data=json.dumps({"month": "2024-02"}),
                    content_type="application/json")

        r = client.get("/monthly-update/2024-02")
        assert r.status_code == 200
        html = r.data.decode()
        assert "copyFromPrevBtn" in html
        assert "January 2024" in html

    def test_month_detail_no_copy_button_when_has_snapshots(self, client, db):
        """Month with existing snapshots does not show copy button."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 1000.00)
        make_snapshot(db, acct.id, 2024, 2, 2000.00)

        r = client.get("/monthly-update/2024-02")
        assert r.status_code == 200
        html = r.data.decode()
        assert "copyFromPrevBtn" not in html

    def test_month_detail_no_copy_button_when_no_prior(self, client, db):
        """First-ever month has no copy button."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 1, 5000.00)

        r = client.get("/monthly-update/2024-01")
        assert r.status_code == 200
        html = r.data.decode()
        assert "copyFromPrevBtn" not in html


# ---------------------------------------------------------------------------
# DELETE /api/months/<month_str>
# ---------------------------------------------------------------------------

class TestMonthDelete:
    def test_delete_month_removes_snapshots(self, client, db):
        """Deleting a month removes all its snapshots."""
        acct = make_account(db)
        make_snapshot(db, acct.id, 2024, 5, 7500.00)

        r = client.delete("/api/months/2024-05")
        assert r.status_code == 200
        assert r.get_json()["success"] is True

        remaining = AccountSnapshot.query.filter_by(
            snapshot_date=date(2024, 5, 1)
        ).count()
        assert remaining == 0

    def test_delete_month_bad_format_returns_400(self, client, db):
        r = client.delete("/api/months/badmonth")
        assert r.status_code == 400
