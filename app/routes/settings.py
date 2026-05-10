"""
Settings routes:
  /settings, /api/settings, /api/classifications, /api/classify/*
"""
import logging
import os

from flask import render_template, jsonify, request

from app.routes import main_bp
from app.routes.helpers import _get_app_setting, _set_app_setting
from app.models import TickerClassification
from app import db

logger = logging.getLogger(__name__)


@main_bp.route('/settings')
def settings_page():
    """App settings: AI classification toggle and API key management."""
    enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    api_key_set = bool(_get_app_setting('anthropic_api_key'))
    snapshot_timing = _get_app_setting('snapshot_timing', 'end_of_month')
    show_rental = _get_app_setting('show_rental_income', 'false') == 'true'
    return render_template('settings.html', classification_enabled=enabled, api_key_set=api_key_set,
                           snapshot_timing=snapshot_timing, show_rental_income=show_rental)


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

    logger.info('Settings saved: classification_enabled=%s',
                _get_app_setting('claude_classification_enabled', 'false'))
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

    if _get_app_setting('claude_classification_enabled', 'false') != 'true':
        return jsonify({'error': 'AI classification is disabled', 'manual_required': True}), 503

    api_key = _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'ANTHROPIC_API_KEY is not configured', 'manual_required': True}), 503

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
