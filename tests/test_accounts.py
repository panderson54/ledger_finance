"""
Tests for Account CRUD page routes:
  GET  /accounts
  GET  /accounts/new
  POST /accounts/new
  GET  /accounts/<id>/edit
  POST /accounts/<id>/edit
  POST /accounts/<id>/archive
"""
from app.models import Account, AccountSnapshot
from app import db as _db


# ---------------------------------------------------------------------------
# GET /accounts
# ---------------------------------------------------------------------------

def test_accounts_page_loads_empty(client):
    r = client.get("/accounts")
    assert r.status_code == 200


def test_accounts_page_shows_active_account(client, sample_account):
    r = client.get("/accounts")
    assert r.status_code == 200
    assert b"Test Checking" in r.data


def test_accounts_page_hides_archived_by_default(client, db, sample_account):
    sample_account.is_active = False
    _db.session.commit()
    r = client.get("/accounts")
    assert b"Test Checking" not in r.data


def test_accounts_page_archived_filter_shows_archived(client, db, sample_account):
    sample_account.is_active = False
    _db.session.commit()
    r = client.get("/accounts?show=archived")
    assert b"Test Checking" in r.data


# ---------------------------------------------------------------------------
# GET /accounts/new
# ---------------------------------------------------------------------------

def test_account_new_get_loads(client):
    r = client.get("/accounts/new")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# POST /accounts/new
# ---------------------------------------------------------------------------

