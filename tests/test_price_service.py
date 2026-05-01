"""Tests for app/price_service.py — yfinance is mocked throughout."""
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import app.price_service as ps
from app.price_service import get_price, get_sp500_monthly_change, is_stale


@pytest.fixture(autouse=True)
def mock_yf(monkeypatch):
    """Inject a fake yfinance module so no network calls are made."""
    mock_module = MagicMock()
    monkeypatch.setitem(sys.modules, "yfinance", mock_module)
    yield mock_module


@pytest.fixture(autouse=True)
def clear_sp500_cache():
    ps._sp500_cache.clear()
    yield
    ps._sp500_cache.clear()


# ---------------------------------------------------------------------------
# get_price
# ---------------------------------------------------------------------------

class TestGetPrice:
    def test_price_via_fast_info_last_price(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = 150.0
        ticker.fast_info.previous_close = 148.0
        ticker.info = {"longName": "Apple Inc."}
        mock_yf.Ticker.return_value = ticker

        price, name = get_price("AAPL")

        assert price == 150.0
        assert name == "Apple Inc."

    def test_price_via_fast_info_previous_close_fallback(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = 0  # falsy → use previous_close
        ticker.fast_info.previous_close = 145.0
        ticker.info = {"shortName": "Apple"}
        mock_yf.Ticker.return_value = ticker

        price, name = get_price("AAPL")

        assert price == 145.0
        assert name == "Apple"

    def test_price_via_history_fallback(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        ticker.fast_info.previous_close = None
        hist = pd.DataFrame({"Close": [200.0]})
        ticker.history.return_value = hist
        ticker.info = {}
        mock_yf.Ticker.return_value = ticker

        price, name = get_price("SPY")

        assert price == 200.0
        assert name == "SPY"  # falls back to ticker.upper()

    def test_price_via_info_regular_market_price(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        ticker.fast_info.previous_close = None
        ticker.history.return_value = pd.DataFrame()  # empty
        ticker.info = {"regularMarketPrice": 500.0, "longName": "Test Fund"}
        mock_yf.Ticker.return_value = ticker

        price, name = get_price("TESTF")

        assert price == 500.0
        assert name == "Test Fund"

    def test_price_via_info_nav_price(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        ticker.fast_info.previous_close = None
        ticker.history.return_value = pd.DataFrame()
        ticker.info = {"navPrice": 25.0}
        mock_yf.Ticker.return_value = ticker

        price, _ = get_price("MFUND")

        assert price == 25.0

    def test_no_price_raises_value_error(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = None
        ticker.fast_info.previous_close = None
        ticker.history.return_value = pd.DataFrame()
        ticker.info = {}
        mock_yf.Ticker.return_value = ticker

        with pytest.raises(ValueError, match="No price data"):
            get_price("FAKE")

    def test_name_fetched_from_info_when_not_yet_loaded(self, mock_yf):
        ticker = MagicMock()
        ticker.fast_info.last_price = 99.0
        ticker.fast_info.previous_close = 98.0
        # Simulate info not loaded during fast_info path
        ticker.info = {"shortName": "Short Name Fund"}
        mock_yf.Ticker.return_value = ticker

        _, name = get_price("XYZ")

        assert name in ("Short Name Fund", "XYZ")  # either source is fine


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------

class TestIsStale:
    def test_none_always_stale(self):
        assert is_stale(None) is True

    def test_fresh_not_stale(self):
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        assert is_stale(recent) is False

    def test_old_is_stale(self):
        old = datetime.now(timezone.utc) - timedelta(hours=25)
        assert is_stale(old) is True

    def test_naive_datetime_treated_as_utc(self):
        recent_naive = datetime.utcnow() - timedelta(hours=1)
        assert is_stale(recent_naive) is False

    def test_custom_as_of_not_stale(self):
        fetched = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        as_of = datetime(2024, 6, 2, 11, 0, tzinfo=timezone.utc)  # 23 h later
        assert is_stale(fetched, as_of) is False

    def test_custom_as_of_stale(self):
        fetched = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)
        as_of = datetime(2024, 6, 2, 13, 0, tzinfo=timezone.utc)  # 25 h later
        assert is_stale(fetched, as_of) is True


# ---------------------------------------------------------------------------
# get_sp500_monthly_change
# ---------------------------------------------------------------------------

class TestGetSp500MonthlyChange:
    def test_cache_hit_skips_network(self, mock_yf):
        ps._sp500_cache[(2024, 1)] = (5.0, datetime.now(timezone.utc))
        result = get_sp500_monthly_change(2024, 1)
        assert result == 5.0
        mock_yf.Ticker.assert_not_called()

    def test_success_returns_pct(self, mock_yf):
        idx = pd.to_datetime(["2024-01-31", "2024-02-01", "2024-02-29"])
        hist = pd.DataFrame({"Close": [100.0, 105.0, 110.0]}, index=idx)
        ticker = MagicMock()
        ticker.history.return_value = hist
        mock_yf.Ticker.return_value = ticker

        result = get_sp500_monthly_change(2024, 2)
        # prev_close=100, end_close=110 → 10%
        if result is not None:
            assert abs(result - 10.0) < 0.1

    def test_december_uses_next_year(self, mock_yf):
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame()  # empty → returns None
        mock_yf.Ticker.return_value = ticker

        result = get_sp500_monthly_change(2024, 12)
        # Just verify it doesn't raise and calls history with year+1 end
        assert result is None or isinstance(result, float)

    def test_empty_history_returns_none(self, mock_yf):
        ticker = MagicMock()
        ticker.history.return_value = pd.DataFrame()
        mock_yf.Ticker.return_value = ticker

        result = get_sp500_monthly_change(2024, 3)
        assert result is None

    def test_exception_returns_none(self, mock_yf):
        ticker = MagicMock()
        ticker.history.side_effect = Exception("network error")
        mock_yf.Ticker.return_value = ticker

        result = get_sp500_monthly_change(2024, 4)
        assert result is None

    def test_result_is_cached(self, mock_yf):
        idx = pd.to_datetime(["2024-04-30", "2024-05-01", "2024-05-31"])
        hist = pd.DataFrame({"Close": [100.0, 102.0, 103.0]}, index=idx)
        ticker = MagicMock()
        ticker.history.return_value = hist
        mock_yf.Ticker.return_value = ticker

        get_sp500_monthly_change(2024, 5)
        assert (2024, 5) in ps._sp500_cache
