"""
Deterministic parser for Fidelity/Schwab brokerage monthly statement PDFs.

Pure functions only — no DB/Flask imports — so this is unit-testable with
fabricated statement fixtures. Produces the same ParsedImport shape as
parsing.py (the CSV path); service.py doesn't care which format was used.

Schwab and Fidelity statements are laid out very differently (see
column_map.py for the verified specifics), so row reconstruction is
institution-specific past a shared line-clustering/number-parsing core.
"""
import io
import logging
import re

from app.brokerage_import.column_map import (
    INSTITUTION_MARKERS, PDF_SECTION_HEADINGS, PDF_COLUMN_BANDS,
    PDF_NUMERIC_FIELDS, PDF_COLLISION_FIELDS, SECTION_FINAL_MARKER, SECTION_CONTINUE_MARKERS,
    FIDELITY_SUBSECTION_HEADINGS, CASH_TICKERS, CASH_DESCRIPTION_PATTERNS,
    PDF_TRANSACTION_COLUMN_BANDS, PDF_TRANSACTION_SECTION_HEADINGS, PDF_TRANSACTION_SECTION_STOP_MARKERS,
)
from app.brokerage_import.types import ParsedImport, ParsedAccount, ParsedPosition, ParsedTransaction

logger = logging.getLogger(__name__)

_TICKER_IN_PARENS_RE = re.compile(r'\(([A-Z][A-Z0-9.]{0,9})\)')
# Fidelity account numbers: 2-4 alnum + 5-7 digits (e.g. X83-655756, 603-977090).
_FIDELITY_ACCOUNT_NUMBER_RE = re.compile(r'\b([A-Z0-9]{2,4}-\d{5,7})\b')
# Schwab brokerage account numbers: 4 digits + 4 digits (e.g. 3104-6426).
_SCHWAB_ACCOUNT_NUMBER_RE = re.compile(r'\b(\d{4}-\d{4})\b')
# Schwab Bank account numbers: 10-14 unformatted digits (e.g. 440054642648).
_SCHWAB_BANK_ACCOUNT_NUMBER_RE = re.compile(r'\b(\d{10,14})\b')
_SCHWAB_BANK_ENDING_BALANCE_RE = re.compile(r'Ending Balance\s+\$?([\d,]+\.\d{2})', re.IGNORECASE)
# Wealthfront account numbers: digit + letter + 4-8 alnum (e.g. 8W597901, 8W159VG4).
_WEALTHFRONT_ACCOUNT_NUMBER_RE = re.compile(r'\b([0-9][A-Z][A-Z0-9]{4,8})\b')
# Wealthfront position lines: description ticker qty $price(4dp) $value(2dp).
_WEALTHFRONT_POSITION_RE = re.compile(
    r'^(.*?)\s+([A-Z][A-Z0-9.]{1,9})\s+([\d,]+(?:\.\d+)?)\s+\$([\d,]+\.\d{4})\s+\$([\d,]+\.\d{2})\s*$'
)
_WEALTHFRONT_TOTAL_RE = re.compile(
    r'Total\s+(?:Account\s+)?Value\s*\$?([\d,]+\.\d{2})', re.IGNORECASE
)
_WEALTHFRONT_BALANCE_RE = re.compile(
    r'(?:Total|Account|Ending)\s+(?:Account\s+)?(?:Balance|Value)\s*\$?([\d,]+\.\d{2})', re.IGNORECASE
)
_DATE_MMDD_RE = re.compile(r'^\d{2}/\d{2}$')
# Wealthfront's Green Dot Bank companion (debit-card) account number:
# unformatted digits with dashes, e.g. 1115-4166-6112-63.
_GREENDOT_ACCOUNT_NUMBER_RE = re.compile(r'\b(\d{4}-\d{4}-\d{4}-\d{2})\b')
_GREENDOT_ENDING_BALANCE_RE = re.compile(r'Ending Balance on[^$]*\$?([\d,]+\.\d{2})', re.IGNORECASE)
_STATEMENT_PERIOD_RE = re.compile(
    r'(January|February|March|April|May|June|July|August|September|October|November|December)'
    r'\s*(\d{1,2})(?:-\d{1,2})?,?\s*(\d{4})',
    re.IGNORECASE,
)


def _find_pdf_start(file_bytes: bytes) -> bytes:
    """Strip any bytes preceding the '%PDF-' header (observed upload-pipeline quirk)."""
    idx = file_bytes.find(b'%PDF-')
    if idx <= 0:
        return file_bytes
    logger.warning('PDF import: stripped %d leading bytes before %%PDF- header', idx)
    return file_bytes[idx:]


