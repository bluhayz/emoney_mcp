"""
Tests for calculator wave 2 (#89, #90, #102):
  - get_charitable_giving_strategy   (tax.py)
  - get_tax_gain_harvesting          (tax.py)
  - get_state_tax_estimate           (tax.py)
  - get_healthcare_cost_projection   (planning.py)
  - get_hsa_optimization             (planning.py)

Pure calculators — no live network. Holdings/income/account dependencies are
patched at the scraper-module level (the same seam other tax tests use).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import emoney_mcp.scrapers.tax as tax
import emoney_mcp.scrapers.planning as planning


# Two appreciated taxable lots; NIIT not applicable.
_CGE = {
    "taxable_account_positions": [
        {"ticker": "VTI", "description": "Total Market", "account": "Brokerage",
         "current_value": 50_000, "cost_basis": 20_000, "unrealized_gain": 30_000},
        {"ticker": "AAPL", "description": "Apple", "account": "Brokerage",
         "current_value": 12_000, "cost_basis": 4_000, "unrealized_gain": 8_000},
    ],
    "niit_applies": False,
}
_RET = {
    "retirement_accounts": [{"name": "Traditional IRA", "type": "IRA", "balance": 500_000}],
    "retirement_breakdown": {"hsa": 15_000},
}


# ---------------------------------------------------------------------------
# get_charitable_giving_strategy  (#89)
# ---------------------------------------------------------------------------

class TestCharitableGivingStrategy:

    @pytest.mark.asyncio
    async def test_qcd_recommended_when_eligible(self):
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)), \
             patch.object(tax, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await tax.get_charitable_giving_strategy(
                AsyncMock(), annual_giving=20_000, age=72, filing_status="mfj",
                current_income=120_000)
        vehicles = {s["vehicle"]: s for s in r["strategies"]}
        qcd = vehicles["Qualified Charitable Distribution (QCD)"]
        assert qcd["eligible"] is True
        assert qcd["recommended_amount"] == 20_000  # capped by the gift, not the IRA
        assert qcd["estimated_tax_benefit"] > 0

    @pytest.mark.asyncio
    async def test_qcd_blocked_under_age(self):
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)), \
             patch.object(tax, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await tax.get_charitable_giving_strategy(
                AsyncMock(), annual_giving=20_000, age=60, current_income=120_000)
        qcd = next(s for s in r["strategies"] if s["vehicle"].startswith("Qualified"))
        assert qcd["eligible"] is False
        assert qcd["recommended_amount"] == 0.0

    @pytest.mark.asyncio
    async def test_bunching_flagged_below_standard_deduction(self):
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)), \
             patch.object(tax, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await tax.get_charitable_giving_strategy(
                AsyncMock(), annual_giving=10_000, age=50, current_income=120_000)
        daf = next(s for s in r["strategies"] if "Donor-Advised" in s["vehicle"])
        # $10k/yr < $32,200 std deduction → bunching recommended, >1 year
        assert daf["eligible"] is True
        assert daf["recommended_bunch_years"] >= 2

    @pytest.mark.asyncio
    async def test_appreciated_gift_saves_cap_gains_at_higher_income(self):
        # High income → LTCG rate 15%, so gifting appreciated shares avoids real tax.
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)), \
             patch.object(tax, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await tax.get_charitable_giving_strategy(
                AsyncMock(), annual_giving=20_000, age=50, current_income=300_000)
        sec = next(s for s in r["strategies"] if "appreciated" in s["vehicle"].lower())
        assert sec["eligible"] is True
        assert sec["estimated_tax_benefit"] > 0
        assert len(sec["candidate_lots"]) >= 1

    @pytest.mark.asyncio
    async def test_rejects_nonpositive_giving(self):
        r = await tax.get_charitable_giving_strategy(AsyncMock(), annual_giving=0)
        assert "error" in r


# ---------------------------------------------------------------------------
# get_tax_gain_harvesting  (#90)
# ---------------------------------------------------------------------------

class TestTaxGainHarvesting:

    @pytest.mark.asyncio
    async def test_room_and_plan_in_low_bracket(self):
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)):
            r = await tax.get_tax_gain_harvesting(
                AsyncMock(), filing_status="mfj", annual_income=60_000)
        # taxable = 60000 - 32200 = 27800; 0% ceiling mfj = 98900 → room 71100
        assert r["room_in_0pct_bracket"] == pytest.approx(71_100, abs=1)
        # both lots' gains ($38k) fit under the room
        assert r["harvestable_gain_at_0pct"] == pytest.approx(38_000, abs=1)
        assert len(r["harvest_plan"]) == 2
        assert r["estimated_future_tax_saved"] == pytest.approx(38_000 * 0.15, abs=1)

    @pytest.mark.asyncio
    async def test_no_room_when_income_high(self):
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)):
            r = await tax.get_tax_gain_harvesting(
                AsyncMock(), filing_status="mfj", annual_income=200_000)
        assert r["room_in_0pct_bracket"] == 0.0
        assert r["harvestable_gain_at_0pct"] == 0.0
        assert r["harvest_plan"] == []

    @pytest.mark.asyncio
    async def test_partial_fill_caps_at_room(self):
        # Income leaving only ~$10k of 0% room; harvest is capped to that.
        with patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value=_CGE)):
            r = await tax.get_tax_gain_harvesting(
                AsyncMock(), filing_status="mfj", annual_income=121_100)  # taxable 88900, room 10000
        assert r["room_in_0pct_bracket"] == pytest.approx(10_000, abs=1)
        assert r["harvestable_gain_at_0pct"] == pytest.approx(10_000, abs=1)

    @pytest.mark.asyncio
    async def test_propagates_holdings_error(self):
        with patch.object(tax, "get_capital_gains_exposure",
                          AsyncMock(return_value={"error": "session expired"})):
            r = await tax.get_tax_gain_harvesting(AsyncMock(), annual_income=60_000)
        assert "error" in r


# ---------------------------------------------------------------------------
# get_state_tax_estimate  (#90)
# ---------------------------------------------------------------------------

class TestStateTaxEstimate:

    @pytest.mark.asyncio
    async def test_flat_state_exact(self):
        r = await tax.get_state_tax_estimate(AsyncMock(), state="PA", amount=50_000)
        assert r["state_code"] == "PA"
        assert r["rate_is_flat"] is True
        assert r["estimated_state_tax"] == pytest.approx(50_000 * 0.0307, abs=1)

    @pytest.mark.asyncio
    async def test_no_income_tax_state(self):
        r = await tax.get_state_tax_estimate(AsyncMock(), state="Texas", amount=100_000)
        assert r["state_code"] == "TX"
        assert r["estimated_state_tax"] == 0.0
        assert r["no_state_income_tax"] is True

    @pytest.mark.asyncio
    async def test_washington_ltcg_special_case(self):
        ordinary = await tax.get_state_tax_estimate(AsyncMock(), state="WA", amount=100_000, income_type="ordinary")
        ltcg     = await tax.get_state_tax_estimate(AsyncMock(), state="WA", amount=100_000, income_type="ltcg")
        assert ordinary["estimated_state_tax"] == 0.0
        assert ltcg["estimated_state_tax"] == pytest.approx(7_000, abs=1)
        assert ltcg["no_state_income_tax"] is False  # the LTCG tax does apply

    @pytest.mark.asyncio
    async def test_full_name_resolution(self):
        r = await tax.get_state_tax_estimate(AsyncMock(), state="california", amount=100_000)
        assert r["state_code"] == "CA"
        assert r["estimated_state_tax"] == pytest.approx(13_300, abs=1)

    @pytest.mark.asyncio
    async def test_unknown_state_errors(self):
        r = await tax.get_state_tax_estimate(AsyncMock(), state="ZZ", amount=1_000)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_negative_amount_errors(self):
        r = await tax.get_state_tax_estimate(AsyncMock(), state="CA", amount=-5)
        assert "error" in r


# ---------------------------------------------------------------------------
# get_healthcare_cost_projection  (#102)
# ---------------------------------------------------------------------------

class TestHealthcareCostProjection:

    @pytest.mark.asyncio
    async def test_two_phase_split(self):
        r = await planning.get_healthcare_cost_projection(
            AsyncMock(), current_age=60, retirement_age=62, coverage="couple", life_expectancy=90)
        assert r["people"] == 2
        assert r["phase_totals"]["pre_65_aca_total"] > 0      # ages 62-64 on ACA
        assert r["phase_totals"]["post_65_medicare_total"] > 0
        assert r["total_projected_healthcare_cost"] == pytest.approx(
            r["phase_totals"]["pre_65_aca_total"] + r["phase_totals"]["post_65_medicare_total"], abs=1)
        # 62..90 inclusive = 29 rows
        assert len(r["annual_schedule"]) == 29

    @pytest.mark.asyncio
    async def test_no_pre65_when_retiring_at_65(self):
        r = await planning.get_healthcare_cost_projection(
            AsyncMock(), current_age=64, retirement_age=65, coverage="individual", life_expectancy=85)
        assert r["phase_totals"]["pre_65_aca_total"] == 0.0
        assert r["people"] == 1

    @pytest.mark.asyncio
    async def test_inflation_increases_later_years(self):
        r = await planning.get_healthcare_cost_projection(
            AsyncMock(), current_age=65, retirement_age=65, life_expectancy=90, health_inflation=0.05)
        first = r["annual_schedule"][0]["annual_cost"]
        last = r["annual_schedule"][-1]["annual_cost"]
        assert last > first

    @pytest.mark.asyncio
    async def test_rejects_bad_age(self):
        r = await planning.get_healthcare_cost_projection(AsyncMock(), current_age=0)
        assert "error" in r


# ---------------------------------------------------------------------------
# get_hsa_optimization  (#102)
# ---------------------------------------------------------------------------

class TestHsaOptimization:

    @pytest.mark.asyncio
    async def test_pulls_balance_and_projects(self):
        with patch.object(planning, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await planning.get_hsa_optimization(
                AsyncMock(), current_age=45, coverage="family")
        assert r["current_hsa_balance"] == 15_000
        assert r["balance_source"] == "Emoney retirement accounts"
        assert r["contribution_limit"] == 8_750          # family, under 55
        assert r["projection"]["years_projected"] == 20  # 45 -> 65
        assert r["projection"]["projected_balance"] > 15_000
        assert r["annual_tax_savings"]["combined_if_payroll"] > 0

    @pytest.mark.asyncio
    async def test_catchup_at_55(self):
        r = await planning.get_hsa_optimization(
            AsyncMock(), current_age=58, current_hsa_balance=50_000, coverage="individual")
        assert r["catch_up_eligible_55plus"] is True
        assert r["contribution_limit"] == 4_400 + 1_000  # individual + catch-up

    @pytest.mark.asyncio
    async def test_provided_balance_skips_fetch(self):
        # No patch on get_retirement_accounts — must not be called when balance given.
        r = await planning.get_hsa_optimization(
            AsyncMock(), current_age=40, current_hsa_balance=25_000, coverage="family")
        assert r["balance_source"] == "provided"
        assert r["current_hsa_balance"] == 25_000

    @pytest.mark.asyncio
    async def test_recommends_investing(self):
        r = await planning.get_hsa_optimization(
            AsyncMock(), current_age=40, current_hsa_balance=10_000)
        assert r["invest_vs_spend"]["recommendation"] == "invest"
        assert len(r["triple_tax_advantage"]) == 3
