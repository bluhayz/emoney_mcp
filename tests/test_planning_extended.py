"""
Tests for v1.0.0 planning tools:
  - get_home_equity
  - get_fire_number
  - get_gifting_and_estate_strategy
  - get_debt_overview
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Shared account fixtures
# ---------------------------------------------------------------------------

def _make_accounts(
    net_worth=500_000,
    total_assets=650_000,
    total_liabilities=150_000,
    groups=None,
):
    return {
        "net_worth":         net_worth,
        "total_assets":      total_assets,
        "total_liabilities": total_liabilities,
        "account_groups":    groups or [],
        "account_count":     sum(len(g.get("accounts", [])) for g in (groups or [])),
    }


_FULL_ACCOUNTS = _make_accounts(
    net_worth=800_000,
    total_assets=1_100_000,
    total_liabilities=300_000,
    groups=[
        {
            "group": "Real Estate",
            "total": 500_000,
            "accounts": [
                {"name": "Primary Home",  "type": "RealEstateAsset", "balance": 500_000, "institution": "Zillow"},
            ],
        },
        {
            "group": "Mortgages & Loans",
            "total": -200_000,
            "accounts": [
                {"name": "Home Mortgage",  "type": "Mortgage", "balance": -200_000, "institution": "Wells Fargo"},
                {"name": "Chase Credit Card", "type": "CashAsset", "balance": -8_000, "institution": "Chase"},
            ],
        },
        {
            "group": "Investments",
            "total": 450_000,
            "accounts": [
                {"name": "Drew Brokerage", "type": "InvestmentAsset", "balance": 250_000, "institution": "Fidelity"},
                {"name": "Drew 401k",      "type": "PreTaxSavingsAsset", "balance": 200_000, "institution": "Fidelity"},
            ],
        },
        {
            "group": "Cash & Banking",
            "total": 50_000,
            "accounts": [
                {"name": "Joint Checking", "type": "CashAsset", "balance": 50_000, "institution": "Chase"},
            ],
        },
    ],
)

_INCOME_TXNS = [
    {"date": "2025-06-01", "description": "PAYCHECK", "category": "Paycheck/Salary",
     "amount": 8_000, "is_income": True, "is_excluded": False, "is_pending": False},
    {"date": "2025-05-01", "description": "PAYCHECK", "category": "Paycheck/Salary",
     "amount": 8_000, "is_income": True, "is_excluded": False, "is_pending": False},
]
_SPEND_TXNS = [
    {"date": "2025-06-10", "description": "WHOLE FOODS", "category": "Groceries",
     "amount": 500, "is_income": False, "is_excluded": False, "is_pending": False},
    {"date": "2025-05-15", "description": "CHIPOTLE",    "category": "Dining",
     "amount": 200, "is_income": False, "is_excluded": False, "is_pending": False},
]
_ALL_TXNS = _INCOME_TXNS + _SPEND_TXNS


# ===========================================================================
# get_home_equity
# ===========================================================================

class TestGetHomeEquity:

    @pytest.mark.asyncio
    async def test_returns_equity_and_ltv(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert "total_equity" in result
        assert result["total_equity"] == 300_000   # 500k - 200k
        assert result["total_property_value"] == 500_000
        assert result["total_mortgage_balance"] == 200_000

    @pytest.mark.asyncio
    async def test_properties_list_populated(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert len(result["properties"]) >= 1
        prop = result["properties"][0]
        assert "property_value" in prop
        assert "equity" in prop
        assert "ltv_pct" in prop

    @pytest.mark.asyncio
    async def test_no_properties_returns_zero_equity(self):
        no_property_accounts = _make_accounts(
            groups=[{"group": "Cash", "total": 50_000,
                     "accounts": [{"name": "Checking", "type": "CashAsset", "balance": 50_000, "institution": "Chase"}]}]
        )
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=no_property_accounts):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert result["total_equity"] == 0
        assert result["properties"] == []

    @pytest.mark.asyncio
    async def test_equity_pct_of_net_worth_computed(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert result["equity_pct_of_net_worth"] is not None
        assert 0 < result["equity_pct_of_net_worth"] <= 100

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value={"error": "Card 9 unavailable"}):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert "error" in result


# ===========================================================================
# get_fire_number
# ===========================================================================

class TestGetFireNumber:

    @pytest.mark.asyncio
    async def test_fi_number_equals_spending_over_swr(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data", return_value=(_SPEND_TXNS, True)):
            from emoney_mcp.scrapers.planning import get_fire_number
            result = await get_fire_number(session, swr=0.04)
        annual_spend = sum(t["amount"] for t in _SPEND_TXNS)
        expected_fi  = round(annual_spend / 0.04, 2)
        assert result["fi_number"] == expected_fi

    @pytest.mark.asyncio
    async def test_pct_of_way_there_capped_at_100(self):
        # Large investable assets vs small spending → over 100%
        big_accounts = _make_accounts(net_worth=10_000_000, total_assets=10_000_000)
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=big_accounts), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data", return_value=(_SPEND_TXNS, True)):
            from emoney_mcp.scrapers.planning import get_fire_number
            result = await get_fire_number(session)
        assert result["pct_of_way_there"] <= 100.0

    @pytest.mark.asyncio
    async def test_gap_to_fi_non_negative(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data", return_value=(_SPEND_TXNS, True)):
            from emoney_mcp.scrapers.planning import get_fire_number
            result = await get_fire_number(session)
        assert result["gap_to_fi"] >= 0

    @pytest.mark.asyncio
    async def test_all_required_keys_present(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data", return_value=(_ALL_TXNS, True)):
            from emoney_mcp.scrapers.planning import get_fire_number
            result = await get_fire_number(session)
        for key in ("fi_number", "gap_to_fi", "pct_of_way_there", "monthly_savings_needed",
                    "lean_fi_number_3pt5pct", "fat_fi_number_3pct", "assumptions"):
            assert key in result, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_monthly_savings_needed_has_three_horizons(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data", return_value=(_SPEND_TXNS, True)):
            from emoney_mcp.scrapers.planning import get_fire_number
            result = await get_fire_number(session)
        needed = result["monthly_savings_needed"]
        assert "in_15_years" in needed
        assert "in_20_years" in needed
        assert "in_25_years" in needed

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value={"error": "Session expired"}), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data", return_value=([], False)):
            from emoney_mcp.scrapers.planning import get_fire_number
            result = await get_fire_number(session)
        assert "error" in result


# ===========================================================================
# get_gifting_and_estate_strategy
# ===========================================================================

class TestGetGiftingAndEstateStrategy:

    @pytest.mark.asyncio
    async def test_below_exemption_no_estate_tax(self):
        small_estate = _make_accounts(net_worth=500_000, total_assets=500_000, total_liabilities=0)
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=small_estate):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            result = await get_gifting_and_estate_strategy(session, filing_status="mfj")
        assert result["estate_snapshot"]["estimated_estate_tax"] == 0
        assert result["estate_snapshot"]["estate_tax_exposed"] is False

    @pytest.mark.asyncio
    async def test_large_estate_shows_tax_exposure(self):
        large_estate = _make_accounts(net_worth=30_000_000, total_assets=30_000_000)
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=large_estate):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            result = await get_gifting_and_estate_strategy(session, filing_status="mfj")
        assert result["estate_snapshot"]["estate_tax_exposed"] is True
        assert result["estate_snapshot"]["estimated_estate_tax"] > 0

    @pytest.mark.asyncio
    async def test_annual_exclusion_scales_with_recipients(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            r2 = await get_gifting_and_estate_strategy(session, num_recipients=2)
            r4 = await get_gifting_and_estate_strategy(session, num_recipients=4)
        assert r4["annual_gifting"]["total_annual_exclusion_capacity"] == \
               r2["annual_gifting"]["total_annual_exclusion_capacity"] * 2

    @pytest.mark.asyncio
    async def test_mfj_gift_splitting_enabled(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            result = await get_gifting_and_estate_strategy(session, filing_status="mfj")
        assert result["annual_gifting"]["gift_splitting_available"] is True

    @pytest.mark.asyncio
    async def test_single_no_gift_splitting(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            result = await get_gifting_and_estate_strategy(session, filing_status="single")
        assert result["annual_gifting"]["gift_splitting_available"] is False

    @pytest.mark.asyncio
    async def test_strategies_not_empty(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            result = await get_gifting_and_estate_strategy(session)
        assert isinstance(result["strategies"], list)
        assert len(result["strategies"]) >= 1

    @pytest.mark.asyncio
    async def test_529_superfunding_amount(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.planning import get_gifting_and_estate_strategy
            result = await get_gifting_and_estate_strategy(session, filing_status="mfj")
        # MFJ: $18k × 2 donors × 5 years = $180k per beneficiary
        assert result["529_superfunding"]["max_per_beneficiary"] == 18_000 * 2 * 5


# ===========================================================================
# get_debt_overview
# ===========================================================================

class TestGetDebtOverview:

    @pytest.mark.asyncio
    async def test_debts_classified_by_type(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.accounts import get_debt_overview
            result = await get_debt_overview(session)
        debt_types = {d["type"] for d in result["debts"]}
        assert "mortgage" in debt_types
        assert "credit_card" in debt_types

    @pytest.mark.asyncio
    async def test_total_debt_sum_of_balances(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.accounts import get_debt_overview
            result = await get_debt_overview(session)
        computed_total = round(sum(d["balance"] for d in result["debts"]), 2)
        assert result["summary"]["total_debt"] == computed_total

    @pytest.mark.asyncio
    async def test_interest_fields_present(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.accounts import get_debt_overview
            result = await get_debt_overview(session)
        for d in result["debts"]:
            assert "est_monthly_interest" in d
            assert "est_annual_interest" in d
            assert d["est_monthly_interest"] >= 0

    @pytest.mark.asyncio
    async def test_mortgage_uses_mortgage_apr(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.accounts import get_debt_overview
            result = await get_debt_overview(session, assumed_mortgage_apr=0.07)
        mortgage = next((d for d in result["debts"] if d["type"] == "mortgage"), None)
        assert mortgage is not None
        assert mortgage["assumed_apr_pct"] == pytest.approx(7.0, rel=0.01)

    @pytest.mark.asyncio
    async def test_no_debt_returns_zero(self):
        no_debt = _make_accounts(
            groups=[{"group": "Cash", "total": 100_000,
                     "accounts": [{"name": "Savings", "type": "CashAsset", "balance": 100_000, "institution": "Chase"}]}]
        )
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=no_debt):
            from emoney_mcp.scrapers.accounts import get_debt_overview
            result = await get_debt_overview(session)
        assert result["summary"]["total_debt"] == 0
        assert result["debts"] == []

    @pytest.mark.asyncio
    async def test_by_type_aggregated(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=_FULL_ACCOUNTS):
            from emoney_mcp.scrapers.accounts import get_debt_overview
            result = await get_debt_overview(session)
        assert isinstance(result["by_type"], dict)
        for t, info in result["by_type"].items():
            assert "total_balance" in info
            assert info["total_balance"] >= 0
