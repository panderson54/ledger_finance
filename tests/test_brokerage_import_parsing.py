"""
Unit tests for app/brokerage_import/parsing.py (CSV positions export parser)
and app/brokerage_import/matching.py.

Fixtures are BEST-EFFORT — built from well-documented Fidelity/Schwab CSV
export conventions since no real export sample was available when written.
Verify against a real export and adjust app/brokerage_import/column_map.py
if real column headers differ.
"""
from app.brokerage_import.parsing import (
    sniff_institution, _locate_header_row, parse_positions_csv,
)
from app.brokerage_import.matching import suggest_matches
from app.brokerage_import.types import ParsedAccount


FIDELITY_CSV = """Account Name,Account Number,Symbol,Description,Quantity,Last Price,Current Value
Individual,X12345678,VTI,VANGUARD TOTAL STOCK MKT ETF,10.5,280.11,2941.16
Individual,X12345678,SPAXX,FIDELITY GOVERNMENT MONEY MARKET,150.0,1.00,150.00
"""

SCHWAB_CSV = """"Positions for account Brokerage as of 07/23/2026 10:00 AM ET"
Symbol,Description,Quantity,Price,Market Value
VTSAX,VANGUARD TOTAL STOCK MKT,100,180.18,18018.00
SWVXX,SCHWAB PRIME ADVANTAGE MONEY,500,1.00,500.00

Brokerage Products: Not FDIC Insured - No Bank Guarantee - May Lose Value
"""

GENERIC_UNKNOWN_CSV = """Symbol,Description,Quantity,Price,Market Value
XYZ,SOME FUND,10,50.00,500.00
"""


class TestSniffInstitution:
    def test_fidelity(self):
        assert sniff_institution('Fidelity Investments export') == 'fidelity'

    def test_schwab(self):
        assert sniff_institution('Charles Schwab & Co export') == 'schwab'

    def test_unknown(self):
        assert sniff_institution('Vanguard export') == 'unknown'

    def test_case_insensitive(self):
        assert sniff_institution('FIDELITY brokerage') == 'fidelity'


class TestLocateHeaderRow:
    def test_header_at_row_zero(self):
        lines = ['Symbol,Quantity,Price', 'VTI,10,280.11']
        assert _locate_header_row(lines) == 0

    def test_header_after_preamble(self):
        lines = ['"Positions as of 07/23/2026"', '', 'Symbol,Description,Quantity,Price', 'VTI,x,10,280']
        assert _locate_header_row(lines) == 2

    def test_no_header_found(self):
        lines = ['just some text', 'more text', 'no columns here']
        assert _locate_header_row(lines) is None


class TestParsePositionsCsv:
    def test_success_fidelity(self):
        result = parse_positions_csv(FIDELITY_CSV)
        assert result.institution == 'fidelity'
        assert not result.errors
        assert len(result.accounts) == 1
        account = result.accounts[0]
        assert account.account_number_last4 == '5678'
        tickers = {p.ticker for p in account.positions}
        assert tickers == {'VTI'}
        assert account.cash_total == 150.00  # SPAXX folded into cash
        assert round(account.computed_total, 2) == 3091.16

    def test_success_schwab_multiline_preamble_and_footer(self):
        result = parse_positions_csv(SCHWAB_CSV)
        assert result.institution == 'schwab'
        assert not result.errors
        assert len(result.accounts) == 1
        account = result.accounts[0]
        tickers = {p.ticker for p in account.positions}
        assert tickers == {'VTSAX'}
        assert account.cash_total == 500.00  # SWVXX folded into cash

    def test_empty_file(self):
        result = parse_positions_csv('')
        assert result.errors

    def test_no_data_rows(self):
        result = parse_positions_csv('Symbol,Quantity,Price\n')
        assert not result.errors
        assert result.warnings

    def test_invalid_garbage_input(self):
        result = parse_positions_csv('this is not a csv at all\njust prose text')
        assert result.errors
        assert result.accounts == []

    def test_unknown_institution_still_parses_with_warning(self):
        result = parse_positions_csv(GENERIC_UNKNOWN_CSV)
        assert result.institution == 'unknown'
        assert any('institution' in w.lower() for w in result.warnings)
        assert len(result.accounts) == 1
        assert result.accounts[0].positions[0].ticker == 'XYZ'

    def test_discrepancy_not_flagged_without_reported_total(self):
        # CSV format has no printed subtotal to cross-check against.
        result = parse_positions_csv(FIDELITY_CSV)
        assert result.accounts[0].reported_total is None


class TestSuggestMatches:
    def _parsed(self, institution='fidelity', last4='5678'):
        return ParsedAccount(
            key=f'{institution}:Individual:{last4}', institution=institution,
            account_name='Individual', account_number_last4=last4,
        )

    def test_exact_match(self):
        parsed = [self._parsed()]
        ledger = [{'id': 1, 'name': 'Fidelity Brokerage', 'institution': 'Fidelity', 'account_number': '5678'}]
        results = suggest_matches(parsed, ledger)
        assert results[0]['confidence'] == 'exact'
        assert results[0]['suggested_account_id'] == 1

    def test_institution_only_match(self):
        parsed = [self._parsed(last4='9999')]
        ledger = [{'id': 1, 'name': 'Fidelity Brokerage', 'institution': 'Fidelity', 'account_number': '5678'}]
        results = suggest_matches(parsed, ledger)
        assert results[0]['confidence'] == 'institution_only'
        assert results[0]['suggested_account_id'] == 1

    def test_no_match(self):
        parsed = [self._parsed(institution='fidelity')]
        ledger = [{'id': 1, 'name': 'Schwab Brokerage', 'institution': 'Schwab', 'account_number': '5678'}]
        results = suggest_matches(parsed, ledger)
        assert results[0]['confidence'] == 'none'
        assert results[0]['suggested_account_id'] is None

    def test_ambiguous_same_institution_no_account_numbers(self):
        parsed = [self._parsed(last4=None)]
        ledger = [
            {'id': 1, 'name': 'Fidelity A', 'institution': 'Fidelity', 'account_number': None},
            {'id': 2, 'name': 'Fidelity B', 'institution': 'Fidelity', 'account_number': None},
        ]
        results = suggest_matches(parsed, ledger)
        assert results[0]['confidence'] == 'none'
        assert len(results[0]['candidate_accounts']) == 2

    def test_schwab_bank_matches_schwab_ledger_account(self):
        # schwab_bank parsed accounts should find ledger accounts tagged 'schwab'
        parsed = [ParsedAccount(
            key='schwab_bank:Schwab Bank Investor Checking:2648',
            institution='schwab_bank', account_name='Schwab Bank Investor Checking',
            account_number_last4='2648',
        )]
        ledger = [{'id': 5, 'name': 'Schwab Checking', 'institution': 'schwab', 'account_number': '2648'}]
        results = suggest_matches(parsed, ledger)
        assert results[0]['confidence'] == 'exact'
        assert results[0]['suggested_account_id'] == 5

    def test_schwab_bank_institution_only_when_no_number_match(self):
        parsed = [ParsedAccount(
            key='schwab_bank:Schwab Bank Investor Checking:9999',
            institution='schwab_bank', account_name='Schwab Bank Investor Checking',
            account_number_last4='9999',
        )]
        ledger = [{'id': 5, 'name': 'Schwab Checking', 'institution': 'schwab', 'account_number': '2648'}]
        results = suggest_matches(parsed, ledger)
        assert results[0]['confidence'] == 'institution_only'
