"""
Ticker classification via Claude API.
Returns asset_class, market_cap_tilt, and sector_weights (allocation splits) for a ticker.
Results are cached in the ticker_classifications table to avoid redundant API calls.
"""
import json
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

VALID_ASSET_CLASSES = {'domestic', 'international', 'bonds', 'cash'}
VALID_CAP_CLASSES = {'large', 'mid', 'small', None}
ALLOCATION_CLASSES = ['domestic', 'international', 'bonds', 'cash']

_SYSTEM_PROMPT = """You are a financial data assistant. Given a stock or fund ticker symbol, classify it for personal portfolio tracking.

Return ONLY a valid JSON object with exactly these keys — no prose, no markdown fences:
{
  "asset_class": "<domestic|international|bonds|cash>",
  "market_cap_tilt": "<large|mid|small|null>",
  "sector_weights": {
    "domestic": <number>,
    "international": <number>,
    "bonds": <number>,
    "cash": <number>
  }
}

Definitions:
- asset_class: the single dominant asset class
  - domestic: US equities (ETFs, mutual funds, individual stocks)
  - international: non-US equities
  - bonds: fixed income of any geography
  - cash: money market, stable value, short-term treasury
- market_cap_tilt: for equity holdings only; null for bonds, cash, or truly blended funds
  - large: large-cap tilt (e.g. S&P 500, total market, mega-cap)
  - mid: mid-cap tilt
  - small: small-cap tilt
- sector_weights: percentage of the holding in each asset class, summing to exactly 100
  - For single-class holdings: one class at 100, rest at 0
  - For blended/target-date funds: use approximate current allocation

Examples:
- VTI:  {"asset_class":"domestic","market_cap_tilt":"large","sector_weights":{"domestic":100,"international":0,"bonds":0,"cash":0}}
- VXUS: {"asset_class":"international","market_cap_tilt":null,"sector_weights":{"domestic":0,"international":100,"bonds":0,"cash":0}}
- BND:  {"asset_class":"bonds","market_cap_tilt":null,"sector_weights":{"domestic":0,"international":0,"bonds":100,"cash":0}}
- VTTSX:{"asset_class":"domestic","market_cap_tilt":null,"sector_weights":{"domestic":54,"international":36,"bonds":10,"cash":0}}"""


def _validate_classification(data: dict) -> dict:
    """Validate and normalize a parsed classification dict. Raises ValueError on any issue."""
    ac = data.get('asset_class')
    if ac not in VALID_ASSET_CLASSES:
        raise ValueError(f"Invalid asset_class '{ac}'; must be one of {sorted(VALID_ASSET_CLASSES)}")

    cap = data.get('market_cap_tilt')
    if cap in ('null', ''):
        cap = None
    if cap not in VALID_CAP_CLASSES:
        raise ValueError(f"Invalid market_cap_tilt '{cap}'")

    weights = data.get('sector_weights')
    if not isinstance(weights, dict):
        raise ValueError("sector_weights must be a JSON object")
    for cls in ALLOCATION_CLASSES:
        if cls not in weights:
            raise ValueError(f"sector_weights missing key '{cls}'")
        try:
            weights[cls] = float(weights[cls])
        except (TypeError, ValueError):
            raise ValueError(f"sector_weights['{cls}'] must be a number")
    total = sum(weights.values())
    if abs(total - 100.0) > 1.0:
        raise ValueError(f"sector_weights must sum to 100 (got {total:.2f})")

    return {
        'asset_class': ac,
        'market_cap_tilt': cap,
        'sector_weights': weights,
    }


def classify_ticker(ticker: str, api_key: str, use_web_search: bool = False) -> dict:
    """
    Call the Claude API to classify a ticker symbol.

    Args:
        ticker:         Ticker symbol (e.g. 'VTI', 'VXUS').
        api_key:        Anthropic API key (resolved by caller; not read from env here).
        use_web_search: Enable web search tool for obscure tickers.

    Returns:
        dict with keys: asset_class, market_cap_tilt, sector_weights

    Raises:
        RuntimeError   if anthropic is not installed or api_key is empty
        ValueError     if Claude returns unparseable or invalid JSON
        Exception      on API errors (network, rate limit, etc.)
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic is not installed. Run: pip install 'anthropic>=0.50.0'")

    if not api_key:
        raise RuntimeError("Anthropic API key is not configured")

    client = anthropic.Anthropic(api_key=api_key)

    tools = []
    if use_web_search:
        tools = [{'type': 'web_search_20250305', 'name': 'web_search'}]

    kwargs = dict(
        model='claude-opus-4-7',
        max_tokens=512,
        system=[{
            'type': 'text',
            'text': _SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': f'Classify this investment ticker: {ticker.strip().upper()}',
        }],
    )
    if tools:
        kwargs['tools'] = tools

    response = client.messages.create(**kwargs)

    # Extract the last text block (web search may emit tool_use blocks first)
    text_blocks = [b.text for b in response.content if hasattr(b, 'text')]
    if not text_blocks:
        raise ValueError(f"No text content in Claude response for ticker '{ticker}'")
    raw = text_blocks[-1].strip()

    # Strip markdown code fences if present
    if raw.startswith('```'):
        parts = raw.split('```')
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON for ticker '{ticker}': {e}. Raw: {raw[:300]}")

    result = _validate_classification(data)
    logger.info('Classified: ticker=%s asset_class=%s cap=%s', ticker, result['asset_class'], result['market_cap_tilt'])
    return result


def get_or_classify(ticker: str, api_key: str, use_web_search: bool = False) -> tuple[dict, bool]:
    """
    Return classification for a ticker, using the DB cache if available.

    Returns:
        (classification_dict, from_cache: bool)

    The classification_dict has keys: ticker, asset_class, market_cap_tilt, sector_weights, source.
    Raises exceptions from classify_ticker() on API failure when cache misses.
    """
    from app.models import TickerClassification
    from app import db

    ticker = ticker.strip().upper()

    cached = TickerClassification.query.filter_by(ticker=ticker).first()
    if cached:
        return {
            'ticker': cached.ticker,
            'asset_class': cached.asset_class,
            'market_cap_tilt': cached.market_cap_tilt,
            'sector_weights': cached.weights_dict(),
            'source': cached.source,
        }, True

    result = classify_ticker(ticker, api_key, use_web_search=use_web_search)

    row = TickerClassification(
        ticker=ticker,
        asset_class=result['asset_class'],
        market_cap_tilt=result['market_cap_tilt'],
        sector_weights=json.dumps(result['sector_weights']),
        source='claude',
        classified_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.session.add(row)
    db.session.commit()

    result['ticker'] = ticker
    result['source'] = 'claude'
    return result, False
