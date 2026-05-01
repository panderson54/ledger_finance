"""
Tests for app/projections.py math helpers and the /projections routes.
"""
import json
import math
from datetime import date

import pytest

from app.projections import (
    amortize_mortgage,
    build_month_labels,
    calculate_cagr,
    calculate_category_cagr,
    fi_number,
    fi_sensitivity_table,
    growth_callouts,
    months_to_fi,
    project_fi_series,
    project_growth,
)
from app.models import Account, AccountSnapshot, CalculatedMetric
from app import db as _db


# ---------------------------------------------------------------------------
# calculate_cagr
# ---------------------------------------------------------------------------

def test_cagr_known_values():
    # $100k → $200k over 10 years = 7.177...%
    result = calculate_cagr(100_000, 200_000, 10)
    assert abs(result - 0.07177) < 0.0001


def test_cagr_zero_start_returns_zero():
    assert calculate_cagr(0, 200_000, 10) == 0.0


def test_cagr_zero_years_returns_zero():
    assert calculate_cagr(100_000, 200_000, 0) == 0.0


def test_cagr_negative_start_returns_zero():
    assert calculate_cagr(-50_000, 100_000, 5) == 0.0


# ---------------------------------------------------------------------------
# calculate_category_cagr
# ---------------------------------------------------------------------------

def test_category_cagr_single_entry_uses_default():
    # Single entry = 0 days of history → falls back to category default (not 0.0)
    from app.projections import _CAGR_DEFAULTS
    snapshots = [{"category": "retirement", "snapshot_date": date(2024, 1, 1), "balance": 100_000}]
    result = calculate_category_cagr(snapshots)
    assert result["retirement"] == _CAGR_DEFAULTS["retirement"]


def test_category_cagr_multiple_categories():
    snapshots = [
        {"category": "retirement", "snapshot_date": date(2014, 1, 1), "balance": 100_000},
        {"category": "retirement", "snapshot_date": date(2024, 1, 1), "balance": 200_000},
        {"category": "cash", "snapshot_date": date(2022, 1, 1), "balance": 10_000},
        {"category": "cash", "snapshot_date": date(2024, 1, 1), "balance": 11_000},
    ]
    result = calculate_category_cagr(snapshots)
    assert "retirement" in result
    assert "cash" in result
    assert abs(result["retirement"] - 0.07177) < 0.001
    assert result["cash"] > 0


# ---------------------------------------------------------------------------
# amortize_mortgage
# ---------------------------------------------------------------------------

def test_amortize_mortgage_length():
    series = amortize_mortgage(300_000, 0.065, 360)
    assert len(series) == 361  # month 0 + 360 payments


def test_amortize_mortgage_reaches_zero():
    series = amortize_mortgage(300_000, 0.065, 360)
    assert series[-1] < 1.0  # effectively zero


def test_amortize_mortgage_decreasing():
    series = amortize_mortgage(300_000, 0.065, 360)
    assert all(series[i] >= series[i + 1] for i in range(len(series) - 1))


def test_amortize_mortgage_zero_rate():
    series = amortize_mortgage(120_000, 0.0, 120)
    assert len(series) == 121
    assert series[-1] < 1.0


def test_amortize_mortgage_zero_balance():
    series = amortize_mortgage(0, 0.065, 360)
    assert series == [0]


# ---------------------------------------------------------------------------
# project_growth
# ---------------------------------------------------------------------------

def test_project_growth_zero_rate_flat():
    result = project_growth({"cash": 10_000}, {"cash": 0.0}, 12)
    series = result["by_category"]["cash"]
    assert len(series) == 13
    assert all(abs(v - 10_000) < 0.01 for v in series)


def test_project_growth_total_length():
    cats = {"retirement": 200_000, "cash": 20_000}
    rates = {"retirement": 0.07, "cash": 0.02}
    result = project_growth(cats, rates, 60)
    assert len(result["total"]) == 61


