"""Tests for get_long_term_care_analysis (#78) — pure LTC cost/self-insure calculator."""

from unittest.mock import AsyncMock, patch

import pytest

from emoney_mcp.scrapers.planning import get_long_term_care_analysis
from helpers import make_mock_http_session


def _accts(net_worth: float):
    """Minimal get_accounts shape: net worth, no illiquid real estate →
    investable_assets == net_worth (see _calc_investable_assets)."""
    return {"net_worth": net_worth, "account_groups": []}


def _patch_accts(net_worth: float):
    return patch("emoney_mcp.scrapers.planning.get_accounts",
                 new=AsyncMock(return_value=_accts(net_worth)))


class TestLongTermCareAnalysis:
    @pytest.mark.asyncio
    async def test_basic_projection_self_insurable(self):
        session = make_mock_http_session()
        with _patch_accts(5_000_000):
            r = await get_long_term_care_analysis(session, current_age=60, care_age=80, care_years=3)
        # 20 years of inflation → future cost well above today's annual median
        assert r["projected_cost"]["total_care_cost_future_dollars"] > 0
        assert r["self_insure"]["status"] == "self_insurable"
        assert r["self_insure"]["net_need_pct_of_portfolio"] <= 25
        assert len(r["yearly_schedule"]) == 3

    @pytest.mark.asyncio
    async def test_small_portfolio_recommends_insurance(self):
        session = make_mock_http_session()
        with _patch_accts(150_000):
            r = await get_long_term_care_analysis(session, current_age=78, care_age=80, care_years=4)
        assert r["self_insure"]["status"] == "insurance_recommended"

    @pytest.mark.asyncio
    async def test_existing_policy_can_fully_cover(self):
        session = make_mock_http_session()
        with _patch_accts(1_000_000):
            r = await get_long_term_care_analysis(
                session, current_age=79, care_age=80, care_years=2,
                existing_annual_benefit=200_000,  # far above the annual cost
            )
        assert r["projected_cost"]["net_self_pay_need"] == 0
        assert r["self_insure"]["status"] == "covered_by_policy"
        # policy benefit is capped at the actual cost — never exceeds it
        assert (r["projected_cost"]["total_policy_benefit_future"]
                <= r["projected_cost"]["total_care_cost_future_dollars"])

    @pytest.mark.asyncio
    async def test_inflation_makes_future_cost_exceed_today(self):
        session = make_mock_http_session()
        with _patch_accts(2_000_000):
            r = await get_long_term_care_analysis(
                session, current_age=60, care_age=80, care_years=1,
                care_setting="assisted_living", ltc_inflation=0.05,
            )
        today_annual = r["cost_assumptions"]["annual_cost_today_total"]
        first_year = r["projected_cost"]["first_year_annual_cost"]
        assert first_year > today_annual  # 20 yrs of 5% inflation

    @pytest.mark.asyncio
    async def test_couple_scales_cost(self):
        session = make_mock_http_session()
        with _patch_accts(3_000_000):
            single = await get_long_term_care_analysis(session, current_age=70, coverage="individual")
        with _patch_accts(3_000_000):
            couple = await get_long_term_care_analysis(session, current_age=70, coverage="couple")
        assert couple["people"] == 2
        assert (couple["cost_assumptions"]["annual_cost_today_total"]
                == pytest.approx(2 * single["cost_assumptions"]["annual_cost_today_total"]))

    @pytest.mark.asyncio
    async def test_state_index_applied(self):
        session = make_mock_http_session()
        with _patch_accts(2_000_000):
            ny = await get_long_term_care_analysis(session, current_age=70, state="NY")
        assert ny["cost_assumptions"]["regional_multiplier"] == 1.35
        assert "NY" in ny["cost_assumptions"]["regional_multiplier_source"]

    @pytest.mark.asyncio
    async def test_cost_multiplier_overrides_state(self):
        session = make_mock_http_session()
        with _patch_accts(2_000_000):
            r = await get_long_term_care_analysis(
                session, current_age=70, state="NY", cost_multiplier=2.0)
        assert r["cost_assumptions"]["regional_multiplier"] == 2.0
        assert "cost_multiplier" in r["cost_assumptions"]["regional_multiplier_source"]

    @pytest.mark.asyncio
    async def test_daily_cost_override(self):
        session = make_mock_http_session()
        with _patch_accts(2_000_000):
            r = await get_long_term_care_analysis(session, current_age=70, daily_cost=400)
        assert r["cost_assumptions"]["annual_cost_today_per_person"] == pytest.approx(400 * 365)

    @pytest.mark.asyncio
    async def test_fractional_care_years(self):
        session = make_mock_http_session()
        with _patch_accts(2_000_000):
            r = await get_long_term_care_analysis(session, current_age=79, care_age=80, care_years=2.5)
        # 2 full years + 1 partial → 3 schedule rows, last one is the half year
        assert len(r["yearly_schedule"]) == 3

    @pytest.mark.asyncio
    async def test_invalid_inputs(self):
        session = make_mock_http_session()
        assert "error" in await get_long_term_care_analysis(session, current_age=0)
        assert "error" in await get_long_term_care_analysis(session, current_age=80, care_age=70)
        assert "error" in await get_long_term_care_analysis(session, current_age=70, care_years=0)
        assert "error" in await get_long_term_care_analysis(session, current_age=70, care_setting="spa")

    @pytest.mark.asyncio
    async def test_accounts_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts",
                   new=AsyncMock(return_value={"error": "Card 9 unavailable"})):
            r = await get_long_term_care_analysis(session, current_age=70)
        assert "error" in r