def _open_pdf_pdfminer(file_bytes: bytes):
    """
    Thin pdfplumber-compatible wrapper using pdfminer.six for platforms where
    pdfplumber's pypdfium2 dependency cannot be built (e.g. armv6l Raspberry Pi).
    Provides .pages[].extract_text() and .pages[].extract_words() only.
    """
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextBoxHorizontal, LTTextLine, LTChar

    class _Page:
        def __init__(self, layout):
            self._layout = layout
            self._height = layout.height

        def extract_text(self):
            parts = []
            for el in self._layout:
                if isinstance(el, LTTextBoxHorizontal):
                    parts.append(el.get_text().strip())
            return '\n'.join(parts)

        def extract_words(self):
            words = []
            page_height = self._height
            for el in self._layout:
                if not isinstance(el, LTTextBoxHorizontal):
                    continue
                for line in el:
                    if not isinstance(line, LTTextLine):
                        continue
                    # Convert pdfminer bottom-up y to pdfplumber top-down 'top'.
                    line_top = page_height - line.y1
                    buf: list[str] = []
                    word_x0: float | None = None
                    for char in line:
                        if isinstance(char, LTChar):
                            ch = char.get_text()
                            if ch.strip():
                                if word_x0 is None:
                                    word_x0 = char.x0
                                buf.append(ch)
                            else:
                                if buf:
                                    words.append({'text': ''.join(buf), 'x0': word_x0, 'top': line_top})
                                    buf = []
                                    word_x0 = None
                    if buf:
                        words.append({'text': ''.join(buf), 'x0': word_x0, 'top': line_top})
            return words

    class _PDF:
        def __init__(self, pages):
            self.pages = pages

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    pages = [_Page(layout) for layout in extract_pages(io.BytesIO(file_bytes))]
    return _PDF(pages)


def _open_pdf(file_bytes: bytes):
    cleaned = _find_pdf_start(file_bytes)
    try:
        import pdfplumber
        return pdfplumber.open(io.BytesIO(cleaned))
    except ImportError:
        return _open_pdf_pdfminer(cleaned)


def sniff_institution(text: str) -> str:
    lowered = text.lower()
    for institution, markers in INSTITUTION_MARKERS.items():
        if any(m in lowered for m in markers):
            return institution
    return 'unknown'


def _parse_pdf_number(token: str) -> float | None:
    t = (token or '').strip()
    if not t:
        return None
    neg = t.startswith('(') and t.endswith(')')
    t = t.strip('()')
    t = t.replace('$', '').replace(',', '').replace('%', '')
    if not t or t in ('-', '--'):
        return None
    try:
        val = float(t)
    except ValueError:
        return None
    return -val if neg else val


def _cluster_lines(words: list[dict]) -> list[list[dict]]:
    """Group words into text lines by rounded 'top' position, sorted left-to-right."""
    buckets: dict[int, list[dict]] = {}
    for w in words:
        buckets.setdefault(round(w['top']), []).append(w)
    return [sorted(buckets[top], key=lambda w: w['x0']) for top in sorted(buckets)]


def _band_for_x(x0: float, bands: dict[str, tuple[float, float]]) -> str | None:
    for field, (lo, hi) in bands.items():
        if lo <= x0 < hi:
            return field
    return None


class _OpenRow:
    def __init__(self):
        self.description_words: list[str] = []
        self.symbol: str | None = None
        self.ticker_from_desc: str | None = None
        self.numeric: dict[str, float] = {}

    def has_data(self) -> bool:
        return bool(self.description_words) or bool(self.numeric) or self.symbol

    def is_valid(self) -> bool:
        # Most rows are anchored by quantity; bank-sweep-style balance rows
        # (e.g. Schwab's "Charles Schwab Bank") have no quantity/price at all,
        # just an ending balance — still valid if they carry a value.
        return 'quantity' in self.numeric or 'value' in self.numeric


