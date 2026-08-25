"""
Unit tests for app/brokerage_import/pdf_parsing.py.

Fixtures are synthetic PDFs built with reportlab, positioned to match the
verified real Schwab/Fidelity statement layouts documented in
app/brokerage_import/column_map.py (section headings, column x-ranges,
ticker-in-parens for Fidelity). Names/account numbers/dollar figures below
are entirely fabricated — never the data from any real statement.
"""
import io

import pytest
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter

from app.brokerage_import.pdf_parsing import (
    parse_positions_pdf, sniff_institution, _parse_pdf_number, _cluster_lines,
    _reconstruct_rows, _rows_to_positions, _extract_statement_period, _find_pdf_start,
    _discover_fidelity_accounts,
)
from app.brokerage_import.column_map import PDF_COLUMN_BANDS, SECTION_FINAL_MARKER


PAGE_HEIGHT = letter[1]


def _draw_page(c, lines, y_start=740, line_height=12):
    """lines: list of lists of (x, text) tuples, one sub-list per visual line."""
    y = y_start
    for line in lines:
        for x, text in line:
            c.drawString(x, y, text)
        y -= line_height


def _build_pdf(pages: list[list[list[tuple]]]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    for page_lines in pages:
        _draw_page(c, page_lines)
        c.showPage()
    c.save()
    return buf.getvalue()


def _schwab_statement_bytes():
    page1 = [
        [(78, 'Schwab One Account of')],
        [(336, 'Account Number'), (408, 'Statement Period')],
        [(336, '9999-1234'), (408, 'June 1-30, 2026')],
        [(78, 'TEST HOLDER NAME')],
    ]
    page2 = [
        [(78, 'Schwab One Account of')],
        [(336, 'Account Number'), (408, 'Statement Period')],
        [(336, '****-*1234'), (408, 'June 1-30, 2026')],
        [(18, 'Positions - Summary')],
        [(36, 'Beginning Value'), (494, 'Ending Value')],
        [(36, '$10,000.00'), (494, '$10,500.00')],
        [(18, 'Cash and Cash Investments')],
        [(18, 'Type'), (82, 'Symbol'), (129, 'Description'), (295, 'Quantity'), (343, 'Price($)')],
        [(18, 'Money Fund'), (82, 'SWVXX'), (129, 'SCHWAB MONEY FUND'),
         (273, '500.0000'), (343, '1.0000'), (494, '500.00')],
        [(23, 'Total Cash and Cash Investments'), (490, '$500.00')],
        [(18, 'Positions - Mutual Funds')],
        [(18, 'Symbol'), (68, 'Description'), (286, 'Quantity'), (368, 'Price($)')],
        [(18, 'VTSAX'), (68, 'VANGUARD TOTAL STOCK'), (269, '10.0000'), (362, '1,000.00'), (465, '10,000.00')],
        [(23, 'Total Mutual Funds'), (455, '$10,000.00')],
    ]
    return _build_pdf([page1, page2])


def _fidelity_statement_bytes():
    page1 = [
        [(400, 'INVESTMENT REPORT')],
        [(400, 'June 1, 2026 - June 30, 2026')],
        [(31, 'FIDELITY')],
    ]
    page2 = [
        [(400, 'INVESTMENT REPORT')],
        [(28, 'Accounts Included in This Report')],
        [(628, 'Account'), (18, 'Page')],
        [(18, '4'), (30, 'FIDELITY ACCOUNT TEST HOLDER - INDIVIDUAL'), (628, 'X12-345678'),
         (700, '$10,000.00'), (800, '$10,500.62')],
    ]
    page4 = [
        [(628, 'Account #'), (760, 'X12-345678')],
        [(31, 'Holdings')],
        [(31, 'Core Account')],
        [(226, 'Beginning'), (389, 'Price'), (460, 'Ending'), (606, 'Unrealized')],
        [(215, 'Market'), (304, 'Quantity'), (378, 'Per'), (438, 'Market'), (696, 'EAI')],
        [(31, 'Description'), (222, 'Jun'), (238, '1,'), (247, '2026'), (289, 'Jun'), (306, '30,'), (319, '2026')],
        [(31, 'FIDELITY GOVERNMENT MONEY'), (244, '$0.53'), (316, '0.620'), (380, '$1.0000'),
         (467, '$0.62'), (705, '$0.02')],
        [(31, 'MARKET (SPAXX)'), (698, '3.230%')],
        [(31, 'Total Core Account (0% of account'), (244, '$0.53'), (467, '$0.62'), (705, '$0.02')],
        [(31, 'holdings)')],
        [(31, 'Mutual Funds')],
        [(226, 'Beginning'), (389, 'Price'), (460, 'Ending'), (606, 'Unrealized')],
        [(215, 'Market'), (304, 'Quantity'), (378, 'Per'), (438, 'Market'), (696, 'EAI')],
        [(31, 'Description'), (222, 'Jun'), (238, '1,'), (247, '2026'), (289, 'Jun'), (306, '30,'), (319, '2026')],
        [(31, 'Stock Funds')],
        [(31, 'FIDELITY TOTAL MARKET INDEX FUND'), (221, '$9,900.00'), (301, '10.000'),
         (371, '$1,000.0000'), (444, '$10,000.00'), (690, '$20.00')],
        [(31, '(FSKAX)'), (698, '0.940%')],
        [(31, 'Total Stock Funds (100% of account'), (221, '$9,900.00'), (444, '$10,000.00'), (690, '$20.00')],
        [(31, 'holdings)')],
        [(31, 'Total Mutual Funds (100% of account'), (221, '$9,900.00'), (444, '$10,000.00'), (690, '$20.00')],
        [(31, 'holdings)')],
        [(31, 'Total Holdings'), (444, '$10,000.62'), (690, '$20.00')],
    ]
    return _build_pdf([page1, page2, page4])


class TestParsePdfNumber:
    def test_plain_dollar(self):
        assert _parse_pdf_number('$1,234.56') == 1234.56

    def test_negative_parens(self):
        assert _parse_pdf_number('($19.84)') == -19.84

    def test_percent(self):
        assert _parse_pdf_number('3.29%') == pytest.approx(3.29)

    def test_placeholder_text_returns_none(self):
        assert _parse_pdf_number('not') is None
        assert _parse_pdf_number('applicable') is None

    def test_empty_returns_none(self):
        assert _parse_pdf_number('') is None
        assert _parse_pdf_number('--') is None


class TestSniffInstitution:
    def test_schwab(self):
        assert sniff_institution('Charles Schwab statement') == 'schwab'

    def test_fidelity(self):
        assert sniff_institution('Fidelity Investments report') == 'fidelity'

    def test_schwab_bank_wins_over_schwab(self):
        # "Schwab Bank Investor Checking" contains "schwab" — bank marker must come first
        assert sniff_institution('Schwab Bank Investor Checking statement') == 'schwab_bank'

    def test_wealthfront(self):
        assert sniff_institution('Wealthfront Investment Account') == 'wealthfront'

    def test_unknown(self):
        assert sniff_institution('Some other broker') == 'unknown'


class TestFindPdfStart:
    def test_strips_leading_garbage(self):
        raw = b'\xff\xfe%PDF-1.4\n...'
        assert _find_pdf_start(raw) == b'%PDF-1.4\n...'

    def test_no_change_when_already_valid(self):
        raw = b'%PDF-1.4\n...'
        assert _find_pdf_start(raw) == raw


class TestReconstructRowsFidelity:
    """Exercises the state machine directly with fabricated word dicts —
    the same shape pdfplumber.extract_words() returns."""

    def _words(self, tuples, top):
        return [{'text': text, 'x0': x, 'top': top} for x, text in tuples]

    def test_ticker_continuation_merges_into_open_row_not_a_new_one(self):
        lines = [
            self._words([(31, 'FIDELITY'), (31 + 1, 'TOTAL FUND'), (221, '9900.00'),
                         (301, '10.000'), (371, '1000.00'), (444, '10000.00'), (690, '20.00')], top=100),
            self._words([(31, '(FSKAX)'), (698, '0.940%')], top=110),
            self._words([(31, 'Total Stock Funds'), (444, '10000.00')], top=120),
        ]
        bands = PDF_COLUMN_BANDS['fidelity']['mutual_funds']
        rows = _reconstruct_rows(lines, bands, 'total stock funds', [], has_symbol_band=False)
        assert len(rows) == 1
        assert rows[0].ticker_from_desc == 'FSKAX'
        assert rows[0].numeric['quantity'] == 10.0

    def test_stops_at_final_marker_ignores_trailing_content(self):
        lines = [
            self._words([(31, 'FUND A'), (301, '10.000'), (371, '100.00'), (444, '1000.00')], top=100),
            self._words([(31, 'Total Mutual Funds'), (444, '1000.00')], top=110),
            self._words([(31, 'UNRELATED TRAILING TABLE'), (301, '99.000'), (371, '5.00'), (444, '495.00')], top=120),
        ]
        bands = PDF_COLUMN_BANDS['fidelity']['mutual_funds']
        rows = _reconstruct_rows(lines, bands, 'total mutual funds', ['total stock funds', 'total bond funds'],
                                  has_symbol_band=False)
        assert len(rows) == 1  # trailing table never reached

    def test_subsection_heading_lines_skipped_not_captured_as_description(self):
        lines = [
            self._words([(31, 'Stock Funds')], top=90),
            self._words([(31, 'FUND A'), (301, '10.000'), (371, '100.00'), (444, '1000.00')], top=100),
            self._words([(31, 'Total Stock Funds'), (444, '1000.00')], top=110),
        ]
        bands = PDF_COLUMN_BANDS['fidelity']['mutual_funds']
        rows = _reconstruct_rows(lines, bands, 'total mutual funds', ['total stock funds'],
                                  has_symbol_band=False, skip_headings=['Stock Funds'])
        assert len(rows) == 1
        assert 'Stock Funds' not in ' '.join(rows[0].description_words)

    def test_header_row_with_date_fragments_not_misparsed_as_data(self):
        # "Jun 30," at x=306 falls inside the quantity band and would look
        # like a quantity if the header-row guard didn't skip it.
        header = self._words([(31, 'Description'), (289, 'Jun'), (306, '30,'), (319, '2026')], top=80)
        data = self._words([(31, 'FUND A'), (301, '10.000'), (371, '100.00'), (444, '1000.00')], top=100)
        total = self._words([(31, 'Total Mutual Funds'), (444, '1000.00')], top=110)
        bands = PDF_COLUMN_BANDS['fidelity']['mutual_funds']
        rows = _reconstruct_rows([header, data, total], bands, 'total mutual funds', [], has_symbol_band=False)
        assert len(rows) == 1
        assert rows[0].numeric['quantity'] == 10.0


class TestRowsToPositions:
    def test_bank_sweep_style_row_without_ticker_becomes_cash(self):
        from app.brokerage_import.pdf_parsing import _OpenRow
        row = _OpenRow()
        row.numeric['value'] = 1.74
        positions = _rows_to_positions([row])
        assert len(positions) == 1
        assert positions[0].ticker == 'CASH'
        assert positions[0].is_cash is True


class TestExtractStatementPeriod:
    def test_parses_month_range(self):
        d = _extract_statement_period('Statement Period June 1-30, 2026')
        assert d.year == 2026 and d.month == 6 and d.day == 30

    def test_no_match_returns_none(self):
        assert _extract_statement_period('no dates here') is None


class TestParsePositionsPdfEndToEnd:
    def test_success_schwab(self):
        result = parse_positions_pdf(_schwab_statement_bytes())
        assert result.institution == 'schwab'
        assert not result.errors
        assert len(result.accounts) == 1
        account = result.accounts[0]
        assert account.account_number_last4 == '1234'
        tickers = {p.ticker for p in account.positions}
        assert tickers == {'VTSAX'}
        assert account.cash_total == pytest.approx(500.0)
        assert account.reported_total == pytest.approx(10500.0)

    def test_success_fidelity(self):
        result = parse_positions_pdf(_fidelity_statement_bytes())
        assert result.institution == 'fidelity'
        assert not result.errors
        assert len(result.accounts) == 1
        account = result.accounts[0]
        assert account.account_number_last4 == '5678'
        tickers = {p.ticker for p in account.positions}
        assert tickers == {'FSKAX'}
        assert account.cash_total == pytest.approx(0.62)
        assert account.reported_total == pytest.approx(10000.62)

    def test_empty_no_recognized_sections(self):
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=letter)
        c.drawString(100, 700, 'Fidelity statement with no recognizable tables')
        c.showPage()
        c.save()
        result = parse_positions_pdf(buf.getvalue())
        assert result.institution == 'fidelity'
        assert not any(a.positions for a in result.accounts)

    def test_invalid_bytes_no_exception(self):
        result = parse_positions_pdf(b'not a real pdf at all')
        assert result.errors
        assert result.accounts == []

    def test_garbage_prefix_before_pdf_header_still_parses(self):
        raw = _schwab_statement_bytes()
        prefixed = b'\x00\x01\x02' + raw
        result = parse_positions_pdf(prefixed)
        assert result.institution == 'schwab'
        assert not result.errors


