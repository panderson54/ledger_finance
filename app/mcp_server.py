"""Read-only MCP (Model Context Protocol) tools over Ledger's financial data, so an AI
agent can answer questions about net worth, cash flow, allocation, income and FI
projections. No tool creates, updates or deletes anything, refreshes prices or fetches
dividend data: every call runs under read_only_guard, and stale data comes back with a
warning instead. Fields the user would not want shared — institutions, account numbers,
notes, addresses, API keys — are never returned.

Two ways to reach it, both via create_mcp_server():
- locally over stdio: `python -m app.mcp_server`
- remotely over HTTPS at /mcp with a bearer token (see app/mcp_http.py)
"""
import functools
from datetime import datetime

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from app import (
    create_app, db, income_service, portfolio_service, projections_service, read_only_guard, settings_service,
)
from app.dividend_calc import simulate_drip
from app.models import Account, AccountSnapshot, CalculatedMetric, Holding, RecurringEntry, RentalProperty, SpendingEntry
from app.price_service import is_stale
from app.projections import calculate_cagr

DEFAULT_SAVE_RATE_TARGET = 35.0


def _money(value):
    return round(float(value), 2) if value is not None else None


def _month(value):
    return value.strftime('%Y-%m') if value else None


def _parse_month(value, name):
    if value is None:
        return None
    try:
        return datetime.strptime(value, '%Y-%m').date()
    except (TypeError, ValueError):
        raise ValueError(f'{name} must be a month in YYYY-MM format.')


def _month_range(start_month, end_month):
    start, end = _parse_month(start_month, 'start_month'), _parse_month(end_month, 'end_month')
    if start and end and start > end:
        raise ValueError('start_month must not be after end_month.')
    return start, end


def _in_range(query, column, start, end):
    if start:
        query = query.filter(column >= start)
    if end:
        query = query.filter(column <= end)
    return query


def _account_or_error(account_id):
    account = db.session.get(Account, account_id)
    if account is None:
        raise ValueError(f'No account {account_id}. Use list_accounts to find account ids.')
    return account


def _latest_metric():
    return CalculatedMetric.query.order_by(CalculatedMetric.metric_date.desc()).first()


def _stale_price_warnings(holdings):
    stale = [h.ticker for h in holdings if is_stale(h.last_fetched)]
    return [f'prices for {len(stale)} holding(s) are over a day old: {", ".join(sorted(stale))}'] if stale else []


def _yearly(series):
    """Every 12th month of a monthly series (index 0 is now), to keep responses small."""
    return series[::12]


def get_financial_snapshot() -> dict:
    """Current financial position: net worth (total, excluding real estate, liquid), assets,
    liabilities, last month's change, balances by bucket (Cash, Investments, Retirement,
    Real Estate, Liabilities), year-to-date income/expenses/save rate vs target.
    net_worth_non_re excludes real estate and vehicles; net_worth_liquid is liquid assets
    minus all liabilities; save_rate_pct = (income - expenses) / income."""
    metric = _latest_metric()
    ytd = portfolio_service.ytd_cash_flow()
    warnings = [] if metric else ['no monthly snapshots recorded yet']
    return {
        'as_of': _month(metric.metric_date) if metric else None,
        'net_worth': _money(metric.net_worth) if metric else None,
        'net_worth_non_re': _money(metric.net_worth_non_re) if metric else None,
        'net_worth_liquid': _money(metric.net_worth_liquid) if metric else None,
        'total_assets': _money(metric.total_assets) if metric else None,
        'total_liabilities': _money(metric.total_liabilities) if metric else None,
        'monthly_change': _money(metric.monthly_change_amount) if metric else None,
        'monthly_change_pct': _money(metric.monthly_change_pct) if metric else None,
        'balances_by_bucket': portfolio_service.bucket_totals(),
        'ytd': {
            'year': ytd['year'],
            'income': _money(ytd['income']),
            'expenses': _money(ytd['expenses']),
            'save_rate_pct': _money(ytd['save_rate']),
            'save_rate_target_pct': settings_service.get_float_setting('save_rate_target', DEFAULT_SAVE_RATE_TARGET),
        },
        'warnings': warnings,
    }


