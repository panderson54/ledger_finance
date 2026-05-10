"""
Passive income routes:
  /api/passive-income, /api/dividend-data/*, /api/rental-income, /api/portfolio-income
"""
import logging
import os
from datetime import datetime

from flask import jsonify, request

from app.routes import main_bp
from app.routes.helpers import (
    _get_app_setting,
    _set_app_setting,
    _get_api_key_and_check_enabled,
    _rental_to_dict,
)
from app.models import Account, AccountSnapshot, Holding, DividendData, RentalProperty
from app import db
from app.account_categories import INVESTMENT_CATS
from sqlalchemy import func

logger = logging.getLogger(__name__)

PORTFOLIO_INCOME_CATS = {'savings', 'cash', 'checking'}


# ---------------------------------------------------------------------------
# Dividend / Passive Income API
# ---------------------------------------------------------------------------

@main_bp.route('/api/dividend-data/<ticker>')
def api_dividend_data_get(ticker):
    """
    Fetch (or return cached) dividend data for a single ticker.
    Query params:
        force=1   bypass 30-day cache and re-fetch from Claude
    Response 200: dividend data dict + from_cache bool
    Response 503: AI disabled or API key missing
    """
    from app.dividend_service import get_or_fetch

    api_key, err = _get_api_key_and_check_enabled()
    if err:
        return err

    ticker = ticker.strip().upper()
    force  = request.args.get('force', '').lower() in ('1', 'true')

    try:
        data, from_cache = get_or_fetch(ticker, api_key, force=force)
        data['from_cache'] = from_cache
        return jsonify(data)
    except RuntimeError as e:
        return jsonify({'error': str(e), 'manual_required': True}), 503
    except Exception as e:
        logger.error('Dividend data fetch error: ticker=%s error=%s', ticker, e)
        return jsonify({'error': f'Dividend service error: {e}'}), 500


@main_bp.route('/api/dividend-data/<ticker>', methods=['POST'])
def api_dividend_data_upsert(ticker):
    """
    Manually upsert dividend data for a ticker (bypasses Claude).
    Useful for overriding AI-fetched values.
    Request JSON: {annual_yield, dividend_per_share, frequency, payer_type,
                   is_dividend_payer, tax_treatment}
    Response 200: persisted row as dict
    Response 400: validation error
    """
    ticker = ticker.strip().upper()
    body   = request.get_json(silent=True) or {}

    annual_yield = body.get('annual_yield')
    if annual_yield is not None:
        try:
            annual_yield = float(annual_yield)
        except (TypeError, ValueError):
            return jsonify({'error': 'annual_yield must be a number'}), 400
        if annual_yield < 0 or annual_yield > 1:
            return jsonify({'error': 'annual_yield must be a decimal between 0 and 1 (e.g. 0.035 for 3.5%)'}), 400

    row = DividendData.query.filter_by(ticker=ticker).first()
    if not row:
        row = DividendData(ticker=ticker)
        db.session.add(row)

    if annual_yield is not None:
        row.annual_yield = annual_yield
    if 'dividend_per_share' in body:
        row.dividend_per_share = body['dividend_per_share']
    if 'frequency' in body:
        row.frequency = body['frequency'] or None
    if 'payer_type' in body:
        row.payer_type = body['payer_type'] or None
    if 'is_dividend_payer' in body:
        row.is_dividend_payer = bool(body['is_dividend_payer'])
    if 'tax_treatment' in body:
        row.tax_treatment = body['tax_treatment'] or None

    row.last_fetched_at = datetime.utcnow()
    db.session.commit()

    from app.dividend_service import _row_to_dict
    return jsonify(_row_to_dict(row))


