"""
Import and export routes:
  /import, /export/csv, /api/import/preview
"""
import csv
import io
import logging
from datetime import datetime, date

from flask import render_template, jsonify, request, flash, redirect, url_for, make_response
from werkzeug.utils import secure_filename

from app.routes import main_bp
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric, ImportLog
from app import db
from app.import_processor import process_csv, preview_csv
from app.metrics_service import recalculate_metrics as _recalculate_metrics

logger = logging.getLogger(__name__)


@main_bp.route('/import', methods=['GET', 'POST'])
def import_data():
    """Data import interface — GET shows the form, POST processes the uploaded CSV."""
    if request.method == 'GET':
        from_onboarding = request.args.get('from') == 'onboarding'
        recent_logs = ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all()
        return render_template('import.html', recent_logs=recent_logs,
                               from_onboarding=from_onboarding)

    if 'csv_file' not in request.files or request.files['csv_file'].filename == '':
        return render_template('import.html',
                               error="No file selected.",
                               recent_logs=ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all())

    file = request.files['csv_file']
    filename = secure_filename(file.filename)

    if not filename.lower().endswith('.csv'):
        return render_template('import.html',
                               error="Only .csv files are supported.",
                               recent_logs=ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all())

    models = {
        'Account': Account,
        'AccountSnapshot': AccountSnapshot,
        'SpendingEntry': SpendingEntry,
        'CalculatedMetric': CalculatedMetric,
        'ImportLog': ImportLog,
    }

    file_stream = io.StringIO(file.read().decode('utf-8-sig'))
    logger.info('CSV import started: file=%s', filename)
    results = process_csv(file_stream, filename, db, models)

    # Recalculate metrics for every imported month (oldest first so each month's
    # change can reference the previous month's freshly-computed net worth).
    for month_date_str in results.get('month_dates', []):
        _recalculate_metrics(date.fromisoformat(month_date_str))

    if results.get('success'):
        logger.info(
            'CSV import complete: file=%s, accounts=%d, snapshots=%d, spending=%d, warnings=%d',
            filename, results['accounts_created'], results['snapshots_imported'],
            results['spending_imported'], len(results['warnings'])
        )
        if not results['warnings']:
            flash('Data imported successfully. Your dashboard is ready!', 'success')
            return redirect(url_for('main.index'))
    else:
        logger.error('CSV import failed: file=%s, errors=%s', filename, results['errors'])

    recent_logs = ImportLog.query.order_by(ImportLog.import_date.desc()).limit(10).all()
    return render_template('import.html', results=results, recent_logs=recent_logs)


@main_bp.route('/import/template')
def import_template():
    """
    Download a blank CSV template in the Ledger export format so it can be
    filled in and re-imported.  Columns match export_csv exactly:
        Account | Type | Category | Tax Status | Institution | Jan 'YY ... Dec 'YY
    Account rows are seeded from active accounts (or two example rows when
    there are none). All balance cells are blank. Income and Expenses rows
    are always included.
    """
    year = datetime.today().year
    month_labels = [
        datetime(year, m, 1).strftime("%b '") + str(year)[-2:]
        for m in range(1, 13)
    ]
    META_COLS = ['Account', 'Type', 'Category', 'Tax Status', 'Institution']
    blank = [''] * 12

    rows = [META_COLS + month_labels]

    accounts = Account.query.filter_by(is_active=True).order_by(Account.display_order, Account.name).all()
    if accounts:
        for acct in accounts:
            rows.append([
                acct.name,
                acct.account_type,
                acct.category,
                acct.tax_status or '',
                acct.institution or '',
            ] + blank)
    else:
        rows.append(['Example Checking', 'asset', 'checking', 'taxable', ''] + blank)
        rows.append(['Example 401k', 'asset', '401k', 'tax_deferred', ''] + blank)

    rows.append(['Income', '', '', '', ''] + blank)
    rows.append(['Expenses', '', '', '', ''] + blank)

    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    filename = f'ledger_template_{year}.csv'
    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'text/csv'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    return response