def _schwab_bank_statement_bytes():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(50, 750, 'Schwab Bank Investor Checking')
    c.drawString(50, 735, 'Statement Period June 1-30, 2026')
    c.drawString(50, 720, 'Account Number 440054642648')
    c.drawString(50, 705, 'Ending Balance $27,607.79')
    c.showPage()
    c.save()
    return buf.getvalue()


def _wealthfront_investment_bytes():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(50, 750, 'Wealthfront')
    c.drawString(50, 735, 'Individual Investment Account')
    c.drawString(50, 720, 'Account 8W597901')
    c.drawString(50, 705, 'Statement Period June 1-30, 2026')
    c.drawString(50, 690, 'Total Account Value $2,451.23')
    # position line: description ticker qty $price(4dp) $value(2dp)
    c.drawString(50, 675, 'Vanguard ETF VTI 10.0000 $245.1234 $2,451.23')
    c.showPage()
    c.save()
    return buf.getvalue()


def _wealthfront_cash_bytes():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.drawString(50, 750, 'Wealthfront')
    c.drawString(50, 735, 'Joint Cash Account')
    c.drawString(50, 720, 'Account 8W159VG4')
    c.drawString(50, 705, 'Statement Period June 1-30, 2026')
    c.drawString(50, 690, 'Total Balance $142,323.00')
    c.showPage()
    c.save()
    return buf.getvalue()


