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
from datetime import datetime, timedelta

from .accounts import get_accounts
from .goals import get_goals
from .spending import _fetch_snb_data, get_savings_rate


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


# ---------------------------------------------------------------------------
# get_net_worth_projection  (Sprint 3)
# ---------------------------------------------------------------------------

async def get_net_worth_projection(
    http_session,
    target_net_worth: float | None = None,
    annual_return: float = 0.07,
    annual_savings_override: float | None = None,
) -> dict:
    """
    Project net worth forward and answer "When will I hit $X?" questions.

    Uses the current net worth from Emoney and the actual average monthly net
    savings (income − spending) from the last 6 months to drive the projection.
    Investment returns compound on the existing portfolio; new savings are added
    monthly on top.

    Automatically shows milestone years for common targets
    ($500k, $1M, $2M, $5M, $10M) and, if ``target_net_worth`` is provided,
    the specific year and month when that target will be reached.

    Parameters
    ----------
    target_net_worth       : optional target in dollars (e.g. 2_000_000)
    annual_return          : assumed annual portfolio return (default 7%)
    annual_savings_override: override the inferred annual savings amount
    """
    import asyncio

    # Fetch accounts and savings rate in parallel
    accts, savings_result = await asyncio.gather(
        get_accounts(http_session),
        get_savings_rate(http_session, months=6),
    )

    if "error" in accts:
        return accts

    current_nw   = accts.get("net_worth") or 0
    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0

    # Monthly savings: use override, or infer from savings_rate data
    if annual_savings_override is not None:
        monthly_savings = round(annual_savings_override / 12, 2)
        savings_source  = "override"
    elif "error" not in savings_result:
        total_net       = savings_result.get("total_net", 0) or 0
        months_shown    = savings_result.get("months_shown", 6) or 6
        monthly_savings = round(total_net / months_shown, 2)
        savings_source  = f"average over last {months_shown} months"
    else:
        monthly_savings = 0.0
        savings_source  = "unavailable (defaulting to $0)"

    monthly_return = annual_return / 12
    current_year   = datetime.now().year
    current_month  = datetime.now().month

    # Common milestones to track
    _MILESTONES = [500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]

    milestones_hit:   dict[float, dict] = {}
    target_hit:       dict | None = None

    # Project month by month for up to 50 years
    balance       = float(current_nw)
    month_offset  = 0
    yearly_snaps  = []

    for _ in range(50 * 12):
        month_offset += 1
        # Compound existing balance and add new savings
        balance = round(balance * (1 + monthly_return) + monthly_savings, 2)

        proj_year  = current_year + (current_month + month_offset - 1) // 12
        proj_month = (current_month + month_offset - 1) % 12 + 1

        # Check milestones
        for ms in _MILESTONES:
            if ms not in milestones_hit and balance >= ms:
                milestones_hit[ms] = {
                    "milestone":   ms,
                    "year":        proj_year,
                    "month":       proj_month,
                    "years_away":  round(month_offset / 12, 1),
                }

        # Check user target
        if target_net_worth and target_hit is None and balance >= target_net_worth:
            target_hit = {
                "target":     target_net_worth,
                "year":       proj_year,
                "month":      proj_month,
                "years_away": round(month_offset / 12, 1),
                "balance_at_target": round(balance, 2),
            }

        # Annual snapshot (every 12 months)
        if month_offset % 12 == 0:
            years_out = month_offset // 12
            yearly_snaps.append({
                "year":          current_year + years_out,
                "years_out":     years_out,
                "net_worth":     round(balance, 2),
            })

        # Stop projecting once all milestones and the user target are hit
        if len(milestones_hit) == len(_MILESTONES) and (target_net_worth is None or target_hit is not None):
            break

    # Cap yearly snapshots for readability
    yearly_snaps = yearly_snaps[:30]  # 30-year horizon

    return {
        "current_net_worth":      round(current_nw, 2),
        "total_assets":           round(total_assets, 2),
        "total_liabilities":      round(total_liab, 2),
        "monthly_savings":        monthly_savings,
        "annual_savings":         round(monthly_savings * 12, 2),
        "savings_source":         savings_source,
        "annual_return_pct":      round(annual_return * 100, 1),
        "target_net_worth":       target_net_worth,
        "target_reached":         target_hit,
        "milestones": [
            milestones_hit.get(ms, {
                "milestone": ms,
                "year": None,
                "note": "Not reached within 50-year projection",
            })
            for ms in _MILESTONES
        ],
        "30_year_projection":     yearly_snaps,
        "note": (
            "Projection compounds existing net worth at the assumed annual return and "
            "adds the average monthly savings each month. Does not model inflation, "
            "variable income/spending, or tax drag. "
            "A negative monthly_savings means you are currently spending more than you earn."
        ),
    }
