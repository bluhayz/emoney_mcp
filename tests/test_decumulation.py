"""
Tests for the decumulation calculators:
  - get_withdrawal_sequencing_strategy (#84)
  - get_retirement_income_plan (#83)
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _breakdown(taxable, deferred, free):
    return {
        "net_worth": taxable + deferred + free,
        "by_tax_treatment": [
            {"bucket": "Taxable", "value": taxable, "percent": 0},
            {"bucket": "Tax-Deferred", "value": deferred, "percent": 0},
            {"bucket": "Tax-Free", "value": free, "percent": 0},
        ],
    }


class TestWithdrawalSequencing:

    @pytest.mark.asyncio
    async def test_structure_and_consistency(self):
        from emoney_mcp.scrapers.retirement import get_withdrawal_sequencing_strategy
        with patch("emoney_mcp.scrapers.retirement.get_net_worth_breakdown",
                   return_value=_breakdown(400_000, 600_000, 200_000)):
            r = await get_withdrawal_sequencing_strategy(AsyncMock(), annual_need=60_000, years=25)
        assert r["recommended_order"] == ["taxable", "tax_deferred", "roth"]
        for strat in ("tax_efficient_strategy", "proportional_strategy"):
            assert r[strat]["total_tax"] >= 0
            assert r[strat]["years_funded"] > 0
        # The headline number must reconcile with the two strategies.
        expected = round(r["proportional_strategy"]["total_tax"]
                         - r["tax_efficient_strategy"]["total_tax"], 2)
        assert r["estimated_lifetime_tax_saved"] == expected

    @pytest.mark.asyncio
    async def test_single_bucket_strategies_equal(self):
        from emoney_mcp.scrapers.retirement import get_withdrawal_sequencing_strategy
        # With only one bucket, both orders draw identically -> no tax difference.
        with patch("emoney_mcp.scrapers.retirement.get_net_worth_breakdown",
                   return_value=_breakdown(0, 800_000, 0)):
            r = await get_withdrawal_sequencing_strategy(AsyncMock(), annual_need=40_000, years=15)
        assert r["tax_efficient_strategy"]["total_tax"] == r["proportional_strategy"]["total_tax"]
        assert r["estimated_lifetime_tax_saved"] == 0.0

    @pytest.mark.asyncio
    async def test_no_balances_errors(self):
        from emoney_mcp.scrapers.retirement import get_withdrawal_sequencing_strategy
        with patch("emoney_mcp.scrapers.retirement.get_net_worth_breakdown",
                   return_value=_breakdown(0, 0, 0)):
            r = await get_withdrawal_sequencing_strategy(AsyncMock(), annual_need=50_000)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        from emoney_mcp.scrapers.retirement import get_withdrawal_sequencing_strategy
        with patch("emoney_mcp.scrapers.retirement.get_net_worth_breakdown",
                   return_value={"error": "unavailable"}):
            r = await get_withdrawal_sequencing_strategy(AsyncMock(), annual_need=50_000)
        assert "error" in r


class TestRetirementIncomePlan:

    _ACCTS = {
        "net_worth": 1_200_000, "total_assets": 1_200_000, "total_liabilities": 0,
        "account_groups": [
            {"group": "Investments", "accounts": [
                {"name": "Brokerage", "type": "InvestmentAsset", "balance": 1_200_000},
            ]},
        ],
    }

    @pytest.mark.asyncio
    async def test_guaranteed_income_and_gap(self):
        from emoney_mcp.scrapers.retirement import get_retirement_income_plan
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=self._ACCTS):
            r = await get_retirement_income_plan(AsyncMock(), retire_age=65, birth_year=1962,
                                                 annual_spending=80_000,
                                                 social_security_annual=36_000, ss_claim_age=70,
                                                 years=15)
        assert len(r["plan"]) == 15
        # Before SS claim age: no guaranteed income, full spending withdrawn.
        first = r["plan"][0]
        assert first["age"] == 65
        assert first["guaranteed_income"] == 0
        assert first["portfolio_withdrawal"] == 80_000
        # At/after the SS claim age, guaranteed income kicks in and the gap shrinks.
        at_70 = next(row for row in r["plan"] if row["age"] == 70)
        assert at_70["social_security"] == 36_000
        assert at_70["portfolio_withdrawal"] == 80_000 - 36_000

    @pytest.mark.asyncio
    async def test_requires_spending(self):
        from emoney_mcp.scrapers.retirement import get_retirement_income_plan
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=self._ACCTS), \
             patch("emoney_mcp.scrapers.retirement._fetch_snb_data",
                   new=AsyncMock(return_value=([], False))):
            r = await get_retirement_income_plan(AsyncMock(), retire_age=65, birth_year=1962)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        from emoney_mcp.scrapers.retirement import get_retirement_income_plan
        with patch("emoney_mcp.scrapers.retirement.get_accounts",
                   return_value={"error": "Card unavailable"}):
            r = await get_retirement_income_plan(AsyncMock(), retire_age=65, birth_year=1962,
                                                 annual_spending=80_000)
        assert "error" in r
