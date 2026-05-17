"""
Shared utilities for Claude API interactions.
"""
import json
import logging

logger = logging.getLogger(__name__)

# Model constants — update here when upgrading model versions
CLASSIFICATION_MODEL = 'claude-opus-4-7'
DIVIDEND_MODEL = 'claude-haiku-4-5-20251001'
HOLDINGS_IMPORT_MODEL = 'claude-sonnet-4-6'


def make_anthropic_client(api_key: str):
    """
    Construct and return an anthropic.Anthropic client.

    Raises:
        RuntimeError  if the anthropic package is not installed
        RuntimeError  if api_key is empty
    """
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic is not installed. Run: pip install 'anthropic>=0.50.0'")
    if not api_key:
        raise RuntimeError("Anthropic API key is not configured")
    return anthropic.Anthropic(api_key=api_key)


def parse_claude_json_response(response, label: str) -> dict | list:
    """
    Extract and parse a JSON object from a Claude API response.

    Handles responses that contain tool_use blocks (web search) by taking
    the last text block, and strips markdown code fences if present.

    Args:
        response:  The object returned by client.messages.create(...)
        label:     Human-readable label for error messages (e.g. ticker symbol)

    Returns:
        Parsed dict from the response JSON.

    Raises:
        ValueError  if no text block found or JSON cannot be parsed
    """
    text_blocks = [b.text for b in response.content if hasattr(b, 'text')]
    if not text_blocks:
        raise ValueError(f"No text content in Claude response for '{label}'")
    raw = text_blocks[-1].strip()

    # Strip markdown code fences if present
    if raw.startswith('```'):
        parts = raw.split('```')
        raw = parts[1] if len(parts) > 1 else raw
        if raw.startswith('json'):
            raw = raw[4:]
    raw = raw.strip()

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Claude returned non-JSON for '{label}': {e}. Raw: {raw[:300]}")
