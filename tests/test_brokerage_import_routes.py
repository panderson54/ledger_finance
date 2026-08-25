"""
Route-level tests for:
  GET  /brokerage-import
  POST /api/brokerage-import/preview      (starts background job, returns job_id)
  GET  /api/brokerage-import/preview/<id> (poll for result)
  POST /api/brokerage-import/commit
"""
import io
import json
import time
from datetime import date
from unittest.mock import patch

import pytest

from app.models import Holding, AccountSnapshot, CalculatedMetric, ImportLog, Account, BankTransaction, SpendingEntry
from app import db as _db

from tests.conftest import make_investment_account, make_holding, make_transaction_category
from tests.test_brokerage_import_pdf_parsing import _schwab_bank_activity_bytes


FIDELITY_CSV = (
    "Account Name,Account Number,Symbol,Description,Quantity,Last Price,Current Value\n"
    "Individual,X12345678,VTI,VANGUARD TOTAL STOCK MKT ETF,10.5,280.11,2941.16\n"
    "Individual,X12345678,SPAXX,FIDELITY GOVERNMENT MONEY MARKET,150.0,1.00,150.00\n"
).encode('utf-8')


def _upload(client, path, csv_bytes, filename='positions.csv', extra=None):
    data = {'file': (io.BytesIO(csv_bytes), filename)}
    if extra:
        data.update(extra)
    return client.post(path, data=data, content_type='multipart/form-data')


def _preview(client, csv_bytes, filename='positions.csv'):
    """POST preview (gets job_id), then poll GET until the job is done."""
    r = _upload(client, '/api/brokerage-import/preview', csv_bytes, filename)
    assert r.status_code == 200
    job_id = r.get_json()['job_id']
    for _ in range(50):  # up to 5 seconds
        status = client.get(f'/api/brokerage-import/preview/{job_id}').get_json()
        if status['status'] != 'pending':
            return status
        time.sleep(0.1)
    raise TimeoutError('Preview job did not complete in time')


class TestBrokerageImportPage:
    def test_page_redirects_to_import_tab(self, client, db):
        r = client.get('/brokerage-import')
        assert r.status_code == 302
        assert '/import' in r.headers['Location']
        assert 'tab=brokerage' in r.headers['Location']


class TestPreview:
    def test_no_file_returns_400(self, client, db):
        r = client.post('/api/brokerage-import/preview')
        assert r.status_code == 400

    def test_unsupported_extension_returns_400(self, client, db):
        r = _upload(client, '/api/brokerage-import/preview', b'hello', filename='notes.txt')
        assert r.status_code == 400

    def test_post_returns_job_id_immediately(self, client, db):
        r = _upload(client, '/api/brokerage-import/preview', FIDELITY_CSV)
        assert r.status_code == 200
        body = r.get_json()
        assert 'job_id' in body
        assert body['status'] == 'pending'

    def test_poll_unknown_job_returns_404(self, client, db):
        r = client.get('/api/brokerage-import/preview/nonexistent-id')
        assert r.status_code == 404

    def test_valid_csv_parses_to_expected_shape(self, client, db):
        result = _preview(client, FIDELITY_CSV)
        assert result['status'] == 'done'
        body = result['result']
        assert body['institution'] == 'fidelity'
        assert len(body['accounts']) == 1
        assert {p['ticker'] for p in body['accounts'][0]['positions']} == {'VTI'}

    def test_unparseable_file_returns_errors(self, client, db):
        result = _preview(client, b'not,a,recognizable,export\nfoo,bar,baz,qux')
        assert result['status'] in ('done', 'error')
        # errors may surface inside result or at top level depending on parse path
        if result['status'] == 'done':
            assert result['result']['errors']
        else:
            assert result['errors']

    def test_preview_does_not_write_holdings(self, client, db):
        make_investment_account(db.session, name='Fidelity Brokerage')
        _preview(client, FIDELITY_CSV)
        assert Holding.query.count() == 0


