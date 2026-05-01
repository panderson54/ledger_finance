"""
Projection math helpers for the /projections page.

Covers two features:
  - Growth Projection Graphs (item 19): per-category compound growth + mortgage amortization
  - Path to FI Calculator (item 15): FI number, time-to-FI, scenario comparison, sensitivity table
"""
import logging
from datetime import date
from dateutil.relativedelta import relativedelta

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CAGR helpers
# ---------------------------------------------------------------------------

def calculate_cagr(start_value: float, end_value: float, years: float) -> float:
    """
    Compound Annual Growth Rate from start_value to end_value over `years`.
    Returns 0.0 if inputs are invalid (zero/negative start, zero years).
    """
    if start_value <= 0 or years <= 0 or end_value < 0:
        return 0.0
    return (end_value / start_value) ** (1.0 / years) - 1.0


_CAGR_MIN_HISTORY_DAYS = 365   # require ~1 full year before trusting CAGR
_CAGR_MAX = 0.50               # hard cap at 50% — anything higher is a data artifact

# Sensible fallback annual rates (decimal) when history is too short
_CAGR_DEFAULTS: dict[str, float] = {
    "401k":       0.07,
    "ira":        0.07,
    "roth_ira":   0.07,
    "retirement": 0.07,
    "brokerage":  0.07,
    "investment": 0.07,
    "hsa":        0.07,
    "529":        0.06,
    "savings":    0.045,
    "cash":       0.045,
    "checking":   0.00,
    "real_estate":0.035,
    "vehicle":    0.00,
    "mortgage":   0.00,
    "loan":       0.00,
    "credit_card":0.00,
}


def calculate_category_cagr(
    snapshots: list[dict],
) -> dict[str, float]:
    """
    Given a list of dicts with keys {category, snapshot_date, balance},
    return {category: cagr_as_decimal} using first and last snapshot per category.
    snapshot_date should be a datetime.date.

    Falls back to _CAGR_DEFAULTS when history is shorter than _CAGR_MIN_HISTORY_DAYS,
    and caps results at _CAGR_MAX to prevent contribution-inflated artifacts.
    """
    by_category: dict[str, list] = {}
    for row in snapshots:
        cat = row["category"]
        by_category.setdefault(cat, []).append(row)

    result = {}
    for cat, rows in by_category.items():
        rows_sorted = sorted(rows, key=lambda r: r["snapshot_date"])
        first = rows_sorted[0]
        last = rows_sorted[-1]
        elapsed_days = (last["snapshot_date"] - first["snapshot_date"]).days
        if elapsed_days < _CAGR_MIN_HISTORY_DAYS:
            result[cat] = _CAGR_DEFAULTS.get(cat, 0.05)
            continue
        years = elapsed_days / 365.25
        # For liabilities (mortgage) the balance is positive in DB; treat as absolute value
        start_bal = abs(float(first["balance"]))
        end_bal = abs(float(last["balance"]))
        cagr = calculate_cagr(start_bal, end_bal, years)
        result[cat] = min(cagr, _CAGR_MAX)

    return result


# ---------------------------------------------------------------------------
# Mortgage amortization
# ---------------------------------------------------------------------------

def amortize_mortgage(
    current_balance: float,
    annual_rate: float,
    remaining_months: int,
) -> list[float]:
    """
    Return a list of `remaining_months + 1` end-of-month balances (index 0 = today).
    Uses standard fixed-rate amortization. Balance reaches ~0 at the final month.
    annual_rate is a decimal (e.g. 0.065 for 6.5%).
    """
    if remaining_months <= 0 or current_balance <= 0:
        return [current_balance]

    balances = [current_balance]
    r = annual_rate / 12.0

    if r == 0:
        monthly_payment = current_balance / remaining_months
        for _ in range(remaining_months):
            balances.append(max(0.0, balances[-1] - monthly_payment))
        return balances

    # Fixed monthly payment
    pmt = current_balance * r / (1 - (1 + r) ** (-remaining_months))
    bal = current_balance
    for _ in range(remaining_months):
        interest = bal * r
        principal = pmt - interest
        bal = max(0.0, bal - principal)
        balances.append(bal)

    return balances


