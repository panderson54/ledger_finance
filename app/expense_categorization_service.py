"""
Bank-statement transaction categorization via Claude API.
Buckets each parsed checking/savings transaction into one of the user's
TransactionCategory rows (or flags it as a transfer between the user's own
accounts) in a single batched call per statement.
"""
import logging

from app.ai_utils import make_anthropic_client, parse_claude_json_response, EXPENSE_CATEGORIZATION_MODEL

logger = logging.getLogger(__name__)

VALID_CONFIDENCE = {'high', 'medium', 'low'}

_SYSTEM_PROMPT = """You are a personal finance assistant. Given a list of bank transactions and a list of \
categories, assign each transaction to exactly one category.

Return ONLY a valid JSON array — no prose, no markdown fences — where each element is:
{"key": "<transaction key>", "category_id": <category id>, "confidence": "<high|medium|low>"}

Include exactly one element per transaction key given, using only the category ids provided.
Categories with kind "transfer" are for money moving between the user's own accounts (at the \
institutions listed as "the user's own accounts" below) — recognize these by the counterparty name in \
the description (e.g. a withdrawal whose description names one of the user's own institutions). Use \
"low" confidence for any transaction you cannot confidently place — never invent a category id."""


def _build_user_message(transactions: list[dict], categories: list[dict], own_account_hints: list[str]) -> str:
    txn_lines = [
        f'- key={t["key"]} direction={t["direction"]} amount={t["amount"]} description="{t["description"]}"'
        for t in transactions
    ]
    cat_lines = [
        f'- id={c["id"]} kind={c["kind"]} title="{c["title"]}"' + (f' — {c["description"]}' if c.get('description') else '')
        for c in categories
    ]
    hints = ', '.join(own_account_hints) if own_account_hints else '(none provided)'
    return (
        f"The user's own accounts/institutions (for transfer recognition): {hints}\n\n"
        f"Categories:\n" + '\n'.join(cat_lines) + '\n\n'
        f"Transactions:\n" + '\n'.join(txn_lines)
    )


def categorize_transactions(
    transactions: list[dict], categories: list[dict], own_account_hints: list[str], api_key: str,
) -> list[dict]:
    """
    Args:
        transactions:      [{key, description, amount, direction}, ...]
        categories:        [{id, title, description, kind}, ...] — active categories only
        own_account_hints: account/institution names the user owns, for transfer recognition
        api_key:           Anthropic API key (resolved by caller)

    Returns:
        [{key, category_id, confidence}, ...] — one entry per input transaction key.

    Raises:
        RuntimeError  if anthropic is not installed or api_key is empty
        ValueError    if Claude returns unparseable or invalid JSON
        Exception     on API errors (network, rate limit, etc.)
    """
    if not transactions:
        return []

    client = make_anthropic_client(api_key)

    response = client.messages.create(
        model=EXPENSE_CATEGORIZATION_MODEL,
        max_tokens=4096,
        system=[{
            'type': 'text',
            'text': _SYSTEM_PROMPT,
            'cache_control': {'type': 'ephemeral'},
        }],
        messages=[{
            'role': 'user',
            'content': _build_user_message(transactions, categories, own_account_hints),
        }],
    )

    data = parse_claude_json_response(response, 'expense categorization')
    if not isinstance(data, list):
        raise ValueError(f'Expected a JSON array of category assignments, got {type(data).__name__}')

    valid_keys = {t['key'] for t in transactions}
    valid_category_ids = {c['id'] for c in categories}

    results = []
    for entry in data:
        if not isinstance(entry, dict):
            raise ValueError(f'Expected each categorization entry to be an object, got {entry!r}')
        key = entry.get('key')
        category_id = entry.get('category_id')
        confidence = entry.get('confidence')
        if key not in valid_keys:
            raise ValueError(f"Categorization referenced unknown transaction key '{key}'")
        if category_id not in valid_category_ids:
            raise ValueError(f"Categorization referenced unknown category id '{category_id}' for key '{key}'")
        if confidence not in VALID_CONFIDENCE:
            raise ValueError(f"Invalid confidence '{confidence}' for key '{key}'")
        results.append({'key': key, 'category_id': category_id, 'confidence': confidence})

    logger.info('Categorized %d/%d transactions via Claude', len(results), len(transactions))
    return results
