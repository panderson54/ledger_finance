"""
DB-writing orchestration for brokerage import (CSV positions export or PDF
statement). Dependency-injection style (db, models passed in), mirroring
app/import_processor.py, so the parsing modules stay pure/DB-free and this
is the only place that touches the database for this feature.
"""
import json
import logging
from datetime import datetime, timedelta, timezone

from app.brokerage_import.parsing import parse_positions_csv
from app.brokerage_import.pdf_parsing import parse_positions_pdf
from app.brokerage_import.matching import suggest_matches
from app.brokerage_import.transfer_matching import find_transfer_matches, DEFAULT_WINDOW_DAYS
from app.brokerage_import.types import ParsedImport
from app.account_categories import INVESTMENT_CATS, CASH_CATS
from app.import_processor import _log_import

logger = logging.getLogger(__name__)


def _parse_file(file_bytes: bytes, filename: str) -> ParsedImport:
    lower = (filename or '').lower()
    if lower.endswith('.csv'):
        return parse_positions_csv(file_bytes.decode('utf-8-sig'))
    if lower.endswith('.pdf'):
        return parse_positions_pdf(file_bytes)
    result = ParsedImport(institution='unknown', source_format='unknown')
    result.errors.append('Unsupported file type — upload a .csv or .pdf file.')
    return result


def _discrepancy(reported, computed) -> bool:
    if reported is None or computed is None or reported == 0:
        return False
    return abs(computed - reported) / abs(reported) > 0.01


def _transaction_key(acct_key: str, index: int) -> str:
    return f'{acct_key}:{index}'


def _build_transaction_suggestions(db, models, parsed_accounts, matches: dict,
                                    ai_enabled: bool, api_key: str, warnings: list) -> tuple[dict, list, dict]:
    """
    Returns (suggestions, category_dicts, txns_by_acct_key):
        suggestions      {transaction_key: {'category_id', 'confidence'}}
        category_dicts   active TransactionCategory rows, as dicts
        txns_by_acct_key {parsed_account.key: [{'key','date','description','amount','direction'}, ...]}
    """
    Account = models['Account']
    TransactionCategory = models['TransactionCategory']
    BankTransaction = models['BankTransaction']

    active_categories = (
        TransactionCategory.query.filter_by(is_active=True)
        .order_by(TransactionCategory.display_order).all()
    )
    category_dicts = [
        {'id': c.id, 'title': c.title, 'description': c.description or '', 'kind': c.kind}
        for c in active_categories
    ]

    txns_by_acct_key: dict[str, list[dict]] = {}
    batch_txns = []
    for acct in parsed_accounts:
        if not acct.balance_only or not acct.transactions:
            continue
        suggested_account_id = matches.get(acct.key, {}).get('suggested_account_id')
        account_key = str(suggested_account_id) if suggested_account_id else acct.key
        txns = [
            {'key': _transaction_key(acct.key, i), 'date': t.date, 'description': t.description,
             'amount': t.amount, 'direction': t.direction}
            for i, t in enumerate(acct.transactions)
        ]
        txns_by_acct_key[acct.key] = txns
        batch_txns.extend({**t, 'account_key': account_key} for t in txns)

    if not batch_txns:
        return {}, category_dicts, txns_by_acct_key

    # Deterministic cross-account matching — includes already-committed
    # transactions on other accounts (e.g. a transfer whose receiving side
    # was imported in an earlier session) within the matching window.
    min_d = min(t['date'] for t in batch_txns) - timedelta(days=DEFAULT_WINDOW_DAYS)
    max_d = max(t['date'] for t in batch_txns) + timedelta(days=DEFAULT_WINDOW_DAYS)
    existing = BankTransaction.query.filter(BankTransaction.transaction_date.between(min_d, max_d)).all()
    other_txns = [
        {'key': f'existing:{bt.id}', 'account_key': str(bt.account_id), 'date': bt.transaction_date,
         'amount': float(bt.amount), 'direction': bt.direction}
        for bt in existing
    ]
    transfer_matches = find_transfer_matches(batch_txns + other_txns)
    transfer_category = next((c for c in category_dicts if c['kind'] == 'transfer'), None)

    claude_by_key = {}
    if ai_enabled and api_key and category_dicts:
        from app.expense_categorization_service import categorize_transactions
        try:
            claude_input = [
                {'key': t['key'], 'description': t['description'], 'amount': t['amount'], 'direction': t['direction']}
                for t in batch_txns
            ]
            own_hints = sorted({a.institution for a in Account.query.filter_by(is_active=True) if a.institution}
                                | {a.name for a in Account.query.filter_by(is_active=True)})
            results = categorize_transactions(claude_input, category_dicts, own_hints, api_key)
            claude_by_key = {r['key']: r for r in results}
        except Exception as e:
            logger.warning('Expense categorization failed: error=%s', e)
            warnings.append(f'Could not auto-categorize transactions: {e}')

    suggestions = {}
    for t in batch_txns:
        key = t['key']
        if key in transfer_matches and transfer_category:
            suggestions[key] = {'category_id': transfer_category['id'], 'confidence': 'matched'}
        elif key in claude_by_key:
            s = claude_by_key[key]
            suggestions[key] = {'category_id': s['category_id'], 'confidence': s['confidence']}
        else:
            suggestions[key] = {'category_id': None, 'confidence': 'none'}
    return suggestions, category_dicts, txns_by_acct_key