@main_bp.route('/api/passive-income')
def api_passive_income():
    """
    Compute current annual/monthly passive income across all active holdings.
    Auto-fetches missing/stale dividend data via Claude Haiku.
    Query params:
        account_id=<int>   filter to a single account (optional)
    Response 200: {by_holding, by_account, total_annual_income, total_monthly_income,
                   est_after_tax_annual, tax_rate_used, missing_data}
    """
    from app.dividend_service import get_or_fetch, is_dividend_stale
    from app.dividend_calc import calculate_current_income, DEFAULT_TAX_RATE

    account_id_filter = request.args.get('account_id', type=int)
    tax_rate = float(_get_app_setting('drip_tax_rate_pct', str(DEFAULT_TAX_RATE * 100))) / 100

    q = Holding.query.filter_by(is_active=True)
    if account_id_filter:
        q = q.filter_by(account_id=account_id_filter)
    holdings = q.all()

    if not holdings:
        return jsonify({
            'by_holding': [], 'by_account': [],
            'total_annual_income': 0.0, 'total_monthly_income': 0.0,
            'est_after_tax_annual': 0.0, 'tax_rate_used': tax_rate,
            'missing_data': [],
        })

    ai_enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    api_key    = _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '') if ai_enabled else ''

    # Build account lookup for tax_status
    account_ids = {h.account_id for h in holdings}
    accounts    = {a.id: a for a in Account.query.filter(Account.id.in_(account_ids)).all()}

    missing_data = []
    holdings_data = []

    for h in holdings:
        acct = accounts.get(h.account_id)

        # Get or fetch dividend data
        cached = DividendData.query.filter_by(ticker=h.ticker).first()
        if ai_enabled and api_key and (not cached or is_dividend_stale(cached.last_fetched_at)):
            try:
                div_dict, _ = get_or_fetch(h.ticker, api_key)
            except Exception as e:
                logger.warning('Could not fetch dividend data for %s: %s', h.ticker, e)
                div_dict = None
        elif cached:
            from app.dividend_service import _row_to_dict
            div_dict = _row_to_dict(cached)
        else:
            div_dict = None

        if div_dict is None or div_dict.get('fetch_error'):
            missing_data.append(h.ticker)
            annual_yield   = 0.0
            is_payer       = False
            frequency      = None
            payer_type     = None
            tax_treatment  = None
        else:
            annual_yield   = float(div_dict.get('annual_yield') or 0)
            is_payer       = bool(div_dict.get('is_dividend_payer', True))
            frequency      = div_dict.get('frequency')
            payer_type     = div_dict.get('payer_type')
            tax_treatment  = div_dict.get('tax_treatment')

        holdings_data.append({
            'ticker':             h.ticker,
            'shares':             float(h.shares or 0),
            'last_price':         float(h.last_price or 0),
            'annual_yield':       annual_yield,
            'is_dividend_payer':  is_payer,
            'frequency':          frequency,
            'payer_type':         payer_type,
            'tax_treatment':      tax_treatment,
            'account_id':         h.account_id,
            'account_name':       acct.name if acct else 'Unknown',
            'account_tax_status': acct.tax_status if acct else 'taxable',
        })

    result = calculate_current_income(holdings_data, tax_rate=tax_rate)
    result['missing_data'] = missing_data

    # Account-level dividend estimates (investment accounts with expected_dividend_yield set)
    est_accounts = (
        Account.query
        .filter(Account.category.in_(INVESTMENT_CATS))
        .filter(Account.is_active == True)
        .filter(Account.expected_dividend_yield.isnot(None))
        .filter(Account.expected_dividend_yield > 0)
        .order_by(Account.name)
        .all()
    )
    est_ids = [a.id for a in est_accounts]
    est_latest_dates = dict(
        db.session.query(AccountSnapshot.account_id, func.max(AccountSnapshot.snapshot_date))
        .filter(AccountSnapshot.account_id.in_(est_ids))
        .group_by(AccountSnapshot.account_id)
        .all()
    ) if est_ids else {}
    est_balances = {}
    for acct_id, max_date in est_latest_dates.items():
        snap = AccountSnapshot.query.filter_by(account_id=acct_id, snapshot_date=max_date).first()
        if snap:
            est_balances[acct_id] = float(snap.balance)

    account_estimates = []
    for a in est_accounts:
        balance = est_balances.get(a.id, 0.0)
        edy     = float(a.expected_dividend_yield or 0)
        annual  = balance * edy
        account_estimates.append({
            'account_id':       a.id,
            'account_name':     a.name,
            'institution':      a.institution,
            'category':         a.category,
            'balance':          balance,
            'expected_yield':   edy,
            'annual_income':    annual,
            'monthly_income':   annual / 12,
            'edit_url':         f'/accounts/{a.id}/edit',
        })
    result['account_estimates'] = account_estimates
    result['account_estimates_annual'] = sum(e['annual_income'] for e in account_estimates)

    return jsonify(result)


