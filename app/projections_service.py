"""Builds growth and FI projections from the stored history; the math lives in app/projections.py."""
from datetime import date

from dateutil.relativedelta import relativedelta

from app import db
from app import projections as proj
from app.metrics_service import compute_income_contributions
from app.models import Account, AccountSnapshot, CalculatedMetric
from app.portfolio_service import current_balances_by_category

MAX_GROWTH_YEARS = 30
FI_PROJECTION_MONTHS = 50 * 12
SENSITIVITY_SWRS = [0.025, 0.030, 0.035, 0.040, 0.045, 0.050]


def _net_worth_metrics():
    return (
        CalculatedMetric.query
        .filter(CalculatedMetric.net_worth.isnot(None))
        .order_by(CalculatedMetric.metric_date)
        .all()
    )


def default_category_rates() -> dict[str, float]:
    """Historical CAGR per account category, as decimals (liabilities may be negative)."""
    rows = (
        db.session.query(Account.category, AccountSnapshot.snapshot_date, AccountSnapshot.balance)
        .join(Account, Account.id == AccountSnapshot.account_id)
        .filter(Account.is_active == True)  # noqa: E712
        .all()
    )
    return proj.calculate_category_cagr([
        {'category': r.category, 'snapshot_date': r.snapshot_date, 'balance': float(r.balance)}
        for r in rows
    ])


def latest_investable_net_worth() -> float:
    metric = (
        CalculatedMetric.query
        .filter(CalculatedMetric.net_worth_non_re.isnot(None))
        .order_by(CalculatedMetric.metric_date.desc())
        .first()
    )
    return float(metric.net_worth_non_re) if metric else 0.0


def projection_context() -> dict:
    """Historical series and default rates for the /projections page (all JSON-safe)."""
    metrics = _net_worth_metrics()
    portfolio_cagr = 0.0
    nonre = [(m.metric_date, float(m.net_worth_non_re))
             for m in metrics if m.net_worth_non_re and float(m.net_worth_non_re) > 0]
    if len(nonre) >= 2:
        (first_date, first_val), (last_date, last_val) = nonre[0], nonre[-1]
        portfolio_cagr = proj.calculate_cagr(first_val, last_val, (last_date - first_date).days / 365.25)

    latest = metrics[-1] if metrics else None
    save_rates = [float(m.save_rate) for m in metrics if m.save_rate is not None]
    latest_income = float(latest.total_income) if latest and latest.total_income else 0.0
    latest_expenses = float(latest.total_expenses) if latest and latest.total_expenses else 0.0

    return {
        'historical_labels': [m.metric_date.strftime('%Y-%m') for m in metrics],
        'historical_nw': [float(m.net_worth) for m in metrics],
        'historical_nw_nonre': [
            float(m.net_worth_non_re) if m.net_worth_non_re is not None else None for m in metrics
        ],
        'current_by_category': current_balances_by_category(),
        'default_rates_pct': {cat: round(r * 100, 2) for cat, r in default_category_rates().items()},
        'portfolio_cagr_pct': round(portfolio_cagr * 100, 2),
        'current_investable_nw': (
            float(latest.net_worth_non_re) if latest and latest.net_worth_non_re is not None else 0.0
        ),
        'avg_save_rate': round(sum(save_rates) / len(save_rates), 1) if save_rates else 0.0,
        'default_monthly_savings': max(0.0, latest_income - latest_expenses),
        'today_str': date.today().strftime('%Y-%m'),
        'has_data': len(metrics) > 0,
    }


def _planned_expenses(expenses, today, horizon_months):
    planned = []
    for exp in expenses or []:
        try:
            exp_year = int(exp.get('year', 0))
            if exp_year <= 0:
                continue
            month_offset = (exp_year - today.year) * 12 - today.month + 1
            if 0 < month_offset <= horizon_months:
                planned.append({
                    'month_offset': month_offset,
                    'amount': max(0.0, float(exp.get('amount', 0) or 0)),
                    'name': str(exp.get('name', 'Expense'))[:50],
                })
        except (TypeError, ValueError):
            continue
    return planned


def growth_projection(rates_pct, horizon_years, mortgage=None, income=None, expenses=None, current_age=None) -> dict:
    """Month-by-month net worth projection from current balances.

    rates_pct: {category: annual % rate}; mortgage: {annual_rate (%), remaining_months};
    income/expenses as the /api/projections/growth request documents them.
    """
    horizon_months = max(1, min(MAX_GROWTH_YEARS, int(horizon_years))) * 12
    mortgage_params = None
    if mortgage:
        mortgage_params = {
            'annual_rate': float(mortgage.get('annual_rate', 6.5)) / 100,
            'remaining_months': int(mortgage.get('remaining_months', 360)),
        }
    current_by_category = current_balances_by_category()
    contributions, contribution_total = (
        compute_income_contributions(income, current_by_category) if income else ({}, 0.0)
    )
    today = date.today().replace(day=1)
    planned = _planned_expenses(expenses, today, horizon_months)

    result = proj.project_growth(
        current_by_category, {cat: float(pct) / 100 for cat, pct in (rates_pct or {}).items()}, horizon_months,
        mortgage_params=mortgage_params,
        monthly_contributions=contributions or None,
        planned_expenses=planned or None,
    )
    callouts = proj.growth_callouts(
        result['total'], today,
        mortgage_series=result['by_category'].get('mortgage'),
        current_age=int(current_age) if current_age is not None else None,
    )
    for key in ('yr5', 'yr10', 'yr20', 'yr30', 'retirement_nw'):
        if callouts.get(key) is not None:
            callouts[key] = round(callouts[key])

    return {
        'labels': proj.build_month_labels(today, horizon_months),
        'total_projected': [round(v) for v in result['total']],
        'by_category': {cat: [round(v) for v in vals] for cat, vals in result['by_category'].items()},
        'callouts': callouts,
        'monthly_contribution': round(contribution_total),
        'planned_expenses': planned,
    }


def fi_scenario(current_nw, spending, swr_pct, growth_pct, monthly_savings, name='Scenario', current_age=None) -> dict:
    """FI number, date and projected series for one scenario (swr/growth as percentages)."""
    fi_target = proj.fi_number(spending, swr_pct / 100)
    months = proj.months_to_fi(current_nw, fi_target, monthly_savings, growth_pct / 100)
    fi_date = years_to_fi = age_at_fi = None
    if months is not None:
        fi_date = (date.today().replace(day=1) + relativedelta(months=months)).strftime('%Y-%m')
        years_to_fi = round(months / 12, 1)
        if current_age:
            age_at_fi = round(current_age + months / 12, 1)
    series = proj.project_fi_series(current_nw, monthly_savings, growth_pct / 100, FI_PROJECTION_MONTHS)
    return {
        'name': str(name)[:50],
        'fi_number': round(fi_target) if fi_target != float('inf') else None,
        'fi_date': fi_date,
        'years_to_fi': years_to_fi,
        'age_at_fi': age_at_fi,
        'projection': [round(v) for v in series],
    }


def fi_sensitivity(current_nw, monthly_savings, spending, growth_pct) -> dict:
    return proj.fi_sensitivity_table_growth(current_nw, monthly_savings, spending, growth_pct / 100, SENSITIVITY_SWRS)


def fi_labels() -> list[str]:
    return proj.build_month_labels(date.today().replace(day=1), FI_PROJECTION_MONTHS)