def _reconstruct_rows(lines: list[list[dict]], bands: dict[str, tuple[float, float]],
                       final_marker: str, continue_markers: list[str],
                       has_symbol_band: bool, skip_headings: list[str] | None = None) -> list[_OpenRow]:
    """
    Shared state machine: group a section's text lines into per-position rows.
    A line supplying a numeric field already filled on the open row signals a
    new row (flush + start fresh). Text-only lines (description/ticker
    continuations, yield notes) merge into the open row without flushing.

    Stops entirely at the section's own closing "Total ..." line (final_marker)
    so unrelated content further down the page is never swept in.
    continue_markers flush the current row but keep processing (used for
    Fidelity's Stock Funds / Bond Funds sub-groups within Mutual Funds).
    """
    rows: list[_OpenRow] = []
    current = _OpenRow()
    skip_headings_norm = {re.sub(r'\s+', '', h.lower()) for h in (skip_headings or [])}

    def flush():
        nonlocal current
        if current.is_valid():
            rows.append(current)
        current = _OpenRow()

    for line in lines:
        line_text = ' '.join(w['text'] for w in line)
        desc_band_text = ' '.join(
            w['text'] for w in line if _band_for_x(w['x0'], bands) == 'description'
        )

        # Column-header row guard (Fidelity's 3-line header embeds "Jun 30, 2026"
        # date fragments that would otherwise misparse as numeric column data).
        if desc_band_text.strip().lower().startswith('description'):
            continue

        # Sub-grouping heading lines (e.g. Fidelity's "Stock Funds"/"Bond Funds"
        # inside Mutual Funds) — pure labels, not row content.
        if re.sub(r'\s+', '', line_text.strip().lower()) in skip_headings_norm:
            continue

        # Yield-note line ("-- 7-day yield: 3.29%") — informational only, skip.
        if line_text.strip().startswith('--'):
            continue

        # Compare whitespace-insensitively — pdfplumber sometimes merges
        # tightly-kerned multi-word phrases into a single token (observed on
        # Schwab statements, e.g. "TotalCashandCashInvestments" as one word).
        lowered_nospace = re.sub(r'\s+', '', line_text.strip().lower())
        if lowered_nospace.startswith(re.sub(r'\s+', '', final_marker)):
            flush()
            break
        if any(lowered_nospace.startswith(re.sub(r'\s+', '', marker)) for marker in continue_markers):
            flush()
            continue

        # Section/subsection heading lines have no numeric-band content at all.
        field_values: dict[str, float] = {}
        symbol_val = None
        for w in line:
            band = _band_for_x(w['x0'], bands)
            if band is None or band == 'description':
                continue
            if band == 'symbol':
                symbol_val = w['text'].strip()
                continue
            if band in PDF_NUMERIC_FIELDS:
                val = _parse_pdf_number(w['text'])
                if val is not None:
                    field_values[band] = val

        ticker_match = _TICKER_IN_PARENS_RE.search(line_text)

        if not field_values and not symbol_val and not ticker_match and not desc_band_text.strip():
            continue  # pure heading/blank line

        # Collision check: any field this line supplies is already filled -> new row.
        collision = any(f in current.numeric for f in field_values if f in PDF_COLLISION_FIELDS) or (
            has_symbol_band and symbol_val and current.symbol
        )
        if collision:
            flush()

        for f, v in field_values.items():
            current.numeric.setdefault(f, v)
        if symbol_val and not current.symbol:
            current.symbol = symbol_val
        if ticker_match and not current.ticker_from_desc:
            current.ticker_from_desc = ticker_match.group(1)
        if desc_band_text.strip():
            # Strip the parenthetical ticker out of description text.
            cleaned = _TICKER_IN_PARENS_RE.sub('', desc_band_text).strip()
            if cleaned:
                current.description_words.append(cleaned)

    flush()
    return rows


def _rows_to_positions(rows: list[_OpenRow]) -> list[ParsedPosition]:
    positions = []
    for row in rows:
        ticker = (row.symbol or row.ticker_from_desc or '').strip().upper()
        if not ticker and row.numeric.get('value') is not None:
            # Bank-sweep-style balance rows (e.g. Schwab's "Charles Schwab
            # Bank") carry no ticker at all — treat as an unlabeled cash line.
            ticker = 'CASH'
        if not ticker:
            continue
        description = ' '.join(row.description_words).strip()
        quantity = row.numeric.get('quantity')
        price = row.numeric.get('price')
        value = row.numeric.get('value')
        if value is None and price is not None and quantity is not None:
            value = round(quantity * price, 2)
        is_cash = ticker in CASH_TICKERS or any(
            re.search(pat, description.lower()) for pat in CASH_DESCRIPTION_PATTERNS
        )
        positions.append(ParsedPosition(
            ticker=ticker, description=description, quantity=quantity or 0.0,
            price=price, value=value, is_cash=is_cash,
        ))
    return positions


def _account_key(institution: str, account_name: str, account_number_last4: str | None) -> str:
    return f"{institution}:{account_name}:{account_number_last4 or ''}"


def _extract_statement_period(text: str):
    """Return the as-of date (last day of statement month) from a statement-period line."""
    from datetime import date
    import calendar
    matches = _STATEMENT_PERIOD_RE.findall(text)
    if not matches:
        return None
    month_name, _, year = matches[-1]
    try:
        month_num = list(calendar.month_name).index(month_name.capitalize())
        if month_num == 0:
            month_num = list(calendar.month_abbr).index(month_name.capitalize()[:3])
        year_num = int(year)
        last_day = calendar.monthrange(year_num, month_num)[1]
        return date(year_num, month_num, last_day)
    except (ValueError, IndexError):
        return None


# ---------------------------------------------------------------------------
# Schwab
# ---------------------------------------------------------------------------