def get_net_worth_history(start_month: str | None = None, end_month: str | None = None) -> dict:
    """Monthly net worth, net worth excluding real estate, liquid net worth, assets and
    liabilities, plus the compound annual growth rate (CAGR) of net worth over the range.
    Months are YYYY-MM; omit either bound for the full history."""
    start, end = _month_range(start_month, end_month)
    metrics = _in_range(
        CalculatedMetric.query.filter(CalculatedMetric.net_worth.isnot(None)),
        CalculatedMetric.metric_date, start, end,
    ).order_by(CalculatedMetric.metric_date).all()
    cagr_pct = None
    if len(metrics) >= 2 and float(metrics[0].net_worth) > 0:
        years = (metrics[-1].metric_date - metrics[0].metric_date).days / 365.25
        cagr_pct = round(calculate_cagr(float(metrics[0].net_worth), float(metrics[-1].net_worth), years) * 100, 2)
    return {
        'months': [{
            'month': _month(m.metric_date),
            'net_worth': _money(m.net_worth),
            'net_worth_non_re': _money(m.net_worth_non_re),
            'net_worth_liquid': _money(m.net_worth_liquid),
            'total_assets': _money(m.total_assets),
            'total_liabilities': _money(m.total_liabilities),
            'monthly_change': _money(m.monthly_change_amount),
        } for m in metrics],
        'net_worth_cagr_pct': cagr_pct,
        'warnings': [] if metrics else ['no net worth history in this range'],
    }


def get_cash_flow(start_month: str | None = None, end_month: str | None = None) -> dict:
    """Monthly income, expenses and save rate, with each month's entries by source (the
    card/account name), plus the active recurring income and expense templates.
    Months are YYYY-MM; omit either bound for the full history."""
    start, end = _month_range(start_month, end_month)
    metrics = _in_range(
        CalculatedMetric.query.filter(CalculatedMetric.total_income.isnot(None)),
        CalculatedMetric.metric_date, start, end,
    ).order_by(CalculatedMetric.metric_date).all()
    entries = _in_range(SpendingEntry.query, SpendingEntry.entry_date, start, end).all()

    by_month: dict[str, dict] = {}
    for e in entries:
        sources = by_month.setdefault(_month(e.entry_date), {'income': {}, 'expense': {}})
        bucket = sources.setdefault(e.entry_type, {})
        bucket[e.account_name] = round(bucket.get(e.account_name, 0.0) + float(e.amount), 2)

    recurring = RecurringEntry.query.filter_by(is_active=True).order_by(RecurringEntry.display_order).all()
    return {
        'months': [{
            'month': _month(m.metric_date),
            'income': _money(m.total_income),
            'expenses': _money(m.total_expenses),
            'savings': _money(float(m.total_income or 0) - float(m.total_expenses or 0)),
            'save_rate_pct': _money(m.save_rate),
            'by_source': by_month.get(_month(m.metric_date), {'income': {}, 'expense': {}}),
        } for m in metrics],
        'recurring': [
            {'source': r.account_name, 'type': r.entry_type, 'monthly_amount': _money(r.amount)}
            for r in recurring
        ],
        'warnings': [] if metrics else ['no cash flow recorded in this range'],
    }


def list_accounts(include_inactive: bool = False) -> list[dict]:
    """Accounts with their category (e.g. brokerage, 401k, savings, mortgage), asset or
    liability type, tax status, latest balance and the month it was recorded."""
    query = Account.query if include_inactive else Account.query.filter_by(is_active=True)
    accounts = query.order_by(Account.display_order, Account.name).all()
    snapshots = portfolio_service.latest_snapshots([a.id for a in accounts])
    return [{
        'id': a.id,
        'name': a.name,
        'category': a.category,
        'account_type': a.account_type,
        'tax_status': a.tax_status,
        'is_liquid': a.is_liquid,
        'include_in_networth': a.include_in_networth,
        'is_active': a.is_active,
        'latest_balance': _money(snapshots[a.id].balance) if a.id in snapshots else None,
        'latest_month': _month(snapshots[a.id].snapshot_date) if a.id in snapshots else None,
        'apy_pct': round(float(a.apy) * 100, 3) if a.apy is not None else None,
        'expected_dividend_yield_pct': (
            round(float(a.expected_dividend_yield) * 100, 3) if a.expected_dividend_yield is not None else None
        ),
    } for a in accounts]


