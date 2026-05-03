"""
Database models for the Personal Finance Dashboard
"""
from datetime import datetime
from app import db


class Account(db.Model):
    """
    Represents a financial account (asset or liability)
    Examples: Cash, Retirement, Investments, Real Estate, Mortgage
    """
    __tablename__ = 'accounts'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    account_type = db.Column(db.String(20), nullable=False)  # 'asset' or 'liability'
    category = db.Column(db.String(50), nullable=False)  # 'cash', 'retirement', 'investment', 'real_estate', 'mortgage', etc.
    is_liquid = db.Column(db.Boolean, default=True)
    include_in_networth = db.Column(db.Boolean, default=True)
    display_order = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # Extended management fields
    tax_status = db.Column(db.String(20))       # 'taxable', 'tax_deferred', 'tax_free'
    institution = db.Column(db.String(100))
    account_number = db.Column(db.String(20))   # last 4 digits only
    notes = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    display_color = db.Column(db.String(7))     # hex color e.g. '#4a90e2'
    paired_liability_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=True)
    apy = db.Column(db.Numeric(7, 5), nullable=True)  # annual percentage yield for savings accounts

    # Relationships
    snapshots = db.relationship('AccountSnapshot', backref='account', lazy='dynamic', cascade='all, delete-orphan')
    allocations = db.relationship('AssetAllocation', backref='account', lazy='dynamic', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<Account {self.name}>'


class AccountSnapshot(db.Model):
    """
    Monthly snapshot of account balance
    """
    __tablename__ = 'account_snapshots'
    
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    snapshot_date = db.Column(db.Date, nullable=False)  # First day of month
    balance = db.Column(db.Numeric(12, 2), nullable=False)  # Supports up to $9,999,999,999.99
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Composite unique constraint: one snapshot per account per month
    __table_args__ = (
        db.UniqueConstraint('account_id', 'snapshot_date', name='unique_account_snapshot'),
    )
    
    def __repr__(self):
        return f'<AccountSnapshot {self.account.name} on {self.snapshot_date}: ${self.balance}>'


class SpendingEntry(db.Model):
    """
    Tracks income and expenses by account/card
    """
    __tablename__ = 'spending_entries'
    
    id = db.Column(db.Integer, primary_key=True)
    entry_date = db.Column(db.Date, nullable=False)  # First day of month
    account_name = db.Column(db.String(100), nullable=False)  # Card/account name (Chase, Amex, etc.)
    amount = db.Column(db.Numeric(10, 2), nullable=False)
    entry_type = db.Column(db.String(20), nullable=False)  # 'income' or 'expense'
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<SpendingEntry {self.account_name}: ${self.amount} ({self.entry_type})>'


class AssetAllocation(db.Model):
    """
    Tracks the asset class distribution for investment accounts
    Supports tracking allocation changes over time
    """
    __tablename__ = 'asset_allocations'
    
    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    effective_date = db.Column(db.Date, nullable=False)  # When this allocation became effective
    asset_class = db.Column(db.String(50), nullable=False)  # 'domestic_stock', 'international_stock', 'bonds'
    percentage = db.Column(db.Numeric(5, 2), nullable=False)  # 0.00 to 100.00
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (
        db.CheckConstraint('percentage >= 0 AND percentage <= 100', name='valid_percentage'),
    )
    
    def __repr__(self):
        return f'<AssetAllocation {self.account.name}: {self.asset_class} = {self.percentage}%>'


class CalculatedMetric(db.Model):
    """
    Stores pre-calculated monthly metrics for performance
    Recalculated when account snapshots are updated
    """
    __tablename__ = 'calculated_metrics'
    
    id = db.Column(db.Integer, primary_key=True)
    metric_date = db.Column(db.Date, nullable=False, unique=True)
    
    # Net worth metrics
    total_assets = db.Column(db.Numeric(12, 2))
    total_liabilities = db.Column(db.Numeric(12, 2))
    net_worth = db.Column(db.Numeric(12, 2))
    net_worth_non_re = db.Column(db.Numeric(12, 2))  # Excluding real estate
    net_worth_liquid = db.Column(db.Numeric(12, 2))  # Liquid assets only (is_liquid=True) minus liabilities
    
    # Monthly changes
    monthly_change_amount = db.Column(db.Numeric(12, 2))
    monthly_change_pct = db.Column(db.Numeric(6, 2))  # -999.99 to 999.99
    
    # Income/expense metrics
    total_income = db.Column(db.Numeric(10, 2))
    total_expenses = db.Column(db.Numeric(10, 2))
    save_rate = db.Column(db.Numeric(5, 2))  # Percentage: 0.00 to 100.00
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<CalculatedMetric {self.metric_date}: Net Worth ${self.net_worth}>'


class AppSetting(db.Model):
    """
    Stores application settings and user preferences
    """
    __tablename__ = 'app_settings'
    
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False, unique=True)
    value = db.Column(db.Text)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f'<AppSetting {self.key}>'


