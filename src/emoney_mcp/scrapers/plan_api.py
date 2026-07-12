"""
eMoney planning data via the ``internal-api`` BFF.

The My Plan section is driven by ``https://api.emoneyadvisor.com/internal-api``,
an Apigee-gated API authenticated with the SAME credentials as the SNB API:
a Bearer JWT + ``apikey`` header, both scraped from the Spending page
(``_get_snb_credentials``). Requests are scoped to a client and a plan:
``/internal-api/api/clients/<clientId>/plans/<planId>/...``. Both ids are
embedded in the My Plan page HTML (``clientId":"..."`` / ``planId":"..."``).

Public functions
----------------
get_all_goals_funding_status(http_session)
    Unified funding status for every plan goal (retirement, leave-to-heirs, and
    each education/spending goal): probability of success, surplus/shortfall, and
    the retirement funding-vs-expense dollars. Answers "Are my goals on track?"

get_lifetime_cash_flow_projection(http_session, start_year, end_year)
    eMoney's signature year-by-year lifetime plan: per-year inflow, outflow, net
    cash flow, portfolio value, net worth, growth, and withdrawals, plus summary
    stats (peak portfolio, ending net worth, first shortfall/depletion year).

get_plan_assumptions(http_session)
    The advisor's plan-level assumptions: inflation, expected return rates,
    retirement ages, plan horizon, and other modelling parameters.

get_plan_expenses(http_session)
    Goal-level expense definitions from the plan: regular living expenses,
    education funding goals, and other spending goals with their amounts and years.

get_official_plan_projection(http_session)
    eMoney's Monte Carlo probability-of-success and asset-spread percentile bands
    (10th/25th/50th/75th/90th portfolio values by year) from the advisor's plan.

Discovered via live network capture (epic #106, discovery pass 2 / token flow).
Endpoints used:
  GET .../plans/<plan>/projection/montecarlo/goals        (per-goal success)
  GET .../plans/<plan>/projection/goalfunding/retirement  (retirement $ funding)
  GET .../plans/<plan>/projection/linear/cashflow/details (lifetime cash flow)
  GET .../plans/<plan>/assumptions                        (plan assumptions)
  GET .../plans/<plan>/expenses                           (plan expenses)
  GET .../plans/<plan>/projection/montecarlo/probabilityofsuccess
  GET .../plans/<plan>/projection/montecarlo/assetspread
"""

import re

from ._helpers import BASE_URL, _is_compact
from .spending import _get_snb_credentials, _snb_headers

_INTERNAL_API = "https://api.emoneyadvisor.com/internal-api/api"
_MYPLAN_URL = f"{BASE_URL}/ema/CS/MyPlan"

_CLIENT_RE = re.compile(r'clientId"\s*:\s*"([0-9a-fA-F-]{36})"')
_PLAN_RE   = re.compile(r'planId"\s*:\s*"([0-9a-fA-F-]{36})"')


async def _get_plan_ids(http_session) -> tuple[str | None, str | None, dict | None]:
    """
    Resolve (clientId, planId) by scraping the My Plan page.

    Returns ``(clientId, planId, None)`` on success or ``(None, None, error)``.
    """
    http = await http_session.get_http()
    resp = await http.get(_MYPLAN_URL, allow_redirects=True, timeout=20)
    if resp.status_code != 200:
        return None, None, {"error": f"My Plan page returned HTTP {resp.status_code}."}
    if "/ema/SignIn" in str(resp.url):
        return None, None, {"error": "Session expired — call sync_chrome_session or reset_session."}
    html = resp.text
    cm, pm = _CLIENT_RE.search(html), _PLAN_RE.search(html)
    if not cm or not pm:
        return None, None, {"error": "Could not locate clientId/planId on the My Plan page."}
    return cm.group(1), pm.group(1), None


def _pct(p) -> float | None:
    """Convert a 0–1 probability to a rounded percentage."""
    return round(p * 100, 1) if isinstance(p, (int, float)) else None