def _build_summary(txns_by_acct_key: dict, suggestions: dict, category_dicts: list) -> dict:
    """Aggregate suggested category totals across all accounts for the confirm-UI summary strip."""
    categories_by_id = {c['id']: c for c in category_dicts}
    totals: dict[int, float] = {}
    for txns in txns_by_acct_key.values():
        for t in txns:
            cat_id = suggestions.get(t['key'], {}).get('category_id')
            if cat_id is None:
                continue
            totals[cat_id] = totals.get(cat_id, 0.0) + t['amount']

    total_income = sum(amt for cid, amt in totals.items() if categories_by_id[cid]['kind'] == 'income')
    transfer_total = sum(amt for cid, amt in totals.items() if categories_by_id[cid]['kind'] == 'transfer')
    expense_categories = sorted(
        (
            {'category_id': cid, 'title': categories_by_id[cid]['title'], 'amount': round(amt, 2)}
            for cid, amt in totals.items() if categories_by_id[cid]['kind'] == 'expense'
        ),
        key=lambda v: -v['amount'],
    )
    return {
        'total_income': round(total_income, 2),
        'expense_categories': expense_categories,
        'transfer_total': round(transfer_total, 2),
    }


def build_preview(db, models, file_bytes: bytes, filename: str, ai_enabled: bool = False, api_key: str = '') -> dict:
    """Read-only: parse the upload and suggest account + transaction-category matches. No DB writes."""
    parsed = _parse_file(file_bytes, filename)

    Account = models['Account']
    has_balance_only = any(a.balance_only for a in parsed.accounts)
    allowed_cats = INVESTMENT_CATS | CASH_CATS if has_balance_only else INVESTMENT_CATS
    investment_accounts = (
        Account.query.filter(Account.is_active == True, Account.category.in_(allowed_cats))
        .order_by(Account.name).all()
    )
    ledger_dicts = [
        {'id': a.id, 'name': a.name, 'institution': a.institution, 'account_number': a.account_number}
        for a in investment_accounts
    ]
    matches = {m['key']: m for m in suggest_matches(parsed.accounts, ledger_dicts)}

    default_month = (
        parsed.as_of_date.strftime('%Y-%m') if parsed.as_of_date
        else datetime.today().strftime('%Y-%m')
    )

    suggestions, category_dicts, txns_by_acct_key = _build_transaction_suggestions(
        db, models, parsed.accounts, matches, ai_enabled, api_key, parsed.warnings,
    )

    accounts_out = []
    for acct in parsed.accounts:
        match = matches.get(acct.key, {})
        acct_txns = [
            {**t, 'date': t['date'].isoformat(), **suggestions.get(t['key'], {'category_id': None, 'confidence': 'none'})}
            for t in txns_by_acct_key.get(acct.key, [])
        ]
        accounts_out.append({
            'key': acct.key,
            'institution': acct.institution,
            'account_name': acct.account_name,
            'account_number_last4': acct.account_number_last4,
            'suggested_account_id': match.get('suggested_account_id'),
            'suggested_account_name': match.get('suggested_account_name'),
            'confidence': match.get('confidence', 'none'),
            'candidate_accounts': match.get('candidate_accounts', []),
            'positions': [
                {'ticker': p.ticker, 'description': p.description, 'quantity': p.quantity,
                 'price': p.price, 'value': p.value}
                for p in acct.positions
            ],
            'cash_value': round(acct.cash_total, 2),
            'computed_total': acct.computed_total,
            'reported_total': acct.reported_total,
            'total_discrepancy_warning': _discrepancy(acct.reported_total, acct.computed_total),
            'balance_only': acct.balance_only,
            'transactions': acct_txns,
        })

    logger.info(
        'Brokerage import preview: filename=%s institution=%s format=%s accounts=%d transactions=%d',
        filename, parsed.institution, parsed.source_format, len(accounts_out), len(suggestions),
    )

    return {
        'institution': parsed.institution,
        'source_format': parsed.source_format,
        'as_of_date': parsed.as_of_date.isoformat() if parsed.as_of_date else None,
        'default_month': default_month,
        'accounts': accounts_out,
        'investment_accounts': [{'id': a.id, 'name': a.name} for a in investment_accounts],
        'transaction_categories': category_dicts,
        'summary': _build_summary(txns_by_acct_key, suggestions, category_dicts),
        'warnings': parsed.warnings,
        'errors': parsed.errors,
    }


