"""
Tests for the mortgage analysis suite (#99):
  - get_mortgage_amortization_schedule
  - get_mortgage_refinance_analysis
  - get_mortgage_payoff_vs_invest
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestAmortization:

    @pytest.mark.asyncio
    async def test_standard_30yr(self):
        from emoney_mcp.scrapers.planning import get_mortgage_amortization_schedule
        r = await get_mortgage_amortization_schedule(AsyncMock(), balance=400_000,
                                                     annual_rate=0.06, years_remaining=30)
        # Known: $400k @ 6% / 30yr ≈ $2,398/mo.
        assert abs(r["monthly_payment"] - 2398.20) < 1.0
        assert r["months_to_payoff"] == 360
        assert r["total_interest"] > 0
        # Last schedule row should be ~zero balance.
        assert r["yearly_schedule"][-1]["ending_balance"] < 1.0

    @pytest.mark.asyncio
    async def test_extra_payment_saves_interest_and_time(self):
        from emoney_mcp.scrapers.planning import get_mortgage_amortization_schedule
        r = await get_mortgage_amortization_schedule(AsyncMock(), balance=400_000,
                                                     annual_rate=0.06, years_remaining=30,
                                                     extra_monthly=500)
        assert r["months_to_payoff"] < 360
        assert r["interest_saved_vs_no_extra"] > 0

    @pytest.mark.asyncio
    async def test_bad_input(self):
        from emoney_mcp.scrapers.planning import get_mortgage_amortization_schedule
        r = await get_mortgage_amortization_schedule(AsyncMock(), balance=0,
                                                     annual_rate=0.06, years_remaining=30)
        assert "error" in r


class TestRefinance:

    @pytest.mark.asyncio
    async def test_lower_rate_saves_monthly(self):
        from emoney_mcp.scrapers.planning import get_mortgage_refinance_analysis
        r = await get_mortgage_refinance_analysis(AsyncMock(), balance=400_000,
                                                  current_rate=0.07, current_years_remaining=28,
                                                  new_rate=0.055, new_term_years=30,
                                                  closing_costs=6_000)
        assert r["monthly_savings"] > 0
        assert r["break_even_months"] == round(6_000 / r["monthly_savings"], 1)

    @pytest.mark.asyncio
    async def test_no_savings_no_breakeven(self):
        from emoney_mcp.scrapers.planning import get_mortgage_refinance_analysis
        r = await get_mortgage_refinance_analysis(AsyncMock(), balance=400_000,
                                                  current_rate=0.05, current_years_remaining=25,
                                                  new_rate=0.07, new_term_years=25,
                                                  closing_costs=6_000)
        assert r["monthly_savings"] < 0
        assert r["break_even_months"] is None


class TestPayoffVsInvest:

    @pytest.mark.asyncio
    async def test_high_return_favors_investing(self):
        from emoney_mcp.scrapers.planning import get_mortgage_payoff_vs_invest
        r = await get_mortgage_payoff_vs_invest(AsyncMock(), balance=300_000,
                                                annual_rate=0.04, years_remaining=20,
                                                extra_monthly=500, investment_return=0.09)
        assert r["advantage"] == "invest"

    @pytest.mark.asyncio
    async def test_structure(self):
        from emoney_mcp.scrapers.planning import get_mortgage_payoff_vs_invest
        r = await get_mortgage_payoff_vs_invest(AsyncMock(), balance=300_000,
                                                annual_rate=0.07, years_remaining=20,
                                                extra_monthly=500)
        assert "interest_saved" in r["pay_off_mortgage"]
        assert "after_tax_value" in r["invest_instead"]
        assert r["advantage"] in ("invest", "pay_off_mortgage")