@main_bp.route('/api/passive-income/projection')
def api_passive_income_projection():
    """
    Run a DRIP simulation for all active holdings.
    Query params (all optional; fall back to app_settings then defaults):
        price_appreciation_rate  float  e.g. 7.0 (percent)
        dividend_growth_rate     float  e.g. 3.0 (percent)
        monthly_contribution     float  e.g. 500 (dollars)
        horizon_years            int    e.g. 20 (max 30)
    Response 200: {labels, drip_on, drip_off, no_action, callouts, assumptions}
    """
    from app.dividend_service import _row_to_dict
    from app.dividend_calc import simulate_drip

    def _param(name, setting_key, default):
        raw = request.args.get(name)
        if raw is not None:
            try:
                val = float(raw)
                _set_app_setting(setting_key, str(val), f'DRIP projection: {name}')
                return val
            except (TypeError, ValueError):
                pass
        stored = _get_app_setting(setting_key)
        if stored:
            try:
                return float(stored)
            except ValueError:
                pass
        return default

    price_appreciation = _param('price_appreciation_rate', 'drip_price_appreciation_pct', 7.0) / 100
    dividend_growth    = _param('dividend_growth_rate',    'drip_dividend_growth_pct',    3.0) / 100
    monthly_contrib    = _param('monthly_contribution',    'drip_monthly_contribution',   0.0)
    horizon_years      = int(min(request.args.get('horizon_years', 20, type=int), 30))

    holdings = Holding.query.filter_by(is_active=True).all()
    holdings_data = []
    for h in holdings:
        cached = DividendData.query.filter_by(ticker=h.ticker).first()
        div_dict = _row_to_dict(cached) if cached else {}
        holdings_data.append({
            'ticker':            h.ticker,
            'shares':            float(h.shares or 0),
            'last_price':        float(h.last_price or 0),
            'annual_yield':      float(div_dict.get('annual_yield') or 0),
            'is_dividend_payer': bool(div_dict.get('is_dividend_payer', False)) if cached else False,
            'frequency':         div_dict.get('frequency'),
        })

    result = simulate_drip(
        holdings_data,
        horizon_years=horizon_years,
        price_appreciation_rate=price_appreciation,
        dividend_growth_rate=dividend_growth,
        monthly_contribution=monthly_contrib,
    )
    result['assumptions'] = {
        'price_appreciation_rate': price_appreciation * 100,
        'dividend_growth_rate':    dividend_growth * 100,
        'monthly_contribution':    monthly_contrib,
        'horizon_years':           horizon_years,
    }
    return jsonify(result)


# ---------------------------------------------------------------------------
# Portfolio Income (savings/cash accounts with APY set)
# ---------------------------------------------------------------------------

