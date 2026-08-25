"""
Tests for /api/transaction-categories CRUD routes (app/routes/settings.py).
"""
from datetime import date

from app import db as _db
from app.models import TransactionCategory, BankTransaction, Account


class TestListTransactionCategories:
    def test_empty(self, client, db):
        assert client.get('/api/transaction-categories').get_json() == []

    def test_returns_rows_ordered(self, client, db):
        _db.session.add(TransactionCategory(title='Zeta', kind='expense', display_order=1))
        _db.session.add(TransactionCategory(title='Alpha', kind='income', display_order=0))
        _db.session.commit()
        data = client.get('/api/transaction-categories').get_json()
        assert [c['title'] for c in data] == ['Alpha', 'Zeta']


class TestCreateTransactionCategory:
    def test_success(self, client, db):
        r = client.post('/api/transaction-categories', json={
            'title': 'Utilities', 'kind': 'expense', 'description': 'Power, water, etc.',
        })
        assert r.status_code == 201
        data = r.get_json()
        assert data['title'] == 'Utilities'
        assert data['kind'] == 'expense'
        assert data['description'] == 'Power, water, etc.'
        assert data['is_active'] is True

    def test_missing_body(self, client, db):
        r = client.post('/api/transaction-categories', content_type='application/json', data='')
        assert r.status_code == 400

    def test_missing_title(self, client, db):
        r = client.post('/api/transaction-categories', json={'kind': 'expense'})
        assert r.status_code == 400

    def test_invalid_kind(self, client, db):
        r = client.post('/api/transaction-categories', json={'title': 'X', 'kind': 'bogus'})
        assert r.status_code == 400

    def test_duplicate_title_rejected(self, client, db):
        _db.session.add(TransactionCategory(title='Groceries', kind='expense'))
        _db.session.commit()
        r = client.post('/api/transaction-categories', json={'title': 'groceries', 'kind': 'expense'})
        assert r.status_code == 400
        assert 'already exists' in r.get_json()['error']


class TestUpdateTransactionCategory:
    def test_success(self, client, db):
        cat = TransactionCategory(title='Old', kind='expense')
        _db.session.add(cat)
        _db.session.commit()
        r = client.put(f'/api/transaction-categories/{cat.id}', json={
            'title': 'New', 'description': 'updated', 'is_active': False,
        })
        assert r.status_code == 200
        data = r.get_json()
        assert data['title'] == 'New'
        assert data['description'] == 'updated'
        assert data['is_active'] is False

    def test_not_found(self, client, db):
        assert client.put('/api/transaction-categories/99999', json={'title': 'X'}).status_code == 404

    def test_invalid_kind(self, client, db):
        cat = TransactionCategory(title='T', kind='expense')
        _db.session.add(cat)
        _db.session.commit()
        r = client.put(f'/api/transaction-categories/{cat.id}', json={'kind': 'bogus'})
        assert r.status_code == 400

    def test_empty_title_rejected(self, client, db):
        cat = TransactionCategory(title='T', kind='expense')
        _db.session.add(cat)
        _db.session.commit()
        r = client.put(f'/api/transaction-categories/{cat.id}', json={'title': ''})
        assert r.status_code == 400

    def test_duplicate_title_rejected(self, client, db):
        _db.session.add(TransactionCategory(title='A', kind='expense'))
        b = TransactionCategory(title='B', kind='expense')
        _db.session.add(b)
        _db.session.commit()
        r = client.put(f'/api/transaction-categories/{b.id}', json={'title': 'A'})
        assert r.status_code == 400


class TestDeleteTransactionCategory:
    def test_success(self, client, db):
        cat = TransactionCategory(title='Del', kind='expense')
        _db.session.add(cat)
        _db.session.commit()
        r = client.delete(f'/api/transaction-categories/{cat.id}')
        assert r.status_code == 200
        assert r.get_json()['success'] is True
        assert _db.session.get(TransactionCategory, cat.id) is None

    def test_not_found(self, client, db):
        assert client.delete('/api/transaction-categories/99999').status_code == 404

    def test_blocked_when_referenced_by_transactions(self, client, db):
        cat = TransactionCategory(title='Used', kind='expense')
        account = Account(name='Checking', account_type='asset', category='checking', is_active=True)
        _db.session.add_all([cat, account])
        _db.session.commit()
        _db.session.add(BankTransaction(
            account_id=account.id, transaction_date=date(2026, 7, 1), month_date=date(2026, 7, 1),
            description='Test', amount=10, direction='debit', category_id=cat.id,
        ))
        _db.session.commit()
        r = client.delete(f'/api/transaction-categories/{cat.id}')
        assert r.status_code == 400
        assert _db.session.get(TransactionCategory, cat.id) is not None