# ---------------------------------------------------------------------------
# Growth projection (item 19)
# ---------------------------------------------------------------------------

def project_growth(
    current_by_category: dict[str, float],
    annual_rates: dict[str, float],
    horizon_months: int,
    mortgage_params: dict | None = None,
    monthly_contributions: dict[str, float] | None = None,
    planned_expenses: list[dict] | None = None,
) -> dict:
    """
    Project total net worth and per-category balances month by month.

    current_by_category: {category: current_balance}  (mortgage balance is positive)
    annual_rates: {category: decimal_rate}
    horizon_months: number of months to project
    mortgage_params: optional {annual_rate, remaining_months} — if provided, overrides
                     compound growth for the 'mortgage' category with amortization.
    monthly_contributions: optional {category: monthly_amount} — added each month
    planned_expenses: optional [{month_offset: int, amount: float, name: str}] —
                      lump-sum deductions applied at the specified month; deducted
                      from cash/savings first, then investment categories.

    Returns:
        {
          "by_category": {category: [balance_at_month_0, ..., balance_at_month_n]},
          "total": [net_worth_at_month_0, ..., net_worth_at_month_n],
        }
    """
    n = horizon_months
    contributions = monthly_contributions or {}
    expenses = planned_expenses or []

    # Build category ordering: mortgage uses amortization, others use compound growth
    liability_cats = {"mortgage", "credit_card", "loan"}
    cash_cats = {"cash", "checking", "savings"}
    invest_cats = {"retirement", "brokerage", "401k", "ira", "roth_ira", "hsa", "529", "investment"}

    # Build expense lookup by month offset for fast access
    expense_by_month: dict[int, list[dict]] = {}
    for exp in expenses:
        m_off = int(exp.get("month_offset", 0))
        if 0 < m_off <= n:
            expense_by_month.setdefault(m_off, []).append(exp)

    # Pre-compute amortization schedule for mortgage
    mort_series: list[float] | None = None
    if "mortgage" in current_by_category and mortgage_params:
        amort_rate = mortgage_params.get("annual_rate", 0.065)
        remaining = mortgage_params.get("remaining_months", n)
        mort_series = amortize_mortgage(current_by_category["mortgage"], amort_rate, remaining)
        if len(mort_series) < n + 1:
            mort_series = mort_series + [0.0] * (n + 1 - len(mort_series))
        mort_series = mort_series[: n + 1]

    # Initialise per-category series at month 0 = current balance
    by_category: dict[str, list[float]] = {}
    monthly_rates: dict[str, float] = {}
    for cat, balance in current_by_category.items():
        rate = annual_rates.get(cat, 0.0)
        monthly_rates[cat] = (1 + rate) ** (1 / 12) - 1
        by_category[cat] = [balance]

    # Step month by month so contributions and expenses can be applied at each step
    for m in range(1, n + 1):
        # Apply planned-expense deductions at this month
        month_expenses = expense_by_month.get(m, [])
        if month_expenses:
            # Collect current balances at start of this month (before growth)
            # for proportional deduction ordering
            prev = {cat: by_category[cat][m - 1] for cat in by_category}
            total_expense = sum(e.get("amount", 0.0) for e in month_expenses)

            # Deduct from cash categories first
            remaining_expense = total_expense
            cash_total = sum(max(0.0, prev[c]) for c in cash_cats if c in prev)
            if cash_total > 0 and remaining_expense > 0:
                cash_portion = min(remaining_expense, cash_total)
                for cat in cash_cats:
                    if cat in prev and prev[cat] > 0:
                        ratio = prev[cat] / cash_total
                        prev[cat] = max(0.0, prev[cat] - cash_portion * ratio)
                remaining_expense -= cash_portion

            # Overflow into investment categories
            if remaining_expense > 0:
                invest_total = sum(max(0.0, prev[c]) for c in invest_cats if c in prev)
                if invest_total > 0:
                    for cat in invest_cats:
                        if cat in prev and prev[cat] > 0:
                            ratio = prev[cat] / invest_total
                            prev[cat] = max(0.0, prev[cat] - remaining_expense * ratio)

            # Update previous-month balances to reflect expense deduction
            for cat in by_category:
                if cat in prev:
                    by_category[cat][m - 1] = prev[cat]

        # Grow each category and add monthly contribution
        for cat in by_category:
            if cat == "mortgage" and mort_series is not None:
                by_category[cat].append(mort_series[m])
            else:
                r = monthly_rates[cat]
                prev_bal = by_category[cat][m - 1]
                contrib = contributions.get(cat, 0.0)
                new_bal = prev_bal * (1 + r) + contrib
                # Liabilities are non-negative; contributions don't reduce liabilities
                if cat in liability_cats:
                    new_bal = max(0.0, new_bal)
                by_category[cat].append(new_bal)

    # Total = assets - liabilities
    totals = []
    for m in range(n + 1):
        assets = sum(v[m] for cat, v in by_category.items() if cat not in liability_cats)
        liabilities = sum(v[m] for cat, v in by_category.items() if cat in liability_cats)
        totals.append(assets - liabilities)

    return {"by_category": by_category, "total": totals}


