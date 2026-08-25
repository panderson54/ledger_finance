"""
Brokerage import routes:
  GET  /brokerage-import
  POST /api/brokerage-import/preview      → starts background parse, returns job_id
  GET  /api/brokerage-import/preview/<id> → poll for parse result
  POST /api/brokerage-import/commit
"""
import json
import logging
import threading
import time
import uuid

from flask import current_app, redirect, render_template, request, jsonify, url_for

from app.routes import main_bp
from app.routes.helpers import _bad_request, _get_app_setting, _get_anthropic_api_key, _parse_month_str
from app.models import (
    Account, Holding, HoldingAllocation, AccountSnapshot, ImportLog,
    TransactionCategory, BankTransaction, SpendingEntry,
)
from app import db
from app.brokerage_import import build_preview, commit_import

logger = logging.getLogger(__name__)

# In-memory store for background PDF-parse jobs keyed by UUID.
# Jobs older than 1 hour are pruned on each new request.
_preview_jobs: dict[str, dict] = {}
_preview_jobs_lock = threading.Lock()


def _cleanup_old_jobs() -> None:
    cutoff = time.monotonic() - 3600
    with _preview_jobs_lock:
        stale = [k for k, v in _preview_jobs.items() if v.get('created_at', 0) < cutoff]
        for k in stale:
            del _preview_jobs[k]


_MODELS = {
    'Account': Account, 'Holding': Holding, 'HoldingAllocation': HoldingAllocation,
    'AccountSnapshot': AccountSnapshot, 'ImportLog': ImportLog,
    'TransactionCategory': TransactionCategory, 'BankTransaction': BankTransaction,
    'SpendingEntry': SpendingEntry,
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
    """Start a background parse job; returns {job_id} immediately so the client can poll."""
    file, err = _valid_upload()
    if err:
        return err

    file_bytes = file.read()
    filename = file.filename
    job_id = str(uuid.uuid4())

    expense_ai_enabled = _get_app_setting('claude_expense_categorization_enabled', 'false') == 'true'
    expense_api_key = _get_anthropic_api_key() if expense_ai_enabled else ''

    with _preview_jobs_lock:
        _preview_jobs[job_id] = {'status': 'pending', 'created_at': time.monotonic()}

    app = current_app._get_current_object()

    def _run():
        with app.app_context():
            try:
                result = build_preview(db, _MODELS, file_bytes, filename, expense_ai_enabled, expense_api_key)
                with _preview_jobs_lock:
                    _preview_jobs[job_id].update({'status': 'done', 'result': result})
            except Exception as e:
                logger.warning('Brokerage import preview failed: filename=%s error=%s', filename, e)
                with _preview_jobs_lock:
                    _preview_jobs[job_id].update(
                        {'status': 'error', 'message': f'Could not process this file: {e}'}
                    )

    threading.Thread(target=_run, daemon=True).start()
    _cleanup_old_jobs()
    return jsonify({'job_id': job_id, 'status': 'pending'})


@main_bp.route('/api/brokerage-import/preview/<job_id>', methods=['GET'])
def api_brokerage_import_preview_status(job_id):
    """Poll endpoint for a background preview parse job."""
    with _preview_jobs_lock:
        job = dict(_preview_jobs.get(job_id) or {})

    if not job:
        return jsonify({'status': 'not_found'}), 404
    if job['status'] == 'pending':
        return jsonify({'status': 'pending'})
    if job['status'] == 'error':
        return jsonify({'status': 'error', 'errors': [job['message']], 'accounts': [], 'warnings': []})
    # Done — clean up and return result.
    with _preview_jobs_lock:
        _preview_jobs.pop(job_id, None)
    return jsonify({'status': 'done', 'result': job['result']})


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

    try:
        transaction_categories_raw = json.loads(request.form.get('transaction_categories') or '{}')
        if not isinstance(transaction_categories_raw, dict):
            raise ValueError
    except (ValueError, TypeError):
        return _bad_request('transaction_categories must be a JSON object')
    transaction_categories = {
        k: (int(v) if v not in (None, '') else None) for k, v in transaction_categories_raw.items()
    }

    ai_enabled = _get_app_setting('claude_classification_enabled', 'false') == 'true'
    api_key = _get_anthropic_api_key() if ai_enabled else ''

    try:
        result = commit_import(db, _MODELS, file.read(), file.filename, mapping, month_date, ai_enabled, api_key,
                                transaction_categories)
    except Exception as e:
        logger.warning('Brokerage import commit failed: filename=%s error=%s', file.filename, e)
        return jsonify({'success': False, 'error': f'Could not process this file: {e}', 'error_type': 'server'}), 500

    if not result['success']:
        status = 500 if result.get('error_type') == 'server' else 400
        return jsonify(result), status
    return jsonify(result)
