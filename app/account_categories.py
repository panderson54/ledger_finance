"""
Shared account category constants used across routes, projections, and import_processor.
"""

# Categories that hold investable assets and need allocation splits
INVESTMENT_CATS: frozenset[str] = frozenset({
    'brokerage', 'retirement', '401k', 'ira', 'roth_ira', 'hsa', '529', 'investment',
})

# Categories that are automatically 100% cash in allocation math
CASH_CATS: frozenset[str] = frozenset({'savings', 'checking', 'cash'})

# Liability categories
LIABILITY_CATS: frozenset[str] = frozenset({'mortgage', 'credit_card', 'loan'})

# All valid account categories (used for form validation)
ALL_CATEGORIES: list[str] = [
    'cash', 'checking', 'savings', 'brokerage', 'retirement',
    '401k', 'ira', 'roth_ira', 'hsa', '529', 'investment',
    'real_estate', 'vehicle', 'mortgage', 'loan', 'credit_card',
]

# Fixed asset classes used in allocation tracking
ALLOCATION_CLASSES: list[str] = ['domestic', 'international', 'bonds', 'cash']

# Display buckets that group account categories on charts and summaries
BUCKET_MAP: dict[str, str] = {
    'cash': 'Cash', 'checking': 'Cash', 'savings': 'Cash',
    'retirement': 'Retirement', '401k': 'Retirement', 'ira': 'Retirement',
    'roth_ira': 'Retirement', 'hsa': 'Retirement', '529': 'Retirement',
    'brokerage': 'Investments', 'investment': 'Investments',
    'real_estate': 'Real Estate', 'vehicle': 'Real Estate',
    'mortgage': 'Liabilities', 'loan': 'Liabilities', 'credit_card': 'Liabilities',
}
BUCKET_ORDER: list[str] = ['Cash', 'Investments', 'Retirement', 'Real Estate', 'Liabilities']