@main_bp.route('/api/portfolio-income', methods=['GET'])
def api_portfolio_income_list():
    """
    Return all active savings/cash/checking accounts that have an APY set,
    combined with their latest snapshot balance to compute interest income.
    """
    savings_accounts = (
        Account.query
        .filter(Account.category.in_(PORTFOLIO_INCOME_CATS))
        .filter(Account.is_active == True)
        .filter(Account.apy.isnot(None))
        .filter(Account.apy > 0)
        .order_by(Account.name)
        .all()
    )

    account_ids = [a.id for a in savings_accounts]
    latest_dates = dict(
        db.session.query(AccountSnapshot.account_id, func.max(AccountSnapshot.snapshot_date))
        .filter(AccountSnapshot.account_id.in_(account_ids))
        .group_by(AccountSnapshot.account_id)
        .all()
    ) if account_ids else {}
    latest_balances = {}
    for acct_id, max_date in latest_dates.items():
        snap = AccountSnapshot.query.filter_by(account_id=acct_id, snapshot_date=max_date).first()
        if snap:
            latest_balances[acct_id] = float(snap.balance)

    rows = []
    for a in savings_accounts:
        balance = latest_balances.get(a.id, 0.0)
        apy     = float(a.apy or 0)
        annual  = balance * apy
        rows.append({
            'id':               a.id,
            'name':             a.name,
            'institution':      a.institution,
            'category':         a.category,
            'balance':          balance,
            'apy':              apy,
            'annual_interest':  annual,
            'monthly_interest': annual / 12,
            'edit_url':         f'/accounts/{a.id}/edit',
        })

    total_annual  = sum(r['annual_interest']  for r in rows)
    total_monthly = sum(r['monthly_interest'] for r in rows)
    return jsonify({'accounts': rows, 'total_annual': total_annual, 'total_monthly': total_monthly})


# ---------------------------------------------------------------------------
# Rental Income (real estate)
# ---------------------------------------------------------------------------

@main_bp.route('/api/rental-income', methods=['GET'])
def api_rental_income_list():
    properties = RentalProperty.query.filter_by(is_active=True).order_by(RentalProperty.name).all()
    rows = [_rental_to_dict(p) for p in properties]
    total_annual  = sum(r['annual_income']    for r in rows)
    total_monthly = sum(r['effective_monthly'] for r in rows)
    return jsonify({'properties': rows, 'total_annual': total_annual, 'total_monthly': total_monthly})


@main_bp.route('/api/rental-income', methods=['POST'])
def api_rental_income_create():
    data = request.get_json(force=True)
    prop = RentalProperty(
        name=         data.get('name', '').strip(),
        address=      data.get('address', '').strip() or None,
        monthly_rent= float(data.get('monthly_rent') or 0),
        vacancy_rate= float(data.get('vacancy_rate') or 5) / 100,  # UI sends percent, store decimal
        notes=        data.get('notes', '').strip() or None,
    )
    if not prop.name:
        return jsonify({'error': 'Name is required'}), 400
    db.session.add(prop)
    db.session.commit()
    return jsonify(_rental_to_dict(prop)), 201


@main_bp.route('/api/rental-income/<int:prop_id>', methods=['PUT'])
def api_rental_income_update(prop_id):
    prop = RentalProperty.query.get_or_404(prop_id)
    data = request.get_json(force=True)
    if 'name' in data:
        prop.name = data['name'].strip()
    if 'address' in data:
        prop.address = data['address'].strip() or None
    if 'monthly_rent' in data:
        prop.monthly_rent = float(data['monthly_rent'] or 0)
    if 'vacancy_rate' in data:
        prop.vacancy_rate = float(data['vacancy_rate'] or 0) / 100
    if 'notes' in data:
        prop.notes = data['notes'].strip() or None
    if not prop.name:
        return jsonify({'error': 'Name is required'}), 400
    db.session.commit()
    return jsonify(_rental_to_dict(prop))


@main_bp.route('/api/rental-income/<int:prop_id>', methods=['DELETE'])
def api_rental_income_delete(prop_id):
    prop = RentalProperty.query.get_or_404(prop_id)
    db.session.delete(prop)
    db.session.commit()
    return jsonify({'ok': True})
