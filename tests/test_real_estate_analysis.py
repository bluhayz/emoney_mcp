"""Tests for get_real_estate_investment_analysis (#100) — rental property metrics."""

from unittest.mock import AsyncMock, patch

import pytest

from emoney_mcp.scrapers.planning import get_real_estate_investment_analysis
from helpers import make_mock_http_session


def _patch_home_equity(properties):
    return patch("emoney_mcp.scrapers.planning.get_home_equity",
                 new=AsyncMock(return_value={"properties": properties}))


class TestRealEstateInvestmentAnalysis:
    @pytest.mark.asyncio
    async def test_all_cash_metrics(self):
        """No mortgage: cap rate == cash-on-cash, NOI excludes financing."""
        session = make_mock_http_session()
        r = await get_real_estate_investment_analysis(
            session, monthly_rent=2500, property_value=300_000,
            mortgage_balance=0, monthly_operating_expenses=750,
        )
        ie = r["income_and_expenses"]
        ret = r["returns"]
        assert ie["net_operating_income"] == 21_000          # 30000 rent - 9000 opex
        assert ie["annual_cash_flow"] == 21_000              # no debt service
        assert ie["monthly_cash_flow"] == 1_750
        assert ret["cap_rate_pct"] == 7.0
        assert ret["cash_on_cash_pct"] == 7.0                # invested == equity == value
        assert ret["dscr"] is None                           # no debt service
        assert ret["gross_rent_multiplier"] == 10.0

    @pytest.mark.asyncio
    async def test_levered_cash_flow_and_dscr(self):
        session = make_mock_http_session()
        r = await get_real_estate_investment_analysis(
            session, monthly_rent=2500, property_value=300_000,
            mortgage_balance=200_000, monthly_mortgage_payment=1200,
            monthly_operating_expenses=750,
        )
        ie = r["income_and_expenses"]
        ret = r["returns"]
        assert ie["annual_debt_service"] == 14_400
        assert ie["annual_cash_flow"] == 6_600               # 21000 NOI - 14400
        assert r["property"]["equity"] == 100_000
        assert ret["cash_on_cash_pct"] == 6.6                # 6600 / 100000
        assert ret["dscr"] == pytest.approx(1.46, abs=0.01)  # 21000 / 14400

    @pytest.mark.asyncio
    async def test_mortgage_payment_computed_from_rate_and_term(self):
        session = make_mock_http_session()
        r = await get_real_estate_investment_analysis(
            session, monthly_rent=2500, property_value=300_000,
            mortgage_balance=200_000, mortgage_rate=0.06, mortgage_years_remaining=30,
            monthly_operating_expenses=750,
        )
        ie = r["income_and_expenses"]
        assert ie["monthly_mortgage_payment"] == pytest.approx(1199.10, abs=1.0)
        assert "computed" in ie["mortgage_payment_basis"]

    @pytest.mark.asyncio
    async def test_expense_ratio_fallback(self):
        """No expenses provided → estimated as ratio × rent."""
        session = make_mock_http_session()
        r = await get_real_estate_investment_analysis(
            session, monthly_rent=2000, property_value=240_000,
            mortgage_balance=0, operating_expense_ratio=0.5,
        )
        ie = r["income_and_expenses"]
        assert ie["annual_operating_expenses"] == 12_000     # 0.5 * 24000
        assert ie["net_operating_income"] == 12_000
        assert "estimated" in ie["operating_expense_basis"]

    @pytest.mark.asyncio
    async def test_autofill_from_balance_sheet(self):
        session = make_mock_http_session()
        props = [{"account_name": "Rental Condo", "property_value": 400_000,
                  "mortgage_balance": 250_000}]
        with _patch_home_equity(props):
            r = await get_real_estate_investment_analysis(
                session, monthly_rent=3000, monthly_operating_expenses=900)
        assert r["property"]["value"] == 400_000
        assert r["property"]["mortgage_balance"] == 250_000
        assert r["property"]["equity"] == 150_000
        assert "Rental Condo" in r["property"]["data_source"]

    @pytest.mark.asyncio
    async def test_multiple_properties_requires_name(self):
        session = make_mock_http_session()
        props = [
            {"account_name": "Beach House", "property_value": 500_000, "mortgage_balance": 0},
            {"account_name": "City Condo",  "property_value": 300_000, "mortgage_balance": 100_000},
        ]
        with _patch_home_equity(props):
            r = await get_real_estate_investment_analysis(session, monthly_rent=2500)
        assert "error" in r and "Multiple" in r["error"]

        with _patch_home_equity(props):
            r2 = await get_real_estate_investment_analysis(
                session, monthly_rent=2500, property_name="condo")
        assert "error" not in r2
        assert r2["property"]["value"] == 300_000

    @pytest.mark.asyncio
    async def test_no_matching_property(self):
        session = make_mock_http_session()
        with _patch_home_equity([]):
            r = await get_real_estate_investment_analysis(session, monthly_rent=2500)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_negative_cash_flow_and_low_dscr_flagged(self):
        session = make_mock_http_session()
        r = await get_real_estate_investment_analysis(
            session, monthly_rent=1500, property_value=400_000,
            mortgage_balance=350_000, monthly_mortgage_payment=2200,
            monthly_operating_expenses=500,
        )
        # NOI = 18000-6000=12000; debt service=26400 → cash flow -14400, dscr 0.45
        assert r["income_and_expenses"]["annual_cash_flow"] < 0
        assert r["returns"]["dscr"] < 1.0
        joined = " ".join(r["flags"]).lower()
        assert "dscr below 1.0" in joined
        assert "negative cash flow" in joined

    @pytest.mark.asyncio
    async def test_one_percent_rule_ratio(self):
        session = make_mock_http_session()
        r = await get_real_estate_investment_analysis(
            session, monthly_rent=3000, property_value=300_000,
            mortgage_balance=0, monthly_operating_expenses=0,
        )
        assert r["returns"]["one_percent_rule_ratio_pct"] == 1.0  # 3000/300000

    @pytest.mark.asyncio
    async def test_monthly_rent_required(self):
        session = make_mock_http_session()
        assert "error" in await get_real_estate_investment_analysis(
            session, monthly_rent=0, property_value=300_000, mortgage_balance=0)

    @pytest.mark.asyncio
    async def test_home_equity_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_home_equity",
                   new=AsyncMock(return_value={"error": "Card 1 unavailable"})):
            r = await get_real_estate_investment_analysis(session, monthly_rent=2500)
        assert "error" in r
