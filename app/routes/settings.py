"""
Settings routes:
  /settings, /api/settings, /api/classifications, /api/classify/*
"""
import logging

from flask import render_template, jsonify, request
from sqlalchemy import func

from app.routes import main_bp
from app.routes.helpers import (
    _get_app_setting, _set_app_setting, _get_api_key_and_check_enabled,
    _bad_request, _not_found, _transaction_category_to_dict,
)
from app.models import TickerClassification, TransactionCategory, BankTransaction
from app import db

logger = logging.getLogger(__name__)

VALID_CATEGORY_KINDS = ('income', 'expense', 'transfer')


@main_bp.route('/settings')
def settings_page():
    """App settings: AI classification toggle and API key management."""
    enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    expense_categorization_enabled = _get_app_setting('claude_expense_categorization_enabled', 'false') == 'true'
    api_key_set = bool(_get_app_setting('anthropic_api_key'))
    snapshot_timing = _get_app_setting('snapshot_timing', 'end_of_month')
    show_rental = _get_app_setting('show_rental_income', 'false') == 'true'
    transaction_categories = TransactionCategory.query.order_by(
        TransactionCategory.display_order, TransactionCategory.id
    ).all()
    return render_template('settings.html', classification_enabled=enabled, api_key_set=api_key_set,
                           snapshot_timing=snapshot_timing, show_rental_income=show_rental,
                           expense_categorization_enabled=expense_categorization_enabled,
                           transaction_categories=[_transaction_category_to_dict(c) for c in transaction_categories])


@main_bp.route('/api/classifications')
def api_classifications_list():
    """List all cached ticker classifications (for the settings page)."""
    rows = TickerClassification.query.order_by(TickerClassification.ticker).all()
    return jsonify([{
        'ticker':          r.ticker,
        'asset_class':     r.asset_class,
        'market_cap_tilt': r.market_cap_tilt,
        'sector_weights':  r.weights_dict(),
        'source':          r.source,
        'classified_at':   r.classified_at.isoformat() if r.classified_at else None,
    } for r in rows])


@main_bp.route('/api/settings', methods=['POST'])
def api_settings_save():
    """
    Save application settings. Accepts JSON body with optional fields:
        classification_enabled  bool
        anthropic_api_key       str  (blank = keep existing key unchanged)
    """
    data = request.get_json(silent=True) or {}

    if 'classification_enabled' in data:
        val = 'true' if data['classification_enabled'] else 'false'
        _set_app_setting('claude_classification_enabled', val,
                         'Enable AI ticker classification via Claude API')

    if 'anthropic_api_key' in data:
        key = (data['anthropic_api_key'] or '').strip()
        if key:  # blank means "keep existing"
            _set_app_setting('anthropic_api_key', key,
                             'Anthropic API key for ticker classification')

    if 'snapshot_timing' in data:
        val = data['snapshot_timing']
        if val in ('start_of_month', 'end_of_month'):
            _set_app_setting('snapshot_timing', val,
                             'Whether monthly snapshots represent start or end of month for charting')

    if 'show_rental_income' in data:
        val = 'true' if data['show_rental_income'] else 'false'
        _set_app_setting('show_rental_income', val, 'Show Real Estate rental income section on Passive Income tab')

    if 'expense_categorization_enabled' in data:
        val = 'true' if data['expense_categorization_enabled'] else 'false'
        _set_app_setting('claude_expense_categorization_enabled', val,
                         'Enable AI categorization of checking/savings statement transactions via Claude API')

    logger.info('Settings saved: classification_enabled=%s',
                _get_app_setting('claude_classification_enabled', 'false'))
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# CRUD API: TransactionCategory
# ---------------------------------------------------------------------------

@main_bp.route('/api/transaction-categories', methods=['GET'])
def api_transaction_categories_list():
    """List all transaction categories ordered by display_order, id."""
    rows = TransactionCategory.query.order_by(
        TransactionCategory.display_order, TransactionCategory.id
    ).all()
    return jsonify([_transaction_category_to_dict(r) for r in rows])