def _parse_schwab_pdf(pdf) -> ParsedImport:
    result = ParsedImport(institution='schwab', source_format='pdf')

    full_text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    result.as_of_date = _extract_statement_period(full_text)

    account_match = _SCHWAB_ACCOUNT_NUMBER_RE.search(pdf.pages[0].extract_text() or '')
    account_number_full = account_match.group(1) if account_match else None
    last4 = re.sub(r'\D', '', account_number_full)[-4:] if account_number_full else None

    account = ParsedAccount(
        key=_account_key('schwab', 'Schwab Account', last4),
        institution='schwab', account_name='Schwab Account', account_number_last4=last4,
    )

    bands_by_section = PDF_COLUMN_BANDS['schwab']
    final_markers = SECTION_FINAL_MARKER['schwab']
    headings = PDF_SECTION_HEADINGS['schwab']

    for page in pdf.pages:
        words = page.extract_words()
        lines = _cluster_lines(words)
        page_text = page.extract_text() or ''

        for section_key in ('cash', 'mutual_funds', 'etfs'):
            heading = headings[section_key]
            if heading.lower() not in page_text.lower():
                continue
            bands = bands_by_section.get(section_key)
            if not bands:
                continue
            heading_top = next(
                (min(w['top'] for w in line) for line in lines
                 if heading.lower().replace(' ', '') in ''.join(w['text'] for w in line).lower().replace(' ', '')),
                None,
            )
            if heading_top is None:
                continue
            section_lines = [line for line in lines if min(w['top'] for w in line) > heading_top]
            rows = _reconstruct_rows(section_lines, bands, final_markers[section_key], [],
                                      has_symbol_band=True)
            positions = _rows_to_positions(rows)
            for p in positions:
                if p.is_cash:
                    account.cash_total += p.value or 0.0
                else:
                    account.positions.append(p)

    # Best-effort reported total from the "Positions - Summary" Ending Value column.
    try:
        account.reported_total = _extract_schwab_reported_total(pdf)
    except Exception as e:
        logger.warning('Schwab PDF: could not extract reported total: %s', e)

    account.computed_total = round(
        sum((p.value or 0.0) for p in account.positions) + account.cash_total, 2
    )
    if not account.positions and not account.cash_total:
        result.warnings.append('Could not find any recognized positions section in this PDF.')
    else:
        result.accounts.append(account)
    return result


def _extract_schwab_reported_total(pdf) -> float | None:
    for page in pdf.pages:
        text = page.extract_text() or ''
        if 'positions' not in text.lower() or 'summary' not in text.lower():
            continue
        words = page.extract_words()
        lines = _cluster_lines(words)
        heading_top = None
        for line in lines:
            joined = ''.join(w['text'] for w in line).lower()
            if 'positions' in joined and 'summary' in joined:
                heading_top = min(w['top'] for w in line)
                break
        if heading_top is None:
            continue
        # First data row after the heading with several dollar-shaped tokens.
        for line in lines:
            if min(w['top'] for w in line) <= heading_top:
                continue
            numeric_words = [w for w in line if _parse_pdf_number(w['text']) is not None]
            if len(numeric_words) >= 2:
                # "Ending Value as of" column sits around x0 470-520 in the sample.
                candidates = [w for w in numeric_words if 460 <= w['x0'] < 530]
                if candidates:
                    return _parse_pdf_number(candidates[0]['text'])
                break
    return None


# ---------------------------------------------------------------------------
# Fidelity
# ---------------------------------------------------------------------------

def _discover_fidelity_accounts(pdf) -> list[dict]:
    """Parse the 'Accounts Included in This Report' summary table (page 2 in the sample)."""
    accounts = []
    for page in pdf.pages:
        text = page.extract_text() or ''
        if 'accounts included in this report' not in text.lower():
            continue
        pending_name_lines: list[str] = []
        for line in text.splitlines():
            m = _FIDELITY_ACCOUNT_NUMBER_RE.search(line)
            if not m:
                stripped = line.strip()
                # Accumulate non-numeric lines as potential account name fragments;
                # reset on blank or value-only lines (dollar amounts, page refs).
                if stripped and not re.match(r'^[\d\s,.$%-]+$', stripped):
                    pending_name_lines.append(stripped)
                else:
                    pending_name_lines = []
                continue
            account_number = m.group(1)
            name_part = line[:m.start()].strip()
            # Strip a leading page-number token ("4 " or bare "11").
            name_part = re.sub(r'^\d+\s*', '', name_part).strip()
            # Name may appear on the preceding line when only a page number
            # precedes the account number on this line (multi-account statements).
            if not name_part and pending_name_lines:
                name_part = pending_name_lines[-1]
            if name_part:
                accounts.append({'account_number': account_number, 'account_name': name_part})
            pending_name_lines = []
        break
    return accounts


