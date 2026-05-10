"""
Price fetching abstraction — swap backend here without touching callers.
Current implementation: yfinance.
"""
import logging
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)


def get_price(ticker: str) -> tuple[float, str]:
    """
    Fetch current price and display name for a ticker.

    Returns:
        (price: float, display_name: str)

    Raises:
        ValueError  if no price data is available for the ticker
        Exception   on network or parsing errors
    """
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("yfinance is not installed. Run: pip install yfinance")

    t = yf.Ticker(ticker)

    # fast_info is the most reliable path — works for ETFs, stocks, mutual funds,
    # and money markets without a slow full info fetch.
    price_val = None
    try:
        fi = t.fast_info
        price_val = fi.last_price or fi.previous_close
    except Exception:
        pass

    # Fall back to OHLCV history (ETFs and stocks when fast_info is unavailable)
    if not price_val:
        try:
            hist = t.history(period="5d")
            if not hist.empty:
                price_val = float(hist["Close"].iloc[-1])
        except Exception:
            pass

    # Last resort: full info snapshot (slowest, but catches edge cases)
    info: dict = {}
    if not price_val:
        try:
            info = t.info or {}
        except Exception:
            pass
        price_val = (
            info.get("regularMarketPrice")
            or info.get("currentPrice")
            or info.get("previousClose")
            or info.get("navPrice")
        )

    if not price_val:
        raise ValueError(f"No price data found for ticker '{ticker}'")

    price = float(price_val)

    # Fetch name; reuse info dict if we already fetched it above
    if not info:
        try:
            info = t.info or {}
        except Exception:
            pass
    name = info.get("longName") or info.get("shortName") or ticker.upper()

    logger.info("Price fetched: ticker=%s price=%.4f name=%s", ticker, price, name)
    return price, name


class _Sp500Cache:
    """Module-level S&P 500 result cache. Cleared between tests via clear()."""
    def __init__(self):
        self._monthly: dict = {}       # (year, month) -> (pct: float, fetched_at: datetime)
        self._range: dict = {}         # (start_date, end_date) -> (pct, fetched_at)

    def clear(self):
        self._monthly.clear()
        self._range.clear()


_sp500_cache = _Sp500Cache()


def clear_sp500_cache():
    """Reset the S&P 500 in-memory cache (used in tests)."""
    _sp500_cache.clear()


def get_sp500_range_change(start_date, end_date) -> float | None:
    """Return S&P 500 % change from start_date to end_date (inclusive), or None on failure."""
    try:
        import yfinance as yf
    except ImportError:
        return None

    from datetime import timedelta

    cache_key = (start_date, end_date)
    cached = _sp500_cache._range.get(cache_key)
    if cached and (datetime.now(timezone.utc) - cached[1]).total_seconds() < 3600:
        return cached[0]

    try:
        fetch_start = start_date - timedelta(days=10)
        fetch_end = end_date + timedelta(days=1)

        hist = yf.Ticker("^GSPC").history(
            start=fetch_start.isoformat(), end=fetch_end.isoformat()
        )
        if hist.empty:
            return None

        hist_dates = hist.index.date
        pre = hist[hist_dates <= start_date]
        inrange = hist[hist_dates <= end_date]

        if pre.empty or inrange.empty:
            return None

        start_close = float(pre["Close"].iloc[-1])
        end_close = float(inrange["Close"].iloc[-1])
        pct = (end_close - start_close) / start_close * 100

        _sp500_cache._range[cache_key] = (pct, datetime.now(timezone.utc))
        logger.info("S&P 500 range change %s to %s: %.2f%%", start_date, end_date, pct)
        return pct
    except Exception:
        logger.exception("Failed to fetch S&P 500 range change %s to %s", start_date, end_date)
        return None


def get_sp500_monthly_change(year: int, month: int) -> float | None:
    """Return S&P 500 % change for the given calendar month, or None on failure."""
    try:
        import yfinance as yf
    except ImportError:
        return None

    from datetime import timedelta

    cache_key = (year, month)
    cached = _sp500_cache._monthly.get(cache_key)
    if cached and (datetime.now(timezone.utc) - cached[1]).total_seconds() < 86_400:
        return cached[0]

    try:
        month_start = date(year, month, 1)
        if month == 12:
            fetch_end = date(year + 1, 1, 1)
        else:
            fetch_end = date(year, month + 1, 1)
        fetch_start = month_start - timedelta(days=10)

        hist = yf.Ticker("^GSPC").history(
            start=fetch_start.isoformat(), end=fetch_end.isoformat()
        )
        if hist.empty:
            return None

        hist_dates = hist.index.date
        pre = hist[hist_dates < month_start]
        inmonth = hist[hist_dates >= month_start]
        if pre.empty or inmonth.empty:
            return None

        prev_close = float(pre["Close"].iloc[-1])
        end_close = float(inmonth["Close"].iloc[-1])
        pct = (end_close - prev_close) / prev_close * 100

        _sp500_cache._monthly[cache_key] = (pct, datetime.now(timezone.utc))
        logger.info("S&P 500 monthly change %d-%02d: %.2f%%", year, month, pct)
        return pct
    except Exception:
        logger.exception("Failed to fetch S&P 500 monthly change for %d-%02d", year, month)
        return None


def is_stale(last_fetched: datetime | None, as_of: datetime | None = None) -> bool:
    """
    Return True if last_fetched is more than 24 h before as_of (default: now).
    None last_fetched is always stale.
    """
    if last_fetched is None:
        return True
    now = as_of or datetime.now(timezone.utc)
    # Make both offset-aware for comparison
    if last_fetched.tzinfo is None:
        last_fetched = last_fetched.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - last_fetched).total_seconds() > 86_400
