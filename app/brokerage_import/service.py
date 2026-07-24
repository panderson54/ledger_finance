"""
DB-writing orchestration for brokerage import (CSV positions export or PDF
statement). Dependency-injection style (db, models passed in), mirroring
app/import_processor.py, so the parsing modules stay pure/DB-free and this
is the only place that touches the database for this feature.
"""
import json
import logging
from datetime import datetime, timezone

from app.brokerage_import.parsing import parse_positions_csv
from app.brokerage_import.pdf_parsing import parse_positions_pdf
from app.brokerage_import.matching import suggest_matches
from app.brokerage_import.types import ParsedImport
from app.account_categories import INVESTMENT_CATS
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


def build_preview(db, models, file_bytes: bytes, filename: str) -> dict:
    """Read-only: parse the upload and suggest account matches. No DB writes."""
    parsed = _parse_file(file_bytes, filename)

    Account = models['Account']
    investment_accounts = (
        Account.query.filter(Account.is_active == True, Account.category.in_(INVESTMENT_CATS))
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

    accounts_out = []
    for acct in parsed.accounts:
        match = matches.get(acct.key, {})
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
        })

    logger.info(
        'Brokerage import preview: filename=%s institution=%s format=%s accounts=%d',
        filename, parsed.institution, parsed.source_format, len(accounts_out),
    )

    return {
        'institution': parsed.institution,
        'source_format': parsed.source_format,
        'as_of_date': parsed.as_of_date.isoformat() if parsed.as_of_date else None,
        'default_month': default_month,
        'accounts': accounts_out,
        'investment_accounts': [{'id': a.id, 'name': a.name} for a in investment_accounts],
        'warnings': parsed.warnings,
        'errors': parsed.errors,
    }


def commit_import(db, models, file_bytes: bytes, filename: str, mapping: dict,
                   month_date, ai_enabled: bool, api_key: str) -> dict:
    """
    Re-parses file_bytes (stateless — no server-side session between preview
    and commit) and writes Holding/AccountSnapshot rows for each mapped
    account, then recalculates metrics for month_date. All-or-nothing: a
    failure at any point rolls back the whole transaction.
    """
    parsed = _parse_file(file_bytes, filename)
    if parsed.errors:
        return {'success': False, 'errors': parsed.errors, 'warnings': parsed.warnings, 'error_type': 'validation'}

    Account = models['Account']
    Holding = models['Holding']
    HoldingAllocation = models['HoldingAllocation']
    AccountSnapshot = models['AccountSnapshot']
    ImportLog = models['ImportLog']

    validated = []
    errors = []
    for acct in parsed.accounts:
        account_id = mapping.get(acct.key)
        if account_id is None:
            continue  # user chose to skip this detected account
        account = db.session.get(Account, account_id)
        if account is None or not account.is_active or account.category not in INVESTMENT_CATS:
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
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    try:
        for account, parsed_account in validated:
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

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger.warning('Brokerage import commit failed: filename=%s error=%s', filename, e)
        return {'success': False, 'errors': [f'Database error during import: {e}'],
                'warnings': warnings, 'error_type': 'server'}

    from app.metrics_service import recalculate_metrics
    recalculate_metrics(month_date)

    total_records = holdings_created + holdings_updated + holdings_archived + len(validated)
    _log_import(
        db, ImportLog, filename, total_records, 'success' if not warnings else 'partial', None,
        json.dumps({
            'source_format': parsed.source_format, 'institution': parsed.institution,
            'accounts': len(validated), 'holdings_created': holdings_created,
            'holdings_updated': holdings_updated, 'holdings_archived': holdings_archived,
        }),
    )
    logger.info(
        'Brokerage import committed: filename=%s accounts=%d created=%d updated=%d archived=%d classified=%d',
        filename, len(validated), holdings_created, holdings_updated, holdings_archived, classified,
    )

    return {
        'success': True,
        'month': month_date.strftime('%Y-%m'),
        'accounts_updated': len(validated),
        'holdings_created': holdings_created,
        'holdings_updated': holdings_updated,
        'holdings_archived': holdings_archived,
        'snapshots_upserted': len(validated),
        'classified': classified,
        'classification_skipped': classification_skipped,
        'warnings': warnings,
        'errors': [],
    }