def _parse_fidelity_pdf(pdf) -> ParsedImport:
    result = ParsedImport(institution='fidelity', source_format='pdf')

    full_text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    result.as_of_date = _extract_statement_period(full_text)

    discovered = _discover_fidelity_accounts(pdf)
    if not discovered:
        # Single-account fallback: look for any "Account # ..." header.
        m = _FIDELITY_ACCOUNT_NUMBER_RE.search(full_text)
        if m:
            discovered = [{'account_number': m.group(1), 'account_name': 'Fidelity Account'}]

    if not discovered:
        result.errors.append('Could not find any recognized positions section in this PDF.')
        return result

    bands_by_section = PDF_COLUMN_BANDS['fidelity']
    final_markers = SECTION_FINAL_MARKER['fidelity']
    continue_markers_by_section = SECTION_CONTINUE_MARKERS.get('fidelity', {})
    headings = PDF_SECTION_HEADINGS['fidelity']

    for acct_info in discovered:
        account_number = acct_info['account_number']
        last4 = re.sub(r'\D', '', account_number)[-4:]
        account = ParsedAccount(
            key=_account_key('fidelity', acct_info['account_name'], last4),
            institution='fidelity', account_name=acct_info['account_name'],
            account_number_last4=last4,
        )
        header_marker = f'Account # {account_number}'

        for page in pdf.pages:
            page_text = page.extract_text() or ''
            if header_marker not in page_text:
                continue
            words = page.extract_words()
            lines = _cluster_lines(words)

            for section_key in ('core', 'mutual_funds'):
                heading = headings[section_key]
                bands = bands_by_section.get(section_key)
                if not bands:
                    continue
                heading_top = next(
                    (min(w['top'] for w in line) for line in lines
                     if heading.lower() == ''.join(w['text'] for w in line).lower().replace(' ', '')
                     or heading.lower() in ' '.join(w['text'] for w in line).lower()),
                    None,
                )
                if heading_top is None:
                    continue
                section_lines = [line for line in lines if min(w['top'] for w in line) > heading_top]
                rows = _reconstruct_rows(
                    section_lines, bands, final_markers[section_key],
                    continue_markers_by_section.get(section_key, []), has_symbol_band=False,
                    skip_headings=FIDELITY_SUBSECTION_HEADINGS.get(section_key, []),
                )
                positions = _rows_to_positions(rows)
                for p in positions:
                    if p.is_cash:
                        account.cash_total += p.value or 0.0
                    else:
                        account.positions.append(p)

            # "Total Holdings" row gives the account-level reported total.
            reported = _extract_fidelity_total_holdings(lines)
            if reported is not None:
                account.reported_total = reported

        account.computed_total = round(
            sum((p.value or 0.0) for p in account.positions) + account.cash_total, 2
        )
        result.accounts.append(account)

    if not any(a.positions or a.cash_total for a in result.accounts):
        result.warnings.append('No holdings could be extracted from this file.')
    return result


def _extract_fidelity_total_holdings(lines: list[list[dict]]) -> float | None:
    for line in lines:
        text = ' '.join(w['text'] for w in line).lower()
        if not text.startswith('total holdings'):
            continue
        numeric_words = [w for w in line if _parse_pdf_number(w['text']) is not None]
        if numeric_words:
            return _parse_pdf_number(numeric_words[0]['text'])
    return None


# ---------------------------------------------------------------------------
# Bank/cash statement transaction tables (checking/savings — balance_only
# accounts). Row reconstruction is date-anchored (a new value in the 'date'
# band starts a new row) rather than the quantity/symbol-collision engine
# used for positions tables above, since these tables have no anchor field
# that's always present on every row.
# ---------------------------------------------------------------------------

