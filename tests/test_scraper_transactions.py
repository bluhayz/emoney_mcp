"""Tests for get_transactions and get_capital_gains parsing."""

import pytest
from helpers import make_mock_http_session


@pytest.fixture
def http_session():
    return make_mock_http_session(
        endpoint_responses={"GetInvestmentTransactions": "transactions"},
    )


class TestGetTransactions:
    @pytest.mark.asyncio
    async def test_transaction_count(self, http_session):
        from emoney_mcp.scraper import get_transactions
        result = await get_transactions(http_session, days=30)
        assert result["transaction_count"] == 6

    @pytest.mark.asyncio
    async def test_transactions_sorted_newest_first(self, http_session):
        from emoney_mcp.scraper import get_transactions
        result = await get_transactions(http_session, days=30)
        dates = [t["date"] for t in result["transactions"]]
        assert dates == sorted(dates, reverse=True)

    @pytest.mark.asyncio
    async def test_transaction_fields_present(self, http_session):
        from emoney_mcp.scraper import get_transactions
        result = await get_transactions(http_session, days=30)
        tx = result["transactions"][0]
        assert "date" in tx
        assert "type" in tx
        assert "ticker" in tx
        assert "description" in tx
        assert "amount" in tx

    @pytest.mark.asyncio
    async def test_date_format(self, http_session):
        from emoney_mcp.scraper import get_transactions
        result = await get_transactions(http_session, days=30)
        for tx in result["transactions"]:
            if tx["date"]:
                assert len(tx["date"]) == 10
                assert tx["date"][4] == "-"
                assert tx["date"][7] == "-"

    @pytest.mark.asyncio
    async def test_days_clamped_to_365(self, http_session):
        from emoney_mcp.scraper import get_transactions
        result = await get_transactions(http_session, days=9999)
        # Should not error; days is clamped internally
        assert "transaction_count" in result

    @pytest.mark.asyncio
    async def test_start_and_end_date_present(self, http_session):
        from emoney_mcp.scraper import get_transactions
        result = await get_transactions(http_session, days=30)
        assert "start_date" in result
        assert "end_date" in result


class TestGetCapitalGains:
    @pytest.mark.asyncio
    async def test_sell_transactions_identified(self, http_session):
        from emoney_mcp.scraper import get_capital_gains
        result = await get_capital_gains(http_session, year=2026)
        # Fixture has 3 sell transactions (SNOW x2, ESTC x1)
        assert result["sell_transactions"] == 3

    @pytest.mark.asyncio
    async def test_total_proceeds(self, http_session):
        from emoney_mcp.scraper import get_capital_gains
        result = await get_capital_gains(http_session, year=2026)
        # 279213.48 + 14414.46 + 33367.26 = 326995.20
        assert result["total_proceeds"] == pytest.approx(326995.20, rel=0.01)

    @pytest.mark.asyncio
    async def test_dividends_identified(self, http_session):
        from emoney_mcp.scraper import get_capital_gains
        result = await get_capital_gains(http_session, year=2026)
        # Fixture has 2 dividend transactions
        assert result["total_dividends"] == pytest.approx(1250.75 + 875.20, rel=0.01)

    @pytest.mark.asyncio
    async def test_year_in_result(self, http_session):
        from emoney_mcp.scraper import get_capital_gains
        result = await get_capital_gains(http_session, year=2026)
        assert result["year"] == 2026

    @pytest.mark.asyncio
    async def test_sales_detail_present(self, http_session):
        from emoney_mcp.scraper import get_capital_gains
        result = await get_capital_gains(http_session, year=2026)
        assert len(result["sales_detail"]) == 3
        tickers = {s["ticker"] for s in result["sales_detail"]}
        assert "SNOW" in tickers
        assert "ESTC" in tickers