def _rollup_spending_entries(db, models, month_date) -> int:
    """
    Recompute SpendingEntry rows for month_date from the current
    BankTransaction rows (all accounts), grouped by category. One
    SpendingEntry per non-transfer category, keyed by (month_date,
    account_name=category.title) — the field SpendingEntry.account_name was
    already documented for "Card/account name (Chase, Amex, etc.)". A
    category whose rollup goes to zero has its row deleted rather than left
    stale. Returns the number of SpendingEntry rows written (created/updated).
    """
    from sqlalchemy import func

    TransactionCategory = models['TransactionCategory']
    BankTransaction = models['BankTransaction']
    SpendingEntry = models['SpendingEntry']

    sums = dict(
        db.session.query(BankTransaction.category_id, func.sum(BankTransaction.amount))
        .filter(BankTransaction.month_date == month_date)
        .group_by(BankTransaction.category_id)
        .all()
    )

    written = 0
    for category in TransactionCategory.query.filter(TransactionCategory.kind != 'transfer').all():
        amount = sums.get(category.id)
        entry = SpendingEntry.query.filter_by(entry_date=month_date, account_name=category.title).first()
        if not amount:
            if entry:
                db.session.delete(entry)
            continue
        if entry:
            entry.amount = amount
        else:
            db.session.add(SpendingEntry(
                entry_date=month_date, account_name=category.title, amount=amount, entry_type=category.kind,
            ))
        written += 1
    return written


