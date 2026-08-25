"""
Tests for app/expense_categorization_service.py.
"""
import json
from unittest.mock import patch

import pytest

from tests.conftest import make_anthropic_mock_client
from app.expense_categorization_service import categorize_transactions

TRANSACTIONS = [
    {'key': 't1', 'description': 'CHASE CREDIT CRD EPAY', 'amount': 2183.30, 'direction': 'debit'},
    {'key': 't2', 'description': 'META PAYROLL', 'amount': 6435.56, 'direction': 'credit'},
]
CATEGORIES = [
    {'id': 1, 'title': 'Chase Card', 'description': 'Payments to Chase', 'kind': 'expense'},
    {'id': 2, 'title': 'Paycheck/Salary', 'description': '', 'kind': 'income'},
]


class TestCategorizeTransactions:
    def test_success(self):
        response = [
            {'key': 't1', 'category_id': 1, 'confidence': 'high'},
            {'key': 't2', 'category_id': 2, 'confidence': 'high'},
        ]
        mock_client = make_anthropic_mock_client(response_json=response)
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            result = categorize_transactions(TRANSACTIONS, CATEGORIES, ['Wealthfront'], 'sk-test')
        assert result == response
        mock_client.messages.create.assert_called_once()

    def test_empty_transactions_is_noop(self):
        with patch('app.expense_categorization_service.make_anthropic_client') as mock_ctor:
            result = categorize_transactions([], CATEGORIES, [], 'sk-test')
        assert result == []
        mock_ctor.assert_not_called()

    def test_invalid_category_id_raises(self):
        response = [
            {'key': 't1', 'category_id': 999, 'confidence': 'high'},
            {'key': 't2', 'category_id': 2, 'confidence': 'high'},
        ]
        mock_client = make_anthropic_mock_client(response_json=response)
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(ValueError, match='unknown category id'):
                categorize_transactions(TRANSACTIONS, CATEGORIES, [], 'sk-test')

    def test_unknown_transaction_key_raises(self):
        response = [
            {'key': 'bogus', 'category_id': 1, 'confidence': 'high'},
            {'key': 't2', 'category_id': 2, 'confidence': 'high'},
        ]
        mock_client = make_anthropic_mock_client(response_json=response)
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(ValueError, match='unknown transaction key'):
                categorize_transactions(TRANSACTIONS, CATEGORIES, [], 'sk-test')

    def test_invalid_confidence_raises(self):
        response = [
            {'key': 't1', 'category_id': 1, 'confidence': 'certain'},
            {'key': 't2', 'category_id': 2, 'confidence': 'high'},
        ]
        mock_client = make_anthropic_mock_client(response_json=response)
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(ValueError, match='Invalid confidence'):
                categorize_transactions(TRANSACTIONS, CATEGORIES, [], 'sk-test')

    def test_non_array_response_raises(self):
        mock_client = make_anthropic_mock_client(response_json={'not': 'a list'})
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(ValueError, match='JSON array'):
                categorize_transactions(TRANSACTIONS, CATEGORIES, [], 'sk-test')

    def test_malformed_json_raises(self):
        mock_client = make_anthropic_mock_client(response_text='not json at all')
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(ValueError):
                categorize_transactions(TRANSACTIONS, CATEGORIES, [], 'sk-test')

    def test_api_failure_propagates(self):
        mock_client = make_anthropic_mock_client()
        mock_client.messages.create.side_effect = RuntimeError('rate limited')
        with patch('app.expense_categorization_service.make_anthropic_client', return_value=mock_client):
            with pytest.raises(RuntimeError, match='rate limited'):
                categorize_transactions(TRANSACTIONS, CATEGORIES, [], 'sk-test')

    def test_missing_api_key_raises(self):
        with pytest.raises(RuntimeError):
            categorize_transactions(TRANSACTIONS, CATEGORIES, [], '')