@main_bp.route('/api/transaction-categories', methods=['POST'])
def api_transaction_category_create():
    """Create a new transaction category."""
    data = request.get_json()
    if not data:
        return _bad_request('Missing request body')

    title = (data.get('title') or '').strip()
    if not title:
        return _bad_request('title is required')
    if TransactionCategory.query.filter(func.lower(TransactionCategory.title) == title.lower()).first():
        return _bad_request('A category with that title already exists.')

    kind = data.get('kind', '')
    if kind not in VALID_CATEGORY_KINDS:
        return _bad_request('kind must be income, expense, or transfer')

    category = TransactionCategory(
        title=title,
        kind=kind,
        description=(data.get('description') or '').strip() or None,
        is_active=bool(data.get('is_active', True)),
        display_order=int(data.get('display_order', 0)),
    )
    db.session.add(category)
    db.session.commit()
    logger.info('Transaction category created: id=%d title=%s kind=%s', category.id, category.title, category.kind)
    return jsonify(_transaction_category_to_dict(category)), 201


@main_bp.route('/api/transaction-categories/<int:category_id>', methods=['PUT'])
def api_transaction_category_update(category_id):
    """Update a transaction category."""
    category = db.session.get(TransactionCategory, category_id)
    if category is None:
        return _not_found('Transaction category')

    data = request.get_json()
    if not data:
        return _bad_request('Missing request body')

    if 'title' in data:
        title = (data['title'] or '').strip()
        if not title:
            return _bad_request('title cannot be empty')
        if TransactionCategory.query.filter(
            func.lower(TransactionCategory.title) == title.lower(), TransactionCategory.id != category_id
        ).first():
            return _bad_request('A category with that title already exists.')
        category.title = title

    if 'kind' in data:
        if data['kind'] not in VALID_CATEGORY_KINDS:
            return _bad_request('kind must be income, expense, or transfer')
        category.kind = data['kind']

    if 'description' in data:
        category.description = (data['description'] or '').strip() or None
    if 'is_active' in data:
        category.is_active = bool(data['is_active'])
    if 'display_order' in data:
        category.display_order = int(data['display_order'])

    db.session.commit()
    logger.info('Transaction category updated: id=%d', category_id)
    return jsonify(_transaction_category_to_dict(category))


@main_bp.route('/api/transaction-categories/<int:category_id>', methods=['DELETE'])
def api_transaction_category_delete(category_id):
    """Delete a transaction category. Refuses if any transactions still reference it."""
    category = db.session.get(TransactionCategory, category_id)
    if category is None:
        return _not_found('Transaction category')

    if BankTransaction.query.filter_by(category_id=category_id).first():
        return _bad_request('Cannot delete a category that has transactions — deactivate it instead.')

    db.session.delete(category)
    db.session.commit()
    logger.info('Transaction category deleted: id=%d', category_id)
    return jsonify({'success': True})


@main_bp.route('/api/classify/<ticker>')
def api_classify_ticker(ticker):
    """
    Classify a ticker's asset class, market cap tilt, and allocation weights.
    Results are cached in ticker_classifications table.

    Query params:
        web_search=1   Enable Claude web search for obscure tickers
        force=1        Bypass DB cache and re-classify

    Response 200: {ticker, asset_class, market_cap_tilt, sector_weights, source, from_cache}
    Response 503: {error, manual_required: true}  — feature disabled or API unavailable
    """
    from app.classification_service import get_or_classify

    api_key, err = _get_api_key_and_check_enabled()
    if err:
        return err

    ticker = ticker.strip().upper()
    if not ticker:
        return jsonify({'error': 'ticker required'}), 400

    use_web_search = request.args.get('web_search', '').lower() in ('1', 'true')

    if request.args.get('force', '').lower() in ('1', 'true'):
        existing = TickerClassification.query.filter_by(ticker=ticker).first()
        if existing:
            db.session.delete(existing)
            db.session.commit()

    try:
        result, from_cache = get_or_classify(ticker, api_key, use_web_search=use_web_search)
        result['from_cache'] = from_cache
        return jsonify(result)
    except RuntimeError as e:
        logger.warning('Classification unavailable: ticker=%s error=%s', ticker, e)
        return jsonify({'error': str(e), 'manual_required': True}), 503
    except ValueError as e:
        logger.error('Classification parse error: ticker=%s error=%s', ticker, e)
        return jsonify({'error': str(e), 'manual_required': True}), 503
    except Exception as e:
        logger.error('Classification API error: ticker=%s error=%s', ticker, e)
        return jsonify({'error': f'Classification service error: {e}', 'manual_required': True}), 503