def commit_import(db, models, file_bytes: bytes, filename: str, mapping: dict,
                   month_date, ai_enabled: bool, api_key: str, transaction_categories: dict | None = None) -> dict:
    """
    Re-parses file_bytes (stateless — no server-side session between preview
    and commit) and writes Holding/AccountSnapshot rows for each mapped
    account (plus BankTransaction rows and a SpendingEntry rollup for
    balance_only accounts with parsed transactions), then recalculates
    metrics for month_date. All-or-nothing: a failure at any point rolls
    back the whole transaction.

    transaction_categories: {transaction_key: category_id} — user-confirmed
    category for each parsed transaction (transaction_key matches the one
    returned by build_preview: f"{parsed_account.key}:{index}").
    """
    parsed = _parse_file(file_bytes, filename)
    if parsed.errors:
        return {'success': False, 'errors': parsed.errors, 'warnings': parsed.warnings, 'error_type': 'validation'}

    transaction_categories = transaction_categories or {}

    Account = models['Account']
    Holding = models['Holding']
    HoldingAllocation = models['HoldingAllocation']
    AccountSnapshot = models['AccountSnapshot']
    BankTransaction = models['BankTransaction']
    TransactionCategory = models['TransactionCategory']
    ImportLog = models['ImportLog']

    valid_category_ids = {c.id for c in TransactionCategory.query.filter_by(is_active=True).all()}

    validated = []
    errors = []
    for acct in parsed.accounts:
        account_id = mapping.get(acct.key)
        if account_id is None:
            continue  # user chose to skip this detected account
        account = db.session.get(Account, account_id)
        allowed = INVESTMENT_CATS | CASH_CATS if acct.balance_only else INVESTMENT_CATS
        if account is None or not account.is_active or account.category not in allowed:
            errors.append(f'Account id {account_id} is not a valid active investment account.')
            continue
        validated.append((account, acct))

    if errors:
        return {'success': False, 'errors': errors, 'warnings': parsed.warnings, 'error_type': 'validation'}
    if not validated:
        return {'success': False, 'errors': ['No accounts were mapped for import.'],
                'warnings': parsed.warnings, 'error_type': 'validation'}

    warnings = list(parsed.warnings)
    holdings_created = holdings_updated = holdings_archived = 0
    classified = classification_skipped = 0
    transactions_imported = 0
    spending_entries_written = 0
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    # Gate the SpendingEntry rollup on this commit actually touching a
    # checking/savings account — running it for a pure brokerage import
    # would needlessly re-scan every category and risk deleting an
    # unrelated, manually-entered SpendingEntry that happens to share a
    # category's title but has no BankTransaction backing this month.
    touches_bank_transactions = any(parsed_account.balance_only for _, parsed_account in validated)

    try:
        for account, parsed_account in validated:
            if parsed_account.balance_only:
                snapshot = AccountSnapshot.query.filter_by(
                    account_id=account.id, snapshot_date=month_date
                ).first()
                if snapshot:
                    snapshot.balance = parsed_account.computed_total
                else:
                    db.session.add(AccountSnapshot(
                        account_id=account.id, snapshot_date=month_date,
                        balance=parsed_account.computed_total,
                    ))

                # Idempotent on re-import: replace this account's transactions for the month.
                BankTransaction.query.filter_by(account_id=account.id, month_date=month_date).delete()
                for i, txn in enumerate(parsed_account.transactions):
                    category_id = transaction_categories.get(_transaction_key(parsed_account.key, i))
                    if category_id not in valid_category_ids:
                        warnings.append(
                            f'Transaction on {txn.date.isoformat()} ({txn.description[:40]}) has no valid '
                            f'category — it will not be included in the expense/income summary.'
                        )
                        continue
                    db.session.add(BankTransaction(
                        account_id=account.id, transaction_date=txn.date, month_date=month_date,
                        description=txn.description, amount=txn.amount, direction=txn.direction,
                        category_id=category_id,
                    ))
                    transactions_imported += 1
                continue

            existing_holdings = {
                h.ticker.upper(): h for h in Holding.query.filter_by(account_id=account.id).all()
            }
            seen_tickers = set()

            for pos in parsed_account.positions:
                seen_tickers.add(pos.ticker)
                holding = existing_holdings.get(pos.ticker)
                is_new = holding is None
                if is_new:
                    holding = Holding(account_id=account.id, ticker=pos.ticker)
                    db.session.add(holding)
                holding.is_active = True
                holding.shares = pos.quantity
                holding.name = pos.description or pos.ticker
                if pos.price is not None:
                    holding.last_price = pos.price
                    holding.last_fetched = now

                if is_new:
                    holdings_created += 1
                    db.session.flush()  # populate holding.id for allocation rows
                    if ai_enabled and api_key:
                        try:
                            from app.classification_service import get_or_classify
                            cls_result, _ = get_or_classify(pos.ticker, api_key)
                            holding.cap_class = cls_result.get('market_cap_tilt')
                            for asset_cls, pct in cls_result.get('sector_weights', {}).items():
                                if pct > 0:
                                    db.session.add(HoldingAllocation(
                                        holding_id=holding.id, asset_class=asset_cls, percentage=pct,
                                    ))
                            classified += 1
                        except Exception as e:
                            logger.warning('Classification failed: ticker=%s error=%s', pos.ticker, e)
                            warnings.append(f'Could not classify new ticker {pos.ticker}: {e}')
                            classification_skipped += 1
                    else:
                        classification_skipped += 1
                else:
                    holdings_updated += 1

            for ticker, holding in existing_holdings.items():
                if ticker not in seen_tickers and holding.is_active:
                    holding.is_active = False
                    holdings_archived += 1

            snapshot = AccountSnapshot.query.filter_by(
                account_id=account.id, snapshot_date=month_date
            ).first()
            if snapshot:
                snapshot.balance = parsed_account.computed_total
            else:
                db.session.add(AccountSnapshot(
                    account_id=account.id, snapshot_date=month_date,
                    balance=parsed_account.computed_total,
                ))

        if touches_bank_transactions:
            db.session.flush()
            spending_entries_written = _rollup_spending_entries(db, models, month_date)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning('Brokerage import commit failed: filename=%s error=%s', filename, e)
        return {'success': False, 'errors': [f'Database error during import: {e}'],
                'warnings': warnings, 'error_type': 'server'}

    from app.metrics_service import recalculate_metrics
    recalculate_metrics(month_date)

    total_records = holdings_created + holdings_updated + holdings_archived + len(validated) + transactions_imported
    _log_import(
        db, ImportLog, filename, total_records, 'success' if not warnings else 'partial', None,
        json.dumps({
            'source_format': parsed.source_format, 'institution': parsed.institution,
            'accounts': len(validated), 'holdings_created': holdings_created,
            'holdings_updated': holdings_updated, 'holdings_archived': holdings_archived,
            'transactions_imported': transactions_imported,
        }),
    )
    logger.info(
        'Brokerage import committed: filename=%s accounts=%d created=%d updated=%d archived=%d classified=%d '
        'transactions=%d spending_entries=%d',
        filename, len(validated), holdings_created, holdings_updated, holdings_archived, classified,
        transactions_imported, spending_entries_written,
    )

    return {
        'success': True,
        'month': month_date.strftime('%Y-%m'),
        'accounts_updated': len(validated),
        'holdings_created': holdings_created,
        'holdings_updated': holdings_updated,
        'holdings_archived': holdings_archived,
        'snapshots_upserted': len(validated),
        'transactions_imported': transactions_imported,
        'spending_entries_written': spending_entries_written,
        'classified': classified,
        'classification_skipped': classification_skipped,
        'warnings': warnings,
        'errors': [],
    }