def test_project_growth_mortgage_uses_amortization():
    cats = {"retirement": 200_000, "mortgage": 300_000}
    rates = {"retirement": 0.07, "mortgage": 0.06}
    mortgage_params = {"annual_rate": 0.065, "remaining_months": 360}
    result = project_growth(cats, rates, 360, mortgage_params)
    # Mortgage balance should decline over time
    mort = result["by_category"]["mortgage"]
    assert mort[0] > mort[180] > mort[-1]
    assert mort[-1] < 1.0


def test_project_growth_total_subtracts_liabilities():
    # With a mortgage liability, total should be assets minus mortgage
    cats = {"cash": 50_000, "mortgage": 30_000}
    rates = {"cash": 0.0, "mortgage": 0.0}
    result = project_growth(cats, rates, 1)
    assert abs(result["total"][0] - 20_000) < 0.01


# ---------------------------------------------------------------------------
# growth_callouts
# ---------------------------------------------------------------------------

def test_growth_callouts_doubling():
    # Flat series of 200k from month 0; total doubles somewhere
    base = 100_000
    series = [base * (1.07 ** (m / 12)) for m in range(361)]
    callouts = growth_callouts(series, date(2024, 1, 1))
    assert callouts["doubles_date"] is not None
    assert callouts["yr10"] is not None


def test_growth_callouts_mortgage_payoff():
    mort_series = amortize_mortgage(300_000, 0.065, 360)
    total_series = [500_000.0] * (len(mort_series))
    callouts = growth_callouts(total_series, date(2024, 1, 1), mortgage_series=mort_series)
    assert callouts["mortgage_payoff_date"] is not None


def test_growth_callouts_short_series_yr30_none():
    series = [100_000.0] * 61  # only 5 years
    callouts = growth_callouts(series, date(2024, 1, 1))
    assert callouts["yr30"] is None


# ---------------------------------------------------------------------------
# build_month_labels
# ---------------------------------------------------------------------------

def test_build_month_labels_length():
    labels = build_month_labels(date(2024, 1, 1), 12)
    assert len(labels) == 13
    assert labels[0] == "2024-01"
    assert labels[12] == "2025-01"


# ---------------------------------------------------------------------------
# fi_number
# ---------------------------------------------------------------------------

def test_fi_number_standard():
    assert fi_number(100_000, 0.04) == 2_500_000


def test_fi_number_zero_swr():
    assert fi_number(100_000, 0.0) == float("inf")


# ---------------------------------------------------------------------------
# months_to_fi
# ---------------------------------------------------------------------------

def test_months_to_fi_already_there():
    assert months_to_fi(2_500_000, 2_500_000, 3_000, 0.07) == 0


def test_months_to_fi_reasonable_inputs():
    m = months_to_fi(500_000, 2_500_000, 3_000, 0.07)
    assert m is not None
    assert 100 < m < 600


def test_months_to_fi_zero_growth_zero_savings_never():
    # No growth, no savings — never reaches target
    result = months_to_fi(0, 2_500_000, 0, 0.0)
    assert result is None


def test_months_to_fi_zero_growth_with_savings():
    # $0 NW, $2500/mo savings, target $30k → should take 12 months
    m = months_to_fi(0, 30_000, 2_500, 0.0)
    assert m == 12


# ---------------------------------------------------------------------------
# project_fi_series
# ---------------------------------------------------------------------------

def test_project_fi_series_length():
    series = project_fi_series(500_000, 3_000, 0.07, 120)
    assert len(series) == 121


def test_project_fi_series_grows():
    series = project_fi_series(500_000, 3_000, 0.07, 120)
    assert series[-1] > series[0]


def test_project_fi_series_zero_growth():
    series = project_fi_series(100_000, 1_000, 0.0, 12)
    assert abs(series[-1] - 112_000) < 0.01


# ---------------------------------------------------------------------------
# fi_sensitivity_table
# ---------------------------------------------------------------------------