class Holding(db.Model):
    """
    Ticker-level holding within an investment account.
    Value is always computed (shares × last_price) — never stored.
    AccountSnapshot.balance remains the net-worth source of truth.
    """
    __tablename__ = 'holdings'

    id           = db.Column(db.Integer, primary_key=True)
    account_id   = db.Column(db.Integer, db.ForeignKey('accounts.id'), nullable=False)
    ticker       = db.Column(db.String(20), nullable=False)
    name         = db.Column(db.String(100))                  # display label
    shares       = db.Column(db.Numeric(18, 8), nullable=False)
    last_price   = db.Column(db.Numeric(12, 4))               # cached; refreshed manually
    last_fetched = db.Column(db.DateTime)
    is_active    = db.Column(db.Boolean, default=True)        # archive on sell
    cap_class    = db.Column(db.String(10))                   # 'large'|'mid'|'small'|None (#11b)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)

    account      = db.relationship('Account', backref=db.backref('holdings', lazy='dynamic'))
    allocations  = db.relationship('HoldingAllocation', backref='holding',
                                   lazy='dynamic', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Holding {self.ticker} × {self.shares}>'


class HoldingAllocation(db.Model):
    """
    Asset-class split for a single holding.
    Simple holdings: one row at 100%.
    Blended/target-date funds: multiple rows summing to 100%.
    """
    __tablename__ = 'holding_allocations'

    id          = db.Column(db.Integer, primary_key=True)
    holding_id  = db.Column(db.Integer, db.ForeignKey('holdings.id'), nullable=False)
    asset_class = db.Column(db.String(20), nullable=False)   # domestic|international|bonds|cash
    percentage  = db.Column(db.Numeric(5, 2), nullable=False)

    __table_args__ = (
        db.CheckConstraint('percentage >= 0 AND percentage <= 100', name='ha_valid_pct'),
    )

    def __repr__(self):
        return f'<HoldingAllocation holding_id={self.holding_id} {self.asset_class}={self.percentage}%>'


class TickerClassification(db.Model):
    """
    Cached Claude API classification for a ticker symbol.
    One row per unique ticker; shared across all accounts/holdings that hold it.
    """
    __tablename__ = 'ticker_classifications'

    id              = db.Column(db.Integer, primary_key=True)
    ticker          = db.Column(db.String(20), nullable=False, unique=True)
    asset_class     = db.Column(db.String(20), nullable=False)   # dominant: domestic|international|bonds|cash
    market_cap_tilt = db.Column(db.String(10), nullable=True)    # large|mid|small|None
    sector_weights  = db.Column(db.Text, nullable=False)         # JSON: {"domestic":X,"international":Y,"bonds":Z,"cash":W}
    source          = db.Column(db.String(20), default='claude') # 'claude'|'manual'
    classified_at   = db.Column(db.DateTime, default=datetime.utcnow)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def weights_dict(self) -> dict:
        import json
        return json.loads(self.sector_weights)

    def __repr__(self):
        return f'<TickerClassification {self.ticker} → {self.asset_class}>'


class DividendData(db.Model):
    """
    Cached dividend data per ticker, populated by Claude Haiku on demand.
    One row per unique ticker; 30-day TTL for staleness checks.
    """
    __tablename__ = 'dividend_data'

    id                 = db.Column(db.Integer, primary_key=True)
    ticker             = db.Column(db.String(20), nullable=False, unique=True)
    annual_yield       = db.Column(db.Numeric(8, 6))       # decimal, e.g. 0.035000 = 3.5%
    dividend_per_share = db.Column(db.Numeric(10, 4))      # most recent annual DPS
    frequency          = db.Column(db.String(20))          # monthly|quarterly|semi-annual|annual
    payer_type         = db.Column(db.String(30))          # dividend_stock|reit|etf|bond_fund|cef
    is_dividend_payer  = db.Column(db.Boolean, nullable=False, default=True)
    tax_treatment      = db.Column(db.String(20))          # qualified|ordinary|return_of_capital
    source_notes       = db.Column(db.Text)                # JSON: {ttm_yield, payout_ratio, cut_risk, as_of_date}
    last_fetched_at    = db.Column(db.DateTime)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

    def notes_dict(self) -> dict:
        import json
        try:
            return json.loads(self.source_notes) if self.source_notes else {}
        except Exception:
            return {}

    def __repr__(self):
        return f'<DividendData {self.ticker} yield={self.annual_yield}>'


class RecurringEntry(db.Model):
    """
    Template entries automatically applied when a new month is initialized.
    """
    __tablename__ = 'recurring_entries'

    id            = db.Column(db.Integer, primary_key=True)
    account_name  = db.Column(db.String(100), nullable=False)
    amount        = db.Column(db.Numeric(10, 2), nullable=False)
    entry_type    = db.Column(db.String(20), nullable=False)   # 'income' | 'expense'
    notes         = db.Column(db.Text)
    is_active     = db.Column(db.Boolean, default=True, nullable=False)
    display_order = db.Column(db.Integer, default=0, nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f'<RecurringEntry {self.account_name}: ${self.amount} ({self.entry_type})>'


class RentalProperty(db.Model):
    """
    Real estate rental property for passive income tracking.
    Net annual income = monthly_rent × (1 - vacancy_rate) × 12.
    """
    __tablename__ = 'rental_properties'

    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    address       = db.Column(db.String(200))
    monthly_rent  = db.Column(db.Numeric(10, 2), default=0, nullable=False)
    vacancy_rate  = db.Column(db.Numeric(5, 4), default=0.05, nullable=False)  # e.g. 0.0500 = 5%
    is_active     = db.Column(db.Boolean, default=True, nullable=False)
    notes         = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def effective_monthly(self):
        return float(self.monthly_rent or 0) * (1 - float(self.vacancy_rate or 0))

    @property
    def annual_income(self):
        return self.effective_monthly * 12

    def __repr__(self):
        return f'<RentalProperty {self.name}>'


class ImportLog(db.Model):
    """
    Tracks data imports for auditing and debugging
    """
    __tablename__ = 'import_logs'
    
    id = db.Column(db.Integer, primary_key=True)
    import_date = db.Column(db.DateTime, default=datetime.utcnow)
    file_name = db.Column(db.String(255))
    records_imported = db.Column(db.Integer)
    status = db.Column(db.String(20))  # 'success', 'partial', 'failed'
    error_message = db.Column(db.Text)
    details = db.Column(db.Text)  # JSON string with additional details
    
    def __repr__(self):
        return f'<ImportLog {self.file_name} - {self.status}>'