def _reconstruct_transaction_rows(pdf, institution: str, year: int, month: int) -> list[ParsedTransaction]:
    """
    Walk every page looking for the institution's transaction-table section
    (bounded by PDF_TRANSACTION_SECTION_HEADINGS/_STOP_MARKERS) and emit one
    ParsedTransaction per row that carries a debit or credit amount.
    Rows with only a balance (Beginning/Ending Balance) are discarded.
    """
    bands = PDF_TRANSACTION_COLUMN_BANDS.get(institution)
    heading = PDF_TRANSACTION_SECTION_HEADINGS.get(institution)
    stop_marker = PDF_TRANSACTION_SECTION_STOP_MARKERS.get(institution)
    if not bands or not heading:
        return []

    from datetime import date

    rows: list[dict] = []
    current: dict | None = None

    def flush():
        nonlocal current
        if current and (current['debit'] is not None or current['credit'] is not None):
            rows.append(current)
        current = None

    in_section = False
    for page in pdf.pages:
        lines = _cluster_lines(page.extract_words())
        for line in lines:
            line_text = ' '.join(w['text'] for w in line)
            lowered = line_text.strip().lower()

            if not in_section:
                if lowered.startswith(heading):
                    in_section = True
                continue

            if lowered.startswith(stop_marker):
                flush()
                in_section = False
                continue

            # Repeated column-header row on continuation pages.
            if lowered.startswith('date') and 'description' in lowered:
                continue
            if lowered in ('posted', 'posted description debits credits balance'):
                continue

            # Repeated page letterhead/footer (copyright line, "(continued)"
            # section headings, "Page X of Y") — flush rather than merge, so
            # a row still open at a page break doesn't absorb this boilerplate
            # (which would otherwise land in the description band, e.g. the
            # account holder's running-header name) into its description.
            if '©' in line_text or 'all rights reserved' in lowered or '(continued)' in lowered \
                    or re.match(r'^page \d+ of \d+$', lowered):
                flush()
                continue

            date_token = next(
                (w['text'].strip() for w in line
                 if _band_for_x(w['x0'], bands) == 'date' and _DATE_MMDD_RE.match(w['text'].strip())),
                None,
            )
            if date_token:
                flush()
                current = {'date_str': date_token, 'description_words': [], 'debit': None, 'credit': None}

            if current is None:
                continue  # stray line before any row has opened

            desc_text = ' '.join(w['text'] for w in line if _band_for_x(w['x0'], bands) == 'description').strip()
            if desc_text:
                current['description_words'].append(desc_text)

            for w in line:
                band = _band_for_x(w['x0'], bands)
                if band not in ('debits', 'credits'):
                    continue
                val = _parse_pdf_number(w['text'])
                if val is not None:
                    current[band[:-1]] = val  # 'debits' -> 'debit', 'credits' -> 'credit'

    flush()

    transactions = []
    for r in rows:
        try:
            m, d = r['date_str'].split('/')
            txn_date = date(year, int(m), int(d))
        except ValueError:
            continue
        description = ' '.join(r['description_words']).strip()
        if r['debit'] is not None:
            transactions.append(ParsedTransaction(date=txn_date, description=description, amount=r['debit'], direction='debit'))
        elif r['credit'] is not None:
            transactions.append(ParsedTransaction(date=txn_date, description=description, amount=r['credit'], direction='credit'))
    return transactions


# ---------------------------------------------------------------------------
# Schwab Bank
# ---------------------------------------------------------------------------

def _parse_schwab_bank_pdf(pdf) -> ParsedImport:
    result = ParsedImport(institution='schwab_bank', source_format='pdf')

    full_text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    result.as_of_date = _extract_statement_period(full_text)

    acct_m = _SCHWAB_BANK_ACCOUNT_NUMBER_RE.search(full_text)
    account_number_full = acct_m.group(1) if acct_m else None
    last4 = account_number_full[-4:] if account_number_full else None

    bal_m = _SCHWAB_BANK_ENDING_BALANCE_RE.search(full_text)
    balance = float(bal_m.group(1).replace(',', '')) if bal_m else 0.0

    account = ParsedAccount(
        key=_account_key('schwab_bank', 'Schwab Bank Investor Checking', last4),
        institution='schwab_bank',
        account_name='Schwab Bank Investor Checking',
        account_number_last4=last4,
        cash_total=balance,
        computed_total=balance,
        reported_total=balance,
        balance_only=True,
    )
    if result.as_of_date:
        try:
            account.transactions = _reconstruct_transaction_rows(
                pdf, 'schwab_bank', result.as_of_date.year, result.as_of_date.month,
            )
        except Exception as e:
            logger.warning('Schwab Bank PDF: transaction parsing failed: %s', e)
            result.warnings.append('Could not extract transaction detail from this statement — only the balance was imported.')
    result.accounts.append(account)
    return result


# ---------------------------------------------------------------------------
# Wealthfront
# ---------------------------------------------------------------------------

