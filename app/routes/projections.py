"""
Projections routes:
  /projections, /api/projections/*
"""
import logging
from datetime import date

from dateutil.relativedelta import relativedelta
from flask import render_template, jsonify, request

from app.routes import main_bp
from app.routes.helpers import _build_projections_context
from app.models import Account, AccountSnapshot, CalculatedMetric
from app import db
from app import projections as proj
from app.metrics_service import compute_income_contributions as _compute_income_contributions

logger = logging.getLogger(__name__)


@main_bp.route('/projections')
def projections():
    ctx = _build_projections_context()
    return render_template('projections.html', **ctx)


@main_bp.route('/api/projections/growth', methods=['POST'])
def api_projections_growth():
    """
    Compute a growth projection and return chart data + callouts.

    Request JSON:
        rates         {category: annual_pct_rate}  e.g. {"retirement": 7.0}
        horizon_years int  (1–30)
        mortgage      optional {annual_rate: float (%), remaining_months: int}
        current_age   optional int
        income        optional {mode, gross_annual, tax_rate_pct, net_annual,
                                save_rate_pct, distribution, invest_pct}
        expenses      optional [{name, amount, year}]

    Response JSON:
        labels                list[str]  'YYYY-MM'
        total_projected       list[float]
        by_category           {category: list[float]}
        callouts              {yr5, yr10, yr20, yr30, doubles_date,
                               mortgage_payoff_date, retirement_nw, retirement_label}
        monthly_contribution  float  total monthly savings amount
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    rates_pct: dict = data.get('rates', {})
    horizon_years = int(data.get('horizon_years', 20))
    horizon_years = max(1, min(30, horizon_years))
    horizon_months = horizon_years * 12

    mortgage_raw = data.get('mortgage')
    mortgage_params = None
    if mortgage_raw:
        mortgage_params = {
            'annual_rate': float(mortgage_raw.get('annual_rate', 6.5)) / 100,
            'remaining_months': int(mortgage_raw.get('remaining_months', 360)),
        }

    # Build current balances
    accounts = Account.query.filter_by(is_active=True).all()
    current_by_category: dict[str, float] = {}
    for account in accounts:
        latest = (
            AccountSnapshot.query
            .filter_by(account_id=account.id)
            .order_by(AccountSnapshot.snapshot_date.desc())
            .first()
        )
        if latest:
            bal = float(latest.balance)
            current_by_category[account.category] = (
                current_by_category.get(account.category, 0.0) + bal
            )

    # Convert pct rates to decimals
    annual_rates = {cat: float(pct) / 100 for cat, pct in rates_pct.items()}

    # Income → monthly contributions per category
    income_raw = data.get('income')
    monthly_contributions: dict[str, float] = {}
    monthly_contribution_total = 0.0
    if income_raw:
        monthly_contributions, monthly_contribution_total = _compute_income_contributions(
            income_raw, current_by_category
        )

    # Planned expenses → convert year to month offset from today
    today = date.today().replace(day=1)
    planned_expenses: list[dict] = []
    for exp in (data.get('expenses') or []):
        try:
            exp_year = int(exp.get('year', 0))
            if exp_year <= 0:
                continue
            month_offset = (exp_year - today.year) * 12 - today.month + 1
            if 0 < month_offset <= horizon_months:
                planned_expenses.append({
                    'month_offset': month_offset,
                    'amount': max(0.0, float(exp.get('amount', 0) or 0)),
                    'name': str(exp.get('name', 'Expense'))[:50],
                })
        except (TypeError, ValueError):
            continue

    result = proj.project_growth(
        current_by_category, annual_rates, horizon_months,
        mortgage_params=mortgage_params,
        monthly_contributions=monthly_contributions or None,
        planned_expenses=planned_expenses or None,
    )

    proj_labels = proj.build_month_labels(today, horizon_months)

    current_age = data.get('current_age')
    if current_age is not None:
        current_age = int(current_age)

    mort_series = result['by_category'].get('mortgage')
    callouts = proj.growth_callouts(
        result['total'], today,
        mortgage_series=mort_series,
        current_age=current_age,
    )
    for key in ('yr5', 'yr10', 'yr20', 'yr30', 'retirement_nw'):
        if callouts.get(key) is not None:
            callouts[key] = round(callouts[key])

    return jsonify({
        'labels': proj_labels,
        'total_projected': [round(v) for v in result['total']],
        'by_category': {cat: [round(v) for v in vals]
                        for cat, vals in result['by_category'].items()},
        'callouts': callouts,
        'monthly_contribution': round(monthly_contribution_total),
        'planned_expenses': [
            {'month_offset': e['month_offset'], 'name': e['name'], 'amount': e['amount']}
            for e in planned_expenses
        ],
    })


@main_bp.route('/api/projections/fi', methods=['POST'])
def api_projections_fi():
    """
    Compute FI projections for up to 3 scenarios.

    Request JSON:
        scenarios  list of {name, spending, swr, growth_rate, monthly_savings}
                   swr and growth_rate are percentages (e.g. 4.0 = 4%)
        current_age  optional int

    Response JSON:
        current_investable_nw  float
        scenarios  list of {name, fi_number, fi_date, years_to_fi, age_at_fi, projection}
        sensitivity {swr_labels, spending_labels, data}
    """
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({'error': 'Invalid JSON'}), 400

    scenarios_raw = data.get('scenarios')
    if not scenarios_raw or not isinstance(scenarios_raw, list):
        return jsonify({'error': 'scenarios list required'}), 400

    current_age = data.get('current_age')

    # Latest investable NW
    latest_metric = (
        CalculatedMetric.query
        .filter(CalculatedMetric.net_worth_non_re.isnot(None))
        .order_by(CalculatedMetric.metric_date.desc())
        .first()
    )
    current_nw = float(latest_metric.net_worth_non_re) if latest_metric else 0.0

    today = date.today().replace(day=1)
    projection_months = 50 * 12  # 50 years max display

    scenario_results = []
    for raw in scenarios_raw[:3]:
        spending = float(raw.get('spending', 80_000))
        swr = float(raw.get('swr', 4.0)) / 100
        growth_rate = float(raw.get('growth_rate', 7.0)) / 100
        monthly_savings = float(raw.get('monthly_savings', 0))
        name = str(raw.get('name', 'Scenario'))[:50]

        fi_target = proj.fi_number(spending, swr)
        m = proj.months_to_fi(current_nw, fi_target, monthly_savings, growth_rate)
        fi_date = None
        years_to_fi = None
        age_at_fi = None
        if m is not None:
            fi_date = (today + relativedelta(months=m)).strftime('%Y-%m')
            years_to_fi = round(m / 12, 1)
            if current_age:
                age_at_fi = round(current_age + m / 12, 1)

        series = proj.project_fi_series(current_nw, monthly_savings, growth_rate, projection_months)

        scenario_results.append({
            'name': name,
            'fi_number': round(fi_target) if fi_target != float('inf') else None,
            'fi_date': fi_date,
            'years_to_fi': years_to_fi,
            'age_at_fi': age_at_fi,
            'projection': [round(v) for v in series],
        })

    # Sensitivity table uses the active scenario (sent as active_index, position in submitted list)
    active_idx = max(0, min(int(data.get('active_index', 0)), len(scenarios_raw) - 1))
    active_raw = scenarios_raw[active_idx]
    sens_growth = float(active_raw.get('growth_rate', 7.0)) / 100
    sens_savings = float(active_raw.get('monthly_savings', 0))
    sens_spending = float(active_raw.get('spending', 80_000))
    swr_range = [0.025, 0.030, 0.035, 0.040, 0.045, 0.050]
    sensitivity = proj.fi_sensitivity_table_growth(
        current_nw, sens_savings, sens_spending, sens_growth, swr_range
    )

    proj_labels = proj.build_month_labels(today, projection_months)

    return jsonify({
        'current_investable_nw': round(current_nw),
        'labels': proj_labels,
        'scenarios': scenario_results,
        'sensitivity': sensitivity,
    })
