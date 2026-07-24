"""
Pure account-matching logic: map parsed institutional accounts to existing
Ledger accounts. Operates on plain dicts (not ORM instances) so it stays
DB-free and unit-testable; callers convert Account rows to dicts first.
"""
from app.brokerage_import.types import ParsedAccount


def suggest_matches(parsed_accounts: list[ParsedAccount], ledger_accounts: list[dict]) -> list[dict]:
    """
    ledger_accounts: [{'id', 'name', 'institution', 'account_number'}, ...]
    (institution/account_number may be None or empty)

    Returns one entry per parsed account:
        {key, suggested_account_id, suggested_account_name, confidence,
         candidate_accounts: [{'id', 'name'}, ...]}
    confidence is 'exact' | 'institution_only' | 'none'.
    """
    results = []
    for parsed in parsed_accounts:
        same_institution = [
            a for a in ledger_accounts
            if a.get('institution') and parsed.institution in a['institution'].strip().lower()
        ]

        exact = None
        if parsed.account_number_last4:
            for a in same_institution:
                acct_num = (a.get('account_number') or '').strip()
                if acct_num and acct_num[-4:] == parsed.account_number_last4:
                    exact = a
                    break

        if exact:
            results.append({
                'key': parsed.key,
                'suggested_account_id': exact['id'],
                'suggested_account_name': exact['name'],
                'confidence': 'exact',
                'candidate_accounts': [{'id': a['id'], 'name': a['name']} for a in same_institution],
            })
        elif len(same_institution) == 1:
            only = same_institution[0]
            results.append({
                'key': parsed.key,
                'suggested_account_id': only['id'],
                'suggested_account_name': only['name'],
                'confidence': 'institution_only',
                'candidate_accounts': [{'id': only['id'], 'name': only['name']}],
            })
        else:
            results.append({
                'key': parsed.key,
                'suggested_account_id': None,
                'suggested_account_name': None,
                'confidence': 'none',
                'candidate_accounts': [{'id': a['id'], 'name': a['name']} for a in same_institution],
            })
    return results
