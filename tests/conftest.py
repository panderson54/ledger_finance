import os

# Must be set before app is imported so create_app picks up the in-memory URI
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from sqlalchemy.pool import StaticPool

from app import create_app
from app import db as _db
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric, AssetAllocation, AppSetting, TickerClassification


@pytest.fixture(scope="session")
def app():
    application = create_app()
    application.config.update(
        TESTING=True,
        SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
        SQLALCHEMY_ENGINE_OPTIONS={
            "connect_args": {"check_same_thread": False},
            "poolclass": StaticPool,
        },
    )
    with application.app_context():
        _db.create_all()
    yield application


@pytest.fixture(scope="function")
def db(app):
    with app.app_context():
        yield _db
        # Teardown: delete all rows in dependency order
        _db.session.query(AccountSnapshot).delete()
        _db.session.query(SpendingEntry).delete()
        _db.session.query(CalculatedMetric).delete()
        _db.session.query(AssetAllocation).delete()
        _db.session.query(Account).delete()
        _db.session.query(TickerClassification).delete()
        _db.session.query(AppSetting).delete()
        _db.session.commit()


@pytest.fixture(scope="function")
def client(app, db):
    return app.test_client()


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_account(db):
    account = Account(
        name="Test Checking",
        account_type="asset",
        category="checking",
        is_liquid=True,
        include_in_networth=True,
        is_active=True,
    )
    _db.session.add(account)
    _db.session.commit()
    return account


@pytest.fixture
def sample_liability(db):
    account = Account(
        name="Test Credit Card",
        account_type="liability",
        category="credit_card",
        is_liquid=False,
        include_in_networth=True,
        is_active=True,
    )
    _db.session.add(account)
    _db.session.commit()
    return account


@pytest.fixture
def sample_snapshot(db, sample_account):
    from datetime import date
    snapshot = AccountSnapshot(
        account_id=sample_account.id,
        snapshot_date=date(2024, 1, 1),
        balance=5000.00,
    )
    _db.session.add(snapshot)
    _db.session.commit()
    return snapshot


@pytest.fixture
def sample_spending(db):
    from datetime import date
    entries = [
        SpendingEntry(
            entry_date=date(2024, 1, 1),
            account_name="Employer",
            amount=3000.00,
            entry_type="income",
        ),
        SpendingEntry(
            entry_date=date(2024, 1, 1),
            account_name="Chase",
            amount=1200.00,
            entry_type="expense",
        ),
    ]
    _db.session.add_all(entries)
    _db.session.commit()
    return entries
