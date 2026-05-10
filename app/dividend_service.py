"""
Dividend data fetching via Claude Haiku.
Results are cached in the dividend_data table with a 30-day TTL.
Mirrors the structure of classification_service.py.
"""
import json
import logging
from datetime import datetime, timezone, timedelta

from app.ai_utils import make_anthropic_client, parse_claude_json_response, DIVIDEND_MODEL

logger = logging.getLogger(__name__)

DIVIDEND_DATA_TTL_DAYS = 30

VALID_FREQUENCIES = {'monthly', 'quarterly', 'semi-annual', 'annual', None}
VALID_PAYER_TYPES = {'dividend_stock', 'reit', 'etf', 'bond_fund', 'cef', 'muni_fund', 'non_payer'}
VALID_TAX_TREATMENTS = {'qualified', 'ordinary', 'return_of_capital', None}

_SYSTEM_PROMPT = """You are a financial data assistant specializing in dividend analysis. Given a stock or fund ticker symbol, return its dividend information for personal finance tracking.

Return ONLY a valid JSON object with exactly these keys — no prose, no markdown fences:
{
  "is_dividend_payer": <true|false>,
  "annual_yield": <float, TTM yield as decimal 0-1, e.g. 0.035 for 3.5%; use 0 if no dividend>,
  "dividend_per_share": <float, most recent annual DPS; use 0 if no dividend>,
  "frequency": "<monthly|quarterly|semi-annual|annual|null>",
  "payer_type": "<dividend_stock|reit|etf|bond_fund|cef|muni_fund|non_payer>",
  "tax_treatment": "<qualified|ordinary|return_of_capital|null>",
  "payout_ratio": <float, 0-1, or null if not applicable>,
  "cut_risk": "<low|medium|high|null>",
  "ttm_yield": true
}

Rules:
- annual_yield MUST be a decimal between 0 and 1 (NOT a percentage like 3.5 — use 0.035)
- is_dividend_payer: false for growth stocks and non-payers (GOOG, BRK.B, etc.)
- payer_type: use 'non_payer' when is_dividend_payer is false
- frequency: null when is_dividend_payer is false
- tax_treatment: 'qualified' for most US stocks/ETFs; 'ordinary' for REITs, bond funds, CEFs; null for non-payers
- payout_ratio: null for ETFs and funds (use issuer-reported where available)
- cut_risk: qualitative assessment of dividend sustainability

Examples:
- VYM:  {"is_dividend_payer":true,"annual_yield":0.031,"dividend_per_share":3.52,"frequency":"quarterly","payer_type":"etf","tax_treatment":"qualified","payout_ratio":null,"cut_risk":"low","ttm_yield":true}
- O:    {"is_dividend_payer":true,"annual_yield":0.057,"dividend_per_share":3.08,"frequency":"monthly","payer_type":"reit","tax_treatment":"ordinary","payout_ratio":0.76,"cut_risk":"low","ttm_yield":true}
- BND:  {"is_dividend_payer":true,"annual_yield":0.042,"dividend_per_share":2.10,"frequency":"monthly","payer_type":"bond_fund","tax_treatment":"ordinary","payout_ratio":null,"cut_risk":"low","ttm_yield":true}
- GOOG: {"is_dividend_payer":false,"annual_yield":0,"dividend_per_share":0,"frequency":null,"payer_type":"non_payer","tax_treatment":null,"payout_ratio":null,"cut_risk":null,"ttm_yield":true}"""


def is_dividend_stale(last_fetched_at) -> bool:
    """Return True if last_fetched_at is older than DIVIDEND_DATA_TTL_DAYS or is None."""
    if last_fetched_at is None:
        return True
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - last_fetched_at) > timedelta(days=DIVIDEND_DATA_TTL_DAYS)


def _validate_dividend_data(data: dict) -> dict:
    """
    Validate and normalize a parsed dividend data dict.
    Raises ValueError on missing required keys.
    Coerces annual_yield > 1 to decimal (guards against Claude returning percentages).
    """
    if 'is_dividend_payer' not in data:
        raise ValueError("Missing required key 'is_dividend_payer'")

    is_payer = bool(data['is_dividend_payer'])

    annual_yield = float(data.get('annual_yield') or 0)
    if annual_yield > 1.0:
        logger.warning('annual_yield=%s looks like a percentage — dividing by 100', annual_yield)
        annual_yield = annual_yield / 100.0

    frequency = data.get('frequency')
    if frequency in ('null', '', 'none'):
        frequency = None
    if frequency not in VALID_FREQUENCIES:
        logger.warning('Unknown frequency %r — defaulting to None', frequency)
        frequency = None

    payer_type = data.get('payer_type', 'non_payer')
    if payer_type not in VALID_PAYER_TYPES:
        logger.warning('Unknown payer_type %r — defaulting to etf', payer_type)
        payer_type = 'etf'

    tax_treatment = data.get('tax_treatment')
    if tax_treatment in ('null', '', 'none'):
        tax_treatment = None
    if tax_treatment not in VALID_TAX_TREATMENTS:
        logger.warning('Unknown tax_treatment %r — defaulting to None', tax_treatment)
        tax_treatment = None

    payout_ratio = data.get('payout_ratio')
    if payout_ratio is not None:
        try:
            payout_ratio = float(payout_ratio)
            if payout_ratio > 1.0:
                payout_ratio = payout_ratio / 100.0
        except (TypeError, ValueError):
            payout_ratio = None

    cut_risk = data.get('cut_risk')
    if cut_risk in ('null', '', 'none'):
        cut_risk = None

    notes = {
        'ttm_yield': True,
        'payout_ratio': payout_ratio,
        'cut_risk': cut_risk,
        'as_of_date': datetime.now(timezone.utc).date().isoformat(),
    }

    return {
        'is_dividend_payer': is_payer,
        'annual_yield': annual_yield,
        'dividend_per_share': float(data.get('dividend_per_share') or 0),
        'frequency': frequency,
        'payer_type': payer_type,
        'tax_treatment': tax_treatment,
        'source_notes': json.dumps(notes),
    }


