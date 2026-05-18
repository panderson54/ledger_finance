"""Holdings import service: extract ticker/share data from brokerage screenshots or pasted text via Claude."""
import base64
import logging

from app.ai_utils import make_anthropic_client, parse_claude_json_response, HOLDINGS_IMPORT_MODEL

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You analyze brokerage account holdings data from screenshots or pasted text. "
    "Extract every holding where you can clearly identify both the ticker symbol and share quantity. "
    "Return ONLY a JSON array of objects: [{\"ticker\": \"AAPL\", \"shares\": 12.5}, ...] "
    "Rules: ticker must be uppercase, 1-10 characters. shares must be a positive number. "
    "Omit any holding where either value is unclear. "
    "Return an empty array [] if no holdings can be extracted."
)


def _call_claude(messages: list, api_key: str) -> list[dict]:
    client = make_anthropic_client(api_key)
    response = client.messages.create(
        model=HOLDINGS_IMPORT_MODEL,
        max_tokens=1024,
        system=[{
            'type': 'text',
            'text': _SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=messages,
    )
    result = parse_claude_json_response(response, 'holdings_import')
    holdings = result if isinstance(result, list) else result.get('holdings', [])
    return _validate_holdings(holdings)


def extract_holdings_from_image(image_bytes: bytes, mime_type: str, api_key: str) -> list[dict]:
    """Return list of {ticker, shares} dicts extracted from a brokerage screenshot image."""
    b64 = base64.standard_b64encode(image_bytes).decode()
    return _call_claude([{
        'role': 'user',
        'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': mime_type, 'data': b64}},
            {'type': 'text', 'text': 'Extract all holdings (ticker and shares) from this screenshot.'},
        ],
    }], api_key)


def extract_holdings_from_text(text: str, api_key: str) -> list[dict]:
    """Return list of {ticker, shares} dicts extracted from pasted holdings text."""
    if not text.strip():
        return []
    return _call_claude([{
        'role': 'user',
        'content': f'Extract all holdings (ticker and shares) from this pasted holdings data:\n\n{text}',
    }], api_key)


def _validate_holdings(raw: list) -> list[dict]:
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get('ticker', '')).upper().strip()
        try:
            shares = float(item['shares'])
        except (KeyError, TypeError, ValueError):
            continue
        if ticker and 1 <= len(ticker) <= 10 and shares > 0:
            out.append({'ticker': ticker, 'shares': shares})
    return out