def growth_callouts(
    total_series: list[float],
    start_date: date,
    mortgage_series: list[float] | None = None,
    current_age: int | None = None,
) -> dict:
    """
    Derive human-readable callouts from a monthly total series.

    Returns:
        yr5, yr10, yr20, yr30 — projected totals at those horizons (or None if series too short)
        doubles_date — ISO date string when NW first doubles from month-0 value (or None)
        mortgage_payoff_date — ISO date string when mortgage balance first hits 0 (or None)
        retirement_nw — projected NW at retirement age 65 (or end of horizon if age unknown/out of range)
        retirement_label — human-readable description of the retirement_nw value
    """
    base = total_series[0] if total_series else 0.0

    def at_year(y):
        idx = y * 12
        return total_series[idx] if idx < len(total_series) else None

    doubles_date = None
    if base > 0:
        for i, val in enumerate(total_series):
            if val >= base * 2:
                doubles_date = (start_date + relativedelta(months=i)).isoformat()
                break

    payoff_date = None
    if mortgage_series:
        for i, bal in enumerate(mortgage_series):
            if bal <= 0:
                payoff_date = (start_date + relativedelta(months=i)).isoformat()
                break

    # Retirement callout — projected NW at age 65, or end-of-horizon fallback
    retirement_nw: float | None = None
    retirement_label: str = "End of horizon"
    if total_series:
        if current_age is not None and current_age < 65:
            months_to_ret = (65 - current_age) * 12
            if months_to_ret < len(total_series):
                retirement_nw = total_series[months_to_ret]
                ret_date = (start_date + relativedelta(months=months_to_ret)).strftime("%Y-%m")
                retirement_label = f"Age 65 ({ret_date})"
            else:
                retirement_nw = total_series[-1]
                # retirement_label stays "End of horizon"
        elif current_age is not None and current_age >= 65:
            retirement_nw = total_series[0]
            retirement_label = "Already retired"
        else:
            retirement_nw = total_series[-1]
            # retirement_label stays "End of horizon"

    return {
        "yr5": at_year(5),
        "yr10": at_year(10),
        "yr20": at_year(20),
        "yr30": at_year(30),
        "doubles_date": doubles_date,
        "mortgage_payoff_date": payoff_date,
        "retirement_nw": retirement_nw,
        "retirement_label": retirement_label,
    }


def build_month_labels(start_date: date, n_months: int) -> list[str]:
    """Return list of 'YYYY-MM' strings for months 0..n_months."""
    return [
        (start_date + relativedelta(months=i)).strftime("%Y-%m")
        for i in range(n_months + 1)
    ]


# ---------------------------------------------------------------------------
# FI projection (item 15)
# ---------------------------------------------------------------------------

def fi_number(annual_spending: float, swr: float) -> float:
    """FI Number = annual spending / safe withdrawal rate. swr is a decimal (e.g. 0.04)."""
    if swr <= 0:
        return float("inf")
    return annual_spending / swr


