"""Passive income from holdings (dividends) and account-level yield estimates."""
import logging

from app import settings_service
from app.account_categories import INVESTMENT_CATS
from app.dividend_calc import DEFAULT_TAX_RATE, calculate_current_income
from app.dividend_service import _row_to_dict, get_or_fetch, is_dividend_stale
from app.models import Account, DividendData, Holding
from app.portfolio_service import latest_snapshots

logger = logging.getLogger(__name__)


def tax_rate() -> float:
    return settings_service.get_float_setting('drip_tax_rate_pct', DEFAULT_TAX_RATE * 100) / 100


def _dividend_dict(ticker, api_key):
    """Cached dividend data for ticker; fetched via Claude only when api_key is given and the cache is stale."""
    cached = DividendData.query.filter_by(ticker=ticker).first()
    if api_key and (not cached or is_dividend_stale(cached.last_fetched_at)):
        try:
            return get_or_fetch(ticker, api_key)[0]
        except Exception as e:
            logger.warning('Could not fetch dividend data for %s: %s', ticker, e)
            return None
    return _row_to_dict(cached) if cached else None


def _account_estimates():
    accounts = (
        Account.query
        .filter(Account.category.in_(INVESTMENT_CATS))
        .filter(Account.is_active == True)  # noqa: E712
        .filter(Account.expected_dividend_yield.isnot(None))
        .filter(Account.expected_dividend_yield > 0)
        .order_by(Account.name)
        .all()
    )
    snapshots = latest_snapshots([a.id for a in accounts]) if accounts else {}
    estimates = []
    for a in accounts:
        balance = float(snapshots[a.id].balance) if a.id in snapshots else 0.0
        expected_yield = float(a.expected_dividend_yield or 0)
        annual = balance * expected_yield
        estimates.append({
            'account_id': a.id,
            'account_name': a.name,
            'institution': a.institution,
            'category': a.category,
            'balance': balance,
            'expected_yield': expected_yield,
            'annual_income': annual,
            'monthly_income': annual / 12,
            'edit_url': f'/accounts/{a.id}/edit',
        })
    return estimates


def passive_income(account_id=None, api_key='') -> dict:
    """Current annual/monthly dividend income across active holdings.

    With no api_key this is read-only: tickers without cached dividend data are
    listed in missing_data instead of being fetched.
    """
    rate = tax_rate()
    query = Holding.query.filter_by(is_active=True)
    if account_id:
        query = query.filter_by(account_id=account_id)
    holdings = query.all()
    if not holdings:
        return {
            'by_holding': [], 'by_account': [],
            'total_annual_income': 0.0, 'total_monthly_income': 0.0,
            'est_after_tax_annual': 0.0, 'tax_rate_used': rate,
            'missing_data': [],
        }

    accounts = {a.id: a for a in Account.query.filter(Account.id.in_({h.account_id for h in holdings})).all()}
    missing_data = []
    holdings_data = []
    for h in holdings:
        acct = accounts.get(h.account_id)
        div = _dividend_dict(h.ticker, api_key)
        if div is None or div.get('fetch_error'):
            missing_data.append(h.ticker)
            div = {'annual_yield': 0.0, 'is_dividend_payer': False}
        holdings_data.append({
            'ticker': h.ticker,
            'shares': float(h.shares or 0),
            'last_price': float(h.last_price or 0),
            'annual_yield': float(div.get('annual_yield') or 0),
            'is_dividend_payer': bool(div.get('is_dividend_payer', True)),
            'frequency': div.get('frequency'),
            'payer_type': div.get('payer_type'),
            'tax_treatment': div.get('tax_treatment'),
            'account_id': h.account_id,
            'account_name': acct.name if acct else 'Unknown',
            'account_tax_status': acct.tax_status if acct else 'taxable',
        })

    result = calculate_current_income(holdings_data, tax_rate=rate)
    result['missing_data'] = missing_data
    result['account_estimates'] = _account_estimates()
    result['account_estimates_annual'] = sum(e['annual_income'] for e in result['account_estimates'])
    return result


def drip_holdings_data() -> list[dict]:
    """Active holdings with their cached dividend data, as simulate_drip expects."""
    rows = []
    for h in Holding.query.filter_by(is_active=True).all():
        cached = DividendData.query.filter_by(ticker=h.ticker).first()
        div = _row_to_dict(cached) if cached else {}
        rows.append({
            'ticker': h.ticker,
            'shares': float(h.shares or 0),
            'last_price': float(h.last_price or 0),
            'annual_yield': float(div.get('annual_yield') or 0),
            'is_dividend_payer': bool(div.get('is_dividend_payer', False)) if cached else False,
            'frequency': div.get('frequency'),
        })
    return rows
