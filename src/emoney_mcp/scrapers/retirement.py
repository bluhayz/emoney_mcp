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

run_monte_carlo_retirement(http_session, ...)
    Runs Monte Carlo simulations (default 1,000 paths) to estimate the
    probability that a retirement portfolio survives a given number of years.
    Uses stochastic annual returns drawn from a normal distribution parameterized
    by mean_return / std_dev, with independent inflation draws each year.
    Returns probability of success, median/10th/90th percentile ending balances,
    worst-case depletion year, and per-year balance percentiles.

get_dynamic_withdrawal_guardrails(http_session, ...)
    Implements the Guyton-Klinger guardrail rules: raises withdrawals when the
    portfolio is outperforming and cuts them when it's underperforming.  Compares
    the current portfolio value against an initial reference (or the estimated
    value at retirement) and returns the guardrail-adjusted annual and monthly
    withdrawal amounts.
"""

import asyncio
import math
import random
import statistics
from datetime import datetime

from .accounts import (
    get_accounts, _calc_investable_assets, get_net_worth_breakdown,
    get_retirement_accounts,
)
from .goals import get_goals
from .spending import _fetch_snb_data, get_savings_rate, _sum_income_spending
from .tax import _compute_tax, _STD_DEDUCTION, _pretax_rmd_balance, _rmd_factor


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
        # Nothing to deplete: already at/below zero. Avoids a ZeroDivisionError in
        # the portfolio*real_return denominator below when investable assets are $0
        # (e.g. net worth held entirely in real estate).
        if portfolio <= 0:
            return 0.0 if withdrawal > 0 else None
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
            "years_to_depletion": round(years, 1) if years is not None else None,
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


# ---------------------------------------------------------------------------
# run_monte_carlo_retirement
# ---------------------------------------------------------------------------

async def run_monte_carlo_retirement(
    http_session,
    simulations: int = 1_000,
    years: int = 30,
    annual_spending: float | None = None,
    mean_return: float = 0.07,
    std_dev: float = 0.15,
    inflation_mean: float = 0.03,
    inflation_std: float = 0.01,
    social_security_annual: float = 0.0,
    withdrawal_rate: float | None = None,
) -> dict:
    """
    Monte Carlo retirement simulation using stochastic annual returns and inflation.

    Parameters
    ----------
    simulations            : number of simulation paths (default 1,000)
    years                  : retirement horizon in years (default 30)
    annual_spending        : annual withdrawal in dollars (default: actual 12-month spend)
    mean_return            : mean annual portfolio return, nominal (default 0.07)
    std_dev                : annual return standard deviation (default 0.15 — blended equity/bond)
    inflation_mean         : mean annual inflation rate (default 0.03)
    inflation_std          : inflation standard deviation (default 0.01)
    social_security_annual : annual Social Security or pension income to offset withdrawals (default 0)
    withdrawal_rate        : if supplied, overrides annual_spending (e.g. 0.04 = 4% of portfolio)
    """
    simulations = max(100, min(simulations, 10_000))
    years       = max(5,   min(years,       60))

    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts

    total_assets  = accts.get("total_assets") or 0
    total_liab    = accts.get("total_liabilities") or 0
    portfolio     = max(0.0, total_assets - total_liab)

    if portfolio <= 0:
        return {"error": "No investable portfolio found."}

    if withdrawal_rate is not None:
        annual_spending = portfolio * withdrawal_rate
    elif annual_spending is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        if ok:
            annual_spending = round(sum(
                t["amount"] for t in txns
                if not t["is_income"] and not t["is_excluded"]
            ), 2)
        else:
            annual_spending = portfolio * 0.04

    net_annual_withdrawal = max(0.0, annual_spending - social_security_annual)

    rng = random.Random(42)   # reproducible seed so same inputs → same result

    ending_balances: list[float] = []
    depletion_years: list[int]   = []
    year_balances: list[list[float]] = [[] for _ in range(years)]

    for _ in range(simulations):
        bal      = portfolio
        depleted = False
        spending = net_annual_withdrawal

        for yr in range(years):
            ret  = rng.gauss(mean_return, std_dev)
            inf  = max(0.0, rng.gauss(inflation_mean, inflation_std))
            bal  = bal * (1 + ret) - spending
            spending *= (1 + inf)

            year_balances[yr].append(max(0.0, bal))

            if bal <= 0 and not depleted:
                depleted = True
                depletion_years.append(yr + 1)
                bal = 0.0

        ending_balances.append(max(0.0, bal))

    successes    = sum(1 for b in ending_balances if b > 0)
    success_rate = round(successes / simulations * 100, 1)

    sorted_end   = sorted(ending_balances)
    n            = len(sorted_end)
    median_end   = round(statistics.median(sorted_end), 2)
    p10_end      = round(sorted_end[int(n * 0.10)], 2)
    p25_end      = round(sorted_end[int(n * 0.25)], 2)
    p75_end      = round(sorted_end[int(n * 0.75)], 2)
    p90_end      = round(sorted_end[int(n * 0.90)], 2)

    worst_depletion = min(depletion_years) if depletion_years else None
    median_depletion = round(statistics.median(depletion_years), 1) if depletion_years else None

    year_summary = []
    for yr, balances in enumerate(year_balances):
        s = sorted(balances)
        m = len(s)
        year_summary.append({
            "year":   yr + 1,
            "p10":    round(s[int(m * 0.10)], 2),
            "median": round(statistics.median(s), 2),
            "p90":    round(s[int(m * 0.90)], 2),
        })

    # Find the SWR (to nearest 0.25%) that achieves 90% success
    safe_swr = None
    for candidate_rate_bp in range(500, 100, -25):
        candidate_rate = candidate_rate_bp / 10_000
        cand_withdrawal = portfolio * candidate_rate - social_security_annual
        cand_successes = 0
        for _ in range(200):
            bal      = portfolio
            spending = cand_withdrawal
            ok       = True
            for _yr in range(years):
                ret  = rng.gauss(mean_return, std_dev)
                inf  = max(0.0, rng.gauss(inflation_mean, inflation_std))
                bal  = bal * (1 + ret) - spending
                spending *= (1 + inf)
                if bal <= 0:
                    ok = False
                    break
            if ok:
                cand_successes += 1
        if cand_successes / 200 >= 0.90:
            safe_swr = candidate_rate
            break

    return {
        "portfolio_value":              round(portfolio, 2),
        "annual_spending":              round(annual_spending, 2),
        "social_security_annual":       round(social_security_annual, 2),
        "net_annual_withdrawal":        round(net_annual_withdrawal, 2),
        "current_withdrawal_rate_pct":  round(net_annual_withdrawal / portfolio * 100, 2) if portfolio else None,
        "simulation_parameters": {
            "simulations":     simulations,
            "years":           years,
            "mean_return_pct": round(mean_return * 100, 1),
            "std_dev_pct":     round(std_dev * 100, 1),
            "inflation_mean_pct": round(inflation_mean * 100, 1),
            "inflation_std_pct":  round(inflation_std * 100, 1),
        },
        "results": {
            "probability_of_success_pct": success_rate,
            "outcome_label": (
                "Excellent" if success_rate >= 90 else
                "Good"      if success_rate >= 80 else
                "Caution"   if success_rate >= 70 else
                "At Risk"   if success_rate >= 60 else
                "Danger"
            ),
            "simulations_succeeded":  successes,
            "simulations_depleted":   simulations - successes,
            "ending_balance": {
                "p10":    p10_end,
                "p25":    p25_end,
                "median": median_end,
                "p75":    p75_end,
                "p90":    p90_end,
            },
            "depletion": {
                "pct_depleted":           round((simulations - successes) / simulations * 100, 1),
                "earliest_depletion_year": worst_depletion,
                "median_depletion_year":   median_depletion,
            },
            "safe_withdrawal_rate_for_90pct_success_pct": (
                round(safe_swr * 100, 2) if safe_swr else None
            ),
        },
        "year_by_year_percentiles": year_summary,
        "interpretation": (
            f"At a {round(net_annual_withdrawal/portfolio*100,1)}% withdrawal rate "
            f"this portfolio has a {success_rate}% chance of lasting {years} years "
            f"across {simulations:,} simulated market scenarios. "
            f"The median ending balance is ${median_end:,.0f}; "
            f"in the worst 10% of scenarios the portfolio ends at ${p10_end:,.0f}."
        ),
        "note": (
            "Returns are drawn each year from a normal distribution — extreme sequences "
            "(e.g. a 2008-style crash in year 1) naturally occur. "
            "std_dev=0.15 approximates a 60/40 blended portfolio; use 0.18–0.20 for all-equity. "
            "Social Security reduces the net withdrawal each year, significantly improving success rates. "
            "This is a statistical model — actual outcomes depend on sequence of returns, fees, taxes, "
            "and spending flexibility."
        ),
    }


# ---------------------------------------------------------------------------
# get_dynamic_withdrawal_guardrails
# ---------------------------------------------------------------------------

async def get_dynamic_withdrawal_guardrails(
    http_session,
    initial_withdrawal_rate: float = 0.05,
    raise_ceiling_pct: float = 20.0,
    cut_floor_pct: float = 20.0,
    raise_guard_pct: float = 20.0,
    cut_guard_pct: float = 20.0,
    initial_portfolio_value: float | None = None,
    current_annual_withdrawal: float | None = None,
) -> dict:
    """
    Apply Guyton-Klinger guardrail rules to determine whether to raise, cut,
    or hold the current withdrawal amount.

    The rules compare the current withdrawal rate (withdrawal / current portfolio)
    against upper and lower guardrail thresholds.  If the current rate drifts
    more than ``raise_guard_pct``% below the initial rate → raise withdrawals 10%.
    If it drifts more than ``cut_guard_pct``% above the initial rate → cut 10%.

    Parameters
    ----------
    initial_withdrawal_rate   : withdrawal rate at retirement start (default 5%)
    raise_ceiling_pct         : max % a raised withdrawal can be above initial (default 20%)
    cut_floor_pct             : max % a cut withdrawal can be below initial (default 20%)
    raise_guard_pct           : how far rate must drop below initial to trigger a raise (default 20%)
    cut_guard_pct             : how far rate must rise above initial to trigger a cut (default 20%)
    initial_portfolio_value   : portfolio value at retirement start (optional; uses current if omitted)
    current_annual_withdrawal : override the inferred annual withdrawal (optional)
    """
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts

    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0
    current_portfolio = max(0.0, total_assets - total_liab)

    if current_portfolio <= 0:
        return {"error": "No investable portfolio found."}

    ref_portfolio = initial_portfolio_value or current_portfolio
    initial_dollar_withdrawal = ref_portfolio * initial_withdrawal_rate

    if current_annual_withdrawal is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        if ok:
            current_annual_withdrawal = round(sum(
                t["amount"] for t in txns
                if not t["is_income"] and not t["is_excluded"]
            ), 2)
        else:
            current_annual_withdrawal = initial_dollar_withdrawal

    current_rate = current_annual_withdrawal / current_portfolio if current_portfolio > 0 else 0.0
    upper_guard  = initial_withdrawal_rate * (1 + cut_guard_pct  / 100)
    lower_guard  = initial_withdrawal_rate * (1 - raise_guard_pct / 100)
    ceiling_amt  = initial_dollar_withdrawal * (1 + raise_ceiling_pct / 100)
    floor_amt    = initial_dollar_withdrawal * (1 - cut_floor_pct   / 100)

    if current_rate > upper_guard:
        action          = "CUT"
        adjusted_annual = max(floor_amt, current_annual_withdrawal * 0.90)
        reason          = (
            f"Current withdrawal rate ({current_rate*100:.2f}%) exceeds the upper guardrail "
            f"({upper_guard*100:.2f}%). Reduce withdrawals by 10% to protect the portfolio."
        )
    elif current_rate < lower_guard:
        action          = "RAISE"
        adjusted_annual = min(ceiling_amt, current_annual_withdrawal * 1.10)
        reason          = (
            f"Current withdrawal rate ({current_rate*100:.2f}%) is below the lower guardrail "
            f"({lower_guard*100:.2f}%). Portfolio is outperforming — raise withdrawals 10%."
        )
    else:
        action          = "HOLD"
        adjusted_annual = current_annual_withdrawal
        reason          = (
            f"Current withdrawal rate ({current_rate*100:.2f}%) is within guardrails "
            f"({lower_guard*100:.2f}% – {upper_guard*100:.2f}%). No adjustment needed."
        )

    portfolio_change_pct = round(
        (current_portfolio - ref_portfolio) / ref_portfolio * 100, 1
    ) if ref_portfolio else None

    return {
        "current_portfolio_value":      round(current_portfolio, 2),
        "reference_portfolio_value":    round(ref_portfolio, 2),
        "portfolio_change_pct":         portfolio_change_pct,
        "initial_withdrawal_rate_pct":  round(initial_withdrawal_rate * 100, 2),
        "current_annual_withdrawal":    round(current_annual_withdrawal, 2),
        "current_withdrawal_rate_pct":  round(current_rate * 100, 2),
        "guardrails": {
            "upper_guard_pct":   round(upper_guard * 100, 2),
            "lower_guard_pct":   round(lower_guard * 100, 2),
            "ceiling_amount":    round(ceiling_amt, 2),
            "floor_amount":      round(floor_amt, 2),
        },
        "action":                    action,
        "reason":                    reason,
        "adjusted_annual_withdrawal": round(adjusted_annual, 2),
        "adjusted_monthly_withdrawal": round(adjusted_annual / 12, 2),
        "change_from_current":        round(adjusted_annual - current_annual_withdrawal, 2),
        "change_pct":                 round((adjusted_annual - current_annual_withdrawal) / current_annual_withdrawal * 100, 1) if current_annual_withdrawal else 0,
        "note": (
            "Guyton-Klinger guardrails dynamically adjust withdrawals to extend portfolio longevity. "
            "A 10% raise/cut is applied each time a guardrail is breached. "
            "The ceiling prevents withdrawals from rising more than raise_ceiling_pct% above the initial amount; "
            "the floor prevents cuts below cut_floor_pct% of the initial amount. "
            "Run this annually (or after a major market move) to keep withdrawals on track."
        ),
    }


# ---------------------------------------------------------------------------
# run_scenario  (v0.8.0)
# ---------------------------------------------------------------------------

async def run_scenario(
    http_session,
    monthly_savings_delta: float = 0.0,
    target_net_worth: float | None = None,
    retirement_age: int | None = None,
    annual_return_pct: float | None = None,
) -> dict:
    """
    Run a what-if scenario alongside a baseline projection and compare results.

    Parameters
    ----------
    monthly_savings_delta : change in monthly savings vs. current (e.g. +500 or -200)
    target_net_worth      : target balance to reach (defaults to retirement goal from Emoney)
    retirement_age        : override the retirement goal age
    annual_return_pct     : override the assumed annual return (e.g. 8 for 8%; default 7)
    """
    accts, savings_result, goals_result = await asyncio.gather(
        get_accounts(http_session),
        get_savings_rate(http_session, months=6),
        get_goals(http_session),
    )

    if "error" in accts:
        return accts

    current_nw = accts.get("net_worth") or 0

    # Monthly savings baseline
    if "error" not in savings_result:
        total_net    = savings_result.get("total_net", 0) or 0
        months_shown = savings_result.get("months_shown", 6) or 6
        baseline_monthly_savings = round(total_net / months_shown, 2)
    else:
        baseline_monthly_savings = 0.0

    # Retirement target and age from goals
    ret_goal_start = None
    if "error" not in goals_result:
        for g in goals_result.get("retirement_goals", []):
            if g.get("start_year"):
                ret_goal_start = g["start_year"]
                break

    current_year  = datetime.now().year
    current_month = datetime.now().month

    base_return  = (annual_return_pct / 100) if annual_return_pct else 0.07
    scen_return  = base_return
    base_monthly = baseline_monthly_savings
    scen_monthly = round(baseline_monthly_savings + monthly_savings_delta, 2)

    # Determine the target
    eff_target = target_net_worth
    if eff_target is None and ret_goal_start:
        years_to_ret = max(0, ret_goal_start - current_year)
        eff_target = round(max(current_nw, 0) * ((1 + base_return) ** years_to_ret), 2)

    def _project(nw, monthly_sav, annual_ret, target):
        """Month-by-month projection returning yearly snapshots and target hit info."""
        monthly_ret = annual_ret / 12
        balance = float(nw)
        target_hit = None
        yearly_snaps = []

        _MILESTONES = [500_000, 1_000_000, 2_000_000, 5_000_000, 10_000_000]
        milestones_hit = {}

        for mo in range(1, 50 * 12 + 1):
            balance = round(balance * (1 + monthly_ret) + monthly_sav, 2)
            proj_year  = current_year + (current_month + mo - 1) // 12
            proj_month = (current_month + mo - 1) % 12 + 1

            for ms in _MILESTONES:
                if ms not in milestones_hit and balance >= ms:
                    milestones_hit[ms] = {"milestone": ms, "year": proj_year, "years_away": round(mo / 12, 1)}

            if target and target_hit is None and balance >= target:
                target_hit = {
                    "target":      target,
                    "year":        proj_year,
                    "month":       proj_month,
                    "years_away":  round(mo / 12, 1),
                }

            if mo % 12 == 0:
                yearly_snaps.append({"year": proj_year, "net_worth": round(balance, 2)})

            all_done = len(milestones_hit) == len(_MILESTONES) and (target is None or target_hit is not None)
            if all_done:
                break

        return yearly_snaps[:30], target_hit, milestones_hit

    base_snaps,  base_target,  base_ms  = _project(current_nw, base_monthly, base_return, eff_target)
    scen_snaps,  scen_target,  scen_ms  = _project(current_nw, scen_monthly, scen_return, eff_target)

    # Comparison year: retirement goal start, or retirement_age + offset, or 20y out
    if ret_goal_start:
        compare_year = ret_goal_start
    elif retirement_age:
        compare_year = current_year + max(0, retirement_age - 40)
    else:
        compare_year = current_year + 20

    base_at_compare = next((s["net_worth"] for s in base_snaps if s["year"] >= compare_year), None)
    scen_at_compare = next((s["net_worth"] for s in scen_snaps if s["year"] >= compare_year), None)

    years_delta = None
    if base_target and scen_target:
        years_delta = round(base_target["years_away"] - scen_target["years_away"], 1)

    nw_delta = None
    if base_at_compare is not None and scen_at_compare is not None:
        nw_delta = round(scen_at_compare - base_at_compare, 2)

    _MILESTONE_KEYS = [500_000, 1_000_000, 2_000_000, 5_000_000]
    milestone_comparison = []
    for ms in _MILESTONE_KEYS:
        bm = base_ms.get(ms, {})
        sm = scen_ms.get(ms, {})
        milestone_comparison.append({
            "milestone":       ms,
            "baseline_year":   bm.get("year"),
            "scenario_year":   sm.get("year"),
            "years_earlier":   round(bm.get("years_away", 0) - sm.get("years_away", 0), 1)
                               if bm and sm else None,
        })

    return {
        "current_net_worth":       round(current_nw, 2),
        "baseline": {
            "monthly_savings":     base_monthly,
            "annual_return_pct":   round(base_return * 100, 1),
            "target_reached":      base_target,
            "net_worth_at_compare_year": base_at_compare,
        },
        "scenario": {
            "monthly_savings_delta": monthly_savings_delta,
            "monthly_savings":     scen_monthly,
            "annual_return_pct":   round(scen_return * 100, 1),
            "target_reached":      scen_target,
            "net_worth_at_compare_year": scen_at_compare,
        },
        "compare_year":            compare_year,
        "delta": {
            "years_to_target_earlier":    years_delta,
            "additional_net_worth_at_compare": nw_delta,
        },
        "target_net_worth":        eff_target,
        "milestone_comparison":    milestone_comparison,
        "note": (
            "Projection compounds existing net worth at the assumed annual return "
            "and adds the monthly savings each month. Does not model inflation, taxes, "
            "or variable income. monthly_savings_delta adjusts the baseline savings amount."
        ),
    }


async def get_financial_independence_roadmap(
    http_session,
    current_age: int | None = None,
    retirement_age: int = 65,
) -> dict:
    """
    Show progress against Fidelity's salary-multiple retirement milestones and
    compute the Coast FI number.

    Fidelity benchmarks (investable assets as multiple of gross income):
      Age 30 → 1×   Age 35 → 2×   Age 40 → 3×
      Age 45 → 4×   Age 50 → 6×   Age 55 → 7×
      Age 60 → 8×   Age 65 → 10×

    Coast FI: The portfolio value needed today such that 7% compounding alone
    reaches the FI number (25× spending) by retirement_age — no further
    contributions required.

    Parameters
    ----------
    current_age   : your current age (optional; enables age-based milestone lookup)
    retirement_age: target retirement age for Coast FI calculation (default 65)
    """
    accts_data, sr_data = await asyncio.gather(
        get_accounts(http_session),
        get_savings_rate(http_session, months=6),
    )
    from .spending import _fetch_snb_data
    txns, snb_ok = await _fetch_snb_data(http_session, days=365)

    if "error" in accts_data:
        return accts_data

    investable = _calc_investable_assets(accts_data)

    # Annual income and spending from SNB
    annual_income, annual_spending = _sum_income_spending(txns) if snb_ok else (0.0, 0.0)

    # Monthly savings from savings_rate tool
    monthly_savings = 0.0
    if "error" not in sr_data:
        net_total = sr_data.get("total_net", 0) or 0
        months_sh = sr_data.get("months_shown", 6) or 6
        monthly_savings = round(net_total / months_sh, 2)

    # Fidelity benchmarks
    fidelity_benchmarks = [
        (30, 1), (35, 2), (40, 3), (45, 4),
        (50, 6), (55, 7), (60, 8), (65, 10),
    ]
    benchmarks_out = []
    current_milestone = None
    next_milestone    = None

    for age, multiplier in fidelity_benchmarks:
        target  = round(annual_income * multiplier, 2) if annual_income > 0 else None
        gap     = round(investable - target, 2) if target is not None else None
        on_track = gap is not None and gap >= 0

        entry = {
            "age":        age,
            "multiplier": multiplier,
            "target":     target,
            "gap":        gap,
            "on_track":   on_track,
        }
        benchmarks_out.append(entry)

        if current_age is not None:
            if age <= current_age and on_track:
                current_milestone = entry
            elif age > current_age and next_milestone is None:
                next_milestone = entry

    # FI number: 25× spending
    fi_number = round(annual_spending / 0.04, 2) if annual_spending > 0 else None
    fi_gap    = round(max(0, fi_number - investable), 2) if fi_number else None

    # Years to FI at current savings (future-value iteration)
    years_to_fi = None
    if fi_gap and fi_gap > 0 and monthly_savings > 0:
        assert fi_number is not None  # fi_gap is non-None only when fi_number is
        r = 0.07 / 12
        balance = investable
        n = 0
        while balance < fi_number and n < 600:
            balance = balance * (1 + r) + monthly_savings
            n += 1
        if balance >= fi_number:
            years_to_fi = round(n / 12, 1)

    # Coast FI: amount needed today so growth alone reaches fi_number by retirement_age
    coast_fi_target = None
    coast_gap       = None
    if fi_number and current_age and retirement_age > current_age:
        years_left    = retirement_age - current_age
        coast_fi_target = round(fi_number / ((1.07) ** years_left), 2)
        coast_gap       = round(max(0, coast_fi_target - investable), 2)

    return {
        "as_of":                   datetime.now().strftime("%Y-%m-%d"),
        "current_age":             current_age,
        "retirement_age":          retirement_age,
        "annual_income":           annual_income,
        "annual_spending":         annual_spending,
        "investable_assets":       investable,
        "monthly_savings":         monthly_savings,
        "fidelity_benchmarks":     benchmarks_out,
        "current_milestone":       current_milestone,
        "next_milestone":          next_milestone,
        "fi_number":               fi_number,
        "fi_gap":                  fi_gap,
        "years_to_fi_at_current_pace": years_to_fi,
        "coast_fi": {
            "target_today":    coast_fi_target,
            "current_assets":  investable,
            "gap":             coast_gap,
            "description": (
                f"If you have ${coast_fi_target:,.0f} invested today, 7% growth alone will reach "
                f"your FI number by age {retirement_age} — no further contributions needed."
                if coast_fi_target else "Provide current_age to calculate Coast FI."
            ),
        },
        "note": (
            "Fidelity benchmarks: investable assets as a multiple of gross annual income. "
            "FI number = 25× annual spending (4% SWR). Coast FI assumes 7% annual return. "
            "Investable assets exclude real-estate equity."
        ),
    }


# ---------------------------------------------------------------------------
# get_withdrawal_sequencing_strategy
# ---------------------------------------------------------------------------

async def get_withdrawal_sequencing_strategy(
    http_session,
    annual_need: float,
    filing_status: str = "mfj",
    years: int = 30,
    taxable_gain_fraction: float = 0.5,
    growth_rate: float = 0.05,
) -> dict:
    """
    Compare a tax-efficient withdrawal order (taxable → tax-deferred → Roth)
    against a naive proportional drawdown, and estimate the lifetime tax saved.

    Account balances by tax treatment come from get_net_worth_breakdown.

    Parameters
    ----------
    annual_need           : annual portfolio withdrawal needed (after other income)
    filing_status         : single | mfj | hoh (default mfj)
    years                 : simulation horizon (default 30)
    taxable_gain_fraction : fraction of a taxable-account withdrawal that is
                            embedded gain (taxed at LTCG); the rest is basis
                            (default 0.5)
    growth_rate           : annual portfolio growth assumption (default 0.05)
    """
    years = max(1, min(years, 50))
    fs = filing_status if filing_status in _STD_DEDUCTION else "mfj"
    std = _STD_DEDUCTION[fs]
    ltcg_rate = 0.15

    breakdown = await get_net_worth_breakdown(http_session)
    if "error" in breakdown:
        return breakdown
    buckets = {b["bucket"]: b["value"] for b in breakdown.get("by_tax_treatment", [])}
    start = {
        "taxable":  max(0.0, buckets.get("Taxable", 0.0)),
        "deferred": max(0.0, buckets.get("Tax-Deferred", 0.0)),
        "roth":     max(0.0, buckets.get("Tax-Free", 0.0)),
    }
    if sum(start.values()) <= 0:
        return {"error": "No investable balances found to model withdrawals."}

    def _year_tax(w_taxable: float, w_deferred: float) -> float:
        # Taxable: only the embedded gain is taxed (LTCG). Deferred: ordinary
        # income (after the standard deduction). Roth: tax-free.
        cg = w_taxable * taxable_gain_fraction * ltcg_rate
        ordinary = _compute_tax(max(0.0, w_deferred - std), fs)
        return round(cg + ordinary, 2)

    def _simulate(proportional: bool) -> dict:
        bal = dict(start)
        total_tax = 0.0
        lasted = 0
        for _y in range(years):
            for k in bal:
                bal[k] = round(bal[k] * (1 + growth_rate), 2)
            total = sum(bal.values())
            if total <= 0:
                break
            need = min(annual_need, total)
            draw = {"taxable": 0.0, "deferred": 0.0, "roth": 0.0}
            if proportional:
                for k in bal:
                    draw[k] = round(need * bal[k] / total, 2)
            else:
                remaining = need
                for k in ("taxable", "deferred", "roth"):   # tax-efficient order
                    take = min(remaining, bal[k])
                    draw[k] = round(take, 2)
                    remaining -= take
                    if remaining <= 0:
                        break
            for k in bal:
                bal[k] = round(bal[k] - draw[k], 2)
            total_tax += _year_tax(draw["taxable"], draw["deferred"])
            lasted = _y + 1
            if sum(bal.values()) <= 0:
                break
        return {"total_tax": round(total_tax, 2), "years_funded": lasted,
                "ending_balance": round(sum(bal.values()), 2)}

    efficient = _simulate(proportional=False)
    proportional = _simulate(proportional=True)
    tax_saved = round(proportional["total_tax"] - efficient["total_tax"], 2)

    return {
        "as_of":             datetime.now().strftime("%Y-%m-%d"),
        "annual_need":       round(annual_need, 2),
        "filing_status":     fs,
        "horizon_years":     years,
        "starting_balances": {k: round(v, 2) for k, v in start.items()},
        "recommended_order": ["taxable", "tax_deferred", "roth"],
        "tax_efficient_strategy": efficient,
        "proportional_strategy":  proportional,
        "estimated_lifetime_tax_saved": tax_saved,
        "note": (
            "Simplified model: taxable withdrawals taxed only on the embedded gain "
            "(taxable_gain_fraction at 15% LTCG), tax-deferred at ordinary rates "
            "(after the standard deduction), Roth tax-free. Ignores RMDs, IRMAA, "
            "and the value of leaving Roth to compound — drawing tax-deferred down "
            "in low-bracket years (see get_roth_conversion_ladder) can beat the "
            "strict order. State tax not modeled."
        ),
    }


# ---------------------------------------------------------------------------
# get_retirement_income_plan
# ---------------------------------------------------------------------------

async def get_retirement_income_plan(
    http_session,
    retire_age: int,
    birth_year: int,
    annual_spending: float | None = None,
    social_security_annual: float = 0.0,
    ss_claim_age: int = 67,
    pension_annual: float = 0.0,
    pension_start_age: int = 65,
    years: int = 30,
    growth_rate: float = 0.05,
) -> dict:
    """
    Year-by-year retirement income plan: guaranteed income (Social Security +
    pension) netted against the spending need, with the required portfolio
    withdrawal and resulting withdrawal rate for each year.

    Parameters
    ----------
    retire_age             : age at which retirement (portfolio drawdown) begins
    birth_year             : year of birth
    annual_spending        : retirement spending need (default: 12-mo actual from SNB)
    social_security_annual : annual SS benefit
    ss_claim_age           : age SS begins (default 67)
    pension_annual         : annual pension benefit (default 0)
    pension_start_age      : age pension begins (default 65)
    years                  : plan horizon (default 30)
    growth_rate            : annual portfolio growth assumption (default 0.05)
    """
    years = max(1, min(years, 50))
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts
    portfolio = _calc_investable_assets(accts)

    if annual_spending is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        if ok:
            _income, annual_spending = _sum_income_spending(txns)
        else:
            annual_spending = 0.0
    if not annual_spending or annual_spending <= 0:
        return {"error": "Could not determine annual spending. Pass annual_spending explicitly."}

    rows = []
    depletion_age = None
    bal = portfolio
    for i in range(years):
        age = retire_age + i
        ss = social_security_annual if age >= ss_claim_age else 0.0
        pension = pension_annual if age >= pension_start_age else 0.0
        guaranteed = round(ss + pension, 2)
        gap = round(max(0.0, annual_spending - guaranteed), 2)
        withdrawal = min(gap, max(0.0, bal))
        wr = round(withdrawal / bal * 100, 2) if bal > 0 else None
        bal = round((bal - withdrawal) * (1 + growth_rate), 2)
        if bal <= 0 and depletion_age is None:
            depletion_age = age
        rows.append({
            "age":               age,
            "year":              datetime.now().year + i,
            "guaranteed_income": guaranteed,
            "social_security":   round(ss, 2),
            "pension":           round(pension, 2),
            "spending_need":     round(annual_spending, 2),
            "portfolio_withdrawal": round(withdrawal, 2),
            "withdrawal_rate_pct":  wr,
            "end_portfolio":     max(0.0, bal),
        })

    first = rows[0]
    return {
        "as_of":            datetime.now().strftime("%Y-%m-%d"),
        "retire_age":       retire_age,
        "starting_portfolio": round(portfolio, 2),
        "annual_spending":  round(annual_spending, 2),
        "first_year_withdrawal_rate_pct": first["withdrawal_rate_pct"],
        "depletion_age":    depletion_age,
        "plan":             rows,
        "note": (
            "Guaranteed income (Social Security + pension) is subtracted from the "
            "spending need; the remainder is the required portfolio withdrawal. "
            "Portfolio = investable assets (excludes real-estate equity), growing at "
            f"{round(growth_rate*100,1)}% after withdrawals. Inflation, taxes on "
            "withdrawals, and RMDs are not modeled here — see get_retirement_runway "
            "and get_withdrawal_sequencing_strategy."
        ),
    }


# ---------------------------------------------------------------------------
# get_income_sources_timeline  (#85)
# ---------------------------------------------------------------------------

async def get_income_sources_timeline(
    http_session,
    birth_year: int,
    retirement_age: int | None = None,
    social_security_annual: float = 0.0,
    ss_start_age: int = 67,
    pension_annual: float = 0.0,
    pension_start_age: int | None = None,
    annuity_annual: float = 0.0,
    annuity_start_age: int | None = None,
    mortgage_payment_monthly: float = 0.0,
    mortgage_payoff_age: int | None = None,
) -> dict:
    """
    Build a chronological timeline of when each retirement income stream switches
    on (and when a major liability falls away, freeing cash flow).

    Surfaces the retirement transition at a glance — especially "bridge" gap
    years between leaving work and the first guaranteed income, and the jump in
    taxable income when RMDs begin at 73.

    Events modeled: wages stopping at ``retirement_age``; Social Security at
    ``ss_start_age``; a pension and/or annuity at their start ages; RMDs at 73
    (estimated from the pre-tax IRA/401k balance pulled from Emoney, grown to 73
    at 6%); and the monthly mortgage payment ending at ``mortgage_payoff_age``
    (which frees, not adds, that cash flow).

    Parameters
    ----------
    birth_year             : year of birth (e.g. 1962) — from get_client_profile
    retirement_age         : age wages stop (default: not modeled)
    social_security_annual : annual SS benefit in today's dollars
    ss_start_age           : age SS begins (default 67)
    pension_annual         : annual pension income (default 0 = none)
    pension_start_age      : age the pension begins (defaults to retirement_age)
    annuity_annual         : annual annuity payout (default 0 = none)
    annuity_start_age      : age the annuity begins
    mortgage_payment_monthly : current mortgage P&I payment (frees cash at payoff)
    mortgage_payoff_age    : age the mortgage is paid off
    """
    current_year = datetime.now().year
    current_age = current_year - birth_year
    if current_age < 0 or current_age > 120:
        return {"error": "birth_year looks implausible — check the value."}

    events: list[dict] = []

    def _add(age, label, source, annual_amount, kind):
        if age is None:
            return
        events.append({
            "age":           int(age),
            "year":          current_year + (int(age) - current_age),
            "event":         label,
            "source":        source,
            "annual_amount": round(annual_amount, 2) if annual_amount is not None else None,
            "type":          kind,   # income_start | income_end | cash_flow_freed
        })

    if retirement_age is not None:
        _add(retirement_age, "Wages stop (retirement)", "Employment", None, "income_end")

    if social_security_annual and social_security_annual > 0:
        _add(ss_start_age, "Social Security begins", "Social Security",
             social_security_annual, "income_start")

    if pension_annual and pension_annual > 0:
        p_age = pension_start_age if pension_start_age is not None else retirement_age
        _add(p_age, "Pension begins", "Pension", pension_annual, "income_start")

    if annuity_annual and annuity_annual > 0:
        _add(annuity_start_age, "Annuity payout begins", "Annuity",
             annuity_annual, "income_start")

    # RMDs at 73 — estimate first-year amount from the pre-tax balance.
    rmd_first_year = None
    rmd_note = None
    retirement = await get_retirement_accounts(http_session)
    if "error" not in retirement:
        pretax = _pretax_rmd_balance(retirement)
        if pretax > 0:
            years_to_73 = max(0, 73 - current_age)
            bal_at_73 = pretax * (1.06 ** years_to_73)
            rmd_first_year = round(bal_at_73 / _rmd_factor(73), 2)
            _add(73, "Required Minimum Distributions begin", "Pre-tax IRA/401k",
                 rmd_first_year, "income_start")
        else:
            rmd_note = "No pre-tax IRA/401k balance found — no RMDs modeled."
    else:
        rmd_note = "Pre-tax balance unavailable (session?) — RMD event not modeled."

    if mortgage_payment_monthly and mortgage_payment_monthly > 0 and mortgage_payoff_age is not None:
        _add(mortgage_payoff_age, "Mortgage paid off (cash flow freed)", "Mortgage",
             mortgage_payment_monthly * 12, "cash_flow_freed")

    events.sort(key=lambda e: (e["age"], e["type"] != "income_end"))

    # Identify the bridge gap: from retirement to the first guaranteed income start.
    bridge = None
    if retirement_age is not None:
        starts = [e["age"] for e in events
                  if e["type"] == "income_start" and e["age"] >= retirement_age]
        if starts:
            first_income_age = min(starts)
            if first_income_age > retirement_age:
                bridge = {
                    "from_age":   retirement_age,
                    "to_age":     first_income_age,
                    "gap_years":  first_income_age - retirement_age,
                    "detail": (
                        f"From age {retirement_age} to {first_income_age} there is no new "
                        f"guaranteed income switching on — this 'bridge' period must be funded "
                        f"from the portfolio. It is also the prime Roth-conversion / gain-harvest "
                        f"window (low taxable income before SS and RMDs). See get_roth_conversion_ladder."
                    ),
                }

    return {
        "as_of":            datetime.now().strftime("%Y-%m-%d"),
        "birth_year":       birth_year,
        "current_age":      current_age,
        "timeline":         events,
        "bridge_gap":       bridge,
        "first_year_rmd_estimate": rmd_first_year,
        "rmd_note":         rmd_note,
        "note": (
            "Amounts are in today's dollars and reflect the income that switches on at each "
            "event (not a running total). 'cash_flow_freed' marks a liability ending, which "
            "improves cash flow rather than adding taxable income. RMDs are estimated from the "
            "pre-tax balance grown to age 73 at 6%. Provide Social Security / pension / annuity "
            "figures to populate those events; get them from get_social_security_optimizer and "
            "your benefit statements."
        ),
    }


# ---------------------------------------------------------------------------
# get_sequence_of_returns_stress_test  (#98)
# ---------------------------------------------------------------------------

# S&P 500 total annual returns (approx, dividends reinvested) — used to build
# real adverse-sequence overlays. Refresh the tail occasionally; the early years
# (the 2000 and 2008 crashes) are what drive the stress test and are fixed history.
_SP500_ANNUAL: dict[int, float] = {
    2000: -0.091, 2001: -0.119, 2002: -0.221, 2003: 0.287, 2004: 0.109,
    2005: 0.049,  2006: 0.158,  2007: 0.055,  2008: -0.370, 2009: 0.265,
    2010: 0.151,  2011: 0.021,  2012: 0.160,  2013: 0.324,  2014: 0.137,
    2015: 0.014,  2016: 0.120,  2017: 0.218,  2018: -0.044, 2019: 0.315,
    2020: 0.184,  2021: 0.287,  2022: -0.181, 2023: 0.263,  2024: 0.250,
}


def _blended_sequence(start_year: int, years: int, equity_pct: float,
                      bond_return: float, mean_return: float) -> list[float]:
    """Blend historical S&P returns from ``start_year`` with a flat bond return,
    padding with ``mean_return`` once history runs out."""
    seq = []
    for i in range(years):
        sp = _SP500_ANNUAL.get(start_year + i)
        if sp is None:
            seq.append(mean_return)
        else:
            seq.append(equity_pct * sp + (1 - equity_pct) * bond_return)
    return seq


def _run_return_path(returns: list[float], portfolio: float, net_withdrawal: float,
                     inflation: float = 0.03) -> dict:
    """Deterministic withdrawal walk over a fixed return sequence."""
    bal = portfolio
    spending = net_withdrawal
    depletion_year = None
    min_balance = portfolio
    for yr, ret in enumerate(returns):
        bal = bal * (1 + ret) - spending
        spending *= (1 + inflation)
        if bal < min_balance:
            min_balance = bal
        if bal <= 0 and depletion_year is None:
            depletion_year = yr + 1
            bal = 0.0
            break
    return {
        "ending_balance": round(max(0.0, bal), 2),
        "depleted":       depletion_year is not None,
        "depletion_year": depletion_year,
        "min_balance":    round(min_balance, 2),
    }


async def get_sequence_of_returns_stress_test(
    http_session,
    years: int = 30,
    annual_spending: float | None = None,
    equity_pct: float = 0.6,
    bond_return: float = 0.03,
    mean_return: float = 0.07,
    social_security_annual: float = 0.0,
    withdrawal_rate: float | None = None,
) -> dict:
    """
    Expose sequence-of-returns risk that an averages-based Monte Carlo hides: a
    bad first decade can sink a plan that succeeds on average returns.

    Runs the SAME withdrawal plan over several fixed return paths that share a
    similar long-run average but differ in ORDER:
      - average      : a flat ``mean_return`` every year (the naive baseline)
      - adverse_2000 : the 2000–02 dot-com bust front-loaded (blended by equity_pct)
      - adverse_2008 : the 2008 crash front-loaded
      - favorable    : the adverse_2000 returns in REVERSE order (good decade first)

    The adverse vs. favorable contrast — identical returns, opposite order — is
    the clearest illustration of why withdrawal-phase investors fear a bad start.

    Parameters
    ----------
    years                  : retirement horizon (default 30)
    annual_spending        : annual withdrawal in dollars (default: actual 12-month spend)
    equity_pct             : equity weight used to blend S&P history with bonds (default 0.6)
    bond_return            : flat annual bond return for the non-equity sleeve (default 0.03)
    mean_return            : flat return for the 'average' baseline + history padding (default 0.07)
    social_security_annual : annual SS/pension offset to the withdrawal (default 0)
    withdrawal_rate        : if supplied, overrides annual_spending (e.g. 0.04 = 4% of portfolio)
    """
    years = max(5, min(years, 60))
    equity_pct = min(1.0, max(0.0, equity_pct))

    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts
    portfolio = max(0.0, (accts.get("total_assets") or 0) - (accts.get("total_liabilities") or 0))
    if portfolio <= 0:
        return {"error": "No investable portfolio found."}

    if withdrawal_rate is not None:
        annual_spending = portfolio * withdrawal_rate
    elif annual_spending is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        annual_spending = round(sum(
            t["amount"] for t in txns if not t["is_income"] and not t["is_excluded"]
        ), 2) if ok else portfolio * 0.04

    net_withdrawal = max(0.0, annual_spending - social_security_annual)

    seq_2000 = _blended_sequence(2000, years, equity_pct, bond_return, mean_return)
    seq_2008 = _blended_sequence(2008, years, equity_pct, bond_return, mean_return)
    seq_avg  = [mean_return] * years
    seq_fav  = list(reversed(seq_2000))

    scenarios = {
        "average":      {"label": "Flat average return", "returns": seq_avg},
        "adverse_2000": {"label": "Dot-com bust first (2000 start)", "returns": seq_2000},
        "adverse_2008": {"label": "Great Financial Crisis first (2008 start)", "returns": seq_2008},
        "favorable":    {"label": "Same as adverse_2000 but reversed (good decade first)", "returns": seq_fav},
    }

    results = {}
    for key, sc in scenarios.items():
        r = _run_return_path(sc["returns"], portfolio, net_withdrawal)
        avg_ret = round(sum(sc["returns"]) / len(sc["returns"]) * 100, 2)
        results[key] = {
            "label":              sc["label"],
            "avg_annual_return_pct": avg_ret,
            **r,
        }

    avg_end = results["average"]["ending_balance"]
    adv2000_end = results["adverse_2000"]["ending_balance"]
    fav_end = results["favorable"]["ending_balance"]
    # The 2000 vs favorable pair shares the identical return set.
    sequence_gap = round(fav_end - adv2000_end, 2)

    any_depleted = any(results[k]["depleted"] for k in ("adverse_2000", "adverse_2008"))

    return {
        "as_of":              datetime.now().strftime("%Y-%m-%d"),
        "portfolio_value":    round(portfolio, 2),
        "annual_spending":    round(annual_spending, 2),
        "social_security_annual": round(social_security_annual, 2),
        "net_annual_withdrawal":  round(net_withdrawal, 2),
        "withdrawal_rate_pct":    round(net_withdrawal / portfolio * 100, 2) if portfolio else None,
        "horizon_years":      years,
        "assumptions": {
            "equity_pct":      round(equity_pct * 100, 1),
            "bond_return_pct": round(bond_return * 100, 1),
            "mean_return_pct": round(mean_return * 100, 1),
            "inflation_pct":   3.0,
        },
        "scenarios":          results,
        "sequence_risk": {
            "identical_returns_outcome_gap": sequence_gap,
            "detail": (
                f"'adverse_2000' and 'favorable' use the SAME annual returns in opposite order, "
                f"yet end ${adv2000_end:,.0f} vs ${fav_end:,.0f} — a ${abs(sequence_gap):,.0f} swing "
                f"driven purely by sequence. The flat-average baseline ends ${avg_end:,.0f}."
            ),
            "vulnerable": any_depleted,
        },
        "note": (
            "Same withdrawal plan, different return ORDER. Withdrawing during an early downturn "
            "locks in losses (you sell more shares at low prices), so a bad first decade is far "
            "more dangerous than the same returns later. Returns blend historical S&P 500 totals "
            "(by equity_pct) with a flat bond return; years beyond available history use mean_return. "
            "Taxes and RMDs are not modeled. Complements run_monte_carlo_retirement (which averages "
            "over random sequences)."
        ),
    }


# ---------------------------------------------------------------------------
# model_life_event_scenario  (#97)
# ---------------------------------------------------------------------------

# Recognized life events and their default knobs. Each maps to deltas applied
# to the baseline retirement projection: a one-time lump change to the portfolio,
# a recurring annual-spending change (optionally for a limited number of years),
# and/or a one-time market shock (fractional drop in year 1).
_LIFE_EVENTS = {
    "early_retirement", "home_purchase", "new_child",
    "job_loss", "downsizing", "market_crash",
}


def _project_depletion(portfolio: float, annual_withdrawal: float, years: int,
                       real_return: float, shock_pct: float = 0.0,
                       extra_spend: float = 0.0, extra_spend_years: int = 0) -> dict:
    """Deterministic real-return depletion walk. Returns ending balance, the
    year of depletion (1-based) if any, and the per-year balance path."""
    bal = float(portfolio)
    path = []
    depletion_year = None
    for yr in range(1, years + 1):
        ret = real_return
        if yr == 1 and shock_pct:
            ret = (1 - shock_pct) * (1 + real_return) - 1   # shock then normal growth
        spend = annual_withdrawal + (extra_spend if yr <= extra_spend_years else 0.0)
        bal = bal * (1 + ret) - spend
        if bal <= 0 and depletion_year is None:
            depletion_year = yr
            bal = 0.0
            path.append(0.0)
            break
        path.append(round(bal, 2))
    return {
        "ending_balance": round(max(0.0, bal), 2),
        "depleted":       depletion_year is not None,
        "depletion_year": depletion_year,
        "lasts_full_horizon": depletion_year is None,
        "balance_path":   path,
    }


async def model_life_event_scenario(
    http_session,
    event: str,
    params: dict | None = None,
    years: int = 30,
    annual_spending: float | None = None,
    real_return: float = 0.04,
) -> dict:
    """
    Model a named life event against a baseline retirement projection and report
    the impact — the "what happens to the plan if ___?" question clients ask.

    Runs a deterministic real-return depletion projection twice (baseline vs.
    scenario) and contrasts ending balance and depletion year. Supported events
    and their ``params`` knobs (all optional; sensible defaults applied):

      - early_retirement : {years_earlier (5), annual_savings_foregone (30000)}
            — start drawing sooner; lose the foregone savings and extend the horizon.
      - home_purchase    : {down_payment (100000), monthly_payment_change (0)}
            — lump out of the portfolio + a recurring annual spending increase.
      - new_child        : {annual_cost (15000), years (18), college_lump (0)}
            — temporary annual spending increase, optional college lump at the end.
      - job_loss         : {months (6), monthly_expenses (annual_spending/12)}
            — a one-time portfolio drawdown to cover the income gap.
      - downsizing       : {equity_freed (200000), monthly_spending_change (-500)}
            — lump INTO the portfolio + a recurring spending decrease.
      - market_crash     : {drop_pct (0.30)}
            — a one-time portfolio drop in year 1 (sequence-risk flavored).

    Parameters
    ----------
    event           : one of the supported life events above
    params          : event-specific overrides (object); defaults applied per event
    years           : projection horizon (default 30)
    annual_spending : baseline annual withdrawal (default: actual 12-month spend)
    real_return     : assumed real (after-inflation) portfolio return (default 0.04)
    """
    ev = (event or "").strip().lower()
    if ev not in _LIFE_EVENTS:
        return {"error": (f"Unknown event '{event}'. Supported: "
                          f"{', '.join(sorted(_LIFE_EVENTS))}.")}
    p = params or {}
    years = max(5, min(years, 60))

    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts
    portfolio = _calc_investable_assets(accts)
    if portfolio <= 0:
        return {"error": "No investable portfolio found."}

    if annual_spending is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        annual_spending = round(sum(
            t["amount"] for t in txns if not t["is_income"] and not t["is_excluded"]
        ), 2) if ok else round(portfolio * 0.04, 2)

    # Baseline projection.
    base = _project_depletion(portfolio, annual_spending, years, real_return)

    # Build the scenario deltas.
    scen_portfolio = portfolio
    scen_withdrawal = annual_spending
    scen_years = years
    shock = 0.0
    extra_spend = 0.0
    extra_spend_years = 0
    tradeoff = ""

    if ev == "early_retirement":
        years_earlier = int(p.get("years_earlier", 5))
        foregone = float(p.get("annual_savings_foregone", 30_000))
        scen_portfolio = max(0.0, portfolio - foregone * years_earlier)
        scen_years = min(60, years + years_earlier)
        tradeoff = (f"Retiring {years_earlier} years earlier forgoes ~${foregone*years_earlier:,.0f} "
                    f"of savings and adds {years_earlier} years of withdrawals.")
    elif ev == "home_purchase":
        down = float(p.get("down_payment", 100_000))
        monthly_change = float(p.get("monthly_payment_change", 0))
        scen_portfolio = max(0.0, portfolio - down)
        scen_withdrawal = annual_spending + monthly_change * 12
        tradeoff = (f"A ${down:,.0f} down payment leaves the portfolio"
                    + (f" and adds ${monthly_change*12:,.0f}/yr of housing cost." if monthly_change else "."))
    elif ev == "new_child":
        annual_cost = float(p.get("annual_cost", 15_000))
        child_years = int(p.get("years", 18))
        college_lump = float(p.get("college_lump", 0))
        extra_spend = annual_cost
        extra_spend_years = min(child_years, years)
        scen_portfolio = max(0.0, portfolio - college_lump)
        tradeoff = (f"~${annual_cost:,.0f}/yr for {child_years} years"
                    + (f" plus a ${college_lump:,.0f} college lump." if college_lump else "."))
    elif ev == "job_loss":
        months = int(p.get("months", 6))
        monthly_exp = float(p.get("monthly_expenses", annual_spending / 12))
        gap = monthly_exp * months
        scen_portfolio = max(0.0, portfolio - gap)
        tradeoff = (f"{months} months without income draws ~${gap:,.0f} from the portfolio up front.")
    elif ev == "downsizing":
        equity = float(p.get("equity_freed", 200_000))
        monthly_change = float(p.get("monthly_spending_change", -500))
        scen_portfolio = portfolio + equity
        scen_withdrawal = max(0.0, annual_spending + monthly_change * 12)
        tradeoff = (f"Freeing ${equity:,.0f} of home equity"
                    + (f" and cutting ${abs(monthly_change)*12:,.0f}/yr of spending." if monthly_change else "."))
    elif ev == "market_crash":
        shock = min(0.9, max(0.0, float(p.get("drop_pct", 0.30))))
        tradeoff = (f"A {shock*100:.0f}% market drop in year 1 — the sequence-of-returns danger zone "
                    f"if it lands early in retirement (see get_sequence_of_returns_stress_test).")

    scenario = _project_depletion(scen_portfolio, scen_withdrawal, scen_years, real_return,
                                  shock_pct=shock, extra_spend=extra_spend,
                                  extra_spend_years=extra_spend_years)

    end_delta = round(scenario["ending_balance"] - base["ending_balance"], 2)

    return {
        "as_of":            datetime.now().strftime("%Y-%m-%d"),
        "event":            ev,
        "params_applied":   p,
        "baseline_inputs": {
            "investable_portfolio": round(portfolio, 2),
            "annual_spending":      round(annual_spending, 2),
            "horizon_years":        years,
            "real_return_pct":      round(real_return * 100, 1),
        },
        "scenario_inputs": {
            "investable_portfolio": round(scen_portfolio, 2),
            "annual_spending":      round(scen_withdrawal, 2),
            "horizon_years":        scen_years,
            "one_time_shock_pct":   round(shock * 100, 1) if shock else 0,
            "temporary_extra_spending": round(extra_spend, 2) if extra_spend else 0,
            "temporary_extra_spending_years": extra_spend_years,
        },
        "baseline":         base,
        "scenario":         scenario,
        "impact": {
            "ending_balance_delta": end_delta,
            "baseline_lasts":  base["lasts_full_horizon"],
            "scenario_lasts":  scenario["lasts_full_horizon"],
            "newly_at_risk":   base["lasts_full_horizon"] and not scenario["lasts_full_horizon"],
            "key_tradeoff":    tradeoff,
        },
        "note": (
            "Simplified deterministic projection at a constant real return — it does NOT model "
            "market volatility, taxes, Social Security, or RMDs. Use it to size the directional "
            "impact of a decision, then pressure-test with run_monte_carlo_retirement and "
            "get_sequence_of_returns_stress_test. 'real_return' is after inflation, so spending is "
            "held flat in real terms."
        ),
    }