def _status_from_prob(prob) -> str:
    """Map a probability-of-success to a coarse status band."""
    if not isinstance(prob, (int, float)):
        return "unknown"
    if prob >= 0.85:
        return "On Track"
    if prob >= 0.70:
        return "Monitor"
    return "At Risk"


async def get_all_goals_funding_status(http_session) -> dict:
    """
    Return the funding status of every goal in the client's plan.

    Combines the Monte Carlo goal results (probability of success and mean
    surplus/shortfall for the retirement goal, leave-to-heirs goal, and each
    education/spending goal) with the retirement goal's funding-vs-expense
    dollars. This is the unified "are my goals on track?" view.
    """
    client_id, plan_id, err = await _get_plan_ids(http_session)
    if err:
        return err

    jwt, apikey = await _get_snb_credentials(http_session)
    if not jwt or not apikey:
        return {"error": "Could not retrieve plan API credentials (Spending page). "
                         "Session may have expired — call sync_chrome_session."}
    headers = _snb_headers(jwt, apikey)

    http = await http_session.get_http()
    base = f"{_INTERNAL_API}/clients/{client_id}/plans/{plan_id}"

    async def _get_json(path: str):
        try:
            r = await http.get(f"{base}{path}", headers=headers, timeout=30)
        except Exception as e:
            return None, f"{type(e).__name__}"
        if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
            return None, f"HTTP {r.status_code}"
        return r.json(), None

    goals, g_err = await _get_json("/projection/montecarlo/goals")
    if g_err or not isinstance(goals, dict):
        return {"error": f"Goals projection unavailable ({g_err or 'unexpected body'}). "
                         "The plan's Monte Carlo data may not be calculated yet."}

    funding, _ = await _get_json("/projection/goalfunding/retirement")

    # Retirement goal
    rg = goals.get("retirementGoal") or {}
    retirement = {
        "probability_of_success_pct": _pct(rg.get("probabilityOfSuccess")),
        "has_shortfall":              rg.get("hasShortfall"),
        "first_shortfall_year":       rg.get("firstShortfallYear") or None,
        "mean_surplus":               round(rg.get("meanSurplus"), 2) if isinstance(rg.get("meanSurplus"), (int, float)) else None,
        "status":                     _status_from_prob(rg.get("probabilityOfSuccess")),
    }
    if isinstance(funding, dict):
        fund = funding.get("goalTotalFunding")
        exp  = funding.get("goalTotalExpense")
        retirement["total_funding"] = round(fund, 2) if isinstance(fund, (int, float)) else None
        retirement["total_expense"] = round(exp, 2) if isinstance(exp, (int, float)) else None
        if isinstance(fund, (int, float)) and isinstance(exp, (int, float)) and exp:
            retirement["funded_ratio_pct"] = round(fund / exp * 100, 1)

    # Leave-to-heirs goal
    lh = goals.get("leaveToHeirsGoal") or {}
    leave_to_heirs = {
        "probability_of_success_pct": _pct(lh.get("probabilityOfSuccess")),
        "status":                     _status_from_prob(lh.get("probabilityOfSuccess")),
    } if lh else None

    # Other goals (education, spending, etc.)
    other_goals = []
    for g in goals.get("otherGoals") or []:
        other_goals.append({
            "name":                       g.get("name"),
            "probability_of_success_pct": _pct(g.get("probabilityOfSuccess")),
            "mean_surplus":               round(g.get("meanSurplus"), 2) if isinstance(g.get("meanSurplus"), (int, float)) else None,
            "mean_shortfall":             round(g.get("meanShortfall"), 2) if isinstance(g.get("meanShortfall"), (int, float)) else None,
            "status":                     _status_from_prob(g.get("probabilityOfSuccess")),
        })

    all_statuses = (
        [retirement["status"]]
        + ([leave_to_heirs["status"]] if leave_to_heirs else [])
        + [g["status"] for g in other_goals]
    )
    on_track = sum(1 for s in all_statuses if s == "On Track")

    return {
        "retirement_goal":  retirement,
        "leave_to_heirs_goal": leave_to_heirs,
        "other_goals":      other_goals,
        "summary": {
            "total_goals":    len(all_statuses),
            "goals_on_track": on_track,
            "goals_needing_attention": len(all_statuses) - on_track,
        },
        "note": (
            "Funding status comes from the plan's Monte Carlo projection: "
            "probability_of_success is the share of simulated scenarios in which the "
            "goal is fully funded; status bands are On Track (>=85%), Monitor (70-85%), "
            "At Risk (<70%). Retirement funding/expense dollars are the plan's "
            "total dedicated funding vs. projected goal cost. Figures reflect the advisor's "
            "plan assumptions, not live market values."
        ),
    }