def get_account_history(account_id: int, start_month: str | None = None, end_month: str | None = None) -> dict:
    """Monthly balances for one account (id from list_accounts) and their CAGR over the range."""
    account = _account_or_error(account_id)
    start, end = _month_range(start_month, end_month)
    snaps = _in_range(
        AccountSnapshot.query.filter_by(account_id=account.id), AccountSnapshot.snapshot_date, start, end,
    ).order_by(AccountSnapshot.snapshot_date).all()
    cagr_pct = None
    if len(snaps) >= 2 and float(snaps[0].balance) > 0:
        years = (snaps[-1].snapshot_date - snaps[0].snapshot_date).days / 365.25
        cagr_pct = round(calculate_cagr(float(snaps[0].balance), float(snaps[-1].balance), years) * 100, 2)
    return {
        'account': {'id': account.id, 'name': account.name, 'category': account.category},
        'balances': [{'month': _month(s.snapshot_date), 'balance': _money(s.balance)} for s in snaps],
        'cagr_pct': cagr_pct,
        'warnings': [] if snaps else ['no balances recorded for this account in this range'],
    }


def get_holdings(account_id: int | None = None) -> dict:
    """Ticker-level holdings (all accounts, or one account id from list_accounts): shares,
    last price and when it was fetched, market value, portfolio weight, asset-class split,
    and concentration (top-10 weight, positions over 10%). Prices are not refreshed."""
    query = Holding.query.filter_by(is_active=True)
    if account_id is not None:
        query = query.filter_by(account_id=_account_or_error(account_id).id)
    holdings = query.all()
    names = {a.id: a.name for a in Account.query.filter(Account.id.in_({h.account_id for h in holdings})).all()}

    def value(h):
        return float(h.shares) * float(h.last_price) if h.shares is not None and h.last_price is not None else None

    total = sum(value(h) or 0.0 for h in holdings)
    rows = sorted(({
        'ticker': h.ticker,
        'name': h.name or h.ticker,
        'account_id': h.account_id,
        'account_name': names.get(h.account_id),
        'shares': float(h.shares),
        'last_price': _money(h.last_price),
        'price_fetched_at': h.last_fetched.isoformat() if h.last_fetched else None,
        'value': _money(value(h)),
        'weight_pct': round(value(h) / total * 100, 2) if value(h) and total else None,
        'allocation_pct': {ha.asset_class: float(ha.percentage) for ha in h.allocations.all()},
        'cap_class': h.cap_class,
    } for h in holdings), key=lambda r: r['value'] or 0, reverse=True)
    weights = [r['weight_pct'] or 0 for r in rows]
    return {
        'holdings': rows,
        'total_value': _money(total),
        'concentration': {
            'top_10_weight_pct': round(sum(weights[:10]), 2),
            'positions_over_10_pct': [r['ticker'] for r in rows if (r['weight_pct'] or 0) > 10],
        },
        'warnings': _stale_price_warnings(holdings) if holdings else ['no active holdings'],
    }


def get_asset_allocation() -> dict:
    """Investable portfolio split across domestic / international / bonds / cash: current %
    vs target %, drift, the dollar amount to add (+) or remove (-) per class to rebalance,
    per-account splits, and the market-cap mix of priced holdings. status is on_target
    (max drift <= 2 points), review (<= 5) or rebalance."""
    o = portfolio_service.allocation_overview()
    holdings = [row['holding'] for row in o['all_holdings_rows']]
    warnings = _stale_price_warnings(holdings)
    if o['unclassified']:
        warnings.append(f'${o["unclassified"]:,.2f} of investable balances has no allocation split')
    if not o['total_invested']:
        warnings.append('no investable balances recorded')
    return {
        # With nothing invested every class reads as fully under target, which isn't a rebalance.
        'status': o['status'] if o['total_invested'] else None,
        'total_invested': _money(o['total_invested']),
        'unclassified': _money(o['unclassified']),
        'classes': {cls: {
            'actual_pct': round(o['actuals'][cls], 2),
            'target_pct': round(float(o['targets'].get(cls, 0.0)), 2),
            'drift_pct': round(o['drifts'][cls], 2),
            'amount': _money(o['totals'][cls]),
            'rebalance_amount': _money(o['gaps'][cls]),
        } for cls in o['actuals']},
        'by_account': [{
            'account_id': row['account'].id,
            'account_name': row['account'].name,
            'balance': _money(row['balance']),
            'allocation_pct': {cls: round(pct, 2) for cls, pct in row['splits'].items()},
            'source': row['source'],
        } for row in o['by_account']],
        'market_cap_pct': {k: round(v, 2) for k, v in o['cap_pcts'].items()},
        'warnings': warnings,
    }


