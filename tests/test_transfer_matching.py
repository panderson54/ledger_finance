"""
Unit tests for app/brokerage_import/transfer_matching.py.
"""
from datetime import date

from app.brokerage_import.transfer_matching import find_transfer_matches


def _txn(key, account_key, d, amount, direction):
    return {'key': key, 'account_key': account_key, 'date': d, 'amount': amount, 'direction': direction}


class TestFindTransferMatches:
    def test_matches_opposite_direction_same_amount_different_account(self):
        txns = [
            _txn('a1', 'schwab', date(2026, 7, 29), 6200.0, 'debit'),
            _txn('b1', 'wealthfront', date(2026, 7, 29), 6200.0, 'credit'),
        ]
        matches = find_transfer_matches(txns)
        assert matches == {'a1': 'b1', 'b1': 'a1'}

    def test_no_match_outside_window(self):
        txns = [
            _txn('a1', 'schwab', date(2026, 7, 1), 500.0, 'debit'),
            _txn('b1', 'wealthfront', date(2026, 7, 20), 500.0, 'credit'),
        ]
        matches = find_transfer_matches(txns, window_days=5)
        assert matches == {}

    def test_no_match_same_account(self):
        txns = [
            _txn('a1', 'schwab', date(2026, 7, 1), 500.0, 'debit'),
            _txn('a2', 'schwab', date(2026, 7, 2), 500.0, 'credit'),
        ]
        matches = find_transfer_matches(txns)
        assert matches == {}

    def test_no_match_different_amount(self):
        txns = [
            _txn('a1', 'schwab', date(2026, 7, 1), 500.0, 'debit'),
            _txn('b1', 'wealthfront', date(2026, 7, 1), 499.0, 'credit'),
        ]
        matches = find_transfer_matches(txns)
        assert matches == {}

    def test_no_match_same_direction(self):
        txns = [
            _txn('a1', 'schwab', date(2026, 7, 1), 500.0, 'debit'),
            _txn('b1', 'wealthfront', date(2026, 7, 1), 500.0, 'debit'),
        ]
        matches = find_transfer_matches(txns)
        assert matches == {}

    def test_empty_input(self):
        assert find_transfer_matches([]) == {}

    def test_each_partner_used_at_most_once(self):
        # Three same-amount debits on account A, only one same-amount credit on account B —
        # only the closest-dated debit should be matched, not all three.
        txns = [
            _txn('a1', 'schwab', date(2026, 7, 1), 500.0, 'debit'),
            _txn('a2', 'schwab', date(2026, 7, 3), 500.0, 'debit'),
            _txn('a3', 'schwab', date(2026, 7, 10), 500.0, 'debit'),
            _txn('b1', 'wealthfront', date(2026, 7, 2), 500.0, 'credit'),
        ]
        matches = find_transfer_matches(txns)
        assert matches.get('b1') == 'a1'
        assert 'a2' not in matches
        assert 'a3' not in matches
