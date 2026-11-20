"""
Projections routes:
  /projections, /api/projections/*
"""
import logging
from flask import render_template, jsonify, request

from app.routes import main_bp
from app import projections_service

logger = logging.getLogger(__name__)


@main_bp.route('/projections')
def projections():
    return render_template('projections.html', **projections_service.projection_context())


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
    return jsonify(projections_service.growth_projection(
        data.get('rates', {}), data.get('horizon_years', 20),
        mortgage=data.get('mortgage'), income=data.get('income'),
        expenses=data.get('expenses'), current_age=data.get('current_age'),
    ))


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
    current_nw = projections_service.latest_investable_net_worth()
    scenario_results = [
        projections_service.fi_scenario(
            current_nw,
            float(raw.get('spending', 80_000)), float(raw.get('swr', 4.0)),
            float(raw.get('growth_rate', 7.0)), float(raw.get('monthly_savings', 0)),
            name=raw.get('name', 'Scenario'), current_age=current_age,
        )
        for raw in scenarios_raw[:3]
    ]

    # Sensitivity table uses the active scenario (sent as active_index, position in submitted list)
    active_idx = max(0, min(int(data.get('active_index', 0)), len(scenarios_raw) - 1))
    active_raw = scenarios_raw[active_idx]
    sensitivity = projections_service.fi_sensitivity(
        current_nw, float(active_raw.get('monthly_savings', 0)),
        float(active_raw.get('spending', 80_000)), float(active_raw.get('growth_rate', 7.0)),
    )

    return jsonify({
        'current_investable_nw': round(current_nw),
        'labels': projections_service.fi_labels(),
        'scenarios': scenario_results,
        'sensitivity': sensitivity,
    })
