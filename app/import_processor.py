"""
CSV Import Processor for Personal Finance Dashboard

Supports two import formats:

1. Generic format (original):
   - Column 1: Category name (Cash, Retirement, Investments, Real Estate,
                Mortgage, Income, Expenses, Net Worth, Net Worth Non-RE,
                % Change, Save Rate)
   - Remaining columns: Monthly snapshots (Jan '24, Feb '24, etc.)

2. Ledger export format (produced by GET /export/csv):
   - Column 1: Account name (any named account)
   - Columns 2-5: Type, Category, Tax Status, Institution  (account metadata)
   - Remaining columns: Monthly snapshots (Jan '24, Feb '24, etc.)
   - Named accounts are matched by name; created from metadata if not found.

Both formats accept:
  - Currency values: $1,234,567 format
  - Percentage values: 1.23% format
"""
import logging
import re
import json
import pandas as pd
from datetime import datetime, date

logger = logging.getLogger(__name__)


# Metadata column names used by the Ledger export format (after the Account column)
LEDGER_META_COLS = ['type', 'category', 'tax status', 'institution']

# Maps lowercase category name -> account metadata
ACCOUNT_DEFINITIONS = {
    'cash':        {'account_type': 'asset',     'category': 'cash',        'is_liquid': True,  'display_order': 1},
    'retirement':  {'account_type': 'asset',     'category': 'retirement',  'is_liquid': False, 'display_order': 2},
    'investments': {'account_type': 'asset',     'category': 'investment',  'is_liquid': True,  'display_order': 3},
    'real estate': {'account_type': 'asset',     'category': 'real_estate', 'is_liquid': False, 'display_order': 4},
    'mortgage':    {'account_type': 'liability', 'category': 'mortgage',    'is_liquid': False, 'display_order': 5},
}

# Maps lowercase category name -> SpendingEntry.entry_type
SPENDING_ROWS = {
    'income':   'income',
    'expenses': 'expense',
    'expense':  'expense',
}

# Rows whose values come from our own calculations — skip as source data
# but harvest into CalculatedMetric if present
CALCULATED_METRIC_MAP = {
    'net worth':         'net_worth',
    'net worth non-re':  'net_worth_non_re',
    '% change':          'monthly_change_pct',
    'save rate':         'save_rate',
}


