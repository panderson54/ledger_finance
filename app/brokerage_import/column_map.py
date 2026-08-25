"""
Single place to adjust institution-specific field/column mappings for both the
CSV parser (parsing.py) and the PDF parser (pdf_parsing.py). If a real export
or statement turns out to differ from what's mapped here, this is the only
file that should need to change.
"""

# Substrings sniffed (case-insensitive) in file text to detect institution.
INSTITUTION_MARKERS: dict[str, list[str]] = {
    'fidelity': ['fidelity'],
    # schwab_bank must come before schwab — "Schwab Bank" text also contains "schwab"
    'schwab_bank': ['schwab bank investor checking', 'charles schwab bank, ssb'],
    'schwab': ['schwab', 'charles schwab'],
    'wealthfront': ['wealthfront'],
}

# --- CSV "Positions" export -------------------------------------------------
# BEST-EFFORT — no real Fidelity/Schwab CSV export sample was available when
# this was written; verify against a real export and adjust here.

# field -> ordered list of candidate header labels (matched case-insensitively,
# exact-cell match after strip())
FIELD_CANDIDATES: dict[str, list[str]] = {
    'account_name': ['account name', 'account'],
    'account_number': ['account number', 'account #', 'acct #', 'account num'],
    'symbol': ['symbol', 'ticker', 'ticker symbol'],
    'description': ['description', 'name', 'security description'],
    'quantity': ['quantity', 'qty (quantity)', 'qty'],
    'price': ['last price', 'price', 'quote price', 'price ($)', 'price($)'],
    'value': ['current value', 'market value', 'value', 'mkt val ($)', 'market value($)'],
}

# Minimum set of fields a row must have to be accepted as the CSV header row.
MANDATORY_FIELDS: tuple[str, ...] = ('symbol', 'quantity')

# Substrings identifying the start of trailing legal/disclaimer text — used to
# bound the tabular region of a CSV export.
FOOTER_MARKERS: list[str] = [
    'brokerage services are provided by',
    'is not affiliated with',
    'date downloaded',
    'the data and information',
    'for informational purposes',
]

# Money-market/sweep vehicles folded into cash_total instead of becoming
# Holding rows. SPAXX/FDRXX/FCASH/FZFXX are verified (Fidelity statement
# sample); SWVXX/SWGXX are verified (Schwab statement sample). CASH is a
# generic catch-all some exports use for uninvested cash rows.
CASH_TICKERS: set[str] = {'SPAXX', 'FDRXX', 'FCASH', 'FZFXX', 'SWVXX', 'SWGXX', 'CASH', 'TIMXX'}
CASH_DESCRIPTION_PATTERNS: list[str] = [
    r'money market', r'\bsweep\b', r'cash reserves', r'government money market',
]

# --- PDF statements ----------------------------------------------------------
# Verified against real Schwab and Fidelity statement PDFs (structure only;
# no account data reproduced). Section headings mark where a positions table
# starts on a page; PDF_COLUMN_BANDS give the approximate x-coordinate range
# (in PDF points) of each logical field within that section, derived from the
# verified samples with generous margins for digit-count/right-alignment
# drift. 'quantity' is the anchor field used to detect a new row.

PDF_SECTION_HEADINGS: dict[str, dict[str, str]] = {
    'schwab': {
        'cash': 'Cash and Cash Investments',
        'mutual_funds': 'Positions - Mutual Funds',
        'etfs': 'Positions - Exchange Traded Funds',
        # Unverified — no sample statement contained these account types.
        'stocks': 'Positions - Stocks',
        'bonds': 'Positions - Bonds',
        'options': 'Positions - Options',
    },
    'fidelity': {
        'core': 'Core Account',
        'mutual_funds': 'Mutual Funds',
        # Unverified — Fidelity's own endnote confirms this subsection exists
        # in some statement months even though not present in the sample.
        'etps': 'Exchange Traded Products',
        'stocks': 'Individual Stocks',
        'bonds': 'Individual Bonds',
    },
}

# Per-section closing marker: the row whose "Total ..." line ends that
# section entirely (row reconstruction stops here, discarding anything else
# on the page past this point — e.g. the next section, or unrelated tables
# further down the same page).
SECTION_FINAL_MARKER: dict[str, dict[str, str]] = {
    'schwab': {
        'cash': 'total cash and cash investments',
        'mutual_funds': 'total mutual funds',
        'etfs': 'total exchange traded funds',
        'stocks': 'total stocks',
        'bonds': 'total bonds',
        'options': 'total options',
    },
    'fidelity': {
        'core': 'total core account',
        'mutual_funds': 'total mutual funds',
    },
}

