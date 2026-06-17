"""
Tests for calculator wave 4 (#97, #81):
  - model_life_event_scenario        (retirement.py)
  - get_estate_liquidity_analysis    (planning.py)

Pure calculators — no live network. Account/breakdown dependencies are patched
at the scraper-module level.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import emoney_mcp.scrapers.retirement as ret
import emoney_mcp.scrapers.planning as pl


_ACCTS = {"net_worth": 2_000_000, "total_assets": 2_300_000,
          "total_liabilities": 300_000, "account_groups": []}

_NWB = {"net_worth": 2_000_000, "total_assets": 2_300_000, "by_liquidity": [
    {"bucket": "Liquid", "value": 150_000, "percent": 6.5},
    {"bucket": "Semi-liquid", "value": 650_000, "percent": 28.3},
    {"bucket": "Illiquid", "value": 1_500_000, "percent": 65.2}]}


# ---------------------------------------------------------------------------
# model_life_event_scenario  (#97)
# ---------------------------------------------------------------------------

class TestModelLifeEventScenario:

    async def _run(self, event, params=None, **kw):
        with patch.object(ret, "get_accounts", AsyncMock(return_value=_ACCTS)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            return await ret.model_life_event_scenario(
                AsyncMock(), event=event, params=params or {}, **kw)

    @pytest.mark.asyncio
    async def test_unknown_event_errors(self):
        r = await self._run("alien_invasion")
        assert "error" in r

    @pytest.mark.asyncio
    async def test_market_crash_reduces_outcome(self):
        r = await self._run("market_crash", {"drop_pct": 0.4}, years=30)
        assert r["scenario"]["ending_balance"] < r["baseline"]["ending_balance"]
        assert r["scenario_inputs"]["one_time_shock_pct"] == 40.0

    @pytest.mark.asyncio
    async def test_downsizing_improves_outcome(self):
        # Frees equity into the portfolio and cuts spending → better than baseline.
        r = await self._run("downsizing", {"equity_freed": 200_000, "monthly_spending_change": -500})
        assert r["impact"]["ending_balance_delta"] > 0

    @pytest.mark.asyncio
    async def test_home_purchase_lump_and_recurring(self):
        r = await self._run("home_purchase", {"down_payment": 300_000, "monthly_payment_change": 1_500})
        # portfolio reduced by the down payment; spending raised by 12×payment
        assert r["scenario_inputs"]["investable_portfolio"] == pytest.approx(
            r["baseline_inputs"]["investable_portfolio"] - 300_000, abs=1)
        assert r["scenario_inputs"]["annual_spending"] == pytest.approx(
            r["baseline_inputs"]["annual_spending"] + 18_000, abs=1)

    @pytest.mark.asyncio
    async def test_new_child_temporary_spending(self):
        r = await self._run("new_child", {"annual_cost": 15_000, "years": 18})
        assert r["scenario_inputs"]["temporary_extra_spending"] == 15_000
        assert r["scenario_inputs"]["temporary_extra_spending_years"] == 18

    @pytest.mark.asyncio
    async def test_defaults_applied_without_params(self):
        r = await self._run("job_loss")
        # default 6 months of expenses drawn from the portfolio
        assert r["scenario_inputs"]["investable_portfolio"] < r["baseline_inputs"]["investable_portfolio"]
        assert r["impact"]["key_tradeoff"]

    @pytest.mark.asyncio
    async def test_no_portfolio_errors(self):
        empty = {"net_worth": 0, "total_assets": 0, "total_liabilities": 0, "account_groups": []}
        with patch.object(ret, "get_accounts", AsyncMock(return_value=empty)):
            r = await ret.model_life_event_scenario(AsyncMock(), event="market_crash")
        assert "error" in r


# ---------------------------------------------------------------------------
# get_estate_liquidity_analysis  (#81)
# ---------------------------------------------------------------------------

class TestEstateLiquidityAnalysis:

    @pytest.mark.asyncio
    async def test_under_exemption_no_estate_tax(self):
        with patch.object(pl, "get_net_worth_breakdown", AsyncMock(return_value=_NWB)):
            r = await pl.get_estate_liquidity_analysis(AsyncMock(), filing_status="mfj")
        assert r["settlement_need"]["estate_tax"] == 0.0
        # need = debts (300k) + final expenses (15k)
        assert r["settlement_need"]["total"] == pytest.approx(315_000, abs=1)

    @pytest.mark.asyncio
    async def test_liquid_estate_covers_need(self):
        with patch.object(pl, "get_net_worth_breakdown", AsyncMock(return_value=_NWB)):
            r = await pl.get_estate_liquidity_analysis(AsyncMock())
        # marketable = 150k + 650k×0.85 = 702.5k > 315k need
        assert r["marketable_resources"] == pytest.approx(702_500, abs=1)
        assert r["surplus_or_shortfall"] > 0
        assert r["status"] == "liquid"
        assert r["forced_sale_risk"] is False

    @pytest.mark.asyncio
    async def test_large_illiquid_estate_forced_sale_risk(self):
        big = {"net_worth": 20_000_000, "total_assets": 20_300_000, "by_liquidity": [
            {"bucket": "Liquid", "value": 200_000, "percent": 1},
            {"bucket": "Semi-liquid", "value": 800_000, "percent": 4},
            {"bucket": "Illiquid", "value": 19_300_000, "percent": 95}]}
        with patch.object(pl, "get_net_worth_breakdown", AsyncMock(return_value=big)):
            r = await pl.get_estate_liquidity_analysis(AsyncMock(), filing_status="single")
        # 40% × (20M − 13.61M exemption) = 2.556M estate tax
        assert r["settlement_need"]["estate_tax"] == pytest.approx(2_556_000, abs=1)
        assert r["surplus_or_shortfall"] < 0
        assert r["forced_sale_risk"] is True
        assert r["status"] == "forced_sale_risk"

    @pytest.mark.asyncio
    async def test_mfj_doubled_exemption(self):
        # $20M estate is under the $27.2M MFJ exemption → no estate tax.
        big = {"net_worth": 20_000_000, "total_assets": 20_000_000, "by_liquidity": [
            {"bucket": "Liquid", "value": 1_000_000, "percent": 5},
            {"bucket": "Semi-liquid", "value": 0, "percent": 0},
            {"bucket": "Illiquid", "value": 19_000_000, "percent": 95}]}
        with patch.object(pl, "get_net_worth_breakdown", AsyncMock(return_value=big)):
            r = await pl.get_estate_liquidity_analysis(AsyncMock(), filing_status="mfj")
        assert r["settlement_need"]["estate_tax"] == 0.0

    @pytest.mark.asyncio
    async def test_propagates_breakdown_error(self):
        with patch.object(pl, "get_net_worth_breakdown",
                          AsyncMock(return_value={"error": "session expired"})):
            r = await pl.get_estate_liquidity_analysis(AsyncMock())
        assert "error" in r
