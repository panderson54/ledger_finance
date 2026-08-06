"""
Deterministic parser for Fidelity/Schwab brokerage "Positions" CSV exports.

Pure functions only — no DB/Flask imports — so this is unit-testable with
fabricated CSV text. See app/brokerage_import/service.py for the DB-writing
step that consumes ParsedImport.
"""
import io
import logging
import re

import pandas as pd

from app.import_processor import _parse_currency
from app.brokerage_import.column_map import (
    INSTITUTION_MARKERS, FIELD_CANDIDATES, MANDATORY_FIELDS, FOOTER_MARKERS,
    CASH_TICKERS, CASH_DESCRIPTION_PATTERNS,
)
from app.brokerage_import.types import ParsedImport, ParsedAccount, ParsedPosition

logger = logging.getLogger(__name__)

_AS_OF_PATTERNS = [
    re.compile(r'as of\s+(\d{1,2}/\d{1,2}/\d{2,4})', re.IGNORECASE),
    re.compile(r'date downloaded[:\s]+(\d{1,2}/\d{1,2}/\d{2,4})', re.IGNORECASE),
]


def sniff_institution(text: str) -> str:
    """Return 'fidelity', 'schwab', or 'unknown' based on marker substrings."""
    lowered = text.lower()
    for institution, markers in INSTITUTION_MARKERS.items():
        if any(m in lowered for m in markers):
            return institution
    return 'unknown'


def _extract_as_of_date(text: str):
    from datetime import datetime
    for pattern in _AS_OF_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1)
        for fmt in ('%m/%d/%Y', '%m/%d/%y'):
            try:
                return datetime.strptime(raw, fmt).date()
            except ValueError:
                continue
    return None


def _locate_header_row(lines: list[str]) -> int | None:
    """Scan the first ~15 non-empty lines for one matching all MANDATORY_FIELDS."""
    checked = 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        checked += 1
        if checked > 15:
            break
        cells = [c.strip().lower() for c in line.split(',')]
        matched = {}
        for field, candidates in FIELD_CANDIDATES.items():
            for cell in cells:
                if cell in candidates:
                    matched[field] = True
                    break
        if all(f in matched for f in MANDATORY_FIELDS):
            return i
    return None


def _locate_footer_start(lines: list[str], header_idx: int) -> int:
    """First line at/after header_idx whose lowercase text hits a FOOTER_MARKERS substring."""
    for i in range(header_idx, len(lines)):
        lowered = lines[i].lower()
        if any(marker in lowered for marker in FOOTER_MARKERS):
            return i
    return len(lines)


def _map_columns(header_cells: list[str]) -> dict[str, str]:
    """Return {field: actual_column_name} for columns matching FIELD_CANDIDATES."""
    mapping = {}
    for field, candidates in FIELD_CANDIDATES.items():
        for cell in header_cells:
            if cell.strip().lower() in candidates:
                mapping[field] = cell
                break
    return mapping


def _is_cash_row(ticker: str, description: str) -> bool:
    if ticker.upper() in CASH_TICKERS:
        return True
    desc_lower = (description or '').lower()
    return any(re.search(pat, desc_lower) for pat in CASH_DESCRIPTION_PATTERNS)


def _account_key(institution: str, account_name: str, account_number_last4: str | None) -> str:
    return f"{institution}:{account_name}:{account_number_last4 or ''}"


def parse_positions_csv(file_text: str) -> ParsedImport:
    """
    Parse a Fidelity/Schwab positions CSV export into a ParsedImport.
    Never raises — returns ParsedImport with errors populated on failure.
    """
    result = ParsedImport(institution='unknown', source_format='csv')
    if not file_text or not file_text.strip():
        result.errors.append('File is empty.')
        return result

    result.institution = sniff_institution(file_text)
    if result.institution == 'unknown':
        result.warnings.append(
            'Could not confirm institution — parsed using generic column matching.'
        )
    result.as_of_date = _extract_as_of_date(file_text)

    lines = file_text.splitlines()
    header_idx = _locate_header_row(lines)
    if header_idx is None:
        result.errors.append(
            'Could not locate a header row with recognizable Symbol/Quantity columns.'
        )
        return result

    footer_idx = _locate_footer_start(lines, header_idx + 1)
    table_text = '\n'.join(lines[header_idx:footer_idx])

    try:
        df = pd.read_csv(io.StringIO(table_text), header=0)
    except Exception as e:
        result.errors.append(f'Could not parse the CSV table: {e}')
        return result

    if df.empty:
        result.warnings.append('No position rows found in the file.')
        return result

    col_map = _map_columns(list(df.columns))
    if 'symbol' not in col_map or 'quantity' not in col_map:
        result.errors.append('Could not identify Symbol/Quantity columns in the file.')
        return result

    accounts: dict[str, ParsedAccount] = {}
    default_account_key = None

    for _, row in df.iterrows():
        ticker = str(row.get(col_map['symbol'], '')).strip().upper()
        if not ticker or ticker.lower() in ('nan', 'none'):
            continue

        quantity = _parse_currency(row.get(col_map.get('quantity'))) if 'quantity' in col_map else None
        if quantity is None:
            continue

        description = str(row.get(col_map.get('description', ''), '') or '').strip()
        price = _parse_currency(row.get(col_map.get('price'))) if 'price' in col_map else None
        value = _parse_currency(row.get(col_map.get('value'))) if 'value' in col_map else None
        if value is None and price is not None:
            value = round(quantity * price, 2)

        account_name = None
        account_number = None
        if 'account_name' in col_map:
            account_name = str(row.get(col_map['account_name'], '') or '').strip() or None
        if 'account_number' in col_map:
            raw_num = str(row.get(col_map['account_number'], '') or '').strip()
            account_number = raw_num or None

        if account_name is None:
            # No account column — single-account (per-account) export.
            account_name = 'Account'
            if default_account_key is None:
                default_account_key = _account_key(result.institution, account_name, account_number)
            key = default_account_key
        else:
            last4 = re.sub(r'\D', '', account_number)[-4:] if account_number else None
            key = _account_key(result.institution, account_name, last4)

        if key not in accounts:
            last4 = re.sub(r'\D', '', account_number)[-4:] if account_number else None
            accounts[key] = ParsedAccount(
                key=key, institution=result.institution,
                account_name=account_name, account_number_last4=last4,
            )
        account = accounts[key]

        is_cash = _is_cash_row(ticker, description)
        if is_cash:
            account.cash_total += value or 0.0
        else:
            account.positions.append(ParsedPosition(
                ticker=ticker, description=description, quantity=quantity,
                price=price, value=value, is_cash=False,
            ))

    for account in accounts.values():
        account.computed_total = round(
            sum((p.value or 0.0) for p in account.positions) + account.cash_total, 2
        )

    if not accounts:
        result.warnings.append('No holdings could be extracted from this file.')

    result.accounts = list(accounts.values())
    return result
