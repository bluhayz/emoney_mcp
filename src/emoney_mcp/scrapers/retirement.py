"""
Retirement sustainability modeling.

Public functions
----------------
get_retirement_runway(http_session, annual_spending, return_rate)
    Projects how many years the current portfolio can sustain withdrawals.
    If annual_spending is not provided it pulls the actual 12-month spend from
    the SNB API.  Models three return scenarios (4%, 6%, 8%) and shows
    sustainable withdrawal amounts at 3.5%, 4%, and 4.5% SWR.

    The depletion math uses an inflation-adjusted real return so the model
    accounts for purchasing-power erosion over time (assumes 3% inflation).

get_withdrawal_rate_analysis(http_session)
    Projects the portfolio value to the Emoney retirement goal start year
    (at 6% annual return), then shows annual/monthly income at 3%, 3.5%,
    4%, 4.5%, and 5% withdrawal rates along with estimated years funded.
    Pulls the retirement goal start/end year directly from card 2 via
    get_goals(), so no parameters are needed.
"""

import math
from datetime import datetime

from .accounts import get_accounts
from .goals import get_goals
from .spending import _fetch_snb_data


async def get_retirement_runway(
    http_session,
    annual_spending: float | None = None,
    return_rate: float = 0.06,
) -> dict:
    """
    Model how many years the current portfolio can sustain withdrawals.

    If annual_spending is not provided, uses actual 12-month spending from
    the SNB transaction data.  Models three scenarios: conservative (4%),
    base (6%), and optimistic (8%) portfolio returns.

    Parameters
    ----------
    annual_spending : override spending in dollars (default: actual 12-month spend)
    return_rate     : base-case nominal annual return (default 0.06 = 6%)
    """
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts
    total_assets     = accts.get("total_assets") or 0
    total_liabilities = accts.get("total_liabilities") or 0
    net_worth         = accts.get("net_worth") or 0

    investable = max(0.0, total_assets - total_liabilities)

    if annual_spending is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        if ok:
            annual_spending = round(sum(
                t["amount"] for t in txns
                if not t["is_income"] and not t["is_excluded"]
            ), 2)
        else:
            annual_spending = 0.0

    if annual_spending <= 0:
        return {"error": "Could not determine annual spending. Pass annual_spending explicitly."}

    inflation = 0.03  # 3% inflation assumption

    def _years_to_depletion(portfolio: float, withdrawal: float, ret: float, inf: float) -> float | None:
        """Return years until portfolio hits zero. None if it never depletes."""
        real_return = (1 + ret) / (1 + inf) - 1
        if real_return <= 0:
            if withdrawal <= 0:
                return None
            return portfolio / withdrawal
        ratio = withdrawal / (portfolio * real_return)
        if ratio >= 1:
            return portfolio / withdrawal
        try:
            years = -math.log(1 - ratio) / math.log(1 + real_return)
            return years if years > 0 else None
        except Exception:
            return None

    scenarios = []
    for label, ret in [("Conservative (4%)", 0.04), ("Base (6%)", 0.06), ("Optimistic (8%)", 0.08)]:
        years = _years_to_depletion(investable, annual_spending, ret, inflation)
        scenarios.append({
            "scenario":          label,
            "return_rate_pct":   int(ret * 100),
            "years_to_depletion": round(years, 1) if years else None,
            "sustainable":        years is None or years > 30,
        })

    swr = [
        {"swr_pct": 3.5, "annual_amount": round(investable * 0.035, 2), "monthly": round(investable * 0.035 / 12, 2)},
        {"swr_pct": 4.0, "annual_amount": round(investable * 0.040, 2), "monthly": round(investable * 0.040 / 12, 2)},
        {"swr_pct": 4.5, "annual_amount": round(investable * 0.045, 2), "monthly": round(investable * 0.045 / 12, 2)},
    ]

    current_wr = round(annual_spending / investable * 100, 2) if investable > 0 else None

    return {
        "investable_assets":     round(investable, 2),
        "annual_spending":       annual_spending,
        "current_withdrawal_rate_pct": current_wr,
        "spending_covered_by_4pct_rule": annual_spending <= investable * 0.04,
        "inflation_assumption_pct": 3,
        "scenarios":             scenarios,
        "sustainable_withdrawal_amounts": swr,
        "note": (
            "Investable assets = total assets minus liabilities. "
            "Inflation-adjusted real return used for depletion modeling. "
            "'Sustainable' means portfolio survives 30+ years. "
            "Social Security, pensions, and annuities are not factored in — "
            "adding guaranteed income sources would extend runway significantly."
        ),
    }


async def get_withdrawal_rate_analysis(http_session) -> dict:
    """
    Analyze safe withdrawal rate in the context of your Emoney financial plan.

    Combines current portfolio value, retirement goal start year, and
    estimated retirement duration to model what various withdrawal rates
    produce in annual income.
    """
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts

    goals_result = await get_goals(http_session)
    retirement_goals = goals_result.get("retirement_goals", []) if "error" not in goals_result else []

    net_worth   = accts.get("net_worth") or 0
    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0
    investable   = max(0.0, total_assets - total_liab)

    current_year = datetime.now().year
    retirement_start = None
    retirement_end   = None
    goal_name        = None

    if retirement_goals:
        g = retirement_goals[0]
        retirement_start = g.get("start_year")
        retirement_end   = g.get("end_year")
        goal_name        = g.get("name")

    years_to_retirement = (retirement_start - current_year) if retirement_start else None
    retirement_duration = (retirement_end - retirement_start) if (retirement_start and retirement_end) else None

    projected_at_retirement = None
    if years_to_retirement and years_to_retirement > 0:
        projected_at_retirement = round(investable * (1.06 ** years_to_retirement), 2)
    else:
        projected_at_retirement = investable

    wdl_analysis = []
    for rate in [0.03, 0.035, 0.04, 0.045, 0.05]:
        annual = round((projected_at_retirement or investable) * rate, 2)
        monthly = round(annual / 12, 2)
        real_ret = (1.06 / 1.03) - 1
        ratio = rate / real_ret if real_ret > 0 else float("inf")
        try:
            years = -math.log(1 - min(ratio, 0.9999)) / math.log(1 + real_ret)
        except Exception:
            years = 999
        wdl_analysis.append({
            "rate_pct":           rate * 100,
            "annual_income":      annual,
            "monthly_income":     monthly,
            "est_years_funded":   round(years, 1) if years < 999 else None,
            "covers_30yr_plan":   years >= (retirement_duration or 30),
        })

    return {
        "current_investable_assets": round(investable, 2),
        "retirement_goal":           goal_name,
        "retirement_start_year":     retirement_start,
        "retirement_end_year":       retirement_end,
        "retirement_duration_years": retirement_duration,
        "years_to_retirement":       years_to_retirement,
        "projected_portfolio_at_retirement": projected_at_retirement,
        "projected_growth_assumption": "6% annual nominal return",
        "withdrawal_rate_scenarios": wdl_analysis,
        "rule_of_thumb": {
            "4pct_rule_annual": round((projected_at_retirement or investable) * 0.04, 2),
            "4pct_rule_monthly": round((projected_at_retirement or investable) * 0.04 / 12, 2),
            "summary": (
                "The 4% rule suggests a 30-year retirement is historically well-funded "
                "at this withdrawal rate from a diversified equity/bond portfolio."
            ),
        },
    }
