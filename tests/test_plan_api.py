"""
Tests for get_all_goals_funding_status (#96) — plan goals via the internal-api BFF.

The tool resolves clientId/planId (scraped from My Plan), fetches SNB-style
credentials, then GETs two internal-api endpoints. Tests patch the id and
credential helpers and mock the two JSON GETs. No live network.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.scrapers import plan_api

_GOALS_JSON = {
    "retirementGoal": {"hasShortfall": False, "firstShortfallYear": 0,
                       "probabilityOfSuccess": 1.0, "meanSurplus": 82_238_190},
    "leaveToHeirsGoal": {"probabilityOfSuccess": 1.0},
    "otherGoals": [
        {"id": "g1", "name": "Parker Adventures", "meanShortfall": 0,
         "meanSurplus": 0, "probabilityOfSuccess": 1.0},
        {"id": "g2", "name": "Private Education Expense", "meanShortfall": 5000,
         "meanSurplus": 0, "probabilityOfSuccess": 0.62},
    ],
}
_FUNDING_JSON = {"goalTotalFunding": 109_829_514, "goalTotalExpense": 28_959_017}


def _json_resp(body, status=200, ctype="application/json; charset=utf-8"):
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": ctype}
    m.json.return_value = body
    m.text = "{}"
    return m


def _make_session(goals=_GOALS_JSON, funding=_FUNDING_JSON,
                  goals_status=200, funding_status=200):
    async def mock_get(url, **kwargs):
        if "montecarlo/goals" in url:
            return _json_resp(goals, status=goals_status,
                              ctype="application/json" if goals_status == 200 else "text/html")
        if "goalfunding/retirement" in url:
            return _json_resp(funding, status=funding_status,
                              ctype="application/json" if funding_status == 200 else "text/html")
        return _json_resp(None, status=404, ctype="text/html")
    http = AsyncMock()
    http.get = mock_get
    session = AsyncMock()
    session.get_http = AsyncMock(return_value=http)
    return session


def _patches():
    return (
        patch.object(plan_api, "_get_plan_ids",
                     AsyncMock(return_value=("client-guid", "plan-guid", None))),
        patch.object(plan_api, "_get_snb_credentials",
                     AsyncMock(return_value=("jwt-token", "api-key"))),
    )


class TestGetAllGoalsFundingStatus:

    @pytest.mark.asyncio
    async def test_retirement_goal_with_funding(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_all_goals_funding_status(_make_session())
        rg = r["retirement_goal"]
        assert rg["probability_of_success_pct"] == 100.0
        assert rg["status"] == "On Track"
        assert rg["total_funding"] == 109_829_514
        assert rg["total_expense"] == 28_959_017
        assert rg["funded_ratio_pct"] == pytest.approx(379.3, abs=0.1)

    @pytest.mark.asyncio
    async def test_other_goals_status_bands(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_all_goals_funding_status(_make_session())
        by_name = {g["name"]: g for g in r["other_goals"]}
        assert by_name["Parker Adventures"]["status"] == "On Track"          # 100%
        assert by_name["Private Education Expense"]["status"] == "At Risk"    # 62%
        assert by_name["Private Education Expense"]["mean_shortfall"] == 5000

    @pytest.mark.asyncio
    async def test_summary_counts(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_all_goals_funding_status(_make_session())
        # retirement + leave-to-heirs + 2 others = 4 goals; 1 at risk
        assert r["summary"]["total_goals"] == 4
        assert r["summary"]["goals_on_track"] == 3
        assert r["summary"]["goals_needing_attention"] == 1

    @pytest.mark.asyncio
    async def test_funding_optional(self):
        # If the funding endpoint fails, retirement still reports MC status.
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_all_goals_funding_status(
                _make_session(funding_status=500))
        rg = r["retirement_goal"]
        assert rg["probability_of_success_pct"] == 100.0
        assert "total_funding" not in rg or rg.get("total_funding") is None

    @pytest.mark.asyncio
    async def test_goals_endpoint_failure_errors(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_all_goals_funding_status(
                _make_session(goals_status=500))
        assert "error" in r

    @pytest.mark.asyncio
    async def test_plan_id_error_propagates(self):
        with patch.object(plan_api, "_get_plan_ids",
                          AsyncMock(return_value=(None, None, {"error": "Session expired"}))):
            r = await plan_api.get_all_goals_funding_status(AsyncMock())
        assert "error" in r and "expired" in r["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_credentials_errors(self):
        with patch.object(plan_api, "_get_plan_ids",
                          AsyncMock(return_value=("c", "p", None))), \
             patch.object(plan_api, "_get_snb_credentials",
                          AsyncMock(return_value=("", ""))):
            r = await plan_api.get_all_goals_funding_status(AsyncMock())
        assert "error" in r


class TestStatusBand:
    def test_bands(self):
        assert plan_api._status_from_prob(0.95) == "On Track"
        assert plan_api._status_from_prob(0.75) == "Monitor"
        assert plan_api._status_from_prob(0.50) == "At Risk"
        assert plan_api._status_from_prob(None) == "unknown"


def _year(yr, inflow, outflow, pv, nw, growth=0, wd_total=0):
    return {
        "year": yr, "totalCashInflow": inflow, "totalCashOutflow": outflow,
        "netCashFlow": inflow - outflow,
        "withdrawals": {"plannedWithdrawals": {"total": wd_total}, "supplementalWithdrawals": {"total": 0}},
        "portfolioValue": {"totalPortfolioAssets": pv, "totalNetWorth": nw, "portfolioGrowth": growth},
    }


_CASHFLOW = {"years": [
    _year(2026, 500000, 500000, 7_900_000, 8_400_000, 387000, 0),
    _year(2027, 0, 167000, 8_100_000, 8_700_000, 409000, 167000),
    _year(2028, 0, 178000, 8_380_000, 9_000_000, 431000, 178000),
    _year(2029, 0, 190000, 0, 0, 0, 190000),  # depletion year
]}


def _cashflow_session(payload=_CASHFLOW, status=200):
    async def mock_get(url, **kwargs):
        return _json_resp(payload, status=status,
                          ctype="application/json" if status == 200 else "text/html")
    http = AsyncMock(); http.get = mock_get
    session = AsyncMock(); session.get_http = AsyncMock(return_value=http)
    return session


class TestLifetimeCashFlow:

    @pytest.mark.asyncio
    async def test_rows_and_summary(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_lifetime_cash_flow_projection(_cashflow_session())
        assert r["horizon_years"] == 4
        assert r["first_year"] == 2026 and r["last_year"] == 2029
        s = r["summary"]
        assert s["starting_portfolio_value"] == 7_900_000
        assert s["peak_portfolio_year"] == 2028          # 8.38M is the peak before depletion
        assert s["first_negative_cash_flow_year"] == 2027
        assert s["portfolio_depletion_year"] == 2029

    @pytest.mark.asyncio
    async def test_withdrawals_summed(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_lifetime_cash_flow_projection(_cashflow_session())
        assert r["years"][1]["withdrawals"] == 167000
        assert r["years"][1]["net_cash_flow"] == -167000

    @pytest.mark.asyncio
    async def test_year_range_filter(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_lifetime_cash_flow_projection(
                _cashflow_session(), start_year=2027, end_year=2028)
        assert [y["year"] for y in r["years"]] == [2027, 2028]

    @pytest.mark.asyncio
    async def test_endpoint_failure_errors(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_lifetime_cash_flow_projection(_cashflow_session(status=500))
        assert "error" in r

    @pytest.mark.asyncio
    async def test_empty_range_errors(self):
        p1, p2 = _patches()
        with p1, p2:
            r = await plan_api.get_lifetime_cash_flow_projection(
                _cashflow_session(), start_year=2099)
        assert "error" in r
