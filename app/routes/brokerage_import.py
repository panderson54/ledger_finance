"""
Brokerage import routes:
  GET  /brokerage-import
  POST /api/brokerage-import/preview
  POST /api/brokerage-import/commit
"""
import json
import logging

from flask import redirect, render_template, request, jsonify, url_for

from app.routes import main_bp
from app.routes.helpers import _bad_request, _get_app_setting, _get_anthropic_api_key, _parse_month_str
from app.models import Account, Holding, HoldingAllocation, AccountSnapshot, ImportLog
from app import db
from app.brokerage_import import build_preview, commit_import

logger = logging.getLogger(__name__)

_MODELS = {
    'Account': Account, 'Holding': Holding, 'HoldingAllocation': HoldingAllocation,
    'AccountSnapshot': AccountSnapshot, 'ImportLog': ImportLog,
}


def _valid_upload():
    file = request.files.get('file')
    if not file or not file.filename:
        return None, _bad_request('file is required')
    if not file.filename.lower().endswith(('.csv', '.pdf')):
        return None, _bad_request('Only .csv or .pdf files are supported')
    return file, None


@main_bp.route('/brokerage-import')
def brokerage_import_page():
    return redirect(url_for('main.import_data') + '?tab=brokerage')


@main_bp.route('/api/brokerage-import/preview', methods=['POST'])
def api_brokerage_import_preview():
    """Parse an uploaded CSV/PDF and return a preview without writing to the DB."""
    file, err = _valid_upload()
    if err:
        return err
    result = build_preview(db, _MODELS, file.read(), file.filename)
    logger.info('Brokerage import preview requested: filename=%s', file.filename)
    return jsonify(result)


@main_bp.route('/api/brokerage-import/commit', methods=['POST'])
def api_brokerage_import_commit():
    """Commit a previously-previewed CSV/PDF: writes Holding + AccountSnapshot rows."""
    file, err = _valid_upload()
    if err:
        return err

    month_date = _parse_month_str(request.form.get('month', ''))
    if month_date is None:
        return _bad_request('month is required (YYYY-MM)')

    try:
        mapping_raw = json.loads(request.form.get('mapping') or '{}')
        if not isinstance(mapping_raw, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _bad_request('mapping must be a JSON object')
    mapping = {k: (int(v) if v not in (None, '') else None) for k, v in mapping_raw.items()}

    ai_enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    api_key = _get_anthropic_api_key() if ai_enabled else ''

    result = commit_import(db, _MODELS, file.read(), file.filename, mapping, month_date, ai_enabled, api_key)

    if not result['success']:
        status = 500 if result.get('error_type') == 'server' else 400
        return jsonify(result), status
    return jsonify(result)
