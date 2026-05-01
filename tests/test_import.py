"""
Tests for bulk import improvements (item 4) and export (item 13):
  GET  /import/template
  POST /api/import/preview
  GET  /export/csv
  Ledger-format round-trip import
"""
import io
import csv
from datetime import date

from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric
from app import db as _db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_csv(*rows):
    """Build a CSV string from a list of row lists."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in rows:
        writer.writerow(row)
    return buf.getvalue().encode('utf-8')


VALID_CSV = make_csv(
    ["Category", "Jan '24", "Feb '24"],
    ["Cash", "$10,000", "$11,000"],
    ["Retirement", "$50,000", "$51,000"],
    ["Income", "$5,000", "$5,000"],
    ["Expenses", "$3,000", "$3,000"],
)

UNKNOWN_ROW_CSV = make_csv(
    ["Category", "Jan '24"],
    ["Cash", "$10,000"],
    ["Crypto", "$500"],        # unknown category → warning
)

BAD_MONTH_CSV = make_csv(
    ["Category", "NotAMonth"],
    ["Cash", "$10,000"],
)

EMPTY_CSV = b""


def post_preview(client, csv_bytes, filename="test.csv"):
    return client.post(
        '/api/import/preview',
        data={'csv_file': (io.BytesIO(csv_bytes), filename)},
        content_type='multipart/form-data',
    )


# ---------------------------------------------------------------------------
# GET /import/template
# ---------------------------------------------------------------------------

class TestImportTemplate:
    def test_template_returns_200(self, client, db):
        r = client.get('/import/template')
        assert r.status_code == 200

    def test_template_content_type_is_csv(self, client, db):
        r = client.get('/import/template')
        assert 'text/csv' in r.content_type

    def test_template_has_attachment_header(self, client, db):
        r = client.get('/import/template')
        assert 'attachment' in r.headers.get('Content-Disposition', '')
        assert '.csv' in r.headers.get('Content-Disposition', '')

    def test_template_contains_expected_categories(self, client, db):
        r = client.get('/import/template')
        body = r.data.decode('utf-8')
        assert 'Income' in body
        assert 'Expenses' in body

    def test_template_header_row_first(self, client, db):
        r = client.get('/import/template')
        first_line = r.data.decode('utf-8').splitlines()[0]
        assert first_line.startswith('Account')


# ---------------------------------------------------------------------------
# POST /api/import/preview
# ---------------------------------------------------------------------------

class TestImportPreview:
    def test_preview_valid_csv_returns_200(self, client, db):
        r = post_preview(client, VALID_CSV)
        assert r.status_code == 200

    def test_preview_returns_success_true(self, client, db):
        r = post_preview(client, VALID_CSV)
        assert r.get_json()['success'] is True

    def test_preview_counts_snapshots(self, client, db):
        r = post_preview(client, VALID_CSV)
        body = r.get_json()
        # Cash × 2 months + Retirement × 2 months = 4
        assert body['snapshots_imported'] == 4

    def test_preview_counts_spending(self, client, db):
        r = post_preview(client, VALID_CSV)
        body = r.get_json()
        # Income × 2 + Expenses × 2 = 4
        assert body['spending_imported'] == 4

    def test_preview_lists_months(self, client, db):
        r = post_preview(client, VALID_CSV)
        body = r.get_json()
        assert '2024-01' in body['months']
        assert '2024-02' in body['months']

    def test_preview_identifies_new_accounts(self, client, db):
        r = post_preview(client, VALID_CSV)
        body = r.get_json()
        assert 'Cash' in body['accounts_to_create']
        assert 'Retirement' in body['accounts_to_create']

    def test_preview_identifies_existing_accounts(self, client, db):
        from app.models import Account
        from app import db as _db
        acct = Account(name="Cash", account_type="asset", category="cash", is_active=True)
        _db.session.add(acct)
        _db.session.commit()

        r = post_preview(client, VALID_CSV)
        body = r.get_json()
        assert 'Cash' in body['accounts_existing']
        assert 'Cash' not in body['accounts_to_create']

    def test_preview_does_not_write_to_db(self, client, db):
        from app.models import AccountSnapshot
        post_preview(client, VALID_CSV)
        assert AccountSnapshot.query.count() == 0

    def test_preview_warns_on_unknown_category(self, client, db):
        r = post_preview(client, UNKNOWN_ROW_CSV)
        body = r.get_json()
        assert any('Crypto' in w for w in body['warnings'])

    def test_preview_errors_on_bad_month_header(self, client, db):
        r = post_preview(client, BAD_MONTH_CSV)
        body = r.get_json()
        assert body['success'] is False
        assert body['errors']

    def test_preview_no_file_returns_400(self, client, db):
        r = client.post('/api/import/preview')
        assert r.status_code == 400

    def test_preview_non_csv_returns_400(self, client, db):
        r = client.post(
            '/api/import/preview',
            data={'csv_file': (io.BytesIO(b'hello'), 'file.txt')},
            content_type='multipart/form-data',
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# GET /export/csv
# ---------------------------------------------------------------------------

def _seed_export_data(db_obj):
    """Seed one asset account, one liability, income/expense, and a metric."""
    asset = Account(name='Fidelity 401k', account_type='asset',
                    category='retirement', tax_status='tax_deferred',
                    institution='Fidelity', is_active=True, display_order=1)
    liability = Account(name='Home Mortgage', account_type='liability',
                        category='mortgage', is_active=True, display_order=2)
    _db.session.add_all([asset, liability])
    _db.session.flush()

    for yr, mo, bal in [(2024, 1, 450_000), (2024, 2, 455_000)]:
        _db.session.add(AccountSnapshot(
            account_id=asset.id, snapshot_date=date(yr, mo, 1), balance=bal))
    for yr, mo, bal in [(2024, 1, 290_000), (2024, 2, 289_000)]:
        _db.session.add(AccountSnapshot(
            account_id=liability.id, snapshot_date=date(yr, mo, 1), balance=bal))

    _db.session.add_all([
        SpendingEntry(entry_date=date(2024, 1, 1), account_name='Employer',
                      amount=10_000, entry_type='income'),
        SpendingEntry(entry_date=date(2024, 1, 1), account_name='Chase',
                      amount=7_000, entry_type='expense'),
    ])
    _db.session.add(CalculatedMetric(
        metric_date=date(2024, 1, 1), net_worth=160_000,
        net_worth_non_re=160_000, save_rate=30.0,
    ))
    _db.session.commit()
    return asset, liability


class TestExportCsv:
    def test_export_returns_200(self, client, db):
        _seed_export_data(db)
        r = client.get('/export/csv')
        assert r.status_code == 200

    def test_export_empty_db_returns_404(self, client, db):
        r = client.get('/export/csv')
        assert r.status_code == 404

    def test_export_content_type_is_csv(self, client, db):
        _seed_export_data(db)
        r = client.get('/export/csv')
        assert 'text/csv' in r.content_type

    def test_export_has_attachment_header(self, client, db):
        _seed_export_data(db)
        r = client.get('/export/csv')
        disp = r.headers.get('Content-Disposition', '')
        assert 'attachment' in disp
        assert 'ledger_' in disp

    def test_export_header_row_has_metadata_cols(self, client, db):
        _seed_export_data(db)
        body = client.get('/export/csv').data.decode('utf-8')
        first_line = body.splitlines()[0]
        assert 'Account' in first_line
        assert 'Type' in first_line
        assert 'Category' in first_line

    def test_export_contains_account_rows(self, client, db):
        _seed_export_data(db)
        body = client.get('/export/csv').data.decode('utf-8')
        assert 'Fidelity 401k' in body
        assert 'Home Mortgage' in body

    def test_export_contains_spending_rows(self, client, db):
        _seed_export_data(db)
        body = client.get('/export/csv').data.decode('utf-8')
        assert 'Income' in body
        assert 'Expenses' in body

    def test_export_contains_metric_rows(self, client, db):
        _seed_export_data(db)
        body = client.get('/export/csv').data.decode('utf-8')
        assert 'Net Worth' in body
        assert 'Save Rate' in body

    def test_export_date_range_filter(self, client, db):
        _seed_export_data(db)
        # Only ask for Feb '24 — Jan should not appear
        r = client.get('/export/csv?from=2024-02&to=2024-02')
        assert r.status_code == 200
        body = r.data.decode('utf-8')
        reader = list(csv.reader(io.StringIO(body)))
        # Header + 2 account rows; month cols should only contain Feb
        header = reader[0]
        month_headers = header[5:]  # after metadata cols
        assert len(month_headers) == 1
        assert '24' in month_headers[0]  # Feb '24


# ---------------------------------------------------------------------------
# Ledger format detection + import
# ---------------------------------------------------------------------------

LEDGER_CSV = make_csv(
    ["Account", "Type", "Category", "Tax Status", "Institution", "Jan '24", "Feb '24"],
    ["Fidelity 401k", "asset", "retirement", "tax_deferred", "Fidelity", "$450,000", "$455,000"],
    ["Home Mortgage", "liability", "mortgage", "", "", "$290,000", "$289,000"],
    ["Income", "", "", "", "", "$10,000", "$10,000"],
    ["Expenses", "", "", "", "", "$7,000", "$7,000"],
    ["Net Worth", "", "", "", "", "$160,000", "$166,000"],
    ["Save Rate", "", "", "", "", "30%", "30%"],
)


class TestLedgerFormatDetection:
    def test_detect_format_generic(self):
        from app.import_processor import _detect_format
        import pandas as pd, io as _io
        df = pd.read_csv(_io.StringIO("Category,Jan '24\nCash,$1000"))
        fmt, mcols = _detect_format(df)
        assert fmt == 'generic'
        assert "Jan '24" in mcols

    def test_detect_format_ledger(self):
        from app.import_processor import _detect_format
        import pandas as pd, io as _io
        df = pd.read_csv(_io.StringIO(
            "Account,Type,Category,Tax Status,Institution,Jan '24\n"
            "My 401k,asset,retirement,tax_deferred,Fidelity,$450000"
        ))
        fmt, mcols = _detect_format(df)
        assert fmt == 'ledger'
        assert "Jan '24" in mcols


class TestLedgerImport:
    def test_preview_ledger_format_detects_named_accounts(self, client, db):
        r = client.post(
            '/api/import/preview',
            data={'csv_file': (io.BytesIO(LEDGER_CSV), 'ledger_export.csv')},
            content_type='multipart/form-data',
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert 'Fidelity 401k' in body['accounts_to_create']
        assert 'Home Mortgage' in body['accounts_to_create']

    def test_preview_ledger_format_counts_spending(self, client, db):
        r = client.post(
            '/api/import/preview',
            data={'csv_file': (io.BytesIO(LEDGER_CSV), 'ledger_export.csv')},
            content_type='multipart/form-data',
        )
        body = r.get_json()
        # Income × 2 + Expenses × 2 = 4
        assert body['spending_imported'] == 4

    def test_import_ledger_creates_accounts_with_metadata(self, client, db):
        r = client.post(
            '/import',
            data={'csv_file': (io.BytesIO(LEDGER_CSV), 'ledger_export.csv')},
            content_type='multipart/form-data',
        )
        # Should redirect on success (302) or render with results
        assert r.status_code in (200, 302)
        acct = Account.query.filter_by(name='Fidelity 401k').first()
        assert acct is not None
        assert acct.account_type == 'asset'
        assert acct.category == 'retirement'
        assert acct.tax_status == 'tax_deferred'
        assert acct.institution == 'Fidelity'

    def test_import_ledger_creates_snapshots(self, client, db):
        client.post(
            '/import',
            data={'csv_file': (io.BytesIO(LEDGER_CSV), 'ledger_export.csv')},
            content_type='multipart/form-data',
        )
        acct = Account.query.filter_by(name='Fidelity 401k').first()
        assert acct is not None
        snaps = AccountSnapshot.query.filter_by(account_id=acct.id).all()
        assert len(snaps) == 2
        balances = sorted(float(s.balance) for s in snaps)
        assert balances == [450_000.0, 455_000.0]

    def test_import_ledger_existing_account_not_duplicated(self, client, db):
        # Pre-create the account
        existing = Account(name='Fidelity 401k', account_type='asset',
                           category='retirement', is_active=True)
        _db.session.add(existing)
        _db.session.commit()

        client.post(
            '/import',
            data={'csv_file': (io.BytesIO(LEDGER_CSV), 'ledger_export.csv')},
            content_type='multipart/form-data',
        )
        assert Account.query.filter_by(name='Fidelity 401k').count() == 1

    def test_round_trip_export_then_import(self, client, db):
        """Export data then import into a fresh DB — verify accounts and snapshots restored."""
        _seed_export_data(db)
        export_bytes = client.get('/export/csv').data

        # Clear all data
        _db.session.query(AccountSnapshot).delete()
        _db.session.query(SpendingEntry).delete()
        _db.session.query(CalculatedMetric).delete()
        _db.session.query(Account).delete()
        _db.session.commit()

        client.post(
            '/import',
            data={'csv_file': (io.BytesIO(export_bytes), 'ledger_export.csv')},
            content_type='multipart/form-data',
        )
        assert Account.query.filter_by(name='Fidelity 401k').count() == 1
        assert Account.query.filter_by(name='Home Mortgage').count() == 1
        acct = Account.query.filter_by(name='Fidelity 401k').first()
        assert AccountSnapshot.query.filter_by(account_id=acct.id).count() == 2
