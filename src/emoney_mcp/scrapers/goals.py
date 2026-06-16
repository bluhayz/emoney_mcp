"""
Goal tracking, executive financial summary, and composite health scoring.

Public functions
----------------
get_goals(http_session)
    Returns all financial goals from CardSwitcher card 2 — retirement goal
    (start year, end year, % funded), education goals, and any spending goals.
    Goals are split into ``retirement_goals`` and ``spending_goals`` lists.

get_financial_summary(http_session)
    Single-call executive dashboard combining:
      • Net worth and month/year change (cards 9, 11)
      • Investment portfolio value and today's change (card 3)
      • This month's income vs. spending and top-5 categories (SNB API)
      • Goal funding status at a glance (card 2)
    Designed to be the first tool called for broad "how am I doing?" questions.

get_financial_health_score(http_session)
    Composite 0–100 score (A–F letter grade) built from six dimensions:
      1. Savings rate       (25% weight) — from get_savings_rate
      2. Goal funding       (25% weight) — avg % funded across all goals
      3. Debt-to-assets     (20% weight) — total liabilities / total assets
      4. Emergency fund     (15% weight) — months of liquid assets vs. spending
      5. Diversification    (10% weight) — number of investment positions
      6. Net worth trend    (5% weight)  — 6-month net worth % change
"""

import asyncio
from datetime import datetime

from ._helpers import _get_card
from .accounts import get_accounts
from .investments import get_holdings, get_net_worth_history
from .spending import _fetch_snb_data, get_savings_rate


async def get_goals(http_session) -> dict:
    """
    Return financial goals and their funding status from Emoney's plan.

    Source: CardSwitcher/GetCard/2 — contains Goals[] with PercentFunded,
    TotalCost, TotalFunding, and projected dates for each goal.
    """
    http = await http_session.get_http()
    card2 = await _get_card(http, 2)
    if not card2:
        return {"error": "Could not retrieve goals data (Card 2 unavailable). Session may have expired."}

    goals_raw = card2.get("Goals") or []
    goals = []
    for g in goals_raw:
        proj = g.get("Projection") or {}
        goals.append({
            "name":             g.get("Name"),
            "type":             g.get("SubTypeName") or _goal_type_label(g.get("ClientGoalInfoType")),
            "start_year":       g.get("StartYear"),
            "end_year":         g.get("EndYear"),
            "duration":         g.get("Duration"),
            "percent_funded":   proj.get("PercentFunded"),
            "total_cost":       proj.get("TotalCost"),
            "total_funding":    proj.get("TotalFunding"),
            "funding_summary":  proj.get("ProjectedFundingText"),
            "on_track":         (proj.get("PercentFunded") or 0) >= 100,
        })

    retirement = [g for g in goals if g["name"] == "Retirement" or g.get("type") == "Retirement"]
    spending   = [g for g in goals if g not in retirement]

    return {
        "goal_count":       len(goals),
        "all_on_track":     all(g["on_track"] for g in goals),
        "retirement_goals": retirement,
        "spending_goals":   spending,
    }


def _goal_type_label(type_int) -> str:
    return {0: "Education", 1: "Retirement", 2: "Other Spending"}.get(type_int, "Unknown")


