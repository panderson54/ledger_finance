"""
Tests for cascade metric recalculation (PR-03).

When a historical month's snapshot is edited, recalculate_metrics() must
propagate one level forward so the next month's monthly_change fields
stay accurate. The cascade is intentionally single-level (non-recursive).
"""
from datetime import date
from decimal import Decimal

import pytest

from app import db as _db
from app.metrics_service import recalculate_metrics
from app.models import Account, AccountSnapshot, CalculatedMetric


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _asset(name="Savings", balance=10000.0, d=date(2024, 1, 1)):
    acct = Account(name=name, account_type="asset", category="savings",
                   is_liquid=True, include_in_networth=True, is_active=True)
    _db.session.add(acct)
    _db.session.flush()
    snap = AccountSnapshot(account_id=acct.id, snapshot_date=d, balance=balance)
    _db.session.add(snap)
    _db.session.commit()
    return acct


def _add_snap(account_id, balance, d):
    s = AccountSnapshot(account_id=account_id, snapshot_date=d, balance=balance)
    _db.session.add(s)
    _db.session.commit()
    return s


def _metric(d):
    return CalculatedMetric.query.filter_by(metric_date=d).first()


# ---------------------------------------------------------------------------
# Success case: cascade updates next month's change fields
# ---------------------------------------------------------------------------

class TestCascadeUpdatesNextMonth:
    def test_cascade_corrects_next_month_change_amount(self, db):
        acct = _asset(balance=10000.0, d=date(2024, 1, 1))
        _add_snap(acct.id, 11000.0, date(2024, 2, 1))

        recalculate_metrics(date(2024, 1, 1))
        recalculate_metrics(date(2024, 2, 1))

        assert _metric(date(2024, 2, 1)).monthly_change_amount == Decimal('1000.00')

        # Edit Jan balance — cascade should refresh Feb's change fields
        jan_snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 1, 1)).first()
        jan_snap.balance = Decimal('10500.00')
        _db.session.commit()

        recalculate_metrics(date(2024, 1, 1))

        feb_m = CalculatedMetric.query.filter_by(metric_date=date(2024, 2, 1)).first()
        assert feb_m.monthly_change_amount == Decimal('500.00')

    def test_cascade_corrects_next_month_change_pct(self, db):
        acct = _asset(balance=10000.0, d=date(2024, 3, 1))
        _add_snap(acct.id, 11000.0, date(2024, 4, 1))

        recalculate_metrics(date(2024, 3, 1))
        recalculate_metrics(date(2024, 4, 1))

        apr_m = _metric(date(2024, 4, 1))
        assert apr_m.monthly_change_pct == pytest.approx(Decimal('10.00'), abs=Decimal('0.01'))

        # Halve the Mar increase by doubling Mar balance
        mar_snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 3, 1)).first()
        mar_snap.balance = Decimal('11000.00')
        _db.session.commit()

        recalculate_metrics(date(2024, 3, 1))

        apr_m = CalculatedMetric.query.filter_by(metric_date=date(2024, 4, 1)).first()
        assert apr_m.monthly_change_pct == pytest.approx(Decimal('0.00'), abs=Decimal('0.01'))


# ---------------------------------------------------------------------------
# No-op case: no cascade when next month metric is absent
# ---------------------------------------------------------------------------

class TestCascadeNoOp:
    def test_no_cascade_when_next_month_absent(self, db):
        _asset(balance=10000.0, d=date(2024, 6, 1))
        recalculate_metrics(date(2024, 6, 1))

        # No Jul metric — cascade must not create one
        assert _metric(date(2024, 7, 1)) is None

    def test_recalculate_with_cascade_false_skips_next_month(self, db):
        acct = _asset(balance=10000.0, d=date(2024, 8, 1))
        _add_snap(acct.id, 12000.0, date(2024, 9, 1))

        recalculate_metrics(date(2024, 8, 1))
        recalculate_metrics(date(2024, 9, 1))

        # Change Aug and recalculate with cascade disabled
        aug_snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 8, 1)).first()
        aug_snap.balance = Decimal('11000.00')
        _db.session.commit()

        recalculate_metrics(date(2024, 8, 1), _cascade=False)

        # Sep change_amount should still reflect old Aug baseline (10000, not 11000)
        sep_m = CalculatedMetric.query.filter_by(metric_date=date(2024, 9, 1)).first()
        assert sep_m.monthly_change_amount == Decimal('2000.00')


# ---------------------------------------------------------------------------
# Single-level invariant: cascade does not propagate past N+1
# ---------------------------------------------------------------------------

class TestCascadeIsSingleLevel:
    def test_cascade_stops_at_one_level(self, db):
        """Edit month N → cascade updates N+1 only; N+2 stays stale."""
        acct = _asset(balance=10000.0, d=date(2024, 10, 1))
        _add_snap(acct.id, 11000.0, date(2024, 11, 1))
        _add_snap(acct.id, 12000.0, date(2024, 12, 1))

        recalculate_metrics(date(2024, 10, 1))
        recalculate_metrics(date(2024, 11, 1))
        recalculate_metrics(date(2024, 12, 1))

        dec_m = _metric(date(2024, 12, 1))
        assert dec_m.monthly_change_amount == Decimal('1000.00')

        # Edit Oct — cascade goes Oct→Nov; Dec is intentionally untouched
        oct_snap = AccountSnapshot.query.filter_by(
            account_id=acct.id, snapshot_date=date(2024, 10, 1)).first()
        oct_snap.balance = Decimal('10500.00')
        _db.session.commit()

        recalculate_metrics(date(2024, 10, 1))

        nov_m = CalculatedMetric.query.filter_by(metric_date=date(2024, 11, 1)).first()
        assert nov_m.monthly_change_amount == Decimal('500.00')

        # Dec must still show the old value (single-level cascade)
        dec_m = CalculatedMetric.query.filter_by(metric_date=date(2024, 12, 1)).first()
        assert dec_m.monthly_change_amount == Decimal('1000.00')