@main_bp.route('/export/csv')
def export_csv():
    """
    Export all account data as a Ledger-format CSV that can be re-imported
    into a fresh instance to restore full history.

    Query params:
        from  YYYY-MM  earliest month to include (default: all)
        to    YYYY-MM  latest month to include   (default: all)
    """
    from_str = request.args.get('from')
    to_str = request.args.get('to')

    # Collect all months that have snapshot or spending data
    snapshot_dates = {d for (d,) in db.session.query(AccountSnapshot.snapshot_date).distinct()}
    spending_dates = {d for (d,) in db.session.query(SpendingEntry.entry_date).distinct()}
    all_dates = sorted(snapshot_dates | spending_dates)

    if from_str:
        try:
            from_date = datetime.strptime(from_str, '%Y-%m').date().replace(day=1)
            all_dates = [d for d in all_dates if d >= from_date]
        except ValueError:
            pass
    if to_str:
        try:
            to_date = datetime.strptime(to_str, '%Y-%m').date().replace(day=1)
            all_dates = [d for d in all_dates if d <= to_date]
        except ValueError:
            pass

    if not all_dates:
        return make_response('No data to export.', 404)

    # Month header labels: "Jan '24"
    month_labels = [d.strftime("%b '") + d.strftime('%y') for d in all_dates]

    # Metadata column names
    META_COLS = ['Account', 'Type', 'Category', 'Tax Status', 'Institution']

    def fmt_currency(v):
        if v is None:
            return ''
        return '${:,.0f}'.format(float(v))

    def fmt_pct(v):
        if v is None:
            return ''
        return '{:.2f}%'.format(float(v))

    rows = []

    # Header row
    rows.append(META_COLS + month_labels)

    # One row per active account, ordered by display_order
    accounts = (
        Account.query
        .filter_by(is_active=True)
        .order_by(Account.display_order, Account.name)
        .all()
    )
    for acct in accounts:
        # Build balance lookup for this account
        snaps = {
            s.snapshot_date: s.balance
            for s in AccountSnapshot.query.filter_by(account_id=acct.id).all()
        }
        balances = [fmt_currency(snaps.get(d)) for d in all_dates]
        rows.append([
            acct.name,
            acct.account_type,
            acct.category,
            acct.tax_status or '',
            acct.institution or '',
        ] + balances)

    # Income row — aggregate all income entries per month
    income_by_month = {}
    for entry in SpendingEntry.query.filter_by(entry_type='income').all():
        if entry.entry_date in income_by_month:
            income_by_month[entry.entry_date] += float(entry.amount)
        else:
            income_by_month[entry.entry_date] = float(entry.amount)
    if income_by_month:
        rows.append(
            ['Income', '', '', '', ''] +
            [fmt_currency(income_by_month.get(d)) for d in all_dates]
        )

    # Expenses row — aggregate all expense entries per month
    expenses_by_month = {}
    for entry in SpendingEntry.query.filter_by(entry_type='expense').all():
        if entry.entry_date in expenses_by_month:
            expenses_by_month[entry.entry_date] += float(entry.amount)
        else:
            expenses_by_month[entry.entry_date] = float(entry.amount)
    if expenses_by_month:
        rows.append(
            ['Expenses', '', '', '', ''] +
            [fmt_currency(expenses_by_month.get(d)) for d in all_dates]
        )

    # Calculated metric rows
    metrics = {
        m.metric_date: m
        for m in CalculatedMetric.query.filter(
            CalculatedMetric.metric_date.in_(all_dates)
        ).all()
    }
    if any(m.net_worth is not None for m in metrics.values()):
        rows.append(
            ['Net Worth', '', '', '', ''] +
            [fmt_currency(metrics[d].net_worth if d in metrics else None) for d in all_dates]
        )
    if any(m.net_worth_non_re is not None for m in metrics.values()):
        rows.append(
            ['Net Worth Non-RE', '', '', '', ''] +
            [fmt_currency(metrics[d].net_worth_non_re if d in metrics else None) for d in all_dates]
        )
    if any(m.monthly_change_pct is not None for m in metrics.values()):
        rows.append(
            ['% Change', '', '', '', ''] +
            [fmt_pct(metrics[d].monthly_change_pct if d in metrics else None) for d in all_dates]
        )
    if any(m.save_rate is not None for m in metrics.values()):
        rows.append(
            ['Save Rate', '', '', '', ''] +
            [fmt_pct(metrics[d].save_rate if d in metrics else None) for d in all_dates]
        )

    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)

    if all_dates:
        start_str = all_dates[0].strftime('%Y-%m')
        end_str = all_dates[-1].strftime('%Y-%m')
        filename = f'ledger_{start_str}_{end_str}.csv'
    else:
        filename = 'ledger_export.csv'

    response = make_response(buf.getvalue())
    response.headers['Content-Type'] = 'text/csv; charset=utf-8'
    response.headers['Content-Disposition'] = f'attachment; filename={filename}'
    logger.info('CSV export: months=%d, accounts=%d', len(all_dates), len(accounts))
    return response


@main_bp.route('/api/import/preview', methods=['POST'])
def api_import_preview():
    """Parse an uploaded CSV and return a preview without writing to the DB."""
    if 'csv_file' not in request.files or request.files['csv_file'].filename == '':
        return jsonify({'error': 'No file provided.'}), 400

    file = request.files['csv_file']
    if not file.filename.lower().endswith('.csv'):
        return jsonify({'error': 'Only .csv files are supported.'}), 400

    models = {
        'Account': Account,
        'AccountSnapshot': AccountSnapshot,
        'SpendingEntry': SpendingEntry,
        'CalculatedMetric': CalculatedMetric,
        'ImportLog': ImportLog,
    }
    file_stream = io.StringIO(file.read().decode('utf-8-sig'))
    results = preview_csv(file_stream, models)
    return jsonify(results)
