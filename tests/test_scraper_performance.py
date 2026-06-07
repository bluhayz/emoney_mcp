"""Tests for get_performance, get_net_worth_history, and get_spending parsing."""

import pytest
from helpers import make_mock_http_session


class TestGetPerformance:
    @pytest.fixture
    def http_session(self):
        return make_mock_http_session(
            card_responses={3: "card3_performance", 11: "card11_changes"},
        )

    @pytest.mark.asyncio
    async def test_investment_portfolio_present(self, http_session):
        from emoney_mcp.scraper import get_performance
        result = await get_performance(http_session)
        assert "investment_portfolio" in result

    @pytest.mark.asyncio
    async def test_current_value(self, http_session):
        from emoney_mcp.scraper import get_performance
        result = await get_performance(http_session)
        assert result["investment_portfolio"]["current_value"] == 8266386.0

    @pytest.mark.asyncio
    async def test_today_change_dollar(self, http_session):
        from emoney_mcp.scraper import get_performance
        result = await get_performance(http_session)
        assert result["investment_portfolio"]["today_change_dollar"] == pytest.approx(-108578.7)

    @pytest.mark.asyncio
    async def test_today_change_percent(self, http_session):
        from emoney_mcp.scraper import get_performance
        result = await get_performance(http_session)
        # -0.01313... * 100 = -1.31%
        assert result["investment_portfolio"]["today_change_percent"] == pytest.approx(-1.31, abs=0.01)

    @pytest.mark.asyncio
    async def test_history_periods_computed(self, http_session):
        from emoney_mcp.scraper import get_performance
        result = await get_performance(http_session)
        periods = result["investment_portfolio"]["history_periods"]
        assert len(periods) >= 1
        # 1 month ago: 8266386 - 7825910 = 440476
        one_month = next((p for p in periods if "1 month" in p["period"]), None)
        assert one_month is not None
        assert one_month["change_dollars"] == pytest.approx(440476.0, rel=0.01)

    @pytest.mark.asyncio
    async def test_net_worth_this_month(self, http_session):
        from emoney_mcp.scraper import get_performance
        result = await get_performance(http_session)
        assert result["net_worth"]["this_month"]["change_dollar"] == pytest.approx(447577.0)
        assert result["net_worth"]["this_month"]["change_percent"] == pytest.approx(5.58, abs=0.01)


class TestGetNetWorthHistory:
    @pytest.fixture
    def http_session(self):
        return make_mock_http_session(card_responses={8: "card8_history"})

    @pytest.mark.asyncio
    async def test_current_net_worth(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session)
        assert result["current_net_worth"] == 8470161.0

    @pytest.mark.asyncio
    async def test_months_shown(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session)
        assert result["months_shown"] == 5

    @pytest.mark.asyncio
    async def test_change_over_period(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session)
        # 8470161 - 6020179 = 2449982
        assert result["change_over_period"]["dollar"] == pytest.approx(2449982.0)

    @pytest.mark.asyncio
    async def test_change_percent(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session)
        assert result["change_over_period"]["percent"] == pytest.approx(40.7, abs=0.5)

    @pytest.mark.asyncio
    async def test_history_has_month_labels(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session)
        for point in result["history"]:
            assert "month" in point
            assert point["month"] is not None
            # Should be YYYY-MM format
            assert len(point["month"]) == 7
            assert point["month"][4] == "-"

    @pytest.mark.asyncio
    async def test_this_month_change(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session)
        assert result["this_month"]["change_dollar"] == pytest.approx(447577.0)

    @pytest.mark.asyncio
    async def test_months_limit_respected(self, http_session):
        from emoney_mcp.scraper import get_net_worth_history
        result = await get_net_worth_history(http_session, months=3)
        assert result["months_shown"] <= 3


class TestGetSpending:
    @pytest.fixture
    def http_session(self):
        return make_mock_http_session(card_responses={13: "card13_cashflow"})

    @pytest.mark.asyncio
    async def test_income(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        assert result["income"] == pytest.approx(13673.7)

    @pytest.mark.asyncio
    async def test_expenses(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        assert result["expenses"] == pytest.approx(15620.6)

    @pytest.mark.asyncio
    async def test_net_cash_flow(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        assert result["net_cash_flow"] == pytest.approx(-1946.9)

    @pytest.mark.asyncio
    async def test_recent_transactions_count(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        assert len(result["recent_transactions"]) == 5

    @pytest.mark.asyncio
    async def test_transaction_fields(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        tx = result["recent_transactions"][0]
        assert "date" in tx
        assert "description" in tx
        assert "amount" in tx
        assert tx["date"] == "2026-06-05"
        assert tx["amount"] == pytest.approx(-249.46)

    @pytest.mark.asyncio
    async def test_period_string_present(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        assert result["period"] != ""

    @pytest.mark.asyncio
    async def test_savings_rate_negative_when_overspending(self, http_session):
        from emoney_mcp.scraper import get_spending
        result = await get_spending(http_session)
        # Expenses > Income so savings rate should be negative
        assert result["savings_rate_pct"] < 0