async def get_financial_summary(http_session) -> dict:
    """
    Return a compact executive summary of the complete financial picture.

    Combines net worth, portfolio performance, this month's cash flow,
    top spending categories, and goal status into a single response.

    Cards 9, 11, 3, and 2 are fetched in parallel via asyncio.gather to
    minimise wall-clock time.  SNB data is fetched afterwards (sequential)
    but typically hits the module-level cache if any other spending tool
    ran earlier in the same conversation turn.
    """
    http = await http_session.get_http()

    # Parallelise the four independent card fetches
    card9, card11, card3, card2 = await asyncio.gather(
        _get_card(http, 9),
        _get_card(http, 11),
        _get_card(http, 3),
        _get_card(http, 2),
    )

    net_worth = (card9 or {}).get("NetWorth")
    assets    = (card9 or {}).get("Assets")
    liab      = (card9 or {}).get("Liabilities")

    nw_mtd = (card11 or {}).get("ChangeThisMonth") or {}
    nw_ytd = (card11 or {}).get("ChangeThisYear")  or {}

    inv_vc     = (card3 or {}).get("ValueChange") or {}
    inv_value  = inv_vc.get("CurrentValue")
    inv_today  = inv_vc.get("Change")
    inv_pct    = inv_vc.get("ChangePercent")

    goals_raw = (card2 or {}).get("Goals") or []
    goals_summary = []
    for g in goals_raw:
        proj = g.get("Projection") or {}
        pct  = proj.get("PercentFunded")
        goals_summary.append({
            "name":           g.get("Name"),
            "percent_funded": pct,
            "on_track":       (pct or 0) >= 100,
        })

    txns, snb_ok = await _fetch_snb_data(http_session, days=35)
    this_month = datetime.now().strftime("%Y-%m")

    month_income   = 0.0
    month_spending = 0.0
    cat_totals: dict[str, float] = {}

    if snb_ok:
        for t in txns:
            if t["date"][:7] != this_month or t["is_excluded"]:
                continue
            if t["is_income"]:
                month_income = round(month_income + t["amount"], 2)
            else:
                month_spending = round(month_spending + t["amount"], 2)
                cat = t["category"]
                cat_totals[cat] = round(cat_totals.get(cat, 0) + t["amount"], 2)

    savings_rate = None
    if month_income > 0:
        savings_rate = round((month_income - month_spending) / month_income * 100, 1)

    top_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "net_worth": {
            "current":             net_worth,
            "total_assets":        assets,
            "total_liabilities":   liab,
            "change_this_month":   nw_mtd.get("Change"),
            "change_this_month_pct": round((nw_mtd.get("ChangePercent") or 0) * 100, 2),
            "change_this_year":    nw_ytd.get("Change") if nw_ytd else None,
        },
        "investment_portfolio": {
            "current_value":     inv_value,
            "today_change":      round(inv_today or 0, 2),
            "today_change_pct":  round((inv_pct or 0) * 100, 2),
        },
        "this_month_cash_flow": {
            "income":           round(month_income, 2),
            "spending":         round(month_spending, 2),
            "net":              round(month_income - month_spending, 2),
            "savings_rate_pct": savings_rate,
            "top_categories":   [{"category": c, "total": round(v, 2)} for c, v in top_cats],
        },
        "goals": goals_summary,
        "all_goals_on_track": all(g["on_track"] for g in goals_summary) if goals_summary else None,
    }


