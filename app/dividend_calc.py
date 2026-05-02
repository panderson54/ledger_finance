"""
Passive income calculation and DRIP projection simulation.
Pure math — no Flask or DB imports. Mirrors projections.py.
"""
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

DEFAULT_TAX_RATE = 0.27  # 22% federal + 5% state, taxable-account dividends only


def calculate_current_income(holdings_data: list[dict], tax_rate: float = DEFAULT_TAX_RATE) -> dict:
    """
    Compute current annual and monthly passive income across all holdings.

    Each dict in holdings_data requires:
        ticker, shares, last_price, annual_yield, is_dividend_payer,
        tax_treatment, account_tax_status (taxable|tax_deferred|tax_free)

    Returns:
        {
            by_holding: [...],
            by_account: [...],
            total_annual_income: float,
            total_monthly_income: float,
            est_after_tax_annual: float,
            tax_rate_used: float,
        }
    """
    by_holding = []
    account_totals: dict[str, dict] = {}
    total_annual = 0.0
    after_tax_annual = 0.0

    for h in holdings_data:
        shares      = float(h.get('shares') or 0)
        price       = float(h.get('last_price') or 0)
        yield_dec   = float(h.get('annual_yield') or 0)
        is_payer    = bool(h.get('is_dividend_payer', True))
        tax_status  = h.get('account_tax_status', 'taxable')
        tax_treat   = h.get('tax_treatment')

        annual_income  = shares * price * yield_dec if is_payer else 0.0
        monthly_income = annual_income / 12

        total_annual += annual_income

        # After-tax: only reduce income from taxable accounts
        if tax_status == 'taxable' and annual_income > 0:
            after_tax_annual += annual_income * (1 - tax_rate)
        else:
            after_tax_annual += annual_income

        acct_id   = str(h.get('account_id', 'unknown'))
        acct_name = h.get('account_name', 'Unknown')
        if acct_id not in account_totals:
            account_totals[acct_id] = {
                'account_id': h.get('account_id'),
                'account_name': acct_name,
                'annual_income': 0.0,
                'monthly_income': 0.0,
            }
        account_totals[acct_id]['annual_income']  += annual_income
        account_totals[acct_id]['monthly_income'] += monthly_income

        by_holding.append({
            'ticker':             h.get('ticker'),
            'shares':             shares,
            'last_price':         price,
            'annual_yield':       yield_dec,
            'is_dividend_payer':  is_payer,
            'frequency':          h.get('frequency'),
            'payer_type':         h.get('payer_type'),
            'tax_treatment':      tax_treat,
            'account_tax_status': tax_status,
            'account_id':         h.get('account_id'),
            'account_name':       acct_name,
            'annual_income':      round(annual_income, 2),
            'monthly_income':     round(monthly_income, 2),
        })

    by_account = [
        {**v, 'annual_income': round(v['annual_income'], 2),
               'monthly_income': round(v['monthly_income'], 2)}
        for v in account_totals.values()
    ]

    return {
        'by_holding':           by_holding,
        'by_account':           by_account,
        'total_annual_income':  round(total_annual, 2),
        'total_monthly_income': round(total_annual / 12, 2),
        'est_after_tax_annual': round(after_tax_annual, 2),
        'tax_rate_used':        tax_rate,
    }