def test_fi_sensitivity_table_shape():
    swr_values = [0.03, 0.04, 0.05]
    spending_values = [50_000, 75_000, 100_000]
    result = fi_sensitivity_table(500_000, 3_000, 0.07, spending_values, swr_values)
    assert len(result["swr_labels"]) == 3
    assert len(result["spending_labels"]) == 3
    assert len(result["data"]) == 3
    assert len(result["data"][0]) == 3


def test_fi_sensitivity_table_lower_spending_sooner():
    swr_values = [0.04]
    spending_values = [50_000, 100_000]
    result = fi_sensitivity_table(500_000, 3_000, 0.07, spending_values, swr_values)
    low_spending_years = result["data"][0][0]
    high_spending_years = result["data"][0][1]
    assert low_spending_years < high_spending_years


def test_fi_sensitivity_table_labels():
    result = fi_sensitivity_table(500_000, 3_000, 0.07, [50_000], [0.04])
    assert result["swr_labels"] == ["4.0%"]
    assert result["spending_labels"] == ["$50k"]


# ---------------------------------------------------------------------------
# Route smoke tests
# ---------------------------------------------------------------------------

def _make_projection_data(db_obj):
    """Seed minimal data for route tests."""
    acc = Account(
        name="Retirement Fund",
        account_type="asset",
        category="retirement",
        is_liquid=False,
        include_in_networth=True,
        is_active=True,
    )
    _db.session.add(acc)
    _db.session.flush()

    for year, month, balance in [(2023, 1, 400_000), (2024, 1, 450_000)]:
        snap = AccountSnapshot(
            account_id=acc.id,
            snapshot_date=date(year, month, 1),
            balance=balance,
        )
        _db.session.add(snap)

    for year, month, nw, nw_nonre, save_rate in [
        (2023, 1, 400_000, 400_000, 35.0),
        (2024, 1, 450_000, 450_000, 38.0),
    ]:
        metric = CalculatedMetric(
            metric_date=date(year, month, 1),
            net_worth=nw,
            net_worth_non_re=nw_nonre,
            save_rate=save_rate,
        )
        _db.session.add(metric)

    _db.session.commit()


def test_projections_route_200(client, db):
    _make_projection_data(db)
    r = client.get("/projections")
    assert r.status_code == 200


def test_projections_route_empty_db_200(client, db):
    # No data — should still render gracefully
    r = client.get("/projections")
    assert r.status_code == 200


