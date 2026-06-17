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

Discovered via live network capture (epic #106, discovery pass 2 / token flow).
Endpoints used:
  GET .../plans/<plan>/projection/montecarlo/goals        (per-goal success)
  GET .../plans/<plan>/projection/goalfunding/retirement  (retirement $ funding)
  GET .../plans/<plan>/projection/linear/cashflow/details (lifetime cash flow)
"""

import re

from ._helpers import BASE_URL
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

    return {
        "horizon_years":   len(rows),
        "first_year":      rows[0]["year"],
        "last_year":       ending["year"],
        "scenario":        "linear (average-return plan assumptions)",
        "summary": {
            "starting_portfolio_value": rows[0]["portfolio_value"],
            "ending_portfolio_value":   ending["portfolio_value"],
            "ending_net_worth":         ending["net_worth"],
            "peak_portfolio_value":     peak["portfolio_value"],
            "peak_portfolio_year":      peak["year"],
            "first_negative_cash_flow_year": first_negative,
            "portfolio_depletion_year": depletion_year,
        },
        "years": rows,
        "note": (
            "Year-by-year lifetime cash flow from the plan's 'linear' projection (average-return "
            "assumptions, not Monte Carlo ranges). net_cash_flow turning negative is normal in "
            "retirement (portfolio funds the gap); portfolio_depletion_year is null when the plan "
            "never runs out. Figures use the advisor's plan assumptions and inflation, not live "
            "market values. For probability-of-success see get_all_goals_funding_status."
        ),
    }