class TestCommit:
    def _commit(self, client, mapping, month='2026-07', csv_bytes=FIDELITY_CSV, transaction_categories=None):
        extra = {'mapping': json.dumps(mapping), 'month': month}
        if transaction_categories is not None:
            extra['transaction_categories'] = json.dumps(transaction_categories)
        return _upload(client, '/api/brokerage-import/commit', csv_bytes, extra=extra)

    def test_missing_file_returns_400(self, client, db):
        r = client.post('/api/brokerage-import/commit', data={'month': '2026-07'})
        assert r.status_code == 400

    def test_missing_month_returns_400(self, client, db):
        r = _upload(client, '/api/brokerage-import/commit', FIDELITY_CSV, extra={'mapping': '{}'})
        assert r.status_code == 400

    def test_malformed_mapping_returns_400(self, client, db):
        r = _upload(client, '/api/brokerage-import/commit', FIDELITY_CSV,
                     extra={'mapping': 'not-json', 'month': '2026-07'})
        assert r.status_code == 400

    def test_malformed_transaction_categories_returns_400(self, client, db):
        r = _upload(client, '/api/brokerage-import/commit', FIDELITY_CSV,
                     extra={'mapping': '{}', 'month': '2026-07', 'transaction_categories': 'not-json'})
        assert r.status_code == 400

    def test_mapping_references_nonexistent_account_returns_400(self, client, db):
        key = 'fidelity:Individual:5678'
        r = self._commit(client, {key: 99999})
        assert r.status_code == 400

    def test_mapping_references_non_investment_account_returns_400(self, client, db):
        cash_acct = make_investment_account(db.session, name='Checking')
        cash_acct.category = 'checking'
        _db.session.commit()
        key = 'fidelity:Individual:5678'
        r = self._commit(client, {key: cash_acct.id})
        assert r.status_code == 400

    def test_full_success_creates_holdings_and_snapshot(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        key = 'fidelity:Individual:5678'
        r = self._commit(client, {key: acct.id})
        assert r.status_code == 200
        body = r.get_json()
        assert body['success'] is True
        assert body['holdings_created'] == 1  # VTI (SPAXX folds into cash)
        assert body['snapshots_upserted'] == 1

        holding = Holding.query.filter_by(account_id=acct.id, ticker='VTI').first()
        assert holding is not None
        assert float(holding.shares) == 10.5

        snapshot = AccountSnapshot.query.filter_by(account_id=acct.id).first()
        assert snapshot is not None
        assert float(snapshot.balance) == 3091.16  # 2941.16 + 150.00 cash

    def test_full_success_triggers_metrics_recalculation(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        key = 'fidelity:Individual:5678'
        self._commit(client, {key: acct.id})
        metric = CalculatedMetric.query.filter_by(metric_date=__import__('datetime').date(2026, 7, 1)).first()
        assert metric is not None
        assert metric.total_assets is not None

    def test_full_success_writes_import_log(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        key = 'fidelity:Individual:5678'
        self._commit(client, {key: acct.id})
        log = ImportLog.query.order_by(ImportLog.id.desc()).first()
        assert log is not None
        assert log.status in ('success', 'partial')

    def test_reactivates_previously_archived_ticker_not_duplicated(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        holding = make_holding(db.session, acct.id, ticker='VTI', shares=1, price=100)
        holding.is_active = False
        _db.session.commit()

        key = 'fidelity:Individual:5678'
        self._commit(client, {key: acct.id})

        holdings = Holding.query.filter_by(account_id=acct.id, ticker='VTI').all()
        assert len(holdings) == 1
        assert holdings[0].is_active is True
        assert float(holdings[0].shares) == 10.5

    def test_active_holding_absent_from_file_gets_archived(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        stale_holding = make_holding(db.session, acct.id, ticker='OLDFUND', shares=5, price=10)

        key = 'fidelity:Individual:5678'
        self._commit(client, {key: acct.id})

        _db.session.refresh(stale_holding)
        assert stale_holding.is_active is False

    def test_ai_disabled_still_creates_holding_without_classification(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        key = 'fidelity:Individual:5678'
        r = self._commit(client, {key: acct.id})
        body = r.get_json()
        assert body['success'] is True
        assert body['classified'] == 0
        assert body['classification_skipped'] == 1
        holding = Holding.query.filter_by(account_id=acct.id, ticker='VTI').first()
        assert holding is not None  # created despite no classification

    def test_skipped_account_not_written(self, client, db):
        make_investment_account(db.session, name='Fidelity Brokerage')
        r = self._commit(client, {'fidelity:Individual:5678': None})
        assert r.status_code == 400  # nothing mapped

    def test_db_failure_rolls_back_returns_500(self, client, db):
        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        key = 'fidelity:Individual:5678'
        with patch.object(_db.session, 'commit', side_effect=Exception('boom')):
            r = self._commit(client, {key: acct.id})
        assert r.status_code == 500
        assert Holding.query.count() == 0

    def test_pure_brokerage_commit_does_not_touch_unrelated_spending_entries(self, client, db):
        # A brokerage-only import must never run the SpendingEntry rollup —
        # a manual entry whose account_name happens to match a
        # TransactionCategory title (but has no BankTransaction backing it)
        # must survive untouched.
        make_transaction_category(db.session, title='Chase Card', kind='expense')
        _db.session.add(SpendingEntry(
            entry_date=date(2026, 7, 1), account_name='Chase Card', amount=123.45, entry_type='expense',
        ))
        _db.session.commit()

        acct = make_investment_account(db.session, name='Fidelity Brokerage')
        key = 'fidelity:Individual:5678'
        self._commit(client, {key: acct.id})

        entry = SpendingEntry.query.filter_by(entry_date=date(2026, 7, 1), account_name='Chase Card').first()
        assert entry is not None
        assert float(entry.amount) == 123.45


class TestCommitTransactions:
    """Checking/savings statement transactions -> BankTransaction + SpendingEntry rollup."""

    def _checking_account(self, name='Schwab Checking'):
        acct = Account(name=name, account_type='asset', category='checking', is_active=True)
        _db.session.add(acct)
        _db.session.commit()
        return acct

    def _preview_schwab_bank(self, client):
        result = _preview(client, _schwab_bank_activity_bytes(), filename='statement.pdf')
        assert result['status'] == 'done'
        return result['result']

    def _commit_pdf(self, client, mapping, transaction_categories, month='2026-07'):
        return _upload(
            client, '/api/brokerage-import/commit', _schwab_bank_activity_bytes(), filename='statement.pdf',
            extra={
                'mapping': json.dumps(mapping), 'month': month,
                'transaction_categories': json.dumps(transaction_categories),
            },
        )

    def _categorize_by_keyword(self, transactions, payroll_id, chase_id, transfer_id, fallback_id):
        txn_cats = {}
        for t in transactions:
            desc = t['description'].lower()
            if 'payroll' in desc:
                txn_cats[t['key']] = payroll_id
            elif 'chase' in desc:
                txn_cats[t['key']] = chase_id
            elif 'wealthfront' in desc:
                txn_cats[t['key']] = transfer_id
            else:
                txn_cats[t['key']] = fallback_id
        return txn_cats

    def test_preview_includes_transactions_for_balance_only_account(self, client, db):
        preview = self._preview_schwab_bank(client)
        acct_preview = preview['accounts'][0]
        assert acct_preview['balance_only'] is True
        assert len(acct_preview['transactions']) == 5

    def test_full_success_creates_bank_transactions_and_spending_rollup(self, client, db):
        acct = self._checking_account()
        payroll = make_transaction_category(db.session, title='Paycheck/Salary', kind='income')
        chase = make_transaction_category(db.session, title='Chase Card', kind='expense')
        transfer = make_transaction_category(db.session, title='Internal Transfer', kind='transfer')
        uncategorized = make_transaction_category(db.session, title='Uncategorized', kind='expense')

        preview = self._preview_schwab_bank(client)
        acct_preview = preview['accounts'][0]
        txn_cats = self._categorize_by_keyword(
            acct_preview['transactions'], payroll.id, chase.id, transfer.id, uncategorized.id,
        )

        r = self._commit_pdf(client, {acct_preview['key']: acct.id}, txn_cats)
        assert r.status_code == 200
        body = r.get_json()
        assert body['transactions_imported'] == 5
        assert BankTransaction.query.filter_by(account_id=acct.id).count() == 5

        entries = {e.account_name: float(e.amount) for e in SpendingEntry.query.filter_by(entry_date=date(2026, 7, 1)).all()}
        assert entries['Paycheck/Salary'] == pytest.approx(6435.56)
        assert entries['Chase Card'] == pytest.approx(2183.30)
        assert 'Internal Transfer' not in entries  # transfer categories never get a SpendingEntry

    def test_metrics_reflect_transaction_rollup(self, client, db):
        acct = self._checking_account()
        payroll = make_transaction_category(db.session, title='Paycheck/Salary', kind='income')
        chase = make_transaction_category(db.session, title='Chase Card', kind='expense')
        transfer = make_transaction_category(db.session, title='Internal Transfer', kind='transfer')
        uncategorized = make_transaction_category(db.session, title='Uncategorized', kind='expense')

        preview = self._preview_schwab_bank(client)
        acct_preview = preview['accounts'][0]
        txn_cats = self._categorize_by_keyword(
            acct_preview['transactions'], payroll.id, chase.id, transfer.id, uncategorized.id,
        )
        self._commit_pdf(client, {acct_preview['key']: acct.id}, txn_cats)

        metric = CalculatedMetric.query.filter_by(metric_date=date(2026, 7, 1)).first()
        assert metric is not None
        assert float(metric.total_income) == pytest.approx(6435.56)

    def test_reimport_same_month_is_idempotent(self, client, db):
        acct = self._checking_account()
        payroll = make_transaction_category(db.session, title='Paycheck/Salary', kind='income')
        chase = make_transaction_category(db.session, title='Chase Card', kind='expense')
        transfer = make_transaction_category(db.session, title='Internal Transfer', kind='transfer')
        uncategorized = make_transaction_category(db.session, title='Uncategorized', kind='expense')

        preview = self._preview_schwab_bank(client)
        acct_preview = preview['accounts'][0]
        txn_cats = self._categorize_by_keyword(
            acct_preview['transactions'], payroll.id, chase.id, transfer.id, uncategorized.id,
        )

        self._commit_pdf(client, {acct_preview['key']: acct.id}, txn_cats)
        self._commit_pdf(client, {acct_preview['key']: acct.id}, txn_cats)

        assert BankTransaction.query.filter_by(account_id=acct.id).count() == 5
        assert SpendingEntry.query.filter_by(entry_date=date(2026, 7, 1), account_name='Chase Card').count() == 1

    def test_reclassifying_moves_spending_entry_and_removes_stale(self, client, db):
        acct = self._checking_account()
        uncategorized = make_transaction_category(db.session, title='Uncategorized', kind='expense')
        chase = make_transaction_category(db.session, title='Chase Card', kind='expense')

        preview = self._preview_schwab_bank(client)
        acct_preview = preview['accounts'][0]
        all_uncategorized = {t['key']: uncategorized.id for t in acct_preview['transactions']}
        self._commit_pdf(client, {acct_preview['key']: acct.id}, all_uncategorized)
        assert SpendingEntry.query.filter_by(entry_date=date(2026, 7, 1), account_name='Uncategorized').first() is not None

        all_chase = {t['key']: chase.id for t in acct_preview['transactions']}
        self._commit_pdf(client, {acct_preview['key']: acct.id}, all_chase)

        assert SpendingEntry.query.filter_by(entry_date=date(2026, 7, 1), account_name='Uncategorized').first() is None
        chase_entry = SpendingEntry.query.filter_by(entry_date=date(2026, 7, 1), account_name='Chase Card').first()
        assert chase_entry is not None

    def test_transaction_with_no_category_mapping_is_skipped_with_warning(self, client, db):
        acct = self._checking_account()
        make_transaction_category(db.session, title='Uncategorized', kind='expense')

        preview = self._preview_schwab_bank(client)
        acct_preview = preview['accounts'][0]

        r = self._commit_pdf(client, {acct_preview['key']: acct.id}, {})  # no categories chosen at all
        assert r.status_code == 200
        body = r.get_json()
        assert body['transactions_imported'] == 0
        assert body['warnings']
        assert BankTransaction.query.filter_by(account_id=acct.id).count() == 0