def _build_pdf_small_font(pages: list[list[list[tuple]]]) -> bytes:
    """Like _build_pdf but at 7pt (matches the real statement's tight column
    spacing — reportlab's 12pt default would overlap adjacent columns placed
    only ~40-50pt apart, as used for the Activity table below)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont('Helvetica', 7)
    for page_lines in pages:
        _draw_page(c, page_lines, line_height=10)
        c.showPage()
        c.setFont('Helvetica', 7)
    c.save()
    return buf.getvalue()


def _schwab_bank_activity_bytes():
    """
    Activity table laid out with the real column x-positions verified in
    PDF_TRANSACTION_COLUMN_BANDS['schwab_bank'] (date~40, description~82,
    debits~448-462, credits~578-598, balance~702). All names/amounts here
    are fabricated.
    """
    page1 = [
        [(50, 'Schwab Bank Investor Checking')],
        [(50, 'Statement Period July 1-31, 2026')],
        [(50, 'Account Number 440054642648')],
        [(50, 'Ending Balance $27,607.79')],
    ]
    page2 = [
        [(40, 'Activity')],
        [(40, 'Date')],
        [(40, 'Posted'), (82, 'Description'), (462, 'Debits'), (588, 'Credits'), (714, 'Balance')],
        [(40, '07/01'), (82, 'Beginning'), (129, 'Balance'), (702, '$23,970.37')],
        [(40, '07/02'), (82, 'Electronic'), (128, 'Deposit'), (578, '$6,435.56'), (702, '$30,405.93')],
        [(82, 'TEST'), (112, 'PAYROLL'), (161, '260702')],
        [(40, '07/06'), (82, 'Electronic'), (128, 'Withdrawal'), (448, '$2,183.30'), (702, '$27,722.63')],
        [(82, 'CHASE'), (119, 'CREDIT'), (159, 'CRD'), (183, 'EPAY')],
        [(40, '07/16'), (82, 'Electronic'), (128, 'Withdrawal'), (456, '$500.00'), (702, '$24,591.67')],
        [(82, 'WEALTHFRONT'), (160, 'EDI'), (180, 'PYMNTS')],
        [(40, '07/16'), (82, 'Check'), (456, '$320.00'), (702, '$24,271.67')],
        [(82, 'Check'), (113, 'Paid'), (136, '#187')],
        [(40, '07/31'), (82, 'Interest'), (118, 'Paid'), (598, '$0.23'), (702, '$27,607.79')],
        [(40, '07/31'), (82, 'Ending'), (118, 'Balance'), (702, '$27,607.79')],
        [(40, 'Checks Paid')],
        [(45, '187'), (160, '07/16/2026'), (356, '$320.00')],
    ]
    return _build_pdf_small_font([page1, page2])


def _schwab_bank_activity_page_break_bytes():
    """
    Same shape as _schwab_bank_activity_bytes but with a row still open
    right at a page break, followed by the repeated page letterhead
    boilerplate (copyright line, account-holder name, "(continued)"
    headings) before the next real row — regression fixture for the
    boilerplate-skip guard in _reconstruct_transaction_rows.
    """
    page1 = [
        [(50, 'Schwab Bank Investor Checking')],
        [(50, 'Statement Period July 1-31, 2026')],
        [(50, 'Account Number 440054642648')],
        [(50, 'Ending Balance $27,607.79')],
    ]
    page2 = [
        [(40, 'Activity')],
        [(40, 'Date')],
        [(40, 'Posted'), (82, 'Description'), (462, 'Debits'), (588, 'Credits'), (714, 'Balance')],
        [(40, '07/06'), (82, 'Electronic'), (128, 'Withdrawal'), (448, '$2,468.46'), (702, '$25,254.17')],
        [(82, 'AMEX'), (113, 'EPAYMENT'), (171, 'ACH'), (194, 'PMT')],
        # Page footer, appearing right after the last row on this page —
        # matches the real statement's layout (footer at the bottom of the
        # PRECEDING page, before the next page's own letterhead starts).
        [(703, 'Page'), (729, '2'), (738, 'of'), (750, '6')],
        [(108, '©'), (115, '2026'), (133, 'Test'), (159, 'Bank,'), (186, 'All'), (233, 'rights'), (252, 'reserved.')],
    ]
    page3 = [
        [(482, 'Account'), (517, 'Number'), (623, 'Statement'), (664, 'Period')],
        [(202, 'TEST'), (244, 'HOLDER'), (290, 'NAME')],
        [(40, 'Schwab'), (88, 'Bank'), (121, 'Investor')],
        [(239, '(continued)')],
        [(40, 'Activity'), (78, '(continued)')],
        [(40, 'Date')],
        [(40, 'Posted'), (82, 'Description'), (462, 'Debits'), (588, 'Credits'), (714, 'Balance')],
        [(40, '07/13'), (82, 'Electronic'), (128, 'Withdrawal'), (456, '$162.50'), (702, '$25,091.67')],
        [(82, 'TEST'), (143, 'VENDOR')],
    ]
    return _build_pdf_small_font([page1, page2, page3])


def _wealthfront_cash_activity_bytes():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    lines = [
        'Wealthfront',
        'Joint Cash Account',
        'Account 8W159VG4',
        'Statement Period July 1-31, 2026',
        'Ending Balance $140,166.35',
        'Deposits/Credits to Wealthfront Brokerage',
        'Date Method Status Amount',
        '7/2/2026 ACH Received $3,200.00',
        'Total $3,200.00',
        'Withdrawals/Debits from Wealthfront Brokerage',
        'Date Method Status Initiator Amount',
        '7/3/2026 ACH Direct Withdrawal Disbursed SEATTLEUTILTIES -$166.22',
        'Total -$166.22',
        'Transfer between Wealthfront and Program Banks',
        'Date Method Amount',
        '7/2/2026 Transfer to Program Banks -$3,200.00',
        'Total -$3,200.00',
        'INTEREST',
        'Date Interest Period Amount',
        '7/1/2026 June 2026 $10.00',
        'Total $10.00',
    ]
    y = 750
    for line in lines:
        c.drawString(50, y, line)
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


def _greendot_activity_bytes():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    lines = [
        'Test Holder',
        'STATEMENT PERIOD Jul. 14, 2026 to Aug. 13, 2026',
        'ACCOUNT NUMBER 1115-4166-6112-63',
        'ACCOUNT SUMMARY',
        'Beginning Balance on Jul. 14, 2026 $1,000.00',
        'Credits + $50.00',
        'Debits - $20.00',
        'Ending Balance on Aug. 13, 2026 $1,030.00',
        'TRANSACTIONS',
        'DATE DESCRIPTION AMOUNT',
        '7/20/2026 Refund from Merchant $50.00',
        '7/22/2026 Debit Card Purchase -$20.00',
        'SWEEP TRANSACTIONS',
        'DATE DESCRIPTION AMOUNT',
        '7/20/2026 Sweep to Wealthfront Cash -$50.00',
        '1. Statement for banking services provided by Green Dot Bank, Member FDIC.',
    ]
    y = 750
    for line in lines:
        c.drawString(50, y, line)
        y -= 14
    c.showPage()
    c.save()
    return buf.getvalue()


class TestParseSchwabBankTransactions:
    def test_extracts_transactions_skips_balance_rows(self):
        result = parse_positions_pdf(_schwab_bank_activity_bytes())
        acct = result.accounts[0]
        assert len(acct.transactions) == 5  # excludes Beginning/Ending Balance and Checks Paid summary

    def test_transaction_directions_and_amounts(self):
        result = parse_positions_pdf(_schwab_bank_activity_bytes())
        by_desc = {t.description: t for t in result.accounts[0].transactions}
        payroll = next(t for t in result.accounts[0].transactions if 'PAYROLL' in t.description)
        assert payroll.direction == 'credit'
        assert payroll.amount == pytest.approx(6435.56)
        chase = next(t for t in result.accounts[0].transactions if 'CHASE' in t.description)
        assert chase.direction == 'debit'
        assert chase.amount == pytest.approx(2183.30)
        interest = next(t for t in result.accounts[0].transactions if t.description == 'Interest Paid')
        assert interest.direction == 'credit'
        assert interest.amount == pytest.approx(0.23)

    def test_transaction_dates_use_statement_year(self):
        result = parse_positions_pdf(_schwab_bank_activity_bytes())
        payroll = next(t for t in result.accounts[0].transactions if 'PAYROLL' in t.description)
        assert payroll.date.isoformat() == '2026-07-02'

    def test_checks_paid_summary_not_double_counted(self):
        result = parse_positions_pdf(_schwab_bank_activity_bytes())
        check_txns = [t for t in result.accounts[0].transactions if 'Check' in t.description]
        assert len(check_txns) == 1  # only from Activity, not the Checks Paid summary table

    def test_page_break_boilerplate_not_merged_into_description(self):
        result = parse_positions_pdf(_schwab_bank_activity_page_break_bytes())
        acct = result.accounts[0]
        assert len(acct.transactions) == 2
        amex = next(t for t in acct.transactions if 'AMEX' in t.description)
        assert 'reserved' not in amex.description.lower()
        assert 'TEST HOLDER NAME' not in amex.description
        assert '(continued)' not in amex.description
        vendor = next(t for t in acct.transactions if 'VENDOR' in t.description)
        assert vendor.amount == pytest.approx(162.50)


class TestParseWealthfrontCashTransactions:
    def test_extracts_deposits_withdrawals_and_interest(self):
        result = parse_positions_pdf(_wealthfront_cash_activity_bytes())
        acct = result.accounts[0]
        assert len(acct.transactions) == 3

    def test_program_bank_transfer_section_excluded(self):
        result = parse_positions_pdf(_wealthfront_cash_activity_bytes())
        acct = result.accounts[0]
        assert not any(t.amount == pytest.approx(3200.0) and t.direction == 'debit' for t in acct.transactions)

    def test_directions_and_descriptions(self):
        result = parse_positions_pdf(_wealthfront_cash_activity_bytes())
        acct = result.accounts[0]
        deposit = next(t for t in acct.transactions if t.direction == 'credit' and t.amount == pytest.approx(3200.0))
        assert deposit.description == 'ACH Received'
        withdrawal = next(t for t in acct.transactions if t.direction == 'debit')
        assert 'SEATTLEUTILTIES' in withdrawal.description
        assert withdrawal.amount == pytest.approx(166.22)
        interest = next(t for t in acct.transactions if t.amount == pytest.approx(10.0))
        assert interest.direction == 'credit'


class TestParseWealthfrontGreenDot:
    def test_sniffed_as_wealthfront_balance_only(self):
        result = parse_positions_pdf(_greendot_activity_bytes())
        assert result.institution == 'wealthfront'
        acct = result.accounts[0]
        assert acct.balance_only is True
        assert acct.cash_total == pytest.approx(1030.0)

    def test_extracts_transactions_excludes_sweep(self):
        result = parse_positions_pdf(_greendot_activity_bytes())
        acct = result.accounts[0]
        assert len(acct.transactions) == 2
        refund = next(t for t in acct.transactions if t.direction == 'credit')
        assert refund.amount == pytest.approx(50.0)
        purchase = next(t for t in acct.transactions if t.direction == 'debit')
        assert purchase.amount == pytest.approx(20.0)


class TestParseSchwaBank:
    def test_institution_and_balance_only(self):
        result = parse_positions_pdf(_schwab_bank_statement_bytes())
        assert result.institution == 'schwab_bank'
        assert not result.errors
        assert len(result.accounts) == 1
        acct = result.accounts[0]
        assert acct.balance_only is True
        assert acct.positions == []
        assert acct.cash_total == pytest.approx(27607.79)
        assert acct.computed_total == pytest.approx(27607.79)

    def test_account_number_last4_extracted(self):
        result = parse_positions_pdf(_schwab_bank_statement_bytes())
        assert result.accounts[0].account_number_last4 == '2648'

    def test_statement_period_parsed(self):
        result = parse_positions_pdf(_schwab_bank_statement_bytes())
        assert result.as_of_date is not None
        assert result.as_of_date.month == 6


class TestParseWealthfrontInvestment:
    def test_institution_and_positions(self):
        result = parse_positions_pdf(_wealthfront_investment_bytes())
        assert result.institution == 'wealthfront'
        assert not result.errors
        assert len(result.accounts) == 1
        acct = result.accounts[0]
        assert acct.balance_only is False
        tickers = {p.ticker for p in acct.positions}
        assert 'VTI' in tickers

    def test_position_values_parsed(self):
        result = parse_positions_pdf(_wealthfront_investment_bytes())
        vti = next(p for p in result.accounts[0].positions if p.ticker == 'VTI')
        assert vti.quantity == pytest.approx(10.0)
        assert vti.price == pytest.approx(245.1234)
        assert vti.value == pytest.approx(2451.23)

    def test_reported_total_extracted(self):
        result = parse_positions_pdf(_wealthfront_investment_bytes())
        assert result.accounts[0].reported_total == pytest.approx(2451.23)

    def test_account_number_last4(self):
        result = parse_positions_pdf(_wealthfront_investment_bytes())
        assert result.accounts[0].account_number_last4 == '7901'


class TestDiscoverFidelityAccounts:
    """Unit tests for _discover_fidelity_accounts with mock PDF objects."""

    class _MockPage:
        def __init__(self, text):
            self._text = text

        def extract_text(self):
            return self._text

    class _MockPDF:
        def __init__(self, pages):
            self.pages = pages

    def _pdf(self, *page_texts):
        return self._MockPDF([self._MockPage(t) for t in page_texts])

    def test_single_account_inline_name(self):
        # Name and account number on same line (typical single-account statement).
        pdf = self._pdf(
            'Accounts Included in This Report\n'
            '4 FIDELITY ACCOUNT (INDIVIDUAL TOD) X83-655756 6000.00 6100.00\n'
        )
        accounts = _discover_fidelity_accounts(pdf)
        assert len(accounts) == 1
        assert accounts[0]['account_number'] == 'X83-655756'
        assert 'FIDELITY' in accounts[0]['account_name']

    def test_multi_account_name_on_preceding_line(self):
        # Multi-account statement where account name is on the line BEFORE
        # the page-number + account-number line (the real-world 16-page case).
        pdf = self._pdf(
            'Statement Page 1\n',
            'Accounts Included in This Report\n'
            'FIDELITY ACCOUNT (INDIVIDUAL TOD)\n'
            '4 X83-655756 6000.00 6100.00\n'
            'NH COLLEGE PORTFOLIO (529)\n'
            '9 603-977090 6200.84 6176.24\n'
            'UTMA CUSTODIAL FOR MINOR\n'
            '11 Z54-190313 5033.46 5006.31\n',
        )
        accounts = _discover_fidelity_accounts(pdf)
        assert len(accounts) == 3
        assert accounts[0]['account_number'] == 'X83-655756'
        assert 'FIDELITY' in accounts[0]['account_name']
        assert accounts[1]['account_number'] == '603-977090'
        assert 'NH COLLEGE' in accounts[1]['account_name']
        assert accounts[2]['account_number'] == 'Z54-190313'
        assert 'UTMA' in accounts[2]['account_name']

    def test_no_accounts_included_page_returns_empty(self):
        pdf = self._pdf('Just a regular page with no account table\n')
        assert _discover_fidelity_accounts(pdf) == []

    def test_page_number_only_prefix_stripped(self):
        # Bare page number before the account number (no name on same line),
        # with name on preceding line — should not create an account named "9".
        pdf = self._pdf(
            'Accounts Included in This Report\n'
            'MY FUND ACCOUNT\n'
            '9 603-977090 6200.84 6176.24\n',
        )
        accounts = _discover_fidelity_accounts(pdf)
        assert len(accounts) == 1
        assert accounts[0]['account_name'] != '9'
        assert 'MY FUND' in accounts[0]['account_name']


class TestParseWealthfrontCash:
    def test_institution_and_balance_only(self):
        result = parse_positions_pdf(_wealthfront_cash_bytes())
        assert result.institution == 'wealthfront'
        assert not result.errors
        assert len(result.accounts) == 1
        acct = result.accounts[0]
        assert acct.balance_only is True
        assert acct.positions == []
        assert acct.cash_total == pytest.approx(142323.00)

    def test_account_number_last4(self):
        result = parse_positions_pdf(_wealthfront_cash_bytes())
        # 8W159VG4[-4:] == '9VG4'
        assert result.accounts[0].account_number_last4 == '9VG4'
