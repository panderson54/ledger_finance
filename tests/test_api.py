"""
Tests for JSON API endpoints:
  POST   /api/snapshots
  PUT    /api/snapshots/<id>
  DELETE /api/snapshots/<id>
  POST   /api/spending
  PUT    /api/spending/<id>
  DELETE /api/spending/<id>
  GET    /api/networth-history
  GET    /api/months
  POST   /api/months/init
"""
import json
from datetime import date

from app.models import AccountSnapshot, CalculatedMetric
from app import db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def post_json(client, url, data):
    return client.post(url, data=json.dumps(data), content_type="application/json")


def put_json(client, url, data):
    return client.put(url, data=json.dumps(data), content_type="application/json")


# ---------------------------------------------------------------------------
# POST /api/snapshots
# ---------------------------------------------------------------------------

def test_snapshot_create_success(client, sample_account):
    r = post_json(client, "/api/snapshots", {
        "account_id": sample_account.id,
        "month": "2024-03",
        "balance": 7500.00,
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["account_id"] == sample_account.id
    assert body["balance"] == 7500.00
    assert body["month"] == "2024-03"
    assert "id" in body


def test_snapshot_create_upserts_existing(client, sample_snapshot):
    r = post_json(client, "/api/snapshots", {
        "account_id": sample_snapshot.account_id,
        "month": "2024-01",
        "balance": 9999.00,
    })
    assert r.status_code == 200
    assert r.get_json()["balance"] == 9999.00


def test_snapshot_create_missing_fields_returns_400(client):
    r = post_json(client, "/api/snapshots", {"account_id": 1})
    assert r.status_code == 400


def test_snapshot_create_bad_month_format_returns_400(client, sample_account):
    r = post_json(client, "/api/snapshots", {
        "account_id": sample_account.id,
        "month": "January 2024",
        "balance": 100,
    })
    assert r.status_code == 400


def test_snapshot_create_unknown_account_returns_404(client):
    r = post_json(client, "/api/snapshots", {
        "account_id": 9999,
        "month": "2024-03",
        "balance": 100,
    })
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# PUT /api/snapshots/<id>
# ---------------------------------------------------------------------------

def test_snapshot_update_success(client, sample_snapshot):
    r = put_json(client, f"/api/snapshots/{sample_snapshot.id}", {"balance": 8888.00})
    assert r.status_code == 200
    assert r.get_json()["balance"] == 8888.00


def test_snapshot_update_missing_balance_returns_400(client, sample_snapshot):
    r = put_json(client, f"/api/snapshots/{sample_snapshot.id}", {"notes": "oops"})
    assert r.status_code == 400


def test_snapshot_update_not_found_returns_404(client):
    r = put_json(client, "/api/snapshots/9999", {"balance": 100})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/snapshots/<id>
# ---------------------------------------------------------------------------

def test_snapshot_delete_success(client, sample_snapshot):
    r = client.delete(f"/api/snapshots/{sample_snapshot.id}")
    assert r.status_code == 200
    assert r.get_json()["success"] is True


def test_snapshot_delete_not_found_returns_404(client):
    r = client.delete("/api/snapshots/9999")
    assert r.status_code == 404


def test_snapshot_delete_removes_from_db(client, db, sample_snapshot):
    snap_id = sample_snapshot.id
    client.delete(f"/api/snapshots/{snap_id}")
    assert _db.session.get(AccountSnapshot, snap_id) is None


# ---------------------------------------------------------------------------
# POST /api/spending
# ---------------------------------------------------------------------------

def test_spending_create_income_returns_201(client):
    r = post_json(client, "/api/spending", {
        "month": "2024-02",
        "account_name": "Employer",
        "amount": 4000.00,
        "entry_type": "income",
    })
    assert r.status_code == 201
    body = r.get_json()
    assert body["amount"] == 4000.00
    assert body["entry_type"] == "income"
    assert "id" in body


def test_spending_create_expense_returns_201(client):
    r = post_json(client, "/api/spending", {
        "month": "2024-02",
        "account_name": "Chase Visa",
        "amount": 1500.00,
        "entry_type": "expense",
    })
    assert r.status_code == 201


def test_spending_create_invalid_type_returns_400(client):
    r = post_json(client, "/api/spending", {
        "month": "2024-02",
        "account_name": "Chase",
        "amount": 100,
        "entry_type": "transfer",
    })
    assert r.status_code == 400


def test_spending_create_missing_month_returns_400(client):
    r = post_json(client, "/api/spending", {
        "account_name": "Chase",
        "amount": 100,
        "entry_type": "expense",
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# PUT /api/spending/<id>
# ---------------------------------------------------------------------------

def test_spending_update_amount(client, sample_spending):
    income_entry = sample_spending[0]
    r = put_json(client, f"/api/spending/{income_entry.id}", {"amount": 5000.00})
    assert r.status_code == 200
    assert r.get_json()["amount"] == 5000.00


def test_spending_update_invalid_type_returns_400(client, sample_spending):
    entry = sample_spending[0]
    r = put_json(client, f"/api/spending/{entry.id}", {"entry_type": "wire"})
    assert r.status_code == 400


def test_spending_update_not_found_returns_404(client):
    r = put_json(client, "/api/spending/9999", {"amount": 100})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/spending/<id>
# ---------------------------------------------------------------------------

def test_spending_delete_success(client, sample_spending):
    entry = sample_spending[1]
    r = client.delete(f"/api/spending/{entry.id}")
    assert r.status_code == 200
    assert r.get_json()["success"] is True


def test_spending_delete_not_found_returns_404(client):
    r = client.delete("/api/spending/9999")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/networth-history
# ---------------------------------------------------------------------------

def test_networth_history_empty(client):
    r = client.get("/api/networth-history")
    assert r.status_code == 200
    body = r.get_json()
    assert "dates" in body
    assert "net_worth" in body
    assert body["dates"] == []


def test_networth_history_with_data(client, db):
    metric = CalculatedMetric(
        metric_date=date(2024, 1, 1),
        net_worth=50000,
        net_worth_non_re=40000,
        total_assets=60000,
        total_liabilities=10000,
    )
    _db.session.add(metric)
    _db.session.commit()
    r = client.get("/api/networth-history")
    body = r.get_json()
    assert len(body["dates"]) == 1
    assert body["net_worth"][0] == 50000.0


# ---------------------------------------------------------------------------
# GET /api/months
# ---------------------------------------------------------------------------

def test_api_months_empty(client):
    r = client.get("/api/months")
    assert r.status_code == 200
    assert r.get_json() == []


def test_api_months_returns_entry_after_snapshot(client, sample_snapshot):
    r = client.get("/api/months")
    body = r.get_json()
    assert len(body) == 1
    assert body[0]["month"] == "2024-01"


# ---------------------------------------------------------------------------
# POST /api/months/init
# ---------------------------------------------------------------------------

def test_months_init_creates_new_month(client):
    r = post_json(client, "/api/months/init", {"month": "2024-06"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["month"] == "2024-06"
    assert "redirect" in body


def test_months_init_idempotent(client):
    post_json(client, "/api/months/init", {"month": "2024-07"})
    r = post_json(client, "/api/months/init", {"month": "2024-07"})
    assert r.status_code == 200


def test_months_init_missing_month_returns_400(client):
    r = post_json(client, "/api/months/init", {})
    assert r.status_code == 400


def test_months_init_bad_format_returns_400(client):
    r = post_json(client, "/api/months/init", {"month": "March 2024"})
    assert r.status_code == 400