def months_to_fi(
    current_investable_nw: float,
    fi_target: float,
    monthly_savings: float,
    annual_growth_rate: float,
) -> int | None:
    """
    Return number of months until investable NW reaches fi_target.
    Uses compound growth + monthly contribution:
        FV(m) = PV * (1+r)^m + PMT * ((1+r)^m - 1) / r
    Returns None if target is never reached within 600 months (50 years).
    """
    if current_investable_nw >= fi_target:
        return 0

    r = annual_growth_rate / 12.0
    pv = current_investable_nw
    pmt = monthly_savings
    max_months = 600

    for m in range(1, max_months + 1):
        if r == 0:
            fv = pv + pmt * m
        else:
            growth = (1 + r) ** m
            fv = pv * growth + pmt * (growth - 1) / r
        if fv >= fi_target:
            return m

    return None


def project_fi_series(
    current_investable_nw: float,
    monthly_savings: float,
    annual_growth_rate: float,
    n_months: int,
) -> list[float]:
    """
    Return month-by-month investable NW values (length n_months + 1).
    Index 0 = today.
    """
    r = annual_growth_rate / 12.0
    series = [current_investable_nw]
    pv = current_investable_nw
    for m in range(1, n_months + 1):
        if r == 0:
            fv = pv + monthly_savings * m
        else:
            growth = (1 + r) ** m
            fv = pv * growth + monthly_savings * (growth - 1) / r
        series.append(fv)
    return series


def fi_sensitivity_table(
    current_investable_nw: float,
    monthly_savings: float,
    annual_growth_rate: float,
    spending_values: list[float],
    swr_values: list[float],
) -> dict:
    """
    Build a sensitivity table of years-to-FI across spending × SWR combinations.

    Returns:
        {
          "swr_labels": [...],        # e.g. ["3.0%", "3.5%", "4.0%", "4.5%", "5.0%"]
          "spending_labels": [...],   # e.g. ["$50k", "$75k", "$100k"]
          "data": [[years_or_None, ...], ...]   # rows = swr, cols = spending
        }
    """
    swr_labels = [f"{s * 100:.1f}%" for s in swr_values]
    spending_labels = [f"${int(sp / 1000)}k" for sp in spending_values]
    data = []
    for swr in swr_values:
        row = []
        for sp in spending_values:
            target = fi_number(sp, swr)
            m = months_to_fi(current_investable_nw, target, monthly_savings, annual_growth_rate)
            row.append(round(m / 12, 1) if m is not None else None)
        data.append(row)
    return {"swr_labels": swr_labels, "spending_labels": spending_labels, "data": data}


def fi_sensitivity_table_growth(
    current_investable_nw: float,
    monthly_savings: float,
    annual_spending: float,
    growth_rate_center: float,
    swr_values: list[float],
) -> dict:
    """
    Sensitivity table: SWR (rows) × portfolio growth rate (cols).
    Columns are centred on growth_rate_center ± 1% and ± 2%, clamped to [1%, 15%].

    Returns:
        {
          "swr_labels":  [...],   # e.g. ["3.0%", "3.5%", "4.0%", "4.5%", "5.0%"]
          "rate_labels": [...],   # e.g. ["5.0%", "6.0%", "7.0%", "8.0%", "9.0%"]
          "data": [[years_or_None, ...], ...]   # rows = swr, cols = growth rate
        }
    """
    offsets = [-0.02, -0.01, 0.0, 0.01, 0.02]
    # Clamp, deduplicate, preserve order
    seen: set[float] = set()
    growth_rates: list[float] = []
    for o in offsets:
        r = round(max(0.01, min(0.15, growth_rate_center + o)), 4)
        if r not in seen:
            seen.add(r)
            growth_rates.append(r)

    swr_labels = [f"{s * 100:.1f}%" for s in swr_values]
    rate_labels = [f"{r * 100:.1f}%" for r in growth_rates]
    data = []
    for swr in swr_values:
        row = []
        for gr in growth_rates:
            target = fi_number(annual_spending, swr)
            m = months_to_fi(current_investable_nw, target, monthly_savings, gr)
            row.append(round(m / 12, 1) if m is not None else None)
        data.append(row)
    return {"swr_labels": swr_labels, "rate_labels": rate_labels, "data": data}
