"""Tests for get_holdings parsing."""

import pytest
from helpers import load_fixture, make_mock_http_session


@pytest.fixture
def http_session():
    return make_mock_http_session(
        endpoint_responses={"GetInvestmentData": "investment_data"},
    )


class TestGetHoldings:
    @pytest.mark.asyncio
    async def test_portfolio_value_returned(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        assert result["portfolio_holdings_value"] == 7751984.0

    @pytest.mark.asyncio
    async def test_cash_returned(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        assert result["portfolio_cash"] == 870926.3

    @pytest.mark.asyncio
    async def test_skips_empty_accounts(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        # "Empty Account" has no holdings and should be excluded
        names = [a["account"] for a in result["investment_accounts"]]
        assert "Empty Account" not in names

    @pytest.mark.asyncio
    async def test_account_count(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        assert result["account_count"] == 2  # Individual TOD + Roth IRA

    @pytest.mark.asyncio
    async def test_position_count(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        assert result["position_count"] == 4  # 3 in Individual TOD + 1 in Roth IRA

    @pytest.mark.asyncio
    async def test_gain_loss_calculated(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        # VTSAX: value=1549367.11 - cost=900000 = 649367.11
        individual = next(a for a in result["investment_accounts"] if "Individual" in a["account"])
        vtsax = next(p for p in individual["positions"] if p["ticker"] == "VTSAX")
        assert vtsax["gain_loss"] == pytest.approx(649367.11, rel=0.01)

    @pytest.mark.asyncio
    async def test_total_unrealized_gain_loss(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        # All gains should sum up correctly
        assert result["total_unrealized_gain_loss"] > 0

    @pytest.mark.asyncio
    async def test_no_gain_loss_when_no_cost_basis(self, http_session):
        from emoney_mcp.scraper import get_holdings
        result = await get_holdings(http_session)
        individual = next(a for a in result["investment_accounts"] if "Individual" in a["account"])
        # SPAXX cost_basis == value, gain_loss should be 0
        spaxx = next(p for p in individual["positions"] if p["ticker"] == "SPAXX")
        assert spaxx["gain_loss"] == pytest.approx(0.0)