async def get_financial_health_score(http_session) -> dict:
    """
    Return a single 0-100 composite financial health score with component breakdown.

    Combines six dimensions: savings rate, goal funding, debt-to-asset ratio,
    emergency fund coverage, diversification, and net worth trend.
    """
    errors = []

    # --- Phase 1: parallelise all non-SNB calls --------------------------------
    # get_accounts    → cards 9 + 1
    # get_goals       → card 2
    # get_net_worth_history → card 8
    # get_holdings    → GetInvestmentData
    # All four are independent HTTP calls; running them concurrently cuts
    # wall-clock time from ~5 s to ~1.5 s on a typical connection.
    accts, goals_result, history_result, holdings_result = await asyncio.gather(
        get_accounts(http_session),
        get_goals(http_session),
        get_net_worth_history(http_session, months=6),
        get_holdings(http_session),
    )

    if "error" in accts:
        return accts

    accts.get("net_worth") or 0
    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0

    all_goals = []
    if "error" not in goals_result:
        all_goals = goals_result.get("retirement_goals", []) + goals_result.get("spending_goals", [])

    nw_change_pct = None
    if "error" not in history_result:
        ch = history_result.get("change_over_period", {})
        nw_change_pct = ch.get("percent")

    position_count = holdings_result.get("position_count", 0) if "error" not in holdings_result else 0

    # --- Phase 2: SNB-dependent calls (sequential; first populates cache) -----
    # get_savings_rate fetches + caches the full SNB dataset.  The subsequent
    # _fetch_snb_data(days=90) call is a cache hit (zero extra HTTP requests).
    savings_result   = await get_savings_rate(http_session, months=3)
    avg_savings_rate = savings_result.get("average_savings_rate") if "error" not in savings_result else None

    txns, snb_ok = await _fetch_snb_data(http_session, days=90)
    monthly_spending = 0.0
    if snb_ok:
        monthly_spending = sum(
            t["amount"] for t in txns
            if not t["is_income"] and not t["is_excluded"]
        ) / 3

    # ── Scoring ────────────────────────────────────────────────────────────

    # 1. Savings rate (weight 25)
    if avg_savings_rate is not None:
        if avg_savings_rate >= 20:
            savings_score = 100
        elif avg_savings_rate >= 15:
            savings_score = 85
        elif avg_savings_rate >= 10:
            savings_score = 70
        elif avg_savings_rate >= 5:
            savings_score = 50
        elif avg_savings_rate > 0:
            savings_score = 30
        else:
            savings_score = 0
    else:
        savings_score = 50
        errors.append("savings_rate unavailable")

    # 2. Goal funding (weight 25)
    if all_goals:
        funded_pcts = [g.get("percent_funded") or 0 for g in all_goals]
        avg_funded  = sum(funded_pcts) / len(funded_pcts)
        goal_score  = min(100, int(avg_funded))
    else:
        goal_score = 50
        errors.append("goals unavailable")

    # 3. Debt-to-asset ratio (weight 20)
    if total_assets > 0:
        dta = total_liab / total_assets
        if dta <= 0.05:
            debt_score = 100
        elif dta <= 0.15:
            debt_score = 85
        elif dta <= 0.30:
            debt_score = 65
        elif dta <= 0.50:
            debt_score = 40
        else:
            debt_score = 15
    else:
        debt_score = 50

    # 4. Emergency fund (weight 15): liquid months of spending
    # Sum ALL cash/bank groups — liquid assets can span multiple groups
    # (e.g. "Checking & Savings" plus a separate "Money Market" group), and
    # taking only the first match would understate emergency-fund coverage.
    liquid_assets = sum(
        g.get("total") or 0
        for g in accts.get("account_groups", [])
        if "cash" in g.get("group", "").lower() or "bank" in g.get("group", "").lower()
    )
    if monthly_spending > 0:
        months_covered = liquid_assets / monthly_spending
        if months_covered >= 6:
            emergency_score = 100
        elif months_covered >= 3:
            emergency_score = 70
        elif months_covered >= 1:
            emergency_score = 40
        else:
            emergency_score = 10
    else:
        emergency_score = 60

    # 5. Diversification (weight 10)
    if position_count >= 20:
        diversification_score = 100
    elif position_count >= 10:
        diversification_score = 80
    elif position_count >= 5:
        diversification_score = 55
    elif position_count >= 2:
        diversification_score = 35
    else:
        diversification_score = 10

    # 6. Net worth trend (weight 5)
    if nw_change_pct is not None:
        if nw_change_pct >= 10:
            trend_score = 100
        elif nw_change_pct >= 5:
            trend_score = 80
        elif nw_change_pct >= 0:
            trend_score = 60
        elif nw_change_pct >= -5:
            trend_score = 35
        else:
            trend_score = 10
    else:
        trend_score = 50

    weights = {
        "savings_rate":    0.25,
        "goal_funding":    0.25,
        "debt_to_assets":  0.20,
        "emergency_fund":  0.15,
        "diversification": 0.10,
        "nw_trend":        0.05,
    }
    scores = {
        "savings_rate":    savings_score,
        "goal_funding":    goal_score,
        "debt_to_assets":  debt_score,
        "emergency_fund":  emergency_score,
        "diversification": diversification_score,
        "nw_trend":        trend_score,
    }
    composite = round(sum(scores[k] * weights[k] for k in weights), 1)

    if composite >= 85:
        letter_grade, summary = "A", "Excellent — your finances are in great shape."
    elif composite >= 70:
        letter_grade, summary = "B", "Good — strong fundamentals with room to improve."
    elif composite >= 55:
        letter_grade, summary = "C", "Fair — some important areas need attention."
    elif composite >= 40:
        letter_grade, summary = "D", "Needs work — several key financial metrics are below target."
    else:
        letter_grade, summary = "F", "Urgent attention needed — multiple areas are at risk."

    return {
        "overall_score":  composite,
        "letter_grade":   letter_grade,
        "summary":        summary,
        "components": [
            {
                "name":    k.replace("_", " ").title(),
                "score":   scores[k],
                "weight":  f"{int(weights[k]*100)}%",
                "details": _score_detail(k, scores[k], {
                    "savings_rate":    avg_savings_rate,
                    "goal_funding":    sum(g.get("percent_funded") or 0 for g in all_goals) / max(len(all_goals), 1),
                    "debt_to_assets":  round((total_liab / total_assets * 100) if total_assets else 0, 1),
                    "emergency_fund":  round(liquid_assets / monthly_spending, 1) if monthly_spending > 0 else None,
                    "diversification": position_count,
                    "nw_trend":        nw_change_pct,
                }),
            }
            for k in weights
        ],
        "data_errors": errors if errors else None,
        "note": "Score reflects current snapshot. Improve savings rate and goal funding for the biggest impact.",
    }


