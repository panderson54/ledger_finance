"""
Deterministic transfer detection: an amount/date match between opposite-
direction transactions on two different accounts, used to catch transfers a
description-based (Claude) classification can miss — e.g. a receiving
statement that gives no payee text at all for a deposit (observed on real
Wealthfront Cash Account statements), so the only signal is that some other
account shows a same-amount, same-week withdrawal.

Pure function — no DB/Flask imports. The caller (app/brokerage_import/
service.py) is responsible for assembling the combined transaction list,
including both the accounts being previewed/committed and any already
committed BankTransaction rows it wants considered as transfer partners.
"""

DEFAULT_WINDOW_DAYS = 5


def find_transfer_matches(transactions: list[dict], window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, str]:
    """
    transactions: each dict has 'key' (unique str), 'account_key' (str —
    groups transactions belonging to the same account), 'date' (date),
    'amount' (float), 'direction' ('debit'|'credit').

    Returns {key: matched_partner_key} for every transaction found to have
    an opposite-direction, equal-amount partner on a *different* account
    within window_days. Greedy amount+date-proximity pairing — each
    transaction is used as a partner at most once, so one large transaction
    can't absorb several smaller same-amount coincidences.
    """
    by_key = {t['key']: t for t in transactions}
    matched: set[str] = set()
    matches: dict[str, str] = {}

    for key in sorted(by_key, key=lambda k: by_key[k]['date']):
        if key in matched:
            continue
        txn = by_key[key]
        opposite_direction = 'credit' if txn['direction'] == 'debit' else 'debit'

        best_key, best_delta = None, None
        for other_key, other in by_key.items():
            if other_key == key or other_key in matched:
                continue
            if other['account_key'] == txn['account_key']:
                continue
            if other['direction'] != opposite_direction:
                continue
            if abs(other['amount'] - txn['amount']) > 0.01:
                continue
            delta = abs((other['date'] - txn['date']).days)
            if delta > window_days:
                continue
            if best_delta is None or delta < best_delta:
                best_key, best_delta = other_key, delta

        if best_key:
            matches[key] = best_key
            matches[best_key] = key
            matched.add(key)
            matched.add(best_key)

    return matches