def _parse_wealthfront_investment_pdf(pdf, full_text: str) -> ParsedImport:
    result = ParsedImport(institution='wealthfront', source_format='pdf')
    result.as_of_date = _extract_statement_period(full_text)

    acct_m = _WEALTHFRONT_ACCOUNT_NUMBER_RE.search(full_text)
    last4 = acct_m.group(1)[-4:] if acct_m else None
    lower = full_text.lower()
    if 'joint' in lower:
        account_name = 'Wealthfront Joint Investment Account'
    else:
        account_name = 'Wealthfront Individual Investment Account'

    account = ParsedAccount(
        key=_account_key('wealthfront', account_name, last4),
        institution='wealthfront', account_name=account_name,
        account_number_last4=last4,
    )

    for line in full_text.splitlines():
        m = _WEALTHFRONT_POSITION_RE.match(line.strip())
        if not m:
            continue
        description, ticker, qty_str, price_str, value_str = m.groups()
        ticker = ticker.upper()
        quantity = float(qty_str.replace(',', ''))
        price = float(price_str.replace(',', ''))
        value = float(value_str.replace(',', ''))
        is_cash = ticker in CASH_TICKERS or any(
            re.search(pat, description.lower()) for pat in CASH_DESCRIPTION_PATTERNS
        )
        pos = ParsedPosition(
            ticker=ticker, description=description.strip(),
            quantity=quantity, price=price, value=value, is_cash=is_cash,
        )
        if is_cash:
            account.cash_total += value
        else:
            account.positions.append(pos)

    total_m = _WEALTHFRONT_TOTAL_RE.search(full_text)
    if total_m:
        account.reported_total = float(total_m.group(1).replace(',', ''))

    account.computed_total = round(
        sum((p.value or 0.0) for p in account.positions) + account.cash_total, 2
    )
    if not account.positions and not account.cash_total:
        result.warnings.append('Could not find any positions in this Wealthfront statement.')
    else:
        result.accounts.append(account)
    return result


# Wealthfront cash-account activity lines are cleanly one-row-per-line once
# extracted (unlike Schwab's geometric table), so this is regex/line-based
# rather than column-band clustering: "<M/D/YYYY> <method/initiator text>
# <-$amount|$amount>". Section headings bucket each row as credit/debit, or
# skip entirely — "Transfer between Wealthfront and Program Banks" and the
# daily "Balance and Interest Rate Details" table are Wealthfront's own
# internal sweep bookkeeping, not transactions the user made.
_WF_CASH_ROW_RE = re.compile(r'^(\d{1,2}/\d{1,2}/\d{4})\s+(.+?)\s+(-?\$[\d,]+\.\d{2})$')

_WF_CASH_SECTION_KIND: dict[str, str] = {
    'deposits/credits to wealthfront brokerage': 'credit',
    'withdrawals/debits from wealthfront brokerage': 'debit',
    'transfer between wealthfront and program banks': 'skip',
    'interest': 'credit',
    'miscellaneous credits': 'credit',
    'balance and interest rate details': 'skip',
    'disclosures': 'skip',
}


def _match_dated_row(line: str) -> tuple | None:
    """
    Match a "<M/D/YYYY> <description> <-$amount|$amount>" line (shared by
    both Wealthfront cash-account layouts) and return (date, description,
    signed_amount), or None if the line doesn't match or its parts don't
    parse.
    """
    from datetime import date

    m = _WF_CASH_ROW_RE.match(line)
    if not m:
        return None
    date_str, description, amount_str = m.groups()
    try:
        mm, dd, yyyy = date_str.split('/')
        txn_date = date(int(yyyy), int(mm), int(dd))
    except ValueError:
        return None
    amount = _parse_pdf_number(amount_str)
    if amount is None:
        return None
    return txn_date, description.strip(), amount


def _parse_wf_cash_transactions(full_text: str) -> list[ParsedTransaction]:
    transactions = []
    kind = None
    for raw_line in full_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Section headings carry a trailing footnote digit (e.g. "INTEREST4").
        heading_key = re.sub(r'\d+$', '', line).strip().lower()
        if heading_key in _WF_CASH_SECTION_KIND:
            kind = _WF_CASH_SECTION_KIND[heading_key]
            continue
        if kind in (None, 'skip'):
            continue
        if line.lower().startswith('total') or line.lower().startswith('date '):
            continue
        matched = _match_dated_row(line)
        if matched is None:
            continue
        txn_date, description, amount = matched
        transactions.append(ParsedTransaction(date=txn_date, description=description, amount=abs(amount), direction=kind))
    return transactions


def _parse_wealthfront_cash_pdf(pdf, full_text: str) -> ParsedImport:
    result = ParsedImport(institution='wealthfront', source_format='pdf')
    result.as_of_date = _extract_statement_period(full_text)

    acct_m = _WEALTHFRONT_ACCOUNT_NUMBER_RE.search(full_text)
    last4 = acct_m.group(1)[-4:] if acct_m else None
    lower = full_text.lower()
    if 'joint' in lower:
        account_name = 'Wealthfront Joint Cash Account'
    else:
        account_name = 'Wealthfront Cash Account'

    bal_m = _WEALTHFRONT_TOTAL_RE.search(full_text) or _WEALTHFRONT_BALANCE_RE.search(full_text)
    balance = float(bal_m.group(1).replace(',', '')) if bal_m else 0.0

    account = ParsedAccount(
        key=_account_key('wealthfront', account_name, last4),
        institution='wealthfront', account_name=account_name,
        account_number_last4=last4,
        cash_total=balance,
        computed_total=balance,
        reported_total=balance,
        balance_only=True,
    )
    try:
        account.transactions = _parse_wf_cash_transactions(full_text)
    except Exception as e:
        logger.warning('Wealthfront Cash PDF: transaction parsing failed: %s', e)
        result.warnings.append('Could not extract transaction detail from this statement — only the balance was imported.')
    result.accounts.append(account)
    return result