def _score_detail(component: str, score: int, values: dict) -> str:
    v = values.get(component)
    if component == "savings_rate":
        return f"{v:.1f}% average savings rate" if v is not None else "Data unavailable"
    if component == "goal_funding":
        return f"{v:.0f}% average goal funding" if v is not None else "No goals found"
    if component == "debt_to_assets":
        return f"{v:.1f}% debt-to-asset ratio" if v is not None else "Unknown"
    if component == "emergency_fund":
        return f"{v:.1f} months of expenses covered" if v is not None else "Unknown"
    if component == "diversification":
        return f"{v} investment positions"
    if component == "nw_trend":
        return f"{v:+.1f}% net worth change over 6 months" if v is not None else "Insufficient history"
    return ""


# ---------------------------------------------------------------------------
# get_quick_status  (Sprint 2)
# ---------------------------------------------------------------------------

async def get_quick_status(http_session) -> dict:
    """
    Return an ultra-compact 5-number financial snapshot.

    Designed for quick-check queries like "How am I doing?" where the user
    wants a brief answer rather than a full dashboard.  Calls
    ``get_financial_summary`` internally (which already parallelises its card
    fetches) and extracts only the key metrics, keeping token usage minimal.

    Returns:
      • Net worth and month-to-date change
      • Portfolio today's dollar/percent change
      • This month's savings rate
      • Top spending category this month
      • Goal on-track status (X of Y on track)
    """
    summary = await get_financial_summary(http_session)
    if "error" in summary:
        return summary

    nw   = summary.get("net_worth") or {}
    inv  = summary.get("investment_portfolio") or {}
    cf   = summary.get("this_month_cash_flow") or {}
    goals = summary.get("goals") or []

    top_cat = None
    if cf.get("top_categories"):
        top_cat = cf["top_categories"][0]

    goals_on_track = sum(1 for g in goals if g.get("on_track", False))
    goals_total    = len(goals)

    return {
        "as_of":                         summary.get("as_of"),
        "net_worth":                     nw.get("current"),
        "net_worth_change_this_month":   nw.get("change_this_month"),
        "portfolio_today_change":        inv.get("today_change"),
        "portfolio_today_change_pct":    inv.get("today_change_pct"),
        "this_month_savings_rate_pct":   cf.get("savings_rate_pct"),
        "top_spending_category":         top_cat,
        "goals_on_track":                f"{goals_on_track}/{goals_total}" if goals_total else "no goals",
        "note": "Quick snapshot — call get_financial_summary for full detail.",
    }


# ---------------------------------------------------------------------------
# get_college_savings_gap  (Sprint 3)
# ---------------------------------------------------------------------------