def fetch_dividend_data(ticker: str, api_key: str) -> dict:
    """
    Call Claude Haiku to fetch dividend data for a ticker.

    Returns a validated dict ready to persist to DividendData.
    Raises RuntimeError if anthropic is not installed or api_key is empty.
    Raises ValueError if Claude returns unparseable or invalid JSON.
    """
    client = make_anthropic_client(api_key)

    response = client.messages.create(
        model=DIVIDEND_MODEL,
        max_tokens=512,
        system=[{
            'type': 'text',
            'text': _SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': f'Return dividend data for ticker: {ticker.strip().upper()}',
        }],
    )

    # Extract and parse the JSON response (handles markdown fences)
    data = parse_claude_json_response(response, ticker)

    result = _validate_dividend_data(data)
    logger.info('Fetched dividend data: ticker=%s is_payer=%s yield=%.4f',
                ticker, result['is_dividend_payer'], result['annual_yield'])
    return result


def get_or_fetch(ticker: str, api_key: str, force: bool = False) -> tuple[dict, bool]:
    """
    Return dividend data for a ticker, using the DB cache when fresh.

    Returns:
        (data_dict, from_cache: bool)

    data_dict has keys: ticker, is_dividend_payer, annual_yield, dividend_per_share,
    frequency, payer_type, tax_treatment, source_notes, last_fetched_at.

    On fetch failure, logs the error and stores source_notes with the raw error;
    returns a zero-yield non-payer row so the UI can still render.
    """
    from app.models import DividendData
    from app import db

    ticker = ticker.strip().upper()

    cached = DividendData.query.filter_by(ticker=ticker).first()
    if cached and not force and not is_dividend_stale(cached.last_fetched_at):
        return _row_to_dict(cached), True

    try:
        fetched = fetch_dividend_data(ticker, api_key)
    except Exception as e:
        logger.error('Dividend fetch failed: ticker=%s error=%s', ticker, e)
        if cached:
            # Preserve stale data rather than returning nothing; note the error in source_notes
            error_notes = json.dumps({'error': str(e)[:500], 'as_of_date': datetime.utcnow().date().isoformat()})
            cached.source_notes = error_notes
            db.session.commit()
            return _row_to_dict(cached), True
        # No cached row — re-raise so callers can decide how to handle the failure
        raise

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if cached:
        cached.annual_yield       = fetched['annual_yield']
        cached.dividend_per_share = fetched['dividend_per_share']
        cached.frequency          = fetched['frequency']
        cached.payer_type         = fetched['payer_type']
        cached.is_dividend_payer  = fetched['is_dividend_payer']
        cached.tax_treatment      = fetched['tax_treatment']
        cached.source_notes       = fetched['source_notes']
        cached.last_fetched_at    = now
        db.session.commit()
        return _row_to_dict(cached), False

    row = DividendData(
        ticker             = ticker,
        annual_yield       = fetched['annual_yield'],
        dividend_per_share = fetched['dividend_per_share'],
        frequency          = fetched['frequency'],
        payer_type         = fetched['payer_type'],
        is_dividend_payer  = fetched['is_dividend_payer'],
        tax_treatment      = fetched['tax_treatment'],
        source_notes       = fetched['source_notes'],
        last_fetched_at    = now,
    )
    db.session.add(row)
    db.session.commit()
    return _row_to_dict(row), False


def _row_to_dict(row) -> dict:
    return {
        'ticker':             row.ticker,
        'is_dividend_payer':  row.is_dividend_payer,
        'annual_yield':       float(row.annual_yield or 0),
        'dividend_per_share': float(row.dividend_per_share or 0),
        'frequency':          row.frequency,
        'payer_type':         row.payer_type,
        'tax_treatment':      row.tax_treatment,
        'source_notes':       row.source_notes,
        'last_fetched_at':    row.last_fetched_at.isoformat() if row.last_fetched_at else None,
    }