def _num(v):
    """Round numeric values; pass through None/non-numerics."""
    return round(v, 2) if isinstance(v, (int, float)) else None


def _withdrawals_total(w: dict) -> float:
    """Sum the withdrawal sub-totals for a projection year."""
    if not isinstance(w, dict):
        return 0.0
    total = 0.0
    for sub in w.values():
        if isinstance(sub, dict) and isinstance(sub.get("total"), (int, float)):
            total += sub["total"]
    return round(total, 2)


async def get_lifetime_cash_flow_projection(
    http_session,
    start_year: int | None = None,
    end_year: int | None = None,
) -> dict:
    """
    eMoney's signature year-by-year lifetime cash-flow plan.

    Returns one row per projected year — total cash inflow, outflow, net cash
    flow, portfolio value, net worth, portfolio growth, and total withdrawals —
    plus summary stats (horizon, peak portfolio and its year, ending net worth,
    and the first year net cash flow turns negative or the portfolio depletes).

    The projection reflects the advisor's plan assumptions (the "linear" /
    average-return scenario), not live markets or Monte Carlo ranges.

    Parameters
    ----------
    start_year : optional first calendar year to include (default: plan start)
    end_year   : optional last calendar year to include (default: plan end)
    """
    client_id, plan_id, err = await _get_plan_ids(http_session)
    if err:
        return err

    jwt, apikey = await _get_snb_credentials(http_session)
    if not jwt or not apikey:
        return {"error": "Could not retrieve plan API credentials (Spending page). "
                         "Session may have expired — call sync_chrome_session."}
    headers = _snb_headers(jwt, apikey)

    http = await http_session.get_http()
    url = f"{_INTERNAL_API}/clients/{client_id}/plans/{plan_id}/projection/linear/cashflow/details"
    try:
        resp = await http.get(url, headers=headers, timeout=45)
    except Exception as e:
        return {"error": f"Lifetime cash-flow request failed ({type(e).__name__})."}
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"Lifetime cash-flow projection unavailable (HTTP {resp.status_code}). "
                         "The plan may not be calculated yet."}

    data = resp.json()
    raw_years = data.get("years") if isinstance(data, dict) else None
    if not raw_years:
        return {"error": "Projection returned no yearly data."}

    rows = []
    for y in raw_years:
        yr = y.get("year")
        if start_year is not None and yr is not None and yr < start_year:
            continue
        if end_year is not None and yr is not None and yr > end_year:
            continue
        pv = y.get("portfolioValue") or {}
        rows.append({
            "year":             yr,
            "total_inflow":     _num(y.get("totalCashInflow")),
            "total_outflow":    _num(y.get("totalCashOutflow")),
            "net_cash_flow":    _num(y.get("netCashFlow")),
            "withdrawals":      _withdrawals_total(y.get("withdrawals")),
            "portfolio_value":  _num(pv.get("totalPortfolioAssets")),
            "net_worth":        _num(pv.get("totalNetWorth")),
            "portfolio_growth": _num(pv.get("portfolioGrowth")),
        })

    if not rows:
        return {"error": "No projection years matched the requested range."}

    # Summary stats
    peak = max(rows, key=lambda r: r["portfolio_value"] or 0)
    first_negative = next((r["year"] for r in rows if (r["net_cash_flow"] or 0) < 0), None)
    depletion_year = next((r["year"] for r in rows if (r["portfolio_value"] or 0) <= 0), None)
    ending = rows[-1]

    # Compact mode: keep only key years to reduce payload size (#182).
    years_total = len(rows)
    if _is_compact() and years_total > 10:
        key_years: set[int] = set()
        if rows[0]["year"] is not None:
            key_years.add(rows[0]["year"])
        if ending["year"] is not None:
            key_years.add(ending["year"])
        if peak["year"] is not None:
            key_years.add(peak["year"])
        if first_negative:
            key_years.add(first_negative)
        if depletion_year:
            key_years.add(depletion_year)
        for r in rows:
            if r["year"] is not None and r["year"] % 5 == 0:
                key_years.add(r["year"])
        rows = [r for r in rows if r["year"] in key_years]
        compact_meta: dict = {
            "output_mode":  "compact",
            "years_total":  years_total,
            "years_shown":  len(rows),
        }
    else:
        compact_meta = {}

    return {
        "horizon_years":   years_total,
        "first_year":      rows[0]["year"] if rows else None,
        "last_year":       ending["year"],
        "scenario":        "linear (average-return plan assumptions)",
        "summary": {
            "starting_portfolio_value": rows[0]["portfolio_value"] if rows else None,
            "ending_portfolio_value":   ending["portfolio_value"],
            "ending_net_worth":         ending["net_worth"],
            "peak_portfolio_value":     peak["portfolio_value"],
            "peak_portfolio_year":      peak["year"],
            "first_negative_cash_flow_year": first_negative,
            "portfolio_depletion_year": depletion_year,
        },
        "years": rows,
        **compact_meta,
        "note": (
            "Year-by-year lifetime cash flow from the plan's 'linear' projection (average-return "
            "assumptions, not Monte Carlo ranges). net_cash_flow turning negative is normal in "
            "retirement (portfolio funds the gap); portfolio_depletion_year is null when the plan "
            "never runs out. Figures use the advisor's plan assumptions and inflation, not live "
            "market values. For probability-of-success see get_all_goals_funding_status."
            + (" Set EMONEY_COMPACT= to see all years." if compact_meta else
               " Set EMONEY_COMPACT=1 to truncate to key years only.")
        ),
    }


