"""
Shared parsed-data shapes produced by both the CSV parser (parsing.py) and the
PDF parser (pdf_parsing.py). Nothing downstream of parsing (matching.py,
service.py) cares which format produced a ParsedImport.
"""
from dataclasses import dataclass, field
from datetime import date


@dataclass
class ParsedPosition:
    ticker: str
    description: str
    quantity: float
    price: float | None
    value: float | None       # market value; computed as qty*price if source omits it
    is_cash: bool = False      # True => folded into cash_total, not a Holding row


@dataclass
class ParsedTransaction:
    date: date
    description: str   # raw statement text (payee/ACH descriptor/initiator)
    amount: float       # always positive
    direction: str      # 'debit' | 'credit'


@dataclass
class ParsedAccount:
    key: str                                    # f"{institution}:{account_name}:{account_number_last4 or ''}"
    institution: str
    account_name: str
    account_number_last4: str | None
    positions: list[ParsedPosition] = field(default_factory=list)   # excludes is_cash rows
    cash_total: float = 0.0
    reported_total: float | None = None          # institution-printed subtotal/ending value, if present
    computed_total: float = 0.0                  # sum(positions.value) + cash_total
    balance_only: bool = False                   # True for bank/cash statements — only snapshot, no holdings
    transactions: list[ParsedTransaction] = field(default_factory=list)  # balance_only accounts only


@dataclass
class ParsedImport:
    institution: str                    # 'fidelity' | 'schwab' | 'unknown'
    source_format: str                    # 'csv' | 'pdf'
    as_of_date: date | None = None
    accounts: list[ParsedAccount] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
