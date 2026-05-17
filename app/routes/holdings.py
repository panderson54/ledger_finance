"""
Holdings and price API routes:
  /api/accounts/<id>/holdings, /api/holdings/<id>, /api/prices/*
"""
import logging
import os

from flask import jsonify, request

from app.routes import main_bp
from app.routes.helpers import _holding_to_dict, _bad_request, _not_found, _get_app_setting
from app.models import Account, Holding, HoldingAllocation
from app import db
from app.account_categories import ALLOCATION_CLASSES

_ALLOWED_IMAGE_TYPES = {'image/jpeg', 'image/png', 'image/webp', 'image/gif'}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Holdings CRUD API
# ---------------------------------------------------------------------------

@main_bp.route('/api/accounts/<int:account_id>/holdings')
def api_holdings_list(account_id):
    """List active holdings for an account."""
    account = db.session.get(Account, account_id)
    if account is None:
        return jsonify({'error': 'Account not found'}), 404
    holdings = (
        Holding.query
        .filter_by(account_id=account_id, is_active=True)
        .order_by(Holding.ticker)
        .all()
    )
    return jsonify([_holding_to_dict(h) for h in holdings])


@main_bp.route('/api/accounts/<int:account_id>/holdings', methods=['POST'])
def api_holding_create(account_id):
    """Create a new holding (+ allocation splits) for an account."""
    account = db.session.get(Account, account_id)
    if account is None:
        return jsonify({'error': 'Account not found'}), 404

    data = request.get_json(silent=True) or {}
    ticker = (data.get('ticker') or '').strip().upper()
    if not ticker:
        return jsonify({'error': 'ticker is required'}), 400

    try:
        shares = float(data.get('shares', 0))
        if shares < 0:
            raise ValueError
    except (ValueError, TypeError):
        return jsonify({'error': 'shares must be a non-negative number'}), 400

    allocs_raw: dict = data.get('allocations') or {}
    allocs = {}
    for cls in ALLOCATION_CLASSES:
        try:
            allocs[cls] = float(allocs_raw.get(cls, 0))
        except (ValueError, TypeError):
            allocs[cls] = 0.0
    alloc_sum = sum(allocs.values())
    if alloc_sum > 0 and abs(alloc_sum - 100.0) > 0.5:
        return jsonify({'error': f'Allocation percentages must sum to 100 (got {alloc_sum:.1f})'}), 400

    holding = Holding(
        account_id=account_id,
        ticker=ticker,
        name=data.get('name') or ticker,
        shares=shares,
        cap_class=data.get('cap_class') or None,
    )
    db.session.add(holding)
    db.session.flush()  # populate holding.id

    # Fetch price immediately so the holding has a live value right away
    try:
        from app.price_service import get_price
        from datetime import datetime, timezone
        price, fetched_name = get_price(ticker)
        holding.last_price = price
        holding.name = fetched_name
        holding.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
    except Exception as e:
        logger.warning('Price fetch on holding create failed: ticker=%s error=%s', ticker, e)

    for cls, pct in allocs.items():
        if pct > 0:
            db.session.add(HoldingAllocation(holding_id=holding.id, asset_class=cls, percentage=pct))

    db.session.commit()
    logger.info('Holding created: account_id=%d ticker=%s shares=%s price=%s',
                account_id, ticker, shares, holding.last_price)
    return jsonify(_holding_to_dict(holding)), 201


@main_bp.route('/api/holdings/<int:holding_id>', methods=['PUT'])
def api_holding_update(holding_id):
    """Update shares and/or allocation splits for a holding."""
    holding = db.session.get(Holding, holding_id)
    if holding is None:
        return jsonify({'error': 'Holding not found'}), 404

    data = request.get_json(silent=True) or {}

    if 'shares' in data:
        try:
            holding.shares = float(data['shares'])
        except (ValueError, TypeError):
            return jsonify({'error': 'shares must be a number'}), 400

    if 'name' in data:
        holding.name = (data['name'] or '').strip() or holding.ticker

    if 'cap_class' in data:
        holding.cap_class = data['cap_class'] or None

    if 'allocations' in data:
        allocs_raw = data['allocations'] or {}
        allocs = {}
        for cls in ALLOCATION_CLASSES:
            try:
                allocs[cls] = float(allocs_raw.get(cls, 0))
            except (ValueError, TypeError):
                allocs[cls] = 0.0
        alloc_sum = sum(allocs.values())
        if alloc_sum > 0 and abs(alloc_sum - 100.0) > 0.5:
            return jsonify({'error': f'Allocation percentages must sum to 100 (got {alloc_sum:.1f})'}), 400
        HoldingAllocation.query.filter_by(holding_id=holding.id).delete()
        for cls, pct in allocs.items():
            if pct > 0:
                db.session.add(HoldingAllocation(holding_id=holding.id, asset_class=cls, percentage=pct))

    db.session.commit()
    logger.info('Holding updated: id=%d', holding_id)
    return jsonify(_holding_to_dict(holding))