# ---------------------------------------------------------------------------
# Shared BFF auth helper
# ---------------------------------------------------------------------------

async def _bff_setup(http_session):
    """Return (http, base_url, headers, None) or (None, None, None, error_dict)."""
    client_id, plan_id, err = await _get_plan_ids(http_session)
    if err:
        return None, None, None, err
    jwt, apikey = await _get_snb_credentials(http_session)
    if not jwt or not apikey:
        return None, None, None, {
            "error": "Could not retrieve plan API credentials (Spending page). "
                     "Session may have expired — call sync_chrome_session."
        }
    http    = await http_session.get_http()
    base    = f"{_INTERNAL_API}/clients/{client_id}/plans/{plan_id}"
    headers = _snb_headers(jwt, apikey)
    return http, base, headers, None


async def _bff_get(http, base, headers, path, timeout=30):
    """GET a BFF sub-path; returns (data_dict_or_list, None) or (None, error_str)."""
    try:
        r = await http.get(f"{base}{path}", headers=headers, timeout=timeout)
    except Exception as e:
        return None, f"{type(e).__name__}"
    if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
        return None, f"HTTP {r.status_code}"
    return r.json(), None


# ---------------------------------------------------------------------------
# #178 — get_plan_assumptions
# ---------------------------------------------------------------------------