_HOLDING_INCOME_FIELDS = (
    'ticker', 'account_id', 'account_name', 'account_tax_status', 'shares', 'last_price',
    'annual_yield', 'annual_income', 'monthly_income', 'is_dividend_payer', 'frequency', 'payer_type',
    'tax_treatment',
)
_ACCOUNT_ESTIMATE_FIELDS = ('account_id', 'account_name', 'category', 'balance', 'expected_yield',
                            'annual_income', 'monthly_income')


def get_passive_income(account_id: int | None = None) -> dict:
    """Current passive income: annual and monthly dividend income from holdings (all, or one
    account id), estimated after-tax income (taxable accounts taxed at tax_rate_used), income
    by account, account-level yield estimates, and net rental income. Uses cached dividend
    data only; tickers without it are listed in missing_data (yields are decimals)."""
    if account_id is not None:
        _account_or_error(account_id)
    result = income_service.passive_income(account_id)
    rentals = RentalProperty.query.filter_by(is_active=True).order_by(RentalProperty.name).all()
    warnings = []
    if result['missing_data']:
        warnings.append(f'no cached dividend data for: {", ".join(sorted(result["missing_data"]))}')
    return {
        'total_annual_income': _money(result['total_annual_income']),
        'total_monthly_income': _money(result['total_monthly_income']),
        'est_after_tax_annual': _money(result['est_after_tax_annual']),
        'tax_rate_used': result['tax_rate_used'],
        'by_holding': [{k: row.get(k) for k in _HOLDING_INCOME_FIELDS} for row in result['by_holding']],
        'by_account': result['by_account'],
        'account_estimates': [
            {k: row.get(k) for k in _ACCOUNT_ESTIMATE_FIELDS} for row in result.get('account_estimates', [])
        ],
        'rental_properties': [{
            'name': p.name,
            'monthly_rent': _money(p.monthly_rent),
            'vacancy_rate': float(p.vacancy_rate or 0),
            'net_annual_income': _money(p.annual_income),
        } for p in rentals],
        'missing_data': result['missing_data'],
        'warnings': warnings,
    }


def project_net_worth(
    years: int = 20,
    rates_pct: dict[str, float] | None = None,
    monthly_savings: float | None = None,
) -> dict:
    """Project net worth year by year from current balances (pure computation, nothing saved).
    rates_pct maps account category to annual growth % and defaults to each category's
    historical CAGR (default_rates_pct in the response); any category can be overridden.
    monthly_savings is added to investment and cash accounts in proportion to their balances.
    years is 1-30."""
    if not 1 <= years <= projections_service.MAX_GROWTH_YEARS:
        raise ValueError(f'years must be between 1 and {projections_service.MAX_GROWTH_YEARS}.')
    if monthly_savings is not None and monthly_savings < 0:
        raise ValueError('monthly_savings must not be negative.')
    defaults = {cat: round(r * 100, 2) for cat, r in projections_service.default_category_rates().items()}
    rates = {**defaults, **(rates_pct or {})}
    income = None
    if monthly_savings:
        income = {'mode': 'net', 'net_annual': monthly_savings * 12, 'save_rate_pct': 100, 'distribution': 'pro_rata'}
    result = projections_service.growth_projection(rates, years, income=income)
    return {
        'default_rates_pct': defaults,
        'rates_used_pct': rates,
        'years': [label[:4] for label in _yearly(result['labels'])],
        'total_projected': _yearly(result['total_projected']),
        'by_category': {cat: _yearly(vals) for cat, vals in result['by_category'].items()},
        'callouts': result['callouts'],
        'monthly_savings': result['monthly_contribution'],
    }