# ---------------------------------------------------------------------------
# Wealthfront's Green Dot Bank companion (debit-card) account
#
# NOTE: calibrated against a real statement with zero transactions in its
# TRANSACTIONS section — institution sniffing, balance extraction, and the
# SWEEP TRANSACTIONS exclusion are verified, but the per-row parsing regex
# below is a best-effort guess (same "M/D/YYYY  description  $amount" shape
# used elsewhere in the Wealthfront statement family) that should be
# re-verified against a real statement that actually has transactions.
# ---------------------------------------------------------------------------

def _parse_greendot_transactions(full_text: str) -> list[ParsedTransaction]:
    transactions = []
    in_transactions = False
    for raw_line in full_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lowered = line.lower()
        if lowered.startswith('sweep transactions'):
            in_transactions = False  # internal transfer with the Wealthfront Cash Account — skip
            continue
        if lowered.startswith('transactions'):
            in_transactions = True
            continue
        if not in_transactions or lowered in ('date description amount', 'no transactions'):
            continue
        matched = _match_dated_row(line)
        if matched is None:
            continue
        txn_date, description, amount = matched
        transactions.append(ParsedTransaction(
            date=txn_date, description=description, amount=abs(amount),
            direction='debit' if amount < 0 else 'credit',
        ))
    return transactions


def _parse_wealthfront_greendot_pdf(pdf, full_text: str) -> ParsedImport:
    result = ParsedImport(institution='wealthfront', source_format='pdf')
    result.as_of_date = _extract_statement_period(full_text)

    acct_m = _GREENDOT_ACCOUNT_NUMBER_RE.search(full_text)
    last4 = re.sub(r'\D', '', acct_m.group(1))[-4:] if acct_m else None

    bal_m = _GREENDOT_ENDING_BALANCE_RE.search(full_text)
    balance = float(bal_m.group(1).replace(',', '')) if bal_m else 0.0

    account = ParsedAccount(
        key=_account_key('wealthfront', 'Wealthfront Cash Account (Green Dot)', last4),
        institution='wealthfront', account_name='Wealthfront Cash Account (Green Dot)',
        account_number_last4=last4,
        cash_total=balance,
        computed_total=balance,
        reported_total=balance,
        balance_only=True,
    )
    try:
        account.transactions = _parse_greendot_transactions(full_text)
    except Exception as e:
        logger.warning('Wealthfront Green Dot PDF: transaction parsing failed: %s', e)
        result.warnings.append('Could not extract transaction detail from this statement — only the balance was imported.')
    result.accounts.append(account)
    return result


def _parse_wealthfront_pdf(pdf) -> ParsedImport:
    full_text = '\n'.join(page.extract_text() or '' for page in pdf.pages)
    lower = full_text.lower()
    if 'green dot' in lower:
        return _parse_wealthfront_greendot_pdf(pdf, full_text)
    if 'investment account' in lower or 'etfs' in lower or 'etf' in lower:
        return _parse_wealthfront_investment_pdf(pdf, full_text)
    return _parse_wealthfront_cash_pdf(pdf, full_text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_positions_pdf(file_bytes: bytes) -> ParsedImport:
    """
    Parse a Fidelity/Schwab statement PDF into a ParsedImport.
    Never raises — returns ParsedImport with errors populated on failure.
    """
    try:
        pdf = _open_pdf(file_bytes)
    except Exception as e:
        result = ParsedImport(institution='unknown', source_format='pdf')
        result.errors.append(f'Could not read this PDF file: {e}')
        return result

    try:
        with pdf:
            first_page_text = pdf.pages[0].extract_text() or '' if pdf.pages else ''
            institution = sniff_institution(first_page_text) or sniff_institution(
                '\n'.join((p.extract_text() or '') for p in pdf.pages[:2])
            )
            if institution == 'schwab':
                return _parse_schwab_pdf(pdf)
            if institution == 'schwab_bank':
                return _parse_schwab_bank_pdf(pdf)
            if institution == 'fidelity':
                return _parse_fidelity_pdf(pdf)
            if institution == 'wealthfront':
                return _parse_wealthfront_pdf(pdf)

            result = ParsedImport(institution='unknown', source_format='pdf')
            result.warnings.append(
                'Could not confirm institution — statement layout not recognized.'
            )
            return result
    except Exception as e:
        logger.warning('PDF parsing failed: %s', e)
        result = ParsedImport(institution='unknown', source_format='pdf')
        result.errors.append(f'Could not parse this PDF: {e}')
        return result
