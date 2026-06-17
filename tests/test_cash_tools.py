"""
Tests for the cash/liquidity calculators (#101):
  - get_emergency_fund_analysis
  - get_idle_cash_optimization
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _accts(groups):
    return {"net_worth": 0, "total_assets": 0, "total_liabilities": 0, "account_groups": groups}


def _spend_txns(monthly):
    # 90 days of spending so the tool's /3 average == monthly.
    return [
        {"amount": monthly, "is_income": False, "is_excluded": False, "category": "X", "date": "2026-04-01"},
        {"amount": monthly, "is_income": False, "is_excluded": False, "category": "X", "date": "2026-05-01"},
        {"amount": monthly, "is_income": False, "is_excluded": False, "category": "X", "date": "2026-06-01"},
        {"amount": 9999, "is_income": True, "is_excluded": False, "category": "Pay", "date": "2026-06-02"},
    ]


class TestEmergencyFund:

    @pytest.mark.asyncio
    async def test_funded_status(self):
        from emoney_mcp.scrapers.goals import get_emergency_fund_analysis
        accts = _accts([{"group": "Cash & Banking", "total": 60_000,
                         "accounts": [{"name": "Savings", "balance": 60_000}]}])
        with patch("emoney_mcp.scrapers.goals.get_accounts", return_value=accts), \
             patch("emoney_mcp.scrapers.goals._fetch_snb_data",
                   new=AsyncMock(return_value=(_spend_txns(8_000), True))):
            r = await get_emergency_fund_analysis(AsyncMock(), target_months=6)
        assert r["liquid_cash"] == 60_000
        assert r["monthly_spending"] == 8_000
        assert r["months_covered"] == 7.5
        assert r["target_amount"] == 48_000
        assert r["surplus_or_shortfall"] == 12_000
        assert r["status"] == "funded"

    @pytest.mark.asyncio
    async def test_underfunded_status(self):
        from emoney_mcp.scrapers.goals import get_emergency_fund_analysis
        accts = _accts([{"group": "Cash", "total": 5_000,
                         "accounts": [{"name": "Checking", "balance": 5_000}]}])
        with patch("emoney_mcp.scrapers.goals.get_accounts", return_value=accts), \
             patch("emoney_mcp.scrapers.goals._fetch_snb_data",
                   new=AsyncMock(return_value=(_spend_txns(8_000), True))):
            r = await get_emergency_fund_analysis(AsyncMock(), target_months=6)
        assert r["status"] == "underfunded"
        assert r["surplus_or_shortfall"] < 0

    @pytest.mark.asyncio
    async def test_no_spending_errors(self):
        from emoney_mcp.scrapers.goals import get_emergency_fund_analysis
        accts = _accts([{"group": "Cash", "total": 10_000, "accounts": []}])
        with patch("emoney_mcp.scrapers.goals.get_accounts", return_value=accts), \
             patch("emoney_mcp.scrapers.goals._fetch_snb_data",
                   new=AsyncMock(return_value=([], False))):
            r = await get_emergency_fund_analysis(AsyncMock())
        assert "error" in r


class TestIdleCash:

    @pytest.mark.asyncio
    async def test_uplift_and_deployable(self):
        from emoney_mcp.scrapers.goals import get_idle_cash_optimization
        accts = _accts([{"group": "Cash & Banking", "total": 100_000, "accounts": [
            {"name": "Checking", "balance": 40_000},
            {"name": "Savings",  "balance": 60_000},
        ]}])
        with patch("emoney_mcp.scrapers.goals.get_accounts", return_value=accts):
            r = await get_idle_cash_optimization(AsyncMock(), hysa_apy=0.045,
                                                 assumed_current_apy=0.005, keep_in_checking=20_000)
        assert r["total_cash"] == 100_000
        assert r["deployable_cash"] == 80_000
        # 80k * (0.045 - 0.005) = 3,200
        assert r["estimated_annual_income_uplift"] == 3_200
        assert len(r["cash_accounts"]) == 2
        assert r["cash_accounts"][0]["name"] == "Savings"   # sorted by balance desc

    @pytest.mark.asyncio
    async def test_no_cash(self):
        from emoney_mcp.scrapers.goals import get_idle_cash_optimization
        with patch("emoney_mcp.scrapers.goals.get_accounts", return_value=_accts([])):
            r = await get_idle_cash_optimization(AsyncMock())
        assert r["total_cash"] == 0.0