def simulate_drip(
    holdings_data: list[dict],
    horizon_years: int = 20,
    price_appreciation_rate: float = 0.07,
    dividend_growth_rate: float = 0.03,
    monthly_contribution: float = 0.0,
) -> dict:
    """
    Run a month-by-month DRIP projection simulation.

    Three parallel paths:
        DRIP ON   — dividends reinvested as fractional shares each month
        DRIP OFF  — dividends taken as cash; shares static; contributions invested
        No Action — price appreciation only; no dividends collected, no contributions

    Returns labels (monthly), three value series, and 5/10/20-year callouts.
    """
    if not holdings_data:
        return _empty_projection(horizon_years)

    horizon_months = min(horizon_years, 30) * 12
    today = date.today()

    # Build per-ticker state
    tickers = [h['ticker'] for h in holdings_data]
    drip_shares  = {h['ticker']: float(h.get('shares') or 0) for h in holdings_data}
    cash_shares  = {h['ticker']: float(h.get('shares') or 0) for h in holdings_data}
    base_shares  = {h['ticker']: float(h.get('shares') or 0) for h in holdings_data}

    prices       = {h['ticker']: float(h.get('last_price') or 0) for h in holdings_data}
    yields       = {h['ticker']: float(h.get('annual_yield') or 0) for h in holdings_data}
    is_payer     = {h['ticker']: bool(h.get('is_dividend_payer', True)) for h in holdings_data}

    monthly_price_growth = (1 + price_appreciation_rate) ** (1 / 12)
    monthly_div_growth   = (1 + dividend_growth_rate) ** (1 / 12)

    # Total portfolio value at start
    start_value = sum(drip_shares[t] * prices[t] for t in tickers)

    drip_series     = [round(start_value, 2)]
    cash_series     = [round(start_value, 2)]
    no_action_series = [round(start_value, 2)]

    labels = [today.strftime('%Y-%m')]

    cash_balance_drip_off = 0.0  # accumulated cash dividends in DRIP OFF path

    for m in range(1, horizon_months + 1):
        label_date = today + relativedelta(months=m)
        labels.append(label_date.strftime('%Y-%m'))

        # Apply price appreciation to current prices
        for t in tickers:
            prices[t] *= monthly_price_growth

        # Grow dividend yields
        for t in tickers:
            yields[t] *= monthly_div_growth

        total_portfolio_value_drip = sum(drip_shares[t] * prices[t] for t in tickers)

        # Monthly dividends for DRIP ON path
        drip_month_div = sum(
            drip_shares[t] * prices[t] * (yields[t] / 12)
            for t in tickers if is_payer[t]
        )
        # Reinvest by portfolio weight into each holding
        if total_portfolio_value_drip > 0 and drip_month_div > 0:
            for t in tickers:
                if not is_payer[t] or prices[t] <= 0:
                    continue
                weight = (drip_shares[t] * prices[t]) / total_portfolio_value_drip
                reinvest_amount = drip_month_div * weight
                drip_shares[t] += reinvest_amount / prices[t]

        drip_value = sum(drip_shares[t] * prices[t] for t in tickers)

        # Monthly dividends for DRIP OFF path (cash shares don't change)
        cash_month_div = sum(
            cash_shares[t] * prices[t] * (yields[t] / 12)
            for t in tickers if is_payer[t]
        )
        cash_balance_drip_off += cash_month_div + monthly_contribution

        cash_equity_value = sum(cash_shares[t] * prices[t] for t in tickers)
        cash_value = cash_equity_value + cash_balance_drip_off

        # No Action: price appreciation only
        no_action_value = sum(base_shares[t] * prices[t] for t in tickers)

        drip_series.append(round(drip_value, 2))
        cash_series.append(round(cash_value, 2))
        no_action_series.append(round(no_action_value, 2))

    # Build callouts at 5, 10, 20 years (or horizon if shorter)
    def _idx(yr):
        return min(yr * 12, horizon_months)

    callouts = {}
    for yr in (5, 10, 20):
        idx = _idx(yr)
        if idx <= horizon_months:
            callouts[f'yr{yr}_drip_on']  = drip_series[idx]
            callouts[f'yr{yr}_drip_off'] = cash_series[idx]
            callouts[f'yr{yr}_no_action'] = no_action_series[idx]

    if 'yr20_drip_on' in callouts and 'yr20_drip_off' in callouts:
        callouts['drip_advantage_20yr'] = round(
            callouts['yr20_drip_on'] - callouts['yr20_drip_off'], 2
        )

    return {
        'labels':    labels,
        'drip_on':   drip_series,
        'drip_off':  cash_series,
        'no_action': no_action_series,
        'callouts':  callouts,
    }


def _empty_projection(horizon_years: int) -> dict:
    today = date.today()
    horizon_months = min(horizon_years, 30) * 12
    labels = [
        (today + relativedelta(months=m)).strftime('%Y-%m')
        for m in range(horizon_months + 1)
    ]
    zeros = [0.0] * (horizon_months + 1)
    return {
        'labels': labels,
        'drip_on': zeros,
        'drip_off': zeros,
        'no_action': zeros,
        'callouts': {},
    }