@main_bp.route('/api/holdings/<int:holding_id>', methods=['DELETE'])
def api_holding_archive(holding_id):
    """Archive (soft-delete) a holding."""
    holding = db.session.get(Holding, holding_id)
    if holding is None:
        return jsonify({'error': 'Holding not found'}), 404
    holding.is_active = False
    db.session.commit()
    logger.info('Holding archived: id=%d ticker=%s', holding_id, holding.ticker)
    return jsonify({'success': True})


# ---------------------------------------------------------------------------
# Price service API
# ---------------------------------------------------------------------------

@main_bp.route('/api/prices/<ticker>')
def api_price_lookup(ticker):
    """
    Fetch current price + name for a single ticker.
    Used by the holdings add-row UI to auto-populate price and name on blur.
    """
    from app.price_service import get_price
    ticker = ticker.strip().upper()
    if not ticker:
        return jsonify({'error': 'ticker required'}), 400
    try:
        price, name = get_price(ticker)
        return jsonify({'ticker': ticker, 'price': price, 'name': name})
    except ValueError as e:
        return jsonify({'error': str(e)}), 404
    except Exception as e:
        logger.warning('Price fetch failed: ticker=%s error=%s', ticker, e)
        return jsonify({'error': f'Price fetch failed: {e}'}), 502


@main_bp.route('/api/prices/refresh', methods=['POST'])
def api_prices_refresh():
    """
    Refresh prices for all active holdings.
    Skips holdings whose last_fetched is within the last 24 hours.
    Returns {updated, skipped, failed, errors}.
    """
    from app.price_service import get_price, is_stale
    from datetime import datetime, timezone

    holdings = Holding.query.filter_by(is_active=True).all()
    updated = skipped = failed = 0
    errors = []

    for h in holdings:
        if not is_stale(h.last_fetched):
            skipped += 1
            continue
        try:
            price, name = get_price(h.ticker)
            h.last_price = price
            h.name = name
            h.last_fetched = datetime.now(timezone.utc).replace(tzinfo=None)
            updated += 1
        except Exception as e:
            failed += 1
            errors.append({'ticker': h.ticker, 'error': str(e)})
            logger.warning('Price refresh failed: ticker=%s error=%s', h.ticker, e)

    db.session.commit()
    logger.info('Price refresh: updated=%d skipped=%d failed=%d', updated, skipped, failed)
    return jsonify({'updated': updated, 'skipped': skipped, 'failed': failed, 'errors': errors})


# ---------------------------------------------------------------------------
# Screenshot import API
# ---------------------------------------------------------------------------

@main_bp.route('/api/accounts/<int:account_id>/holdings/import-screenshot', methods=['POST'])
def api_import_holdings_screenshot(account_id):
    """
    Accept a brokerage screenshot image and return extracted {ticker, shares} pairs.
    Does not write to the database — the frontend previews and confirms each holding.
    """
    account = db.session.get(Account, account_id)
    if account is None:
        return _not_found('account')

    api_key = _get_app_setting('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        return jsonify({'error': 'Anthropic API key not configured'}), 503

    file = request.files.get('image')
    if not file:
        return _bad_request('image file is required')

    mime_type = file.content_type or ''
    if mime_type not in _ALLOWED_IMAGE_TYPES:
        return _bad_request(f'unsupported image type: {mime_type}')

    image_bytes = file.read()
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        return _bad_request('image file exceeds 10 MB limit')

    try:
        from app.holdings_import_service import extract_holdings_from_image
        holdings = extract_holdings_from_image(image_bytes, mime_type, api_key)
    except (ValueError, RuntimeError) as exc:
        logger.warning('Screenshot holdings extraction failed: %s', exc)
        return jsonify({'error': str(exc)}), 500

    logger.info('Screenshot import: account_id=%d extracted=%d holdings', account_id, len(holdings))
    return jsonify({'holdings': holdings})