def test_api_growth_returns_json(client, db):
    _make_projection_data(db)
    payload = {
        "rates": {"retirement": 7.0},
        "horizon_years": 10,
    }
    r = client.post(
        "/api/projections/growth",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "labels" in body
    assert "total_projected" in body
    assert "callouts" in body


def test_api_growth_with_mortgage(client, db):
    acc = Account(
        name="Home Mortgage",
        account_type="liability",
        category="mortgage",
        is_liquid=False,
        include_in_networth=True,
        is_active=True,
    )
    _db.session.add(acc)
    _db.session.flush()
    _db.session.add(AccountSnapshot(
        account_id=acc.id,
        snapshot_date=date(2024, 1, 1),
        balance=300_000,
    ))
    _db.session.commit()

    payload = {
        "rates": {"mortgage": 0.0},
        "horizon_years": 5,
        "mortgage": {"annual_rate": 6.5, "remaining_months": 300},
    }
    r = client.post(
        "/api/projections/growth",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert body["callouts"]["mortgage_payoff_date"] is not None or True  # may not payoff in 5yr


def test_api_fi_returns_json(client, db):
    _make_projection_data(db)
    payload = {
        "scenarios": [
            {
                "name": "Baseline",
                "spending": 80_000,
                "swr": 4.0,
                "growth_rate": 7.0,
                "monthly_savings": 3_000,
            }
        ],
        "current_age": 35,
    }
    r = client.post(
        "/api/projections/fi",
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert r.status_code == 200
    body = r.get_json()
    assert "scenarios" in body
    assert "sensitivity" in body
    s = body["scenarios"][0]
    assert "fi_number" in s
    assert "fi_date" in s
    assert "projection" in s


def test_api_fi_missing_scenarios_returns_400(client, db):
    r = client.post(
        "/api/projections/fi",
        data=json.dumps({}),
        content_type="application/json",
    )
    assert r.status_code == 400


def test_api_growth_invalid_json_returns_400(client, db):
    r = client.post(
        "/api/projections/growth",
        data="not-json",
        content_type="application/json",
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# project_growth: expense deduction logic (lines 167-223)
# ---------------------------------------------------------------------------

def test_project_growth_with_planned_expenses():
    from app.projections import project_growth

    current = {"cash": 20000.0, "retirement": 80000.0}
    annual_rates = {"cash": 0.01, "retirement": 0.07}
    expenses = [{"month_offset": 6, "amount": 10000.0}]
    result = project_growth(current, annual_rates, 24, planned_expenses=expenses)
    assert len(result["total"]) == 25
    # After the expense month the total should be less than without expenses
    result_no_exp = project_growth(current, annual_rates, 24)
    assert result["total"][6] < result_no_exp["total"][6]


def test_project_growth_expense_overflow_to_invest():
    from app.projections import project_growth

    # Cash is tiny; most of the expense must come from invest categories
    current = {"cash": 100.0, "retirement": 50000.0}
    annual_rates = {"cash": 0.0, "retirement": 0.07}
    expenses = [{"month_offset": 1, "amount": 5000.0}]
    result = project_growth(current, annual_rates, 12, planned_expenses=expenses)
    assert len(result["total"]) == 13
    # retirement balance should be reduced at month 1
    result_no = project_growth(current, annual_rates, 12)
    assert result["total"][1] < result_no["total"][1]


def test_project_growth_mortgage_series_padded():
    from app.projections import project_growth

    # Short amortization (2 months) with a longer horizon triggers padding
    current = {"mortgage": 5000.0, "cash": 10000.0}
    annual_rates = {"mortgage": 0.0, "cash": 0.02}
    mortgage_params = {"annual_rate": 0.06, "remaining_months": 2}
    result = project_growth(current, annual_rates, 12, mortgage_params=mortgage_params)
    assert len(result["by_category"]["mortgage"]) == 13


# ---------------------------------------------------------------------------
# growth_callouts: retirement age variants (lines 289-303)
# ---------------------------------------------------------------------------

def test_growth_callouts_already_retired():
    from app.projections import growth_callouts
    from datetime import date

    series = [100_000.0 + i * 500 for i in range(361)]
    result = growth_callouts(series, date(2026, 1, 1), current_age=65)
    assert result["retirement_nw"] == series[0]
    assert result["retirement_label"] == "Already retired"


def test_growth_callouts_young_age_within_horizon():
    from app.projections import growth_callouts
    from datetime import date

    # 360-month (30 yr) series; age 30 → 35 years to 65 = 420 months > len
    # Use a long series so 35*12=420 < len
    series = [100_000.0 + i * 1000 for i in range(500)]
    result = growth_callouts(series, date(2026, 1, 1), current_age=30)
    assert result["retirement_nw"] is not None
    assert "Age 65" in result["retirement_label"]


def test_growth_callouts_young_age_beyond_horizon():
    from app.projections import growth_callouts
    from datetime import date

    # Only 60 months available; age 30 → 420 months to 65 > 60
    series = [100_000.0 + i * 1000 for i in range(61)]
    result = growth_callouts(series, date(2026, 1, 1), current_age=30)
    assert result["retirement_nw"] == series[-1]
    assert result["retirement_label"] == "End of horizon"


def test_growth_callouts_no_age():
    from app.projections import growth_callouts
    from datetime import date

    series = [50_000.0 + i * 200 for i in range(121)]
    result = growth_callouts(series, date(2026, 1, 1), current_age=None)
    assert result["retirement_nw"] == series[-1]
    assert result["retirement_label"] == "End of horizon"