def test_account_create_success_redirects(client):
    r = client.post("/accounts/new", data={
        "name": "My Savings",
        "account_type": "asset",
        "category": "savings",
        "is_liquid": "on",
        "include_in_networth": "on",
        "is_active": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"My Savings" in r.data


def test_account_create_persists_to_db(client):
    client.post("/accounts/new", data={
        "name": "Persisted Account",
        "account_type": "asset",
        "category": "cash",
        "is_active": "on",
    }, follow_redirects=True)
    assert Account.query.filter_by(name="Persisted Account").first() is not None


def test_account_create_missing_name_shows_error(client):
    r = client.post("/accounts/new", data={
        "account_type": "asset",
        "category": "savings",
    })
    assert r.status_code == 200
    assert b"Name is required" in r.data


def test_account_create_invalid_type_shows_error(client):
    r = client.post("/accounts/new", data={
        "name": "Bad Type",
        "account_type": "neither",
        "category": "savings",
    })
    assert r.status_code == 200
    assert b"asset or liability" in r.data


def test_account_create_duplicate_name_shows_error(client, sample_account):
    r = client.post("/accounts/new", data={
        "name": "test checking",  # same as "Test Checking", different case
        "account_type": "asset",
        "category": "checking",
    })
    assert r.status_code == 200
    assert b"already exists" in r.data


def test_account_create_invalid_color_shows_error(client):
    r = client.post("/accounts/new", data={
        "name": "Color Test",
        "account_type": "asset",
        "category": "cash",
        "display_color": "red",  # not a valid hex code
    })
    assert r.status_code == 200
    assert b"hex color" in r.data


def test_account_create_valid_color_succeeds(client):
    r = client.post("/accounts/new", data={
        "name": "Color Account",
        "account_type": "asset",
        "category": "cash",
        "display_color": "#4a90e2",
        "is_active": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert Account.query.filter_by(name="Color Account").first() is not None


# ---------------------------------------------------------------------------
# GET /accounts/<id>/edit
# ---------------------------------------------------------------------------

def test_account_edit_get_loads(client, sample_account):
    r = client.get(f"/accounts/{sample_account.id}/edit")
    assert r.status_code == 200
    assert b"Test Checking" in r.data


def test_account_edit_get_404_for_missing(client):
    r = client.get("/accounts/9999/edit")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /accounts/<id>/edit
# ---------------------------------------------------------------------------

def test_account_edit_success(client, sample_account):
    r = client.post(f"/accounts/{sample_account.id}/edit", data={
        "name": "Updated Checking",
        "account_type": "asset",
        "category": "checking",
        "is_liquid": "on",
        "include_in_networth": "on",
        "is_active": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    _db.session.refresh(sample_account)
    assert sample_account.name == "Updated Checking"


def test_account_edit_duplicate_name_rejected(client, db, sample_account):
    other = Account(name="Other Account", account_type="asset", category="cash", is_active=True)
    _db.session.add(other)
    _db.session.commit()
    r = client.post(f"/accounts/{sample_account.id}/edit", data={
        "name": "Other Account",
        "account_type": "asset",
        "category": "checking",
    })
    assert r.status_code == 200
    assert b"already exists" in r.data


def test_account_edit_own_name_allowed(client, sample_account):
    """Saving an account with its own unchanged name should not trigger duplicate error."""
    r = client.post(f"/accounts/{sample_account.id}/edit", data={
        "name": "Test Checking",  # unchanged
        "account_type": "asset",
        "category": "checking",
        "is_liquid": "on",
        "include_in_networth": "on",
        "is_active": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b"already exists" not in r.data


# ---------------------------------------------------------------------------
# POST /accounts/<id>/archive
# ---------------------------------------------------------------------------

def test_account_archive_toggles_to_inactive(client, sample_account):
    r = client.post(f"/accounts/{sample_account.id}/archive")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert body["is_active"] is False


def test_account_archive_toggles_back_to_active(client, db, sample_account):
    sample_account.is_active = False
    _db.session.commit()
    r = client.post(f"/accounts/{sample_account.id}/archive")
    body = r.get_json()
    assert body["is_active"] is True


def test_account_archive_404_for_missing(client):
    r = client.post("/accounts/9999/archive")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /accounts — sort controls
# ---------------------------------------------------------------------------

def test_accounts_sort_by_name(client, db):
    _db.session.add_all([
        Account(name="Zebra Savings", account_type="asset", category="savings", is_active=True),
        Account(name="Alpha Checking", account_type="asset", category="checking", is_active=True),
    ])
    _db.session.commit()
    r = client.get("/accounts?sort=name")
    assert r.status_code == 200
    data = r.data
    assert data.index(b"Alpha Checking") < data.index(b"Zebra Savings")


def test_accounts_sort_by_category(client, db):
    _db.session.add_all([
        Account(name="Z Savings", account_type="asset", category="savings", is_active=True),
        Account(name="A Cash", account_type="asset", category="cash", is_active=True),
    ])
    _db.session.commit()
    r = client.get("/accounts?sort=category")
    assert r.status_code == 200
    data = r.data
    assert data.index(b"A Cash") < data.index(b"Z Savings")


def test_accounts_sort_by_balance(client, db, sample_snapshot):
    # sample_snapshot has balance=5000 for sample_account; add a second account with lower balance
    other = Account(name="Low Balance", account_type="asset", category="cash", is_active=True)
    _db.session.add(other)
    _db.session.commit()
    from datetime import date as _date
    _db.session.add(
        AccountSnapshot(account_id=other.id, snapshot_date=_date(2024, 1, 1), balance=100.0)
    )
    _db.session.commit()
    r = client.get("/accounts?sort=balance")
    assert r.status_code == 200
    data = r.data
    # Higher balance (Test Checking=5000) should appear before Low Balance (100)
    assert data.index(b"Test Checking") < data.index(b"Low Balance")


# ---------------------------------------------------------------------------
# POST /accounts/new — opening balance
# ---------------------------------------------------------------------------

def test_account_create_with_opening_balance_creates_snapshot(client, db):
    from app.models import AccountSnapshot
    r = client.post("/accounts/new", data={
        "name": "New With Balance",
        "account_type": "asset",
        "category": "checking",
        "is_active": "on",
        "opening_balance": "2500.00",
        "opening_month": "2024-03",
    }, follow_redirects=True)
    assert r.status_code == 200
    acct = Account.query.filter_by(name="New With Balance").first()
    assert acct is not None
    snap = AccountSnapshot.query.filter_by(account_id=acct.id).first()
    assert snap is not None
    assert snap.balance == 2500.00


def test_account_create_without_opening_balance_no_snapshot(client, db):
    from app.models import AccountSnapshot
    client.post("/accounts/new", data={
        "name": "No Balance Account",
        "account_type": "asset",
        "category": "cash",
        "is_active": "on",
    }, follow_redirects=True)
    acct = Account.query.filter_by(name="No Balance Account").first()
    assert acct is not None
    assert AccountSnapshot.query.filter_by(account_id=acct.id).count() == 0


# ---------------------------------------------------------------------------
# GET /api/account-balances/<id>
# ---------------------------------------------------------------------------

def test_account_balances_api_returns_account_metadata(client, sample_account):
    r = client.get(f"/api/account-balances/{sample_account.id}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["account_id"] == sample_account.id
    assert body["account_name"] == "Test Checking"
    assert body["account_type"] == "asset"
    assert body["category"] == "checking"
    assert "account_color" in body


def test_account_balances_api_returns_snapshot_data(client, sample_account, sample_snapshot):
    r = client.get(f"/api/account-balances/{sample_account.id}")
    body = r.get_json()
    assert len(body["dates"]) == 1
    assert body["balances"][0] == 5000.0


def test_account_balances_api_empty_account(client, sample_account):
    r = client.get(f"/api/account-balances/{sample_account.id}")
    body = r.get_json()
    assert body["dates"] == []
    assert body["balances"] == []


def test_account_balances_api_returns_color(client, db):
    acct = Account(name="Colored", account_type="asset", category="savings",
                   is_active=True, display_color="#ff5500")
    _db.session.add(acct)
    _db.session.commit()
    r = client.get(f"/api/account-balances/{acct.id}")
    body = r.get_json()
    assert body["account_color"] == "#ff5500"


def test_account_balances_api_404_for_missing(client):
    r = client.get("/api/account-balances/9999")
    assert r.status_code == 404
