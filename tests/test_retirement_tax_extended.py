"""
Tests for v1.0.0 retirement and tax tools:
  - get_financial_independence_roadmap
  - get_annual_tax_advantaged_summary
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_accounts(nw=800_000, assets=1_000_000, liabilities=200_000, groups=None):
    return {
        "net_worth":         nw,
        "total_assets":      assets,
        "total_liabilities": liabilities,
        "account_groups":    groups or [],
    }


_ACCOUNTS = _make_accounts(
    nw=800_000,
    groups=[
        {"group": "Investments", "total": 500_000, "accounts": [
            {"name": "Drew 401k",     "type": "PreTaxSavingsAsset",      "balance": 300_000},
            {"name": "Drew Brokerage","type": "InvestmentAsset",          "balance": 200_000},
        ]},
        {"group": "Real Estate", "total": 400_000, "accounts": [
            {"name": "Primary Home",  "type": "RealEstateAsset",          "balance": 400_000},
        ]},
    ]
)

_NORM_TXNS_INCOME = [
    {"date": "2025-06-01", "description": "PAYCHECK", "category": "Paycheck/Salary",
     "amount": 8_000, "is_income": True, "is_excluded": False, "is_pending": False},
] * 12   # 12 months of $8k income

_NORM_TXNS_SPEND = [
    {"date": "2025-06-10", "description": "EXPENSES", "category": "Groceries",
     "amount": 4_000, "is_income": False, "is_excluded": False, "is_pending": False},
] * 12   # 12 months of $4k spending

_ALL_TXNS = _NORM_TXNS_INCOME + _NORM_TXNS_SPEND

_SAVINGS_RATE_RESULT = {
    "months_shown": 6,
    "total_net":    24_000,
    "average_savings_rate": 50.0,
}

_RETIREMENT_ACCOUNTS_RESULT = {
    "total_retirement_assets": 300_000,
    "total_taxable_assets":    200_000,
    "retirement_breakdown": {
        "401k_403b":     300_000,
        "ira_roth":       80_000,
        "hsa":            15_000,
        "education_529":  50_000,
        "annuities":           0,
        "other":               0,
    },
    "retirement_accounts": [],
}


# ===========================================================================
# get_financial_independence_roadmap
# ===========================================================================

class TestGetFinancialIndependenceRoadmap:

    @pytest.mark.asyncio
    async def test_returns_fidelity_benchmarks(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=_ACCOUNTS), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate", return_value=_SAVINGS_RATE_RESULT), \
             patch("emoney_mcp.scrapers.retirement.get_financial_independence_roadmap.__wrapped__", create=True), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_ALL_TXNS, True)):
            from emoney_mcp.scrapers.retirement import get_financial_independence_roadmap
            result = await get_financial_independence_roadmap(session)
        assert "fidelity_benchmarks" in result
        assert len(result["fidelity_benchmarks"]) == 8  # 30,35,40,45,50,55,60,65

    @pytest.mark.asyncio
    async def test_benchmark_has_required_fields(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=_ACCOUNTS), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate", return_value=_SAVINGS_RATE_RESULT), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_ALL_TXNS, True)):
            from emoney_mcp.scrapers.retirement import get_financial_independence_roadmap
            result = await get_financial_independence_roadmap(session)
        for b in result["fidelity_benchmarks"]:
            assert "age" in b
            assert "multiplier" in b
            assert "on_track" in b

    @pytest.mark.asyncio
    async def test_fi_number_25x_spending(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=_ACCOUNTS), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate", return_value=_SAVINGS_RATE_RESULT), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_ALL_TXNS, True)):
            from emoney_mcp.scrapers.retirement import get_financial_independence_roadmap
            result = await get_financial_independence_roadmap(session)
        # Annual spending ≈ 4000*12 = 48000; FI = 48000/0.04 = 1_200_000
        if result["fi_number"] is not None and result["annual_spending"] > 0:
            expected = round(result["annual_spending"] / 0.04, 2)
            assert abs(result["fi_number"] - expected) < 1  # rounding tolerance

    @pytest.mark.asyncio
    async def test_coast_fi_computed_when_age_provided(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=_ACCOUNTS), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate", return_value=_SAVINGS_RATE_RESULT), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_ALL_TXNS, True)):
            from emoney_mcp.scrapers.retirement import get_financial_independence_roadmap
            result = await get_financial_independence_roadmap(session, current_age=40, retirement_age=65)
        assert result["coast_fi"]["target_today"] is not None
        assert result["coast_fi"]["target_today"] > 0

    @pytest.mark.asyncio
    async def test_coast_fi_target_less_than_fi_number(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=_ACCOUNTS), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate", return_value=_SAVINGS_RATE_RESULT), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_ALL_TXNS, True)):
            from emoney_mcp.scrapers.retirement import get_financial_independence_roadmap
            result = await get_financial_independence_roadmap(session, current_age=40, retirement_age=65)
        if result["fi_number"] and result["coast_fi"]["target_today"]:
            # Coast FI (discounted) must be ≤ FI number
            assert result["coast_fi"]["target_today"] <= result["fi_number"]

    @pytest.mark.asyncio
    async def test_error_propagates_from_accounts(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value={"error": "Session expired"}), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate", return_value=_SAVINGS_RATE_RESULT), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=([], False)):
            from emoney_mcp.scrapers.retirement import get_financial_independence_roadmap
            result = await get_financial_independence_roadmap(session)
        assert "error" in result


# ===========================================================================
# get_annual_tax_advantaged_summary
# ===========================================================================

class TestGetAnnualTaxAdvantagedSummary:

    @pytest.mark.asyncio
    async def test_returns_all_account_types(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value=_RETIREMENT_ACCOUNTS_RESULT):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            result = await get_annual_tax_advantaged_summary(session)
        account_types = [a["account_type"] for a in result["accounts"]]
        assert any("401k" in t for t in account_types)
        assert any("IRA" in t for t in account_types)
        assert any("HSA" in t for t in account_types)

    @pytest.mark.asyncio
    async def test_401k_limit_increases_at_50(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value=_RETIREMENT_ACCOUNTS_RESULT):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            r_under50 = await get_annual_tax_advantaged_summary(session, age=45)
            r_over50  = await get_annual_tax_advantaged_summary(session, age=52)

        def _401k_limit(r):
            for a in r["accounts"]:
                if "401k" in a["account_type"]:
                    return a["annual_limit"]
            return 0

        assert _401k_limit(r_over50) > _401k_limit(r_under50)

    @pytest.mark.asyncio
    async def test_tax_year_in_result(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value=_RETIREMENT_ACCOUNTS_RESULT):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            result = await get_annual_tax_advantaged_summary(session)
        assert "tax_year" in result
        from emoney_mcp.scrapers.tax import _TAX_YEAR
        assert result["tax_year"] == _TAX_YEAR

    @pytest.mark.asyncio
    async def test_key_deadlines_present(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value=_RETIREMENT_ACCOUNTS_RESULT):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            result = await get_annual_tax_advantaged_summary(session)
        assert "key_deadlines" in result
        assert "401k_hsa_deadline" in result["key_deadlines"]

    @pytest.mark.asyncio
    async def test_totals_computed(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value=_RETIREMENT_ACCOUNTS_RESULT):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            result = await get_annual_tax_advantaged_summary(session)
        assert "totals" in result
        assert result["totals"]["combined_401k_ira_hsa_annual_limit"] > 0

    @pytest.mark.asyncio
    async def test_days_left_in_year_non_negative(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value=_RETIREMENT_ACCOUNTS_RESULT):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            result = await get_annual_tax_advantaged_summary(session)
        assert result["totals"]["days_left_in_tax_year"] >= 0

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_retirement_accounts",
                   return_value={"error": "Card 1 unavailable"}):
            from emoney_mcp.scrapers.tax import get_annual_tax_advantaged_summary
            result = await get_annual_tax_advantaged_summary(session)
        assert "error" in result
