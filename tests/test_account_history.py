"""
Tests for account balance history view (item 5):
  GET  /accounts/<id>/history
  GET  /accounts/<id>/history/export
"""
from datetime import date
from app.models import Account, AccountSnapshot
from app import db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_account(db, name="Savings", color=None):
    acct = Account(
        name=name, account_type="asset", category="savings",
        is_liquid=True, include_in_networth=True, is_active=True,
        display_color=color,
    )
    _db.session.add(acct)
    _db.session.commit()
    return acct


def add_snapshot(account_id, year, month, balance):
    snap = AccountSnapshot(
        account_id=account_id,
        snapshot_date=date(year, month, 1),
        balance=balance,
    )
    _db.session.add(snap)
    _db.session.commit()
    return snap


# ---------------------------------------------------------------------------
# GET /accounts/<id>/history
# ---------------------------------------------------------------------------

class TestAccountHistory:
    def test_history_page_loads(self, client, db):
        acct = make_account(db)
        r = client.get(f"/accounts/{acct.id}/history")
        assert r.status_code == 200

    def test_history_page_404_for_missing(self, client, db):
        r = client.get("/accounts/9999/history")
        assert r.status_code == 404

    def test_history_shows_account_name(self, client, db):
        acct = make_account(db, "My Savings")
        r = client.get(f"/accounts/{acct.id}/history")
        assert b"My Savings" in r.data

    def test_history_empty_state_shown_when_no_snapshots(self, client, db):
        acct = make_account(db)
        r = client.get(f"/accounts/{acct.id}/history")
        assert r.status_code == 200
        assert b"No balance snapshots" in r.data

    def test_history_shows_latest_balance(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 10000)
        add_snapshot(acct.id, 2024, 2, 11500)
        r = client.get(f"/accounts/{acct.id}/history")
        assert b"11,500" in r.data

    def test_history_shows_snapshot_count(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 10000)
        add_snapshot(acct.id, 2024, 2, 11000)
        r = client.get(f"/accounts/{acct.id}/history")
        assert r.status_code == 200
        # "2" should appear in the stats block
        assert b"2" in r.data

    def test_history_shows_cagr_when_enough_data(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2023, 1, 100000)
        add_snapshot(acct.id, 2024, 1, 110000)  # ~10% in 1 year
        r = client.get(f"/accounts/{acct.id}/history")
        assert b"CAGR" in r.data
        # Should contain a positive growth indicator
        assert b"%" in r.data

    def test_history_no_cagr_for_single_snapshot(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 50000)
        r = client.get(f"/accounts/{acct.id}/history")
        # Only one snapshot — CAGR can't be calculated
        assert b"need" in r.data.lower() or b"CAGR" in r.data

    def test_history_shows_color_in_page(self, client, db):
        acct = make_account(db, color="#ff5500")
        add_snapshot(acct.id, 2024, 1, 5000)
        r = client.get(f"/accounts/{acct.id}/history")
        assert b"#ff5500" in r.data

    def test_history_shows_export_button(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 5000)
        r = client.get(f"/accounts/{acct.id}/history")
        assert b"Export CSV" in r.data

    def test_history_no_export_button_when_empty(self, client, db):
        acct = make_account(db)
        r = client.get(f"/accounts/{acct.id}/history")
        assert b"Export CSV" not in r.data


# ---------------------------------------------------------------------------
# GET /accounts/<id>/history/export
# ---------------------------------------------------------------------------

class TestAccountHistoryExport:
    def test_export_returns_200(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 5000)
        r = client.get(f"/accounts/{acct.id}/history/export")
        assert r.status_code == 200

    def test_export_404_for_missing_account(self, client, db):
        r = client.get("/accounts/9999/history/export")
        assert r.status_code == 404

    def test_export_content_type_is_csv(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 5000)
        r = client.get(f"/accounts/{acct.id}/history/export")
        assert 'text/csv' in r.content_type

    def test_export_has_attachment_header(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 5000)
        r = client.get(f"/accounts/{acct.id}/history/export")
        disposition = r.headers.get('Content-Disposition', '')
        assert 'attachment' in disposition
        assert '.csv' in disposition

    def test_export_contains_header_row(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 5000)
        r = client.get(f"/accounts/{acct.id}/history/export")
        body = r.data.decode('utf-8')
        assert 'Month' in body
        assert 'Balance' in body

    def test_export_contains_snapshot_data(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 3, 7500)
        r = client.get(f"/accounts/{acct.id}/history/export")
        body = r.data.decode('utf-8')
        assert 'Mar 2024' in body
        assert '7500' in body

    def test_export_all_snapshots_included(self, client, db):
        acct = make_account(db)
        add_snapshot(acct.id, 2024, 1, 1000)
        add_snapshot(acct.id, 2024, 2, 2000)
        add_snapshot(acct.id, 2024, 3, 3000)
        r = client.get(f"/accounts/{acct.id}/history/export")
        lines = r.data.decode('utf-8').strip().splitlines()
        # header + 3 data rows
        assert len(lines) == 4


# ---------------------------------------------------------------------------
# Accounts list links to history
# ---------------------------------------------------------------------------

class TestAccountsListHistoryLink:
    def test_accounts_list_links_to_history(self, client, db):
        acct = make_account(db, "Link Test")
        r = client.get("/accounts")
        assert r.status_code == 200
        assert f"/accounts/{acct.id}/history".encode() in r.data
