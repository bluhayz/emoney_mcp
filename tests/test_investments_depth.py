"""
Tests for the investment-depth tools (#92, #93):
  - get_dividend_income_analysis
  - get_sector_geographic_allocation

#92 sums Income Dividend + Income Interest (excludes Reinvest Dividend offsets).
#93 derives equity geography + style detail from GetInvestmentData.AssetAllocation.
No live network.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.scrapers import investments as inv

_TXNS = {
    "transactions": [
        {"date": "2026-06-10", "type": "Income Dividend", "ticker": "VTSAX", "amount": 1000.0},
        {"date": "2026-05-28", "type": "Income Dividend", "ticker": "SPAXX", "amount": 250.0},
        {"date": "2026-05-28", "type": "Reinvest Dividend", "ticker": "SPAXX", "amount": -250.0},  # offset, excluded
        {"date": "2026-04-01", "type": "Income Interest", "ticker": "BND", "amount": 50.0},
        {"date": "2026-03-01", "type": "Buy", "ticker": "VTSAX", "amount": 0.0},
        {"date": "2026-02-01", "type": "Sell", "ticker": "MSFT", "amount": 5000.0},
    ]
}

_INVDATA = {
    "Holdings": 900_000, "Cash": 100_000,  # portfolio value = 1,000,000
    "AssetAllocation": {
        "AssetTypes": [
            {"AssetTypeID": "equities", "AssetClasses": [
                {"ShortName": "Large Blend", "Value": 400_000},
                {"ShortName": "International", "Value": 150_000},
                {"ShortName": "Emerg Mkts", "Value": 50_000},
            ]},
            {"AssetTypeID": "taxbonds", "AssetClasses": [
                {"ShortName": "Inv Grd Bnd", "Value": 200_000},
            ]},
            {"AssetTypeID": "cash", "AssetClasses": [
                {"ShortName": "Cash", "Value": 200_000},
            ]},
        ]
    },
}


def _resp(body, status=200, ctype="application/json"):
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": ctype}
    m.json.return_value = body
    return m


def _invdata_session(payload=_INVDATA, status=200):
    async def mock_get(url, **kwargs):
        if "GetInvestmentData" in url:
            return _resp(payload, status=status,
                         ctype="application/json" if status == 200 else "text/html")
        return _resp(None, status=404, ctype="text/html")
    http = AsyncMock(); http.get = mock_get
    session = AsyncMock(); session.get_http = AsyncMock(return_value=http)
    return session


# ---------------------------------------------------------------------------
# get_dividend_income_analysis (#92)
# ---------------------------------------------------------------------------

class TestDividendIncome:

    @pytest.mark.asyncio
    async def test_income_sums_exclude_reinvest(self):
        with patch.object(inv, "get_transactions", AsyncMock(return_value=_TXNS)):
            r = await inv.get_dividend_income_analysis(_invdata_session())
        assert r["trailing_dividends"] == 1250.0      # 1000 + 250 (NOT minus the reinvest)
        assert r["trailing_interest"] == 50.0
        assert r["trailing_total_income"] == 1300.0

    @pytest.mark.asyncio
    async def test_yield_and_top_producers(self):
        with patch.object(inv, "get_transactions", AsyncMock(return_value=_TXNS)):
            r = await inv.get_dividend_income_analysis(_invdata_session())
        assert r["portfolio_value"] == 1_000_000
        assert r["portfolio_yield_pct"] == pytest.approx(0.13, abs=0.01)  # 1300/1,000,000
        assert r["top_income_producers"][0] == {"ticker": "VTSAX", "income": 1000.0}

    @pytest.mark.asyncio
    async def test_propagates_txn_error(self):
        with patch.object(inv, "get_transactions",
                          AsyncMock(return_value={"error": "session expired"})):
            r = await inv.get_dividend_income_analysis(_invdata_session())
        assert "error" in r

    @pytest.mark.asyncio
    async def test_no_portfolio_value_yield_none(self):
        with patch.object(inv, "get_transactions", AsyncMock(return_value=_TXNS)):
            r = await inv.get_dividend_income_analysis(_invdata_session(status=500))
        assert r["trailing_total_income"] == 1300.0
        assert r["portfolio_yield_pct"] is None


# ---------------------------------------------------------------------------
# get_sector_geographic_allocation (#93)
# ---------------------------------------------------------------------------

class TestSectorGeographic:

    @pytest.mark.asyncio
    async def test_asset_type_breakdown(self):
        r = await inv.get_sector_geographic_allocation(_invdata_session())
        # total classified = 400+150+50+200+200 = 1,000,000
        assert r["total_portfolio_value"] == 1_000_000
        types = {t["asset_type"]: t["percent"] for t in r["by_asset_type"]}
        assert types["equities"] == 60.0      # 600k / 1m
        assert types["taxbonds"] == 20.0
        assert types["cash"] == 20.0

    @pytest.mark.asyncio
    async def test_equity_geography_split(self):
        r = await inv.get_sector_geographic_allocation(_invdata_session())
        geo = r["equity_geographic"]
        # equity total 600k: US 400k, Intl 150k, EM 50k
        assert geo["us"]["pct_of_equity"] == pytest.approx(66.7, abs=0.1)
        assert geo["international"]["pct_of_equity"] == pytest.approx(25.0, abs=0.1)
        assert geo["emerging_markets"]["pct_of_equity"] == pytest.approx(8.3, abs=0.1)

    @pytest.mark.asyncio
    async def test_class_detail_sorted_and_concentration(self):
        r = await inv.get_sector_geographic_allocation(_invdata_session())
        # sorted desc; Large Blend (400k = 40%) is top and flagged (>=25%)
        assert r["by_asset_class"][0]["asset_class"] == "Large Blend"
        assert any("Large Blend" in f for f in r["concentration_flags"])

    @pytest.mark.asyncio
    async def test_error_on_bad_response(self):
        r = await inv.get_sector_geographic_allocation(_invdata_session(status=500))
        assert "error" in r

    @pytest.mark.asyncio
    async def test_error_on_no_allocation(self):
        r = await inv.get_sector_geographic_allocation(_invdata_session(payload={"Holdings": 1, "Cash": 0}))
        assert "error" in r