async def get_college_savings_gap(
    http_session,
    annual_return: float = 0.06,
    annual_college_inflation: float = 0.05,
) -> dict:
    """
    Estimate the gap between current 529 savings and projected college costs.

    Fetches education goals from Emoney's financial plan and 529 account
    balances, then projects both forward to the goal start year to compute
    whether the user is on track.  Also shows the required monthly
    contribution to fully close any gap.

    Parameters
    ----------
    annual_return            : expected 529 portfolio return (default 6%)
    annual_college_inflation : rate at which college costs grow (default 5%)
    """
    from .accounts import get_retirement_accounts

    accts_task = get_accounts(http_session)
    goals_task = get_goals(http_session)
    ret_task   = get_retirement_accounts(http_session)

    accts, goals_result, retirement = await asyncio.gather(
        accts_task, goals_task, ret_task,
    )

    if "error" in accts:
        return accts

    # --- Extract 529 balance ---
    breakdown = retirement.get("retirement_breakdown", {}) if "error" not in retirement else {}
    total_529  = breakdown.get("education_529", 0.0) or 0.0

    # Also sum from account_groups for per-account detail
    accounts_529 = []
    for grp in accts.get("account_groups", []):
        for a in grp.get("accounts", []):
            name_lower = (a.get("name") or "").lower()
            type_lower = (a.get("type") or "").lower()
            if "529" in name_lower or "529" in type_lower or "education" in name_lower:
                accounts_529.append({
                    "name":    a.get("name"),
                    "balance": a.get("balance") or 0.0,
                })

    # --- Extract education goals ---
    edu_goals = []
    if "error" not in goals_result:
        all_goals = goals_result.get("retirement_goals", []) + goals_result.get("spending_goals", [])
        for g in all_goals:
            gtype = (g.get("type") or "").lower()
            gname = (g.get("name") or "").lower()
            if "education" in gtype or "education" in gname or "college" in gname or "529" in gname:
                edu_goals.append(g)

    current_year = datetime.now().year

    if not edu_goals:
        return {
            "current_529_balance":  round(total_529, 2),
            "accounts_529":         accounts_529,
            "education_goals":      [],
            "message": (
                "No education goals found in your Emoney plan. "
                "Add an education goal in Emoney to see gap analysis."
            ),
        }

    goal_analyses = []
    for g in edu_goals:
        start_year      = g.get("start_year") or (current_year + 10)
        total_cost_plan = g.get("total_cost") or 0.0  # Emoney's estimated cost
        pct_funded      = g.get("percent_funded") or 0.0

        years_until     = max(0, start_year - current_year)

        # Project current 529 balance to goal start year
        projected_529   = round(total_529 * ((1 + annual_return) ** years_until), 2)

        # If Emoney has a total cost, inflate it to start year; else leave as-is
        if total_cost_plan > 0:
            # Total cost from Emoney is in today's dollars; inflate to start year
            projected_cost = round(total_cost_plan * ((1 + annual_college_inflation) ** years_until), 2)
        else:
            projected_cost = None

        gap = None
        if projected_cost:
            gap = round(projected_cost - projected_529, 2)

        # Required monthly contribution to close the gap
        monthly_needed = None
        if gap and gap > 0 and years_until > 0:
            # FV of annuity formula: FV = PMT * [((1+r)^n - 1) / r]
            r_monthly = annual_return / 12
            n_months  = years_until * 12
            if r_monthly > 0:
                fv_factor  = ((1 + r_monthly) ** n_months - 1) / r_monthly
                monthly_needed = round(gap / fv_factor, 2)
            else:
                monthly_needed = round(gap / n_months, 2)

        goal_analyses.append({
            "goal_name":             g.get("name"),
            "start_year":            start_year,
            "years_until":           years_until,
            "emoney_pct_funded":     round(pct_funded, 1),
            "current_529_balance":   round(total_529, 2),
            "projected_529_at_start": projected_529,
            "projected_cost_at_start": projected_cost,
            "funding_gap":           gap,
            "on_track":              (gap is None) or (gap <= 0),
            "monthly_contribution_needed": monthly_needed,
        })

    return {
        "current_529_balance":  round(total_529, 2),
        "accounts_529":         accounts_529,
        "annual_return_pct":    round(annual_return * 100, 1),
        "college_inflation_pct": round(annual_college_inflation * 100, 1),
        "education_goals":      goal_analyses,
        "note": (
            "529 balance projected at the specified annual return. "
            "Projected cost inflates Emoney's total cost estimate at the college inflation rate. "
            "Monthly contribution to close the gap assumes contributions at the same return rate. "
            "Consult a financial advisor for personalized 529 planning."
        ),
    }


# ---------------------------------------------------------------------------
# get_monthly_review  (v0.8.0)
# ---------------------------------------------------------------------------

