import io
import json
from datetime import date, datetime
from decimal import Decimal

import pytest

from app import db as app_db
from app.db_transfer_service import export_database, import_database
from app.models import (
    Account, AccountSnapshot, AppSetting, HoldingAllocation, RecurringEntry, RentalProperty,
)
from tests.conftest import make_holding, make_investment_account


def _clear_all_tables(db):
    for table in reversed(db.metadata.sorted_tables):
        db.session.execute(table.delete())
    db.session.commit()


@pytest.fixture
def clean_db(db):
    # The shared teardown doesn't clear every table, and an import can fill any of them.
    _clear_all_tables(db)
    yield db
    _clear_all_tables(db)


def _seed(db):
    mortgage_holder = Account(name='House', account_type='asset', category='real_estate')
    db.session.add(mortgage_holder)
    db.session.commit()
    # Created after the asset it pairs with, so the export lists the referencing row first.
    mortgage = Account(name='Mortgage', account_type='liability', category='mortgage')
    db.session.add(mortgage)
    db.session.commit()
    mortgage_holder.paired_liability_id = mortgage.id
    db.session.add(AccountSnapshot(account_id=mortgage_holder.id, snapshot_date=date(2026, 1, 1),
                                   balance=Decimal('412345.67')))
    db.session.add(AppSetting(key='snapshot_timing', value='end_of_month'))
    db.session.add(RecurringEntry(account_name='Paycheck', amount=Decimal('5000.00'), entry_type='income'))
    db.session.add(RentalProperty(name='Duplex', monthly_rent=Decimal('1800.00'), vacancy_rate=Decimal('0.0500')))
    db.session.commit()
    brokerage = make_investment_account(db.session)
    holding = make_holding(db.session, brokerage.id, shares=12.34567891)
    db.session.add(HoldingAllocation(holding_id=holding.id, asset_class='domestic', percentage=Decimal('100.00')))
    holding.last_fetched = datetime(2026, 2, 3, 4, 5, 6)
    db.session.commit()


def _tables(payload):
    return payload['tables']


def _upload(client, payload_bytes, confirm='replace'):
    data = {'db_file': (io.BytesIO(payload_bytes), 'ledger_backup.json')}
    if confirm is not None:
        data['confirm'] = confirm
    return client.post('/api/database/import', data=data, content_type='multipart/form-data')


class TestExportDatabase:
    def test_lists_every_table_when_empty(self, clean_db):
        payload = export_database()
        assert payload['format'] == 'ledger-db-export'
        assert set(_tables(payload)) == set(clean_db.metadata.tables)
        assert all(rows == [] for rows in _tables(payload).values())

    def test_serializes_exact_values(self, clean_db):
        _seed(clean_db)
        tables = _tables(export_database())
        assert tables['account_snapshots'][0]['balance'] == '412345.67'
        assert tables['account_snapshots'][0]['snapshot_date'] == '2026-01-01'
        assert tables['holdings'][0]['last_fetched'] == '2026-02-03T04:05:06'
        json.dumps(tables)  # must be JSON-serializable as-is


class TestImportDatabase:
    def test_round_trip_restores_identical_data(self, clean_db):
        _seed(clean_db)
        before = export_database()
        _clear_all_tables(clean_db)

        counts = import_database(json.loads(json.dumps(before)))

        assert counts['accounts'] == 3
        assert _tables(export_database()) == _tables(before)
        house = Account.query.filter_by(name='House').one()
        assert house.paired_liability_id == Account.query.filter_by(name='Mortgage').one().id

    def test_replaces_existing_data(self, clean_db):
        clean_db.session.add(Account(name='Old', account_type='asset', category='checking'))
        clean_db.session.commit()
        payload = export_database()
        payload['tables']['accounts'] = []

        import_database(payload)

        assert Account.query.count() == 0

    def test_empty_export_clears_database(self, clean_db):
        _seed(clean_db)
        import_database({'format': 'ledger-db-export', 'version': 1, 'tables': {}})
        assert Account.query.count() == 0
        assert AppSetting.query.count() == 0

    def test_new_ids_continue_after_imported_ids(self, clean_db):
        payload = export_database()
        payload['tables']['accounts'] = [
            {'id': 40, 'name': 'Imported', 'account_type': 'asset', 'category': 'checking'}]
        import_database(payload)

        acct = Account(name='Created later', account_type='asset', category='checking')
        clean_db.session.add(acct)
        clean_db.session.commit()

        assert acct.id > 40
        assert clean_db.session.get(Account, 40).is_active is True  # omitted columns take model defaults

    @pytest.mark.parametrize('payload, message', [
        ([], 'Not a Ledger database export'),
        ({'format': 'other', 'version': 1, 'tables': {}}, 'Not a Ledger database export'),
        ({'format': 'ledger-db-export', 'version': 99, 'tables': {}}, 'Unsupported export version'),
        ({'format': 'ledger-db-export', 'version': 1, 'tables': {'mystery': []}}, 'Unknown tables'),
        ({'format': 'ledger-db-export', 'version': 1,
          'tables': {'accounts': [{'id': 1, 'name': 'A', 'account_type': 'asset', 'category': 'x', 'color': 1}]}},
         'Unknown columns'),
        ({'format': 'ledger-db-export', 'version': 1,
          'tables': {'account_snapshots': [{'id': 1, 'account_id': 1, 'snapshot_date': 'not-a-date',
                                            'balance': '1'}]}},
         'column "snapshot_date"'),
        ({'format': 'ledger-db-export', 'version': 1,
          'tables': {'accounts': [{'name': 'A', 'account_type': 'asset', 'category': 'x'}]}},
         'has no id'),
    ])
    def test_invalid_payload_leaves_data_untouched(self, clean_db, payload, message):
        _seed(clean_db)
        with pytest.raises(ValueError, match=message):
            import_database(payload)
        assert Account.query.count() == 3

    def test_constraint_failure_rolls_back(self, clean_db):
        from sqlalchemy.exc import IntegrityError
        _seed(clean_db)
        payload = export_database()
        payload['tables']['accounts'].append({**payload['tables']['accounts'][0], 'id': 99})  # duplicate name

        with pytest.raises(IntegrityError):
            import_database(payload)

        assert Account.query.count() == 3
        assert AccountSnapshot.query.count() == 1


