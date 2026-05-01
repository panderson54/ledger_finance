"""Extended route coverage tests — targets uncovered areas to push total above 90%."""
import io
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

from app import db as _db
from app.models import (
    Account,
    AccountSnapshot,
    AppSetting,
    AssetAllocation,
    CalculatedMetric,
    Holding,
    HoldingAllocation,
    RecurringEntry,
    SpendingEntry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _acct(name="Acct", account_type="asset", category="checking",
          include_in_networth=True, is_active=True):
    a = Account(name=name, account_type=account_type, category=category,
                include_in_networth=include_in_networth, is_active=is_active)
    _db.session.add(a)
    _db.session.commit()
    return a


def _snap(account_id, bal=1000.0, d=date(2024, 1, 1)):
    s = AccountSnapshot(account_id=account_id, snapshot_date=d, balance=bal)
    _db.session.add(s)
    _db.session.commit()
    return s


def _metric(d=date(2024, 1, 1), net_worth=50000.0, **kw):
    m = CalculatedMetric(metric_date=d, net_worth=net_worth, **kw)
    _db.session.add(m)
    _db.session.commit()
    return m


def _spend(entry_type="income", amount=1000.0, d=date(2024, 1, 1), name="Test"):
    e = SpendingEntry(entry_date=d, account_name=name, amount=amount, entry_type=entry_type)
    _db.session.add(e)
    _db.session.commit()
    return e


def _holding(account_id, ticker="AAPL", shares=10.0, price=150.0):
    h = Holding(account_id=account_id, ticker=ticker, shares=shares,
                last_price=price, is_active=True)
    _db.session.add(h)
    _db.session.flush()
    ha = HoldingAllocation(holding_id=h.id, asset_class="domestic", percentage=100.0)
    _db.session.add(ha)
    _db.session.commit()
    return h


@pytest.fixture(autouse=True)
def cleanup_extra(db):
    """Clean up tables not in the default conftest teardown."""
    yield
    _db.session.query(HoldingAllocation).delete()
    _db.session.query(Holding).delete()
    _db.session.query(RecurringEntry).delete()
    _db.session.commit()


# ---------------------------------------------------------------------------
# Index route (lines 195-220)
# ---------------------------------------------------------------------------

def test_index_empty_db(client, db):
    r = client.get("/")
    assert r.status_code == 200


def test_index_with_metrics(client, db):
    a = _acct("Checking")
    _snap(a.id, 5000.0)
    _metric(date(2024, 1, 1), net_worth=5000.0, total_assets=5000.0,
            total_liabilities=0.0, net_worth_non_re=5000.0,
            total_income=3000.0, total_expenses=2000.0, save_rate=33.0,
            monthly_change_pct=2.0, monthly_change_amount=100.0)
    with patch("app.price_service.get_sp500_monthly_change", return_value=1.5):
        r = client.get("/")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Accounts list variants (lines 258, 301, 307, 311, 317)
# ---------------------------------------------------------------------------

def test_accounts_show_all(client, db):
    _acct("Live")
    arch = _acct("Gone")
    arch.is_active = False
    _db.session.commit()
    r = client.get("/accounts?show=all")
    assert r.status_code == 200
    assert b"Live" in r.data and b"Gone" in r.data


def test_accounts_show_archived(client, db):
    arch = _acct("Archived")
    arch.is_active = False
    _db.session.commit()
    r = client.get("/accounts?show=archived")
    assert r.status_code == 200
    assert b"Archived" in r.data


def test_accounts_sort_type(client, db):
    assert client.get("/accounts?sort=type").status_code == 200


def test_accounts_sort_tax_status(client, db):
    assert client.get("/accounts?sort=tax_status").status_code == 200


def test_accounts_sort_institution(client, db):
    assert client.get("/accounts?sort=institution").status_code == 200


# ---------------------------------------------------------------------------
# Account edit (lines 438-455, 479-512)
# ---------------------------------------------------------------------------

def test_account_edit_get(client, db):
    a = _acct()
    assert client.get(f"/accounts/{a.id}/edit").status_code == 200


def test_account_edit_post_success(client, db):
    a = _acct("Old Name")
    r = client.post(f"/accounts/{a.id}/edit", data={
        "name": "New Name", "account_type": "asset", "category": "checking",
        "is_active": "on", "include_in_networth": "on",
    }, follow_redirects=True)
    assert r.status_code == 200
    _db.session.refresh(a)
    assert a.name == "New Name"


def test_account_edit_validation_error(client, db):
    a = _acct("EditMe")
    r = client.post(f"/accounts/{a.id}/edit", data={
        "name": "", "account_type": "asset", "category": "checking",
    })
    assert r.status_code == 200
    assert b"required" in r.data.lower()


def test_account_edit_investment_saves_allocations(client, db):
    a = _acct("My 401k", category="401k")
    r = client.post(f"/accounts/{a.id}/edit", data={
        "name": "My 401k", "account_type": "asset", "category": "401k",
        "is_active": "on", "include_in_networth": "on",
        "alloc_domestic": "70", "alloc_international": "15",
        "alloc_bonds": "10", "alloc_cash": "5",
    }, follow_redirects=True)
    assert r.status_code == 200
    assert AssetAllocation.query.filter_by(account_id=a.id).count() > 0


# ---------------------------------------------------------------------------
# Batch accounts API (lines 690-754)
# ---------------------------------------------------------------------------

def test_batch_accounts_success(client, db):
    r = client.post("/api/accounts/batch", json={"accounts": [
        {"name": "B Check", "account_type": "asset", "category": "checking"},
        {"name": "B 401k", "account_type": "asset", "category": "401k",
         "opening_balance": "5000", "opening_month": "2024-01"},
    ]})
    assert r.status_code == 201
    assert len(r.get_json()["created"]) == 2


def test_batch_accounts_no_accounts_key(client, db):
    assert client.post("/api/accounts/batch", json={"foo": "bar"}).status_code == 400


def test_batch_accounts_empty_list(client, db):
    assert client.post("/api/accounts/batch", json={"accounts": []}).status_code == 400


def test_batch_accounts_validation_error(client, db):
    r = client.post("/api/accounts/batch", json={"accounts": [
        {"name": "", "account_type": "asset", "category": "checking"}
    ]})
    assert r.status_code == 422


def test_batch_accounts_duplicate_names(client, db):
    r = client.post("/api/accounts/batch", json={"accounts": [
        {"name": "Dupe", "account_type": "asset", "category": "checking"},
        {"name": "Dupe", "account_type": "asset", "category": "savings"},
    ]})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Other page routes
# ---------------------------------------------------------------------------

def test_visualizations_page(client, db):
    assert client.get("/visualizations").status_code == 200


def test_onboarding_page(client, db):
    assert client.get("/onboarding").status_code == 200


def test_monthly_update_list_with_data(client, db):
    a = _acct()
    _snap(a.id)
    assert client.get("/monthly-update").status_code == 200


# ---------------------------------------------------------------------------
# Visualization API endpoints (lines 782-944)
# ---------------------------------------------------------------------------

def test_save_rate_history_empty(client, db):
    data = client.get("/api/save-rate-history").get_json()
    assert "months" in data and "rolling_12" in data


def test_save_rate_history_with_data(client, db):
    _metric(date(2024, 1, 1), net_worth=10000.0, save_rate=25.0,
            total_income=3000.0, total_expenses=2250.0)
    _metric(date(2024, 2, 1), net_worth=11000.0, save_rate=30.0,
            total_income=3000.0, total_expenses=2100.0)
    data = client.get("/api/save-rate-history").get_json()
    assert len(data["months"]) == 2


def test_allocation_history_empty(client, db):
    data = client.get("/api/allocation-history").get_json()
    assert data == {"months": [], "by_category": {}}


def test_allocation_history_with_data(client, db):
    a = _acct("Savings", category="savings")
    _snap(a.id, 5000.0)
    r = client.get("/api/allocation-history")
    assert r.status_code == 200
    assert len(r.get_json()["months"]) >= 1


def test_asset_distribution_empty(client, db):
    data = client.get("/api/asset-distribution").get_json()
    assert data == {"categories": [], "values": []}


def test_asset_distribution_with_data(client, db):
    a = _acct("Savings", category="savings")
    _snap(a.id, 5000.0)
    data = client.get("/api/asset-distribution").get_json()
    assert "categories" in data


def test_allocation_holdings_summary_empty(client, db):
    data = client.get("/api/allocation/holdings-summary").get_json()
    assert "labels" in data and "values" in data


def test_account_history_api_json(client, db):
    a = _acct()
    _snap(a.id, 1000.0, date(2024, 1, 1))
    _snap(a.id, 1100.0, date(2024, 2, 1))
    data = client.get(f"/api/accounts/{a.id}/history").get_json()
    assert len(data["history"]) == 2


# ---------------------------------------------------------------------------
# Import edge cases (lines 1051-1067)
# ---------------------------------------------------------------------------

def test_import_get_from_onboarding(client, db):
    assert client.get("/import?from=onboarding").status_code == 200


def test_import_post_no_file(client, db):
    r = client.post("/import", data={})
    assert r.status_code == 200 and b"No file" in r.data


def test_import_post_non_csv(client, db):
    r = client.post("/import",
                    data={"csv_file": (io.BytesIO(b"data"), "data.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and b".csv" in r.data


def test_import_template_with_existing_accounts(client, db):
    _acct("My Checking", category="checking")
    r = client.get("/import/template")
    assert r.status_code == 200
    assert b"My Checking" in r.data


# ---------------------------------------------------------------------------
# Export with filters (lines 1166-1289)
# ---------------------------------------------------------------------------

def test_export_csv_no_data_returns_404(client, db):
    assert client.get("/export/csv").status_code == 404


def test_export_csv_from_filter(client, db):
    a = _acct()
    _snap(a.id, 1000.0, date(2024, 1, 1))
    _snap(a.id, 1100.0, date(2024, 3, 1))
    body = client.get("/export/csv?from=2024-02").data.decode()
    assert "Mar" in body and "Jan" not in body


def test_export_csv_to_filter(client, db):
    a = _acct()
    _snap(a.id, 1000.0, date(2024, 1, 1))
    _snap(a.id, 1100.0, date(2024, 3, 1))
    body = client.get("/export/csv?to=2024-01").data.decode()
    assert "Jan" in body and "Mar" not in body


def test_export_csv_income_expense_accumulation(client, db):
    a = _acct()
    _snap(a.id, 1000.0, date(2024, 1, 1))
    # Two income entries same date → tests accumulation branch
    _spend("income", 1000.0, date(2024, 1, 1))
    _spend("income", 500.0, date(2024, 1, 1))
    # Two expense entries same date
    _spend("expense", 800.0, date(2024, 1, 1))
    _spend("expense", 200.0, date(2024, 1, 1))
    body = client.get("/export/csv").data.decode()
    assert "Income" in body and "Expenses" in body


def test_export_csv_metric_rows(client, db):
    a = _acct()
    _snap(a.id, 1000.0, date(2024, 1, 1))
    _metric(date(2024, 1, 1), net_worth=1000.0, net_worth_non_re=900.0,
            monthly_change_pct=1.5, save_rate=30.0)
    body = client.get("/export/csv").data.decode()
    assert "% Change" in body and "Save Rate" in body


# ---------------------------------------------------------------------------
# Months API edge cases (lines 1354-1452)
# ---------------------------------------------------------------------------

def test_api_month_init_with_recurring_entries(client, db):
    _db.session.add(RecurringEntry(account_name="Salary", amount=3000,
                                   entry_type="income", is_active=True, display_order=0))
    _db.session.commit()
    body = client.post("/api/months/init", json={"month": "2025-01"}).get_json()
    assert body["recurring_applied"] == 1


def test_api_month_init_recurring_idempotent(client, db):
    _db.session.add(RecurringEntry(account_name="Salary", amount=3000,
                                   entry_type="income", is_active=True, display_order=0))
    _db.session.commit()
    client.post("/api/months/init", json={"month": "2025-02"})
    body = client.post("/api/months/init", json={"month": "2025-02"}).get_json()
    assert body["recurring_skipped"] == 1


def test_api_month_delete_no_data(client, db):
    assert client.delete("/api/months/2099-01").status_code == 404


def test_api_month_delete_metric_only(client, db):
    _metric(date(2024, 6, 1))
    r = client.delete("/api/months/2024-06")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Snapshot edge case (line 1464)
# ---------------------------------------------------------------------------

def test_api_snapshot_create_empty_body(client, db):
    r = client.post("/api/snapshots", content_type="application/json", data="")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Spending CRUD (lines 1553-1643)
# ---------------------------------------------------------------------------

def test_api_spending_create_success(client, db):
    r = client.post("/api/spending", json={
        "month": "2024-03", "account_name": "Employer",
        "amount": 3000, "entry_type": "income",
    })
    assert r.status_code == 201
    assert r.get_json()["amount"] == 3000.0


def test_api_spending_create_empty_body(client, db):
    r = client.post("/api/spending", content_type="application/json", data="")
    assert r.status_code == 400


def test_api_spending_create_invalid_month(client, db):
    r = client.post("/api/spending", json={
        "month": "not-a-month", "account_name": "X",
        "amount": 100, "entry_type": "expense",
    })
    assert r.status_code == 400


def test_api_spending_create_invalid_entry_type(client, db):
    r = client.post("/api/spending", json={
        "month": "2024-03", "account_name": "X",
        "amount": 100, "entry_type": "invalid",
    })
    assert r.status_code == 400


def test_api_spending_update_success(client, db):
    e = _spend("income", 1000.0)
    r = client.put(f"/api/spending/{e.id}", json={"amount": 2000, "notes": "bonus"})
    assert r.status_code == 200
    assert r.get_json()["amount"] == 2000.0


def test_api_spending_update_empty_body(client, db):
    e = _spend()
    r = client.put(f"/api/spending/{e.id}", content_type="application/json", data="")
    assert r.status_code == 400


def test_api_spending_update_bad_entry_type(client, db):
    e = _spend()
    assert client.put(f"/api/spending/{e.id}", json={"entry_type": "bad"}).status_code == 400


def test_api_spending_update_entry_type_and_notes(client, db):
    e = _spend("income")
    r = client.put(f"/api/spending/{e.id}", json={"entry_type": "expense", "notes": "updated"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["entry_type"] == "expense"
    assert body["notes"] == "updated"


def test_api_spending_update_not_found(client, db):
    assert client.put("/api/spending/99999", json={"amount": 100}).status_code == 404


def test_api_spending_delete_success(client, db):
    e = _spend()
    r = client.delete(f"/api/spending/{e.id}")
    assert r.status_code == 200 and r.get_json()["success"] is True


def test_api_spending_delete_not_found(client, db):
    assert client.delete("/api/spending/99999").status_code == 404


# ---------------------------------------------------------------------------
# Recurring entries CRUD (lines 1651-1758)
# ---------------------------------------------------------------------------

def test_api_recurring_list_empty(client, db):
    assert client.get("/api/recurring-entries").get_json() == []


def test_api_recurring_list_with_data(client, db):
    _db.session.add(RecurringEntry(account_name="Rent", amount=1500,
                                   entry_type="expense", is_active=True, display_order=1))
    _db.session.commit()
    data = client.get("/api/recurring-entries").get_json()
    assert len(data) == 1 and data[0]["account_name"] == "Rent"


def test_api_recurring_create_success(client, db):
    r = client.post("/api/recurring-entries", json={
        "account_name": "Netflix", "amount": 15.99, "entry_type": "expense",
    })
    assert r.status_code == 201
    assert r.get_json()["account_name"] == "Netflix"


def test_api_recurring_create_no_body(client, db):
    r = client.post("/api/recurring-entries", content_type="application/json", data="")
    assert r.status_code == 400


def test_api_recurring_create_missing_name(client, db):
    r = client.post("/api/recurring-entries", json={"amount": 10, "entry_type": "expense"})
    assert r.status_code == 400


def test_api_recurring_create_bad_entry_type(client, db):
    r = client.post("/api/recurring-entries", json={
        "account_name": "T", "amount": 10, "entry_type": "bad"
    })
    assert r.status_code == 400


def test_api_recurring_create_bad_amount(client, db):
    r = client.post("/api/recurring-entries", json={
        "account_name": "T", "amount": -5, "entry_type": "expense"
    })
    assert r.status_code == 400


def test_api_recurring_update_all_fields(client, db):
    e = RecurringEntry(account_name="Old", amount=100, entry_type="expense",
                       is_active=True, display_order=0)
    _db.session.add(e)
    _db.session.commit()
    r = client.put(f"/api/recurring-entries/{e.id}", json={
        "account_name": "New", "amount": 200, "entry_type": "income",
        "is_active": False, "display_order": 5, "notes": "updated",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["account_name"] == "New" and body["amount"] == 200.0


def test_api_recurring_update_not_found(client, db):
    assert client.put("/api/recurring-entries/99999", json={"amount": 10}).status_code == 404


def test_api_recurring_update_bad_entry_type(client, db):
    e = RecurringEntry(account_name="T", amount=100, entry_type="expense",
                       is_active=True, display_order=0)
    _db.session.add(e)
    _db.session.commit()
    assert client.put(f"/api/recurring-entries/{e.id}", json={"entry_type": "bad"}).status_code == 400


def test_api_recurring_update_empty_name(client, db):
    e = RecurringEntry(account_name="T", amount=100, entry_type="expense",
                       is_active=True, display_order=0)
    _db.session.add(e)
    _db.session.commit()
    assert client.put(f"/api/recurring-entries/{e.id}", json={"account_name": ""}).status_code == 400


def test_api_recurring_update_bad_amount(client, db):
    e = RecurringEntry(account_name="T", amount=100, entry_type="expense",
                       is_active=True, display_order=0)
    _db.session.add(e)
    _db.session.commit()
    assert client.put(f"/api/recurring-entries/{e.id}", json={"amount": -1}).status_code == 400


def test_api_recurring_delete_success(client, db):
    e = RecurringEntry(account_name="Del", amount=50, entry_type="expense",
                       is_active=True, display_order=0)
    _db.session.add(e)
    _db.session.commit()
    r = client.delete(f"/api/recurring-entries/{e.id}")
    assert r.status_code == 200 and r.get_json()["success"] is True


def test_api_recurring_delete_not_found(client, db):
    assert client.delete("/api/recurring-entries/99999").status_code == 404


# ---------------------------------------------------------------------------
# Metrics API (lines 1765-1799)
# ---------------------------------------------------------------------------

def test_api_metrics_calculate(client, db):
    a = _acct()
    _snap(a.id, 5000.0)
    r = client.post("/api/metrics/calculate/2024-01")
    assert r.status_code == 200
    assert "net_worth" in r.get_json()


def test_api_metrics_calculate_invalid_month(client, db):
    assert client.post("/api/metrics/calculate/bad").status_code == 400


def test_api_metrics_update_existing(client, db):
    _metric(date(2024, 1, 1))
    r = client.put("/api/metrics/2024-01", json={"net_worth": 99000.0, "save_rate": 40.0})
    assert r.status_code == 200 and r.get_json()["net_worth"] == 99000.0


def test_api_metrics_update_creates_new(client, db):
    r = client.put("/api/metrics/2025-06", json={"net_worth": 50000.0})
    assert r.status_code == 200 and r.get_json()["net_worth"] == 50000.0


def test_api_metrics_update_invalid_month(client, db):
    assert client.put("/api/metrics/bad", json={"net_worth": 1}).status_code == 400


def test_api_metrics_update_empty_body(client, db):
    r = client.put("/api/metrics/2024-01", content_type="application/json", data="")
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Allocation route + targets API (lines 2021-2206)
# ---------------------------------------------------------------------------

def test_allocation_page_empty(client, db):
    assert client.get("/allocation").status_code == 200


def test_allocation_page_with_cash_account(client, db):
    a = _acct("Savings", category="savings")
    _snap(a.id, 10000.0)
    assert client.get("/allocation").status_code == 200


def test_allocation_page_with_investment_and_allocations(client, db):
    a = _acct("401k", category="401k")
    _snap(a.id, 50000.0)
    _db.session.add(AssetAllocation(account_id=a.id, effective_date=date(2024, 1, 1),
                                    asset_class="domestic", percentage=70.0))
    _db.session.commit()
    assert client.get("/allocation").status_code == 200


def test_allocation_page_with_holdings(client, db):
    a = _acct("Brokerage", category="brokerage")
    _snap(a.id, 10000.0)
    _holding(a.id, "VOO", 50.0, 200.0)
    assert client.get("/allocation").status_code == 200


def test_api_allocation_targets_save(client, db):
    r = client.post("/api/allocation/targets", json={
        "domestic": 70.0, "international": 15.0, "bonds": 10.0, "cash": 5.0,
    })
    assert r.status_code == 200 and r.get_json()["ok"] is True


def test_api_allocation_targets_bad_sum(client, db):
    r = client.post("/api/allocation/targets", json={
        "domestic": 50.0, "international": 10.0, "bonds": 10.0, "cash": 5.0,
    })
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Holdings CRUD (lines 2519-2665)
# ---------------------------------------------------------------------------

def test_api_holdings_list_empty(client, db):
    a = _acct("Brokerage", category="brokerage")
    assert client.get(f"/api/accounts/{a.id}/holdings").get_json() == []


def test_api_holdings_list_not_found(client, db):
    assert client.get("/api/accounts/99999/holdings").status_code == 404


def test_api_holding_create_success(client, db):
    a = _acct("Brokerage", category="brokerage")
    with patch("app.price_service.get_price", return_value=(150.0, "Apple Inc.")):
        r = client.post(f"/api/accounts/{a.id}/holdings", json={
            "ticker": "AAPL", "shares": 10,
            "allocations": {"domestic": 100.0},
        })
    assert r.status_code == 201
    body = r.get_json()
    assert body["ticker"] == "AAPL" and body["last_price"] == 150.0


def test_api_holding_create_price_fails_gracefully(client, db):
    a = _acct("Brokerage", category="brokerage")
    with patch("app.price_service.get_price", side_effect=ValueError("no price")):
        r = client.post(f"/api/accounts/{a.id}/holdings", json={"ticker": "FAKE", "shares": 5})
    assert r.status_code == 201  # route tolerates price fetch failure


def test_api_holding_create_account_not_found(client, db):
    assert client.post("/api/accounts/99999/holdings",
                       json={"ticker": "AAPL", "shares": 1}).status_code == 404


def test_api_holding_create_missing_ticker(client, db):
    a = _acct("Brokerage", category="brokerage")
    assert client.post(f"/api/accounts/{a.id}/holdings", json={"shares": 5}).status_code == 400


def test_api_holding_create_negative_shares(client, db):
    a = _acct("Brokerage", category="brokerage")
    assert client.post(f"/api/accounts/{a.id}/holdings",
                       json={"ticker": "AAPL", "shares": -1}).status_code == 400


def test_api_holding_create_bad_allocation_sum(client, db):
    a = _acct("Brokerage", category="brokerage")
    r = client.post(f"/api/accounts/{a.id}/holdings", json={
        "ticker": "AAPL", "shares": 1,
        "allocations": {"domestic": 60.0, "international": 20.0},  # sums to 80
    })
    assert r.status_code == 400


def test_api_holding_update_success(client, db):
    a = _acct("Brokerage", category="brokerage")
    h = _holding(a.id)
    r = client.put(f"/api/holdings/{h.id}", json={
        "shares": 20.0, "name": "Apple", "cap_class": "large",
        "allocations": {"domestic": 80.0, "international": 20.0},
    })
    assert r.status_code == 200 and r.get_json()["shares"] == 20.0


def test_api_holding_update_not_found(client, db):
    assert client.put("/api/holdings/99999", json={"shares": 5}).status_code == 404


def test_api_holding_update_bad_shares(client, db):
    a = _acct("Brokerage", category="brokerage")
    h = _holding(a.id)
    assert client.put(f"/api/holdings/{h.id}", json={"shares": "bad"}).status_code == 400


def test_api_holding_update_bad_allocation(client, db):
    a = _acct("Brokerage", category="brokerage")
    h = _holding(a.id)
    assert client.put(f"/api/holdings/{h.id}",
                      json={"allocations": {"domestic": 60.0}}).status_code == 400


def test_api_holding_archive(client, db):
    a = _acct("Brokerage", category="brokerage")
    h = _holding(a.id)
    r = client.delete(f"/api/holdings/{h.id}")
    assert r.status_code == 200 and r.get_json()["success"] is True
    _db.session.refresh(h)
    assert h.is_active is False


def test_api_holding_archive_not_found(client, db):
    assert client.delete("/api/holdings/99999").status_code == 404


# ---------------------------------------------------------------------------
# Price service API routes (lines 2678-2723)
# ---------------------------------------------------------------------------

def test_api_price_lookup_success(client, db):
    with patch("app.price_service.get_price", return_value=(200.0, "Vanguard S&P500")):
        r = client.get("/api/prices/VOO")
    assert r.status_code == 200
    body = r.get_json()
    assert body["price"] == 200.0 and body["ticker"] == "VOO"


def test_api_price_lookup_not_found(client, db):
    with patch("app.price_service.get_price", side_effect=ValueError("No price data")):
        assert client.get("/api/prices/FAKE").status_code == 404


def test_api_price_lookup_server_error(client, db):
    with patch("app.price_service.get_price", side_effect=Exception("network")):
        assert client.get("/api/prices/ERR").status_code == 502


def test_api_prices_refresh_no_holdings(client, db):
    body = client.post("/api/prices/refresh").get_json()
    assert body["updated"] == 0 and body["skipped"] == 0


def test_api_prices_refresh_updates_stale(client, db):
    a = _acct("Brokerage", category="brokerage")
    h = Holding(account_id=a.id, ticker="AAPL", shares=10,
                last_price=100.0, last_fetched=None, is_active=True)
    _db.session.add(h)
    _db.session.commit()
    with patch("app.price_service.get_price", return_value=(155.0, "Apple")):
        body = client.post("/api/prices/refresh").get_json()
    assert body["updated"] == 1 and body["skipped"] == 0


def test_api_prices_refresh_skips_fresh(client, db):
    a = _acct("Brokerage", category="brokerage")
    fresh = datetime.now(timezone.utc).replace(tzinfo=None)
    h = Holding(account_id=a.id, ticker="AAPL", shares=10,
                last_price=100.0, last_fetched=fresh, is_active=True)
    _db.session.add(h)
    _db.session.commit()
    body = client.post("/api/prices/refresh").get_json()
    assert body["skipped"] == 1


def test_api_prices_refresh_handles_failure(client, db):
    a = _acct("Brokerage", category="brokerage")
    h = Holding(account_id=a.id, ticker="FAKE", shares=5,
                last_price=None, last_fetched=None, is_active=True)
    _db.session.add(h)
    _db.session.commit()
    with patch("app.price_service.get_price", side_effect=ValueError("no price")):
        body = client.post("/api/prices/refresh").get_json()
    assert body["failed"] == 1 and len(body["errors"]) == 1


# ---------------------------------------------------------------------------
# Classification API (lines 2754, 2768-2776)
# ---------------------------------------------------------------------------

def test_api_classify_disabled_by_default(client, db):
    r = client.get("/api/classify/AAPL")
    assert r.status_code == 503
    assert r.get_json()["manual_required"] is True


def test_api_classify_no_api_key(client, db):
    _db.session.add(AppSetting(key="claude_classification_enabled", value="true"))
    _db.session.commit()
    r = client.get("/api/classify/AAPL")
    assert r.status_code == 503


def test_api_classifications_list_empty(client, db):
    assert client.get("/api/classifications").get_json() == []


# ---------------------------------------------------------------------------
# Projections with income / expenses (lines 2365-2399)
# ---------------------------------------------------------------------------

def test_api_projections_growth_with_income_all_cash(client, db):
    a = _acct("Savings", category="savings")
    _snap(a.id, 10000.0)
    r = client.post("/api/projections/growth", json={
        "rates": {"savings": 2.0},
        "horizon_years": 5,
        "income": {
            "mode": "gross",
            "gross_annual": 100000,
            "tax_rate_pct": 25,
            "save_rate_pct": 20,
            "distribution": "all_cash",
        },
    })
    assert r.status_code == 200


def test_api_projections_growth_with_income_net_mode(client, db):
    a = _acct("Savings", category="savings")
    _snap(a.id, 10000.0)
    r = client.post("/api/projections/growth", json={
        "rates": {"savings": 2.0},
        "horizon_years": 5,
        "income": {
            "mode": "net",
            "net_annual": 60000,
            "save_rate_pct": 20,
            "distribution": "all_cash",
        },
    })
    assert r.status_code == 200


def test_api_projections_growth_with_income_pro_rata(client, db):
    a = _acct("Savings", category="savings")
    _snap(a.id, 10000.0)
    r = client.post("/api/projections/growth", json={
        "rates": {"savings": 2.0},
        "horizon_years": 5,
        "income": {
            "mode": "gross",
            "gross_annual": 100000,
            "tax_rate_pct": 25,
            "save_rate_pct": 20,
            "distribution": "pro_rata",
        },
    })
    assert r.status_code == 200


def test_api_projections_growth_with_expenses_and_age(client, db):
    a = _acct("Cash", category="cash")
    _snap(a.id, 50000.0)
    r = client.post("/api/projections/growth", json={
        "rates": {"cash": 1.0},
        "horizon_years": 10,
        "expenses": [{"name": "Car", "amount": 20000, "year": 2028}],
        "current_age": 65,
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["callouts"]["retirement_label"] == "Already retired"


def test_api_projections_growth_with_young_age(client, db):
    a = _acct("retirement", category="retirement")
    _snap(a.id, 100000.0)
    # Route caps horizon at 30 years (360 months). age=58 → 7*12=84 months to 65 < 360.
    r = client.post("/api/projections/growth", json={
        "rates": {"retirement": 7.0},
        "horizon_years": 30,
        "current_age": 58,
    })
    assert r.status_code == 200
    body = r.get_json()
    assert "Age 65" in body["callouts"]["retirement_label"]