async def get_plan_assumptions(http_session) -> dict:
    """
    Return the advisor's plan-level modelling assumptions.

    Surfaces the inputs the advisor configured in the eMoney financial plan:
    inflation rate, expected portfolio return rates, retirement ages, plan
    horizon, and any other modelling parameters embedded in the assumptions
    endpoint. Useful for understanding how the projections were derived and
    for comparing against your own expectations.

    Note: the exact fields returned depend on how the advisor configured the
    plan; not all fields are present in every plan.
    """
    http, base, headers, err = await _bff_setup(http_session)
    if err:
        return err

    data, fetch_err = await _bff_get(http, base, headers, "/assumptions")
    if fetch_err:
        return {"error": f"Plan assumptions endpoint unavailable ({fetch_err}). "
                         "The plan may not be fully configured."}

    if not isinstance(data, dict):
        return {"error": "Plan assumptions returned an unexpected response format."}

    # Surface common known fields with normalised names; pass through any extras.
    def _rate(v):
        return round(v * 100, 3) if isinstance(v, float) and v < 1 else _num(v)

    result: dict = {}

    # Inflation
    inf_rate = data.get("inflationRate") or data.get("inflation")
    if inf_rate is not None:
        result["inflation_rate_pct"] = _rate(inf_rate)

    # Investment return assumptions
    for src_key, dst_key in [
        ("equityReturn",        "equity_return_pct"),
        ("bondReturn",          "bond_return_pct"),
        ("cashReturn",          "cash_return_pct"),
        ("preRetirementReturn", "pre_retirement_return_pct"),
        ("retirementReturn",    "retirement_return_pct"),
        ("portfolioReturn",     "portfolio_return_pct"),
    ]:
        v = data.get(src_key)
        if v is not None:
            result[dst_key] = _rate(v)

    # Retirement ages
    for src_key, dst_key in [
        ("primaryRetirementAge",  "primary_retirement_age"),
        ("spouseRetirementAge",   "spouse_retirement_age"),
        ("retirementAge",         "retirement_age"),
    ]:
        v = data.get(src_key)
        if v is not None:
            result[dst_key] = v

    # Plan horizon / life expectancy
    for src_key, dst_key in [
        ("planHorizon",       "plan_horizon_years"),
        ("lifeExpectancy",    "life_expectancy"),
        ("planEndYear",       "plan_end_year"),
        ("planStartYear",     "plan_start_year"),
    ]:
        v = data.get(src_key)
        if v is not None:
            result[dst_key] = v

    # Social Security
    for src_key, dst_key in [
        ("primarySocialSecurityAge",  "primary_ss_start_age"),
        ("spouseSocialSecurityAge",   "spouse_ss_start_age"),
    ]:
        v = data.get(src_key)
        if v is not None:
            result[dst_key] = v

    # Pass through any remaining top-level fields we haven't mapped
    mapped = {
        "inflationRate", "inflation",
        "equityReturn", "bondReturn", "cashReturn",
        "preRetirementReturn", "retirementReturn", "portfolioReturn",
        "primaryRetirementAge", "spouseRetirementAge", "retirementAge",
        "planHorizon", "lifeExpectancy", "planEndYear", "planStartYear",
        "primarySocialSecurityAge", "spouseSocialSecurityAge",
    }
    extra = {k: v for k, v in data.items() if k not in mapped}
    if extra:
        result["additional_assumptions"] = extra

    result["note"] = (
        "Plan assumptions from the advisor's eMoney financial plan. "
        "Return rates shown as percentages (already converted from 0–1 fractions). "
        "Fields present vary by plan configuration."
    )
    return result


# ---------------------------------------------------------------------------
# #178 — get_plan_expenses
# ---------------------------------------------------------------------------

def _parse_expense(e: dict) -> dict:
    """Normalise a single expense/goal record from the BFF."""
    return {
        "name":         e.get("name") or e.get("description"),
        "type":         e.get("type") or e.get("goalType"),
        "annual_amount": _num(e.get("annualAmount") or e.get("amount")),
        "start_year":   e.get("startYear") or e.get("startDate"),
        "end_year":     e.get("endYear") or e.get("endDate"),
        "total_cost":   _num(e.get("totalCost") or e.get("totalExpense")),
        "is_funded":    e.get("isFunded"),
    }