class TestDatabaseTransferRoutes:
    def test_export_downloads_json_attachment(self, client, clean_db):
        _seed(clean_db)
        resp = client.get('/export/database')
        assert resp.status_code == 200
        assert 'attachment; filename=ledger_backup_' in resp.headers['Content-Disposition']
        assert len(resp.get_json()['tables']['accounts']) == 3

    def test_import_success(self, client, clean_db):
        _seed(clean_db)
        payload = client.get('/export/database').data
        clean_db.session.add(Account(name='Extra', account_type='asset', category='checking'))
        clean_db.session.commit()

        resp = _upload(client, payload)

        assert resp.status_code == 200
        assert resp.get_json()['counts']['accounts'] == 3
        assert Account.query.filter_by(name='Extra').count() == 0

    def test_import_requires_confirmation(self, client, clean_db):
        _seed(clean_db)
        resp = _upload(client, b'{}', confirm=None)
        assert resp.status_code == 400
        assert Account.query.count() == 3

    def test_import_requires_file(self, client, clean_db):
        resp = client.post('/api/database/import', data={'confirm': 'replace'},
                           content_type='multipart/form-data')
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'No file provided.'

    def test_import_rejects_non_json(self, client, clean_db):
        resp = _upload(client, b'Account,Type\n')
        assert resp.status_code == 400
        assert resp.get_json()['error'] == 'File is not valid JSON.'

    def test_import_rejects_invalid_export(self, client, clean_db):
        resp = _upload(client, b'{"format": "other"}')
        assert resp.status_code == 400
        assert 'Not a Ledger database export' in resp.get_json()['error']

    def test_import_reports_inconsistent_data(self, client, clean_db):
        _seed(clean_db)
        payload = client.get('/export/database').get_json()
        payload['tables']['accounts'].append({**payload['tables']['accounts'][0], 'id': 99})
        resp = _upload(client, json.dumps(payload).encode())
        assert resp.status_code == 400
        assert 'nothing was changed' in resp.get_json()['error']
        assert Account.query.count() == 3

    @pytest.mark.parametrize('method, path', [('get', '/export/database'), ('post', '/api/database/import')])
    def test_requires_sign_in(self, anon_client, method, path):
        resp = getattr(anon_client, method)(path)
        assert resp.status_code in (302, 401)


class TestExportScript:
    @pytest.fixture
    def source_app(self, tmp_path, monkeypatch):
        """A separate on-disk database, like the Pi's, that the script's own create_app() connects to."""
        from app import create_app
        monkeypatch.setenv('DATABASE_URL', f'sqlite:///{tmp_path / "finance.db"}')
        source_app = create_app()
        with source_app.app_context():
            app_db.create_all()
        return source_app

    def test_writes_owner_only_file_that_imports(self, source_app, tmp_path, client, capsys):
        from scripts.export_db import main
        with source_app.app_context():
            _seed(app_db)
        out = tmp_path / 'backup.json'

        main(['-o', str(out)])

        assert oct(out.stat().st_mode & 0o777) == '0o600'
        assert 'Exported 9 rows' in capsys.readouterr().out
        resp = _upload(client, out.read_bytes())
        assert resp.status_code == 200
        assert resp.get_json()['counts']['accounts'] == 3

    def test_empty_database_exports_every_table(self, source_app, tmp_path):
        from scripts.export_db import main
        out = tmp_path / 'backup.json'
        main(['-o', str(out)])
        tables = json.loads(out.read_text())['tables']
        assert set(tables) == set(app_db.metadata.tables)
        assert all(rows == [] for rows in tables.values())

    def test_unwritable_output_path_fails(self, source_app, tmp_path):
        from scripts.export_db import main
        with pytest.raises(FileNotFoundError):
            main(['-o', str(tmp_path / 'missing_dir' / 'backup.json')])