def project_financial_independence(
    annual_spending: float | None = None,
    swr_pct: float = 4.0,
    growth_pct: float = 7.0,
    monthly_savings: float | None = None,
    current_age: int | None = None,
) -> dict:
    """Financial independence (FI) projection from current investable net worth (net worth
    excluding real estate): FI number = annual_spending / safe withdrawal rate, the month FI
    is reached, a yearly projection, and a sensitivity table of years to FI across safe
    withdrawal rates and growth rates. annual_spending defaults to the last 12 months of
    expenses; monthly_savings to the latest month's income minus expenses."""
    if not 0 < swr_pct <= 20:
        raise ValueError('swr_pct must be between 0 and 20.')
    if not -50 < growth_pct <= 50:
        raise ValueError('growth_pct must be between -50 and 50.')
    if annual_spending is not None and annual_spending <= 0:
        raise ValueError('annual_spending must be positive.')
    if annual_spending is None:
        recent = CalculatedMetric.query.order_by(CalculatedMetric.metric_date.desc()).limit(12).all()
        annual_spending = sum(float(m.total_expenses or 0) for m in recent)
        if annual_spending <= 0:
            raise ValueError('No expense history to default from; pass annual_spending.')
    if monthly_savings is None:
        monthly_savings = projections_service.projection_context()['default_monthly_savings']
    current_nw = projections_service.latest_investable_net_worth()
    scenario = projections_service.fi_scenario(
        current_nw, annual_spending, swr_pct, growth_pct, monthly_savings, name='FI', current_age=current_age,
    )
    return {
        'current_investable_net_worth': _money(current_nw),
        'annual_spending': _money(annual_spending),
        'monthly_savings': _money(monthly_savings),
        'fi_number': scenario['fi_number'],
        'fi_date': scenario['fi_date'],
        'years_to_fi': scenario['years_to_fi'],
        'age_at_fi': scenario['age_at_fi'],
        'yearly_projection': _yearly(scenario['projection']),
        'sensitivity': projections_service.fi_sensitivity(current_nw, monthly_savings, annual_spending, growth_pct),
    }


def project_dividend_income(
    years: int = 20,
    price_appreciation_pct: float | None = None,
    dividend_growth_pct: float | None = None,
    monthly_contribution: float | None = None,
) -> dict:
    """Simulate dividend reinvestment (DRIP) for current holdings: portfolio value with
    dividends reinvested, taken as cash, or ignored, plus 5/10/20-year callouts. Rates are
    annual %; omitted values use the assumptions saved on the Income page. years is 1-30."""
    if not 1 <= years <= 30:
        raise ValueError('years must be between 1 and 30.')
    price = price_appreciation_pct if price_appreciation_pct is not None else \
        settings_service.get_float_setting('drip_price_appreciation_pct', 7.0)
    growth = dividend_growth_pct if dividend_growth_pct is not None else \
        settings_service.get_float_setting('drip_dividend_growth_pct', 3.0)
    contribution = monthly_contribution if monthly_contribution is not None else \
        settings_service.get_float_setting('drip_monthly_contribution', 0.0)
    result = simulate_drip(
        income_service.drip_holdings_data(), horizon_years=years,
        price_appreciation_rate=price / 100, dividend_growth_rate=growth / 100,
        monthly_contribution=contribution,
    )
    result['assumptions'] = {
        'price_appreciation_pct': price, 'dividend_growth_pct': growth,
        'monthly_contribution': contribution, 'years': years,
    }
    for key in ('labels', 'drip_on', 'drip_off', 'no_action'):
        if key in result:
            result[key] = _yearly(result[key])
    return result


TOOLS = (
    get_financial_snapshot, get_net_worth_history, get_cash_flow, list_accounts, get_account_history,
    get_holdings, get_asset_allocation, get_passive_income,
    project_net_worth, project_financial_independence, project_dividend_income,
)


READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def _in_app_context(flask_app, tool):
    # The SDK runs sync tools on worker threads, so each call opens its own app
    # context (and DB session) instead of relying on one pushed elsewhere.
    @functools.wraps(tool)
    def wrapper(*args, **kwargs):
        with flask_app.app_context(), read_only_guard.read_only():
            try:
                return tool(*args, **kwargs)
            except ValueError as e:
                # The tools raise ValueError for bad input the agent can fix; the SDK
                # hides any message not raised as ToolError, so pass these through.
                raise ToolError(str(e)) from e
    return wrapper


def create_mcp_server(flask_app):
    server = MCPServer('ledger-finance')
    for tool in TOOLS:
        server.add_tool(_in_app_context(flask_app, tool), annotations=READ_ONLY)
    return server


if __name__ == '__main__':
    create_mcp_server(create_app()).run()
