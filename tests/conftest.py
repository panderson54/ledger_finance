import json
import os

# Must be set before app is imported so create_app picks up the in-memory URI
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

import pytest
from unittest.mock import MagicMock
from sqlalchemy.pool import StaticPool

from app import create_app
from app import db as _db
from app.models import Account, AccountSnapshot, SpendingEntry, CalculatedMetric, AssetAllocation, AppSetting, TickerClassification, DividendData, Holding, HoldingAllocation


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
        _db.session.query(HoldingAllocation).delete()
        _db.session.query(Holding).delete()
        _db.session.query(Account).delete()
        _db.session.query(TickerClassification).delete()
        _db.session.query(DividendData).delete()
        _db.session.query(AppSetting).delete()
        _db.session.commit()


@pytest.fixture(scope="function")
def client(app, db):
    return app.test_client()


# ---------------------------------------------------------------------------
# Shared AI mock helpers
# Used by test_classification.py and test_dividend.py
# ---------------------------------------------------------------------------

def make_anthropic_mock_client(response_json: dict | None = None, response_text: str | None = None):
    """
    Return a mock anthropic client whose messages.create() returns a single text block.

    Pass either response_json (dict) or response_text (str). If both are None,
    returns an empty dict JSON response.
    """
    if response_text is None:
        response_text = json.dumps(response_json or {})
    block = MagicMock()
    block.text = response_text
    resp = MagicMock()
    resp.content = [block]
    client = MagicMock()
    client.messages.create.return_value = resp
    return client


def seed_ai_settings(db_session, api_key: str = "sk-test-key"):
    """Seed AppSetting rows needed to enable Claude AI features in tests."""
    db_session.add(AppSetting(key='claude_classification_enabled', value='true', description='test'))
    db_session.add(AppSetting(key='anthropic_api_key', value=api_key, description='test'))
    db_session.commit()


def make_investment_account(db_session, name: str = "Brokerage", tax_status: str = "taxable") -> Account:
    """Create and persist a standard brokerage account for testing."""
    acct = Account(
        name=name,
        account_type="asset",
        category="brokerage",
        is_liquid=True,
        include_in_networth=True,
        is_active=True,
        tax_status=tax_status,
    )
    db_session.add(acct)
    db_session.commit()
    return acct


def make_holding(db_session, account_id: int, ticker: str = "VYM", shares: float = 100, price: float = 50.0) -> Holding:
    """Create and persist a holding for testing."""
    h = Holding(
        account_id=account_id,
        ticker=ticker,
        shares=shares,
        last_price=price,
        is_active=True,
    )
    db_session.add(h)
    db_session.commit()
    return h


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