def _parse_currency(value):
    """Convert '$1,234,567' or '1234567' to float. Returns None on failure."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip()
    if s in ('', '-', 'N/A', 'n/a'):
        return None
    s = re.sub(r'[$,]', '', s)
    try:
        return float(s)
    except ValueError:
        return None


def _parse_percentage(value):
    """Convert '1.23%' or '1.23' to float. Returns None on failure."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    s = str(value).strip().rstrip('%')
    if s in ('', '-', 'N/A', 'n/a'):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_month_date(col_header):
    """
    Parse a month column header to a date (first of that month).
    Handles formats like: "Jan '24", "Jan 2024", "January 2024",
    "Jan-2024", "2024-01", "01/2024".
    Returns None if unparseable.
    """
    col_str = str(col_header).strip()
    # "Jan '24" -> "Jan 2024"
    col_str = re.sub(r"'(\d{2})\s*$", lambda m: '20' + m.group(1), col_str)

    formats = [
        "%b %Y",    # Jan 2024
        "%B %Y",    # January 2024
        "%b-%Y",    # Jan-2024
        "%Y-%m",    # 2024-01
        "%m/%Y",    # 01/2024
        "%m-%Y",    # 01-2024
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(col_str, fmt)
            return date(dt.year, dt.month, 1)
        except ValueError:
            continue
    return None


def _detect_format(df) -> tuple[str, list]:
    """
    Detect whether df is 'ledger' (named-account export) or 'generic' format.

    Returns (format_name, month_cols) where month_cols is the subset of
    column names that represent months (i.e. after any metadata columns).

    Ledger format: first column header is 'account' and the next columns
    include 'type' and 'category' (case-insensitive).
    """
    cols = list(df.columns)
    if not cols:
        return 'generic', cols[1:]

    first = str(cols[0]).strip().lower()
    if first == 'account' and len(cols) > 2:
        next_lower = [str(c).strip().lower() for c in cols[1:5]]
        if 'type' in next_lower and 'category' in next_lower:
            # Ledger format: find where the month columns start
            # (after all recognised metadata cols)
            meta_end = 1
            for c in cols[1:]:
                if str(c).strip().lower() in LEDGER_META_COLS:
                    meta_end += 1
                else:
                    break
            return 'ledger', cols[meta_end:]

    return 'generic', cols[1:]


def _preprocess_csv(file_stream):
    """
    Shared CSV reading and header-parsing logic used by both process_csv and preview_csv.

    Returns a dict with:
        df             pd.DataFrame (or None on read failure)
        fmt            'ledger' | 'generic'
        month_dates    {col_name: date}
        category_col   name of the first column
        errors         list of error strings (non-empty → caller should abort)
        warnings       list of warning strings
    """
    result = {'df': None, 'fmt': None, 'month_dates': {}, 'category_col': None,
              'errors': [], 'warnings': []}

    try:
        result['df'] = pd.read_csv(file_stream, header=0)
    except Exception as e:
        result['errors'].append(f"Could not read CSV file: {e}")
        return result

    df = result['df']
    if df.empty or len(df.columns) < 2:
        result['errors'].append("CSV must have at least two columns: category and one month.")
        return result

    fmt, month_col_names = _detect_format(df)
    result['fmt'] = fmt
    result['category_col'] = df.columns[0]

    for col in month_col_names:
        d = _parse_month_date(col)
        if d:
            result['month_dates'][col] = d
        else:
            result['warnings'].append(f"Skipping column '{col}': could not parse as a month.")

    if not result['month_dates']:
        result['errors'].append("No parseable month columns found in the CSV header.")

    return result


def process_csv(file_stream, filename, db, models):
    """
    Parse and import a CSV file into the database.

    Args:
        file_stream: file-like object of the uploaded CSV
        filename:    original filename string (for logging)
        db:          SQLAlchemy db instance
        models:      dict with keys Account, AccountSnapshot,
                     SpendingEntry, CalculatedMetric, ImportLog

    Returns:
        dict with keys: accounts_created, snapshots_imported,
                        spending_imported, metrics_imported,
                        warnings, errors, success
    """
    Account = models['Account']
    AccountSnapshot = models['AccountSnapshot']
    SpendingEntry = models['SpendingEntry']
    CalculatedMetric = models['CalculatedMetric']
    ImportLog = models['ImportLog']

    results = {
        'accounts_created': 0,
        'snapshots_imported': 0,
        'spending_imported': 0,
        'metrics_imported': 0,
        'warnings': [],
        'errors': [],
        'success': False,
    }

    logger.info('Parsing CSV: file=%s', filename)

    pre = _preprocess_csv(file_stream)
    results['warnings'].extend(pre['warnings'])

    if pre['errors']:
        results['errors'].extend(pre['errors'])
        _log_import(db, ImportLog, filename, 0, 'failed', pre['errors'][0])
        return results

    df = pre['df']
    fmt = pre['fmt']
    month_dates = pre['month_dates']
    category_col = pre['category_col']
    logger.info('CSV format detected: %s', fmt)

    # --- Accumulate calculated metric values across rows ---
    metric_data = {}  # {date: {field: value}}

    # --- Process each row ---
    try:
        for _, row in df.iterrows():
            raw_label = str(row[category_col]).strip()
            if not raw_label or raw_label.lower() in ('nan', 'none', ''):
                continue  # skip blank rows
            key = raw_label.lower()

            if fmt == 'ledger':
                # Named-account format: match by name; spending/metric rows use generic labels
                if key in SPENDING_ROWS:
                    _import_spending_row(row, raw_label, key, month_dates,
                                         db, SpendingEntry, results)
                elif key in CALCULATED_METRIC_MAP:
                    _collect_metric_row(row, key, month_dates, metric_data)
                else:
                    _import_ledger_account_row(row, raw_label, df.columns, month_dates,
                                               db, Account, AccountSnapshot, results)
            else:
                # Generic format
                if key in ACCOUNT_DEFINITIONS:
                    _import_account_row(row, raw_label, month_dates,
                                        db, Account, AccountSnapshot, results)
                elif key in SPENDING_ROWS:
                    _import_spending_row(row, raw_label, key, month_dates,
                                         db, SpendingEntry, results)
                elif key in CALCULATED_METRIC_MAP:
                    _collect_metric_row(row, key, month_dates, metric_data)
                else:
                    results['warnings'].append(
                        f"Unknown category '{raw_label}' — skipped."
                    )

        # --- Upsert CalculatedMetric rows ---
        for month_date, fields in metric_data.items():
            metric = CalculatedMetric.query.filter_by(metric_date=month_date).first()
            if not metric:
                metric = CalculatedMetric(metric_date=month_date)
                db.session.add(metric)
            for field, val in fields.items():
                setattr(metric, field, val)
            results['metrics_imported'] += 1

        db.session.commit()
        results['success'] = True
        # Expose sorted month dates so the caller can trigger recalculation
        results['month_dates'] = sorted(d.isoformat() for d in set(month_dates.values()))

    except Exception as e:
        db.session.rollback()
        logger.exception('DB error during import: file=%s', filename)
        results['errors'].append(f"Database error during import: {e}")
        _log_import(db, ImportLog, filename, 0, 'failed', str(e))
        return results

    total = (results['snapshots_imported'] +
             results['spending_imported'] +
             results['metrics_imported'])
    status = 'success' if not results['errors'] else 'partial'
    _log_import(db, ImportLog, filename, total, status,
                '; '.join(results['errors']) or None,
                json.dumps(results))
    return results


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _import_account_row(row, raw_category, month_dates, db, Account, AccountSnapshot, results):
    acct_def = ACCOUNT_DEFINITIONS[raw_category.lower()]
    account = Account.query.filter(
        Account.name.ilike(raw_category)
    ).first()
    if not account:
        account = Account(
            name=raw_category,
            account_type=acct_def['account_type'],
            category=acct_def['category'],
            is_liquid=acct_def['is_liquid'],
            include_in_networth=True,
            display_order=acct_def['display_order'],
        )
        db.session.add(account)
        db.session.flush()  # populate account.id
        results['accounts_created'] += 1

    for col, month_date in month_dates.items():
        value = _parse_currency(row[col])
        if value is None:
            continue
        snapshot = AccountSnapshot.query.filter_by(
            account_id=account.id, snapshot_date=month_date
        ).first()
        if snapshot:
            snapshot.balance = value
        else:
            db.session.add(AccountSnapshot(
                account_id=account.id,
                snapshot_date=month_date,
                balance=value,
            ))
        results['snapshots_imported'] += 1


def _import_spending_row(row, raw_category, key, month_dates, db, SpendingEntry, results):
    entry_type = SPENDING_ROWS[key]
    for col, month_date in month_dates.items():
        value = _parse_currency(row[col])
        if value is None:
            continue
        existing = SpendingEntry.query.filter_by(
            entry_date=month_date,
            account_name=raw_category,
            entry_type=entry_type,
        ).first()
        if existing:
            existing.amount = value
        else:
            db.session.add(SpendingEntry(
                entry_date=month_date,
                account_name=raw_category,
                amount=value,
                entry_type=entry_type,
            ))
        results['spending_imported'] += 1


def _import_ledger_account_row(row, account_name, all_cols, month_dates,
                                db, Account, AccountSnapshot, results):
    """
    Import an account row from a Ledger-format CSV.

    Reads Type, Category, Tax Status, Institution metadata columns to
    create the account if it doesn't exist yet.
    """
    # Build lowercase col lookup
    col_lower = {str(c).strip().lower(): c for c in all_cols}

    def get_meta(field):
        col = col_lower.get(field)
        if col is None:
            return None
        v = str(row[col]).strip()
        return v if v and v.lower() not in ('nan', 'none', '') else None

    account_type = get_meta('type') or 'asset'
    if account_type not in ('asset', 'liability'):
        account_type = 'asset'
    category = get_meta('category') or 'investment'
    tax_status = get_meta('tax status')
    institution = get_meta('institution')

    account = Account.query.filter(
        Account.name.ilike(account_name)
    ).first()

    if not account:
        account = Account(
            name=account_name,
            account_type=account_type,
            category=category,
            tax_status=tax_status,
            institution=institution,
            is_liquid=account_type == 'asset' and category not in ('real_estate', 'vehicle'),
            include_in_networth=True,
            is_active=True,
        )
        db.session.add(account)
        db.session.flush()
        results['accounts_created'] += 1

    for col, month_date in month_dates.items():
        value = _parse_currency(row[col])
        if value is None:
            continue
        snapshot = AccountSnapshot.query.filter_by(
            account_id=account.id, snapshot_date=month_date
        ).first()
        if snapshot:
            snapshot.balance = value
        else:
            db.session.add(AccountSnapshot(
                account_id=account.id,
                snapshot_date=month_date,
                balance=value,
            ))
        results['snapshots_imported'] += 1


def _collect_metric_row(row, key, month_dates, metric_data):
    field = CALCULATED_METRIC_MAP[key]
    parse_fn = _parse_percentage if key in ('% change', 'save rate') else _parse_currency
    for col, month_date in month_dates.items():
        value = parse_fn(row[col])
        if value is None:
            continue
        if month_date not in metric_data:
            metric_data[month_date] = {}
        metric_data[month_date][field] = value


def preview_csv(file_stream, models):
    """
    Parse a CSV and return a preview of what would be imported, without
    writing anything to the database.

    Returns the same structure as process_csv results, plus:
        'accounts_to_create': list of account names that don't exist yet
        'accounts_existing': list of account names already in the DB
        'months': sorted list of 'YYYY-MM' strings that would be affected
    """
    Account = models['Account']

    results = {
        'accounts_created': 0,
        'snapshots_imported': 0,
        'spending_imported': 0,
        'metrics_imported': 0,
        'warnings': [],
        'errors': [],
        'accounts_to_create': [],
        'accounts_existing': [],
        'months': [],
        'success': False,
    }

    pre = _preprocess_csv(file_stream)
    results['warnings'].extend(pre['warnings'])

    if pre['errors']:
        results['errors'].extend(pre['errors'])
        return results

    df = pre['df']
    fmt = pre['fmt']
    month_dates = pre['month_dates']
    category_col = pre['category_col']
    results['months'] = sorted(d.strftime('%Y-%m') for d in set(month_dates.values()))
    results['format'] = fmt

    for _, row in df.iterrows():
        raw_label = str(row[category_col]).strip()
        if not raw_label or raw_label.lower() in ('nan', 'none', ''):
            continue
        key = raw_label.lower()

        if fmt == 'ledger':
            if key in SPENDING_ROWS:
                for col in month_dates:
                    if _parse_currency(row[col]) is not None:
                        results['spending_imported'] += 1
            elif key in CALCULATED_METRIC_MAP:
                parse_fn = _parse_percentage if key in ('% change', 'save rate') else _parse_currency
                for col in month_dates:
                    if parse_fn(row[col]) is not None:
                        results['metrics_imported'] += 1
            else:
                # Named account row
                account = Account.query.filter(Account.name.ilike(raw_label)).first()
                if account:
                    if raw_label not in results['accounts_existing']:
                        results['accounts_existing'].append(raw_label)
                else:
                    if raw_label not in results['accounts_to_create']:
                        results['accounts_to_create'].append(raw_label)
                        results['accounts_created'] += 1
                for col in month_dates:
                    if _parse_currency(row[col]) is not None:
                        results['snapshots_imported'] += 1
        else:
            if key in ACCOUNT_DEFINITIONS:
                account = Account.query.filter(Account.name.ilike(raw_label)).first()
                if account:
                    if raw_label not in results['accounts_existing']:
                        results['accounts_existing'].append(raw_label)
                else:
                    if raw_label not in results['accounts_to_create']:
                        results['accounts_to_create'].append(raw_label)
                        results['accounts_created'] += 1
                for col in month_dates:
                    if _parse_currency(row[col]) is not None:
                        results['snapshots_imported'] += 1

            elif key in SPENDING_ROWS:
                for col in month_dates:
                    if _parse_currency(row[col]) is not None:
                        results['spending_imported'] += 1

            elif key in CALCULATED_METRIC_MAP:
                parse_fn = _parse_percentage if key in ('% change', 'save rate') else _parse_currency
                for col in month_dates:
                    if parse_fn(row[col]) is not None:
                        results['metrics_imported'] += 1

            else:
                results['warnings'].append(f"Unknown category '{raw_label}' — skipped.")

    results['success'] = True
    return results


def _log_import(db, ImportLog, filename, records, status, error_msg=None, details=None):
    try:
        log = ImportLog(
            file_name=filename,
            records_imported=records,
            status=status,
            error_message=error_msg,
            details=details,
        )
        db.session.add(log)
        db.session.commit()
    except Exception:
        db.session.rollback()