# Per-section "sub-total" markers that flush the current row but do NOT end
# the section (e.g. Fidelity's Mutual Funds section contains a Stock Funds
# sub-group and a Bond Funds sub-group before its own closing total line).
SECTION_CONTINUE_MARKERS: dict[str, dict[str, list[str]]] = {
    'fidelity': {
        'mutual_funds': ['total stock funds', 'total bond funds'],
    },
}

# Fidelity subsection labels nested inside "Mutual Funds" — recognized as
# grouping headings (skipped for row purposes), not as new sections. Passed
# to _reconstruct_rows as extra headings to filter out of row content.
FIDELITY_SUBSECTION_HEADINGS: dict[str, list[str]] = {
    'mutual_funds': ['Stock Funds', 'Bond Funds'],
}

# Per-institution, per-section column bands: field -> (x0_min, x0_max).
# 'description' is intentionally wide/open-ended (text-only band); numeric
# fields use tighter bands with margin. Schwab has a dedicated 'symbol' band;
# Fidelity has none — ticker is regex-extracted from the description text.
PDF_COLUMN_BANDS: dict[str, dict[str, dict[str, tuple[float, float]]]] = {
    'schwab': {
        # 'Type' label column (x0 ~0-65, e.g. "Bank Sweep"/"Money Fund") is
        # deliberately unbanded — not needed for ParsedPosition.
        'cash': {
            'symbol': (65, 115),
            'description': (115, 270),
            'quantity': (270, 330),
            'price': (330, 400),
            'value': (470, 560),   # Ending Balance($) column
        },
        'mutual_funds': {
            'symbol': (0, 55),
            'description': (55, 260),
            'quantity': (260, 340),
            'price': (340, 410),
            'value': (410, 474),   # Market Value($); Cost Basis/Unrealized/% not needed
        },
        'etfs': {
            'symbol': (0, 55),
            'description': (55, 290),
            'quantity': (290, 340),
            'price': (340, 410),
            'value': (410, 474),
        },
    },
    'fidelity': {
        'core': {
            'description': (0, 200),
            'value_begin': (200, 280),
            'quantity': (280, 345),
            'price': (345, 415),
            'value': (415, 495),   # Ending Market Value
            'eai': (660, 730),
        },
        'mutual_funds': {
            'description': (0, 200),
            'value_begin': (200, 280),
            'quantity': (280, 345),
            'price': (345, 415),
            'value': (415, 495),   # Ending Market Value
            'cost_basis': (495, 565),
            'unrealized': (565, 660),
            'eai': (660, 730),
        },
    },
}

# Column bands for the checking/savings "Activity" transaction table
# (date-anchored row reconstruction — see _reconstruct_transaction_rows in
# pdf_parsing.py — rather than the quantity/symbol-collision engine used for
# positions tables above).
PDF_TRANSACTION_COLUMN_BANDS: dict[str, dict[str, tuple[float, float]]] = {
    # Verified against a real Schwab Bank Investor Checking statement.
    'schwab_bank': {
        'date': (0, 55),
        'description': (55, 400),
        'debits': (400, 520),
        'credits': (520, 620),
        'balance': (620, 780),
    },
}

# Heading that marks the start of the transaction table, and the marker that
# ends it (a duplicate/unrelated table on the same or later page — parsing
# stops here so it isn't swept in as more transaction rows).
PDF_TRANSACTION_SECTION_HEADINGS: dict[str, str] = {
    'schwab_bank': 'activity',
}
PDF_TRANSACTION_SECTION_STOP_MARKERS: dict[str, str] = {
    'schwab_bank': 'checks paid',
}

# All recognized numeric bands (used to parse a word into the row dict).
PDF_NUMERIC_FIELDS: tuple[str, ...] = (
    'quantity', 'price', 'value', 'value_begin', 'cost_basis', 'unrealized', 'eai',
)

# Subset of PDF_NUMERIC_FIELDS used to detect "a new row has started" (a line
# supplies a value for a field already filled on the open row -> flush).
# 'eai' is excluded: Fidelity's EAI($)/EY(%) column holds the dollar figure
# on the row's anchor line and the percentage on its ticker-continuation line
# — both legitimately belong to the same row, so treating 'eai' as a
# collision trigger would flush the row before its ticker line is merged in.
PDF_COLLISION_FIELDS: tuple[str, ...] = (
    'quantity', 'price', 'value', 'value_begin', 'cost_basis', 'unrealized',
)