async def get_plan_expenses(http_session) -> dict:
    """
    Return the goal-level expense definitions from the advisor's financial plan.

    Combines three sub-endpoints:
    - ``/expenses``             : all plan expenses (regular living costs + goals)
    - ``/expenses/education``   : education-specific goal details
    - ``/expenses/spending``    : regular spending goal details

    Useful for understanding what goals the advisor modelled (college funding,
    retirement spending, one-time purchases) and their expected costs and timing.

    Note: the exact structure varies by plan; not all sub-endpoints are populated
    in every plan.
    """
    http, base, headers, err = await _bff_setup(http_session)
    if err:
        return err

    import asyncio
    (expenses_raw, exp_err), (edu_raw, edu_err), (spend_raw, spend_err) = \
        await asyncio.gather(
            _bff_get(http, base, headers, "/expenses"),
            _bff_get(http, base, headers, "/expenses/education"),
            _bff_get(http, base, headers, "/expenses/spending"),
            return_exceptions=False,
        )

    if exp_err and edu_err and spend_err:
        return {"error": f"All plan expense endpoints unavailable "
                         f"(expenses: {exp_err}, education: {edu_err}, spending: {spend_err}). "
                         "The plan may not be fully configured."}

    result: dict = {}

    # Top-level expenses
    if isinstance(expenses_raw, (dict, list)):
        items = expenses_raw if isinstance(expenses_raw, list) else \
                expenses_raw.get("expenses") or expenses_raw.get("items") or \
                list(expenses_raw.values()) if isinstance(expenses_raw, dict) else []
        if isinstance(items, list):
            result["expenses"] = [_parse_expense(e) for e in items if isinstance(e, dict)]

    # Education goals
    if isinstance(edu_raw, (dict, list)):
        items = edu_raw if isinstance(edu_raw, list) else \
                edu_raw.get("educationGoals") or edu_raw.get("goals") or \
                edu_raw.get("items") or []
        if isinstance(items, list):
            result["education_goals"] = [_parse_expense(e) for e in items if isinstance(e, dict)]

    # Spending goals
    if isinstance(spend_raw, (dict, list)):
        items = spend_raw if isinstance(spend_raw, list) else \
                spend_raw.get("spendingGoals") or spend_raw.get("goals") or \
                spend_raw.get("items") or []
        if isinstance(items, list):
            result["spending_goals"] = [_parse_expense(e) for e in items if isinstance(e, dict)]

    if not result:
        return {"error": "No expense data returned from the plan. "
                         "The plan may not have any goals configured."}

    result["note"] = (
        "Goal and expense definitions from the advisor's eMoney financial plan. "
        "annual_amount values are in today's dollars (before inflation). "
        "Not all fields are populated in every plan."
    )
    return result


# ---------------------------------------------------------------------------
# #179 — get_official_plan_projection
# ---------------------------------------------------------------------------

def _parse_asset_spread_year(y: dict) -> dict:
    """Extract the portfolio percentile bands for a single projection year."""
    row: dict = {"year": y.get("year")}
    # Try common field shapes: {p10, p25, p50, p75, p90} or {percentiles: {}}
    pct = y.get("percentiles") or {}
    for p_key, out_key in [
        ("p10", "p10"),  ("percentile10", "p10"),  ("10", "p10"),
        ("p25", "p25"),  ("percentile25", "p25"),  ("25", "p25"),
        ("p50", "p50"),  ("percentile50", "p50"),  ("50", "p50"),  ("median", "p50"),
        ("p75", "p75"),  ("percentile75", "p75"),  ("75", "p75"),
        ("p90", "p90"),  ("percentile90", "p90"),  ("90", "p90"),
    ]:
        v = pct.get(p_key) or y.get(p_key)
        if v is not None and out_key not in row:
            row[out_key] = _num(v)
    return row