async def get_monthly_review(http_session) -> dict:
    """
    Compile a structured monthly financial review in a single call.

    Fetches net worth, performance, spending, savings rate, and goal status
    in parallel and returns a unified report with key numbers and action items.
    """
    http = await http_session.get_http()

    # Phase 1: parallel card fetches + savings rate
    card9_t, card11_t, card3_t, card2_t, savings_t = await asyncio.gather(
        _get_card(http, 9),
        _get_card(http, 11),
        _get_card(http, 3),
        _get_card(http, 2),
        get_savings_rate(http_session, months=1),
    )

    # Phase 2: SNB spending data (typically hits cache after savings_rate)
    now = datetime.now()
    this_month = now.strftime("%Y-%m")
    txns, snb_ok = await _fetch_snb_data(http_session, days=35)

    # --- Net worth ---
    nw_current   = (card9_t or {}).get("NetWorth")
    nw_assets    = (card9_t or {}).get("Assets")
    nw_liab      = (card9_t or {}).get("Liabilities")
    nw_mtd_obj   = (card11_t or {}).get("ChangeThisMonth") or {}
    nw_ytd_obj   = (card11_t or {}).get("ChangeThisYear") or {}

    # --- Investments ---
    inv_vc    = (card3_t or {}).get("ValueChange") or {}
    inv_value = inv_vc.get("CurrentValue")
    inv_today = inv_vc.get("Change")
    inv_pct   = inv_vc.get("ChangePercent")

    # --- Spending this month ---
    month_income   = 0.0
    month_spending = 0.0
    cat_totals: dict[str, float] = {}
    if snb_ok:
        for t in txns:
            if t["date"][:7] != this_month or t["is_excluded"]:
                continue
            if t["is_income"]:
                month_income = round(month_income + t["amount"], 2)
            else:
                month_spending = round(month_spending + t["amount"], 2)
                cat = t["category"]
                cat_totals[cat] = round(cat_totals.get(cat, 0) + t["amount"], 2)

    top_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:5]

    # --- Savings rate ---
    avg_savings_rate = None
    if "error" not in savings_t:
        monthly = savings_t.get("monthly", [])
        if monthly:
            avg_savings_rate = monthly[-1].get("savings_rate_pct")

    # --- Goals ---
    goals_raw = (card2_t or {}).get("Goals") or []
    goals_on_track = sum(
        1 for g in goals_raw
        if (g.get("Projection") or {}).get("PercentFunded", 0) >= 100
    )
    goals_total = len(goals_raw)

    # --- Action items ---
    action_items = []
    nw_mtd_change = nw_mtd_obj.get("Change", 0) or 0
    if nw_mtd_change < 0:
        action_items.append(f"Net worth is down ${abs(nw_mtd_change):,.0f} this month — review spending and portfolio.")
    if avg_savings_rate is not None and avg_savings_rate < 10:
        action_items.append(f"Savings rate is {avg_savings_rate:.1f}% — below the 10% target. Consider reducing discretionary spending.")
    if goals_total > 0 and goals_on_track < goals_total:
        off = goals_total - goals_on_track
        action_items.append(f"{off} of {goals_total} financial goals are below 100% funded.")
    if top_cats:
        top_name, top_amt = top_cats[0]
        action_items.append(f"Top spending category this month: {top_name} (${top_amt:,.0f}).")

    return {
        "period":      this_month,
        "as_of":       now.strftime("%Y-%m-%d"),
        "net_worth": {
            "current":           nw_current,
            "total_assets":      nw_assets,
            "total_liabilities": nw_liab,
            "change_mtd":        nw_mtd_obj.get("Change"),
            "change_mtd_pct":    round((nw_mtd_obj.get("ChangePercent") or 0) * 100, 2),
            "change_ytd":        nw_ytd_obj.get("Change") if nw_ytd_obj else None,
        },
        "investments": {
            "current_value":    inv_value,
            "change_today":     round(inv_today or 0, 2),
            "change_today_pct": round((inv_pct or 0) * 100, 2),
        },
        "spending": {
            "income":         round(month_income, 2),
            "total":          round(month_spending, 2),
            "net":            round(month_income - month_spending, 2),
            "top_categories": [{"category": c, "total": round(v, 2)} for c, v in top_cats],
        },
        "savings_rate_pct":  avg_savings_rate,
        "goals": {
            "total":      goals_total,
            "on_track":   goals_on_track,
            "off_track":  goals_total - goals_on_track,
        },
        "action_items": action_items,
    }