async def get_official_plan_projection(http_session) -> dict:
    """
    Return eMoney's own Monte Carlo plan projection: overall probability of
    success and the asset-spread percentile bands (10th/25th/50th/75th/90th
    portfolio values by year).

    This is the advisor-grade projection that powers the eMoney My Plan view —
    it uses the advisor's return assumptions, goal set, and Monte Carlo engine,
    not the simplified local ``run_monte_carlo_retirement`` calculator. Compare
    the two to sanity-check your local projection against the official plan.

    Returns:
    - ``probability_of_success_pct`` : overall plan success probability
    - ``asset_spread`` : per-year portfolio percentile bands
    - ``retirement_projection`` : retirement-specific projection data (if available)
    """
    http, base, headers, err = await _bff_setup(http_session)
    if err:
        return err

    import asyncio
    (pos_raw, pos_err), (spread_raw, spread_err), (ret_raw, ret_err) = \
        await asyncio.gather(
            _bff_get(http, base, headers, "/projection/montecarlo/probabilityofsuccess"),
            _bff_get(http, base, headers, "/projection/montecarlo/assetspread"),
            _bff_get(http, base, headers, "/projection/retirement"),
            return_exceptions=False,
        )

    result: dict = {}

    # Overall probability of success
    if isinstance(pos_raw, dict):
        pos = (
            pos_raw.get("probabilityOfSuccess")
            or pos_raw.get("probability")
            or pos_raw.get("value")
        )
        result["probability_of_success_pct"] = _pct(pos)
        result["probability_status"] = _status_from_prob(pos if isinstance(pos, float) and pos <= 1 else (pos or 0) / 100)
    elif pos_err:
        result["probability_of_success_pct"] = None
        result["probability_note"] = f"Unavailable ({pos_err})"

    # Asset spread / percentile bands
    if isinstance(spread_raw, (dict, list)):
        years_raw = (
            spread_raw if isinstance(spread_raw, list)
            else spread_raw.get("years") or spread_raw.get("data") or []
        )
        if isinstance(years_raw, list):
            all_spread = [
                _parse_asset_spread_year(y) for y in years_raw
                if isinstance(y, dict)
            ]
            # Compact mode: downsample to every-5th year (#182).
            spread_total = len(all_spread)
            if _is_compact() and spread_total > 10:
                spread_out = all_spread[::5] or all_spread
                result["asset_spread"] = spread_out
                result["output_mode"] = "compact"
                result["asset_spread_total"] = spread_total
                result["asset_spread_shown"] = len(spread_out)
            else:
                result["asset_spread"] = all_spread
    if spread_err and "asset_spread" not in result:
        result["asset_spread_note"] = f"Asset spread unavailable ({spread_err})"

    # Retirement projection details
    if isinstance(ret_raw, dict):
        result["retirement_projection"] = {
            "probability_of_success_pct": _pct(
                ret_raw.get("probabilityOfSuccess") or ret_raw.get("probability")
            ),
            "has_shortfall":       ret_raw.get("hasShortfall"),
            "first_shortfall_year": ret_raw.get("firstShortfallYear"),
            "mean_surplus":        _num(ret_raw.get("meanSurplus")),
            "raw":                 ret_raw,  # pass through full response
        }
    elif ret_err:
        result["retirement_projection_note"] = f"Unavailable ({ret_err})"

    if not result or (
        result.get("probability_of_success_pct") is None
        and "asset_spread" not in result
        and "retirement_projection" not in result
    ):
        return {"error": f"Official plan projection unavailable. "
                         f"pos: {pos_err}, spread: {spread_err}, ret: {ret_err}. "
                         "The plan's Monte Carlo data may not be calculated yet."}

    result["note"] = (
        "Official eMoney Monte Carlo plan projection using the advisor's assumptions "
        "and full goal set. probability_of_success_pct is the share of simulated "
        "scenarios in which all goals are fully funded. asset_spread shows the "
        "portfolio-value distribution (10th–90th percentile) by year. "
        "Compare with run_monte_carlo_retirement for a quick local sanity-check."
        + (" Set EMONEY_COMPACT= to see full asset spread."
           if result.get("output_mode") == "compact" else
           " Set EMONEY_COMPACT=1 to downsample asset_spread to every-5th year.")
    )
    return result