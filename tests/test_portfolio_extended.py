"""
Tests for v1.0.0 portfolio tools:
  - get_portfolio_concentration
  - get_net_worth_velocity
  - get_tax_drag_analysis
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _make_investment_data(accounts):
    return {"Accounts": accounts, "Holdings": 0, "Cash": 0}

def _make_account(name, holdings, account_type="InvestmentAsset"):
    return {
        "AccountName": name,
        "MajorType":   account_type,
        "Holdings": holdings,
    }

def _make_holding(ticker, desc, value, cost_basis=None):
    return {
        "Ticker":      ticker,
        "Description": desc,
        "Value":       value,
        "CostBasis":   cost_basis,
    }


_DIVERSIFIED_DATA = _make_investment_data([
    _make_account("Drew Brokerage", [
        _make_holding("VTI",  "Vanguard Total Stock Market ETF",      30_000),
        _make_holding("BND",  "Vanguard Total Bond Market ETF",       25_000),
        _make_holding("VXUS", "Vanguard Total International ETF",    20_000),
        _make_holding("VONE", "Vanguard Russell 1000 ETF",            15_000),
        _make_holding("VTIP", "Vanguard Short-Term Inflation ETF",   10_000),
    ]),
    _make_account("Drew IRA", [
        _make_holding("FSKAX", "Fidelity Total Market Index Fund",    30_000),
        _make_holding("FTIHX", "Fidelity Total International Index", 20_000),
        _make_holding("FXNAX", "Fidelity US Bond Index Fund",        15_000),
        _make_holding("FSMAX", "Fidelity Extended Market Index",     10_000),
        _make_holding("FPADX", "Fidelity Pacific Basin Fund",         5_000),
    ]),
])

_CONCENTRATED_DATA = _make_investment_data([
    _make_account("Drew Brokerage", [
        _make_holding("AAPL",  "Apple Inc",                        80_000),  # >40%
        _make_holding("VTI",   "Vanguard Total Stock Market ETF",  20_000),
        _make_holding("BND",   "Vanguard Total Bond Market ETF",   10_000),
        _make_holding("MSFT",  "Microsoft Corp",                   10_000),
        _make_holding("GOOGL", "Alphabet Inc",                     10_000),
        _make_holding("AMZN",  "Amazon.com Inc",                    5_000),
        _make_holding("META",  "Meta Platforms Inc",                5_000),
    ]),
])


def _make_inv_session(data: dict):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = data

    http = AsyncMock()
    http.get = AsyncMock(return_value=resp)

    session = make_mock_http_session()
    session.get_http = AsyncMock(return_value=http)
    return session


# ===========================================================================
# get_portfolio_concentration
# ===========================================================================

class TestGetPortfolioConcentration:

    @pytest.mark.asyncio
    async def test_diversified_portfolio_gets_good_grade(self):
        session = _make_inv_session(_DIVERSIFIED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session, concentration_threshold_pct=10.0)
        assert result["diversification_grade"] in ("A", "B", "C")

    @pytest.mark.asyncio
    async def test_concentrated_portfolio_gets_poor_grade(self):
        session = _make_inv_session(_CONCENTRATED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session, concentration_threshold_pct=10.0)
        assert result["diversification_grade"] in ("C", "D", "F")

    @pytest.mark.asyncio
    async def test_concentrated_position_flagged(self):
        session = _make_inv_session(_CONCENTRATED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session, concentration_threshold_pct=10.0)
        tickers = [p["ticker"] for p in result["concentrated_positions"]]
        assert "AAPL" in tickers

    @pytest.mark.asyncio
    async def test_top_10_positions_capped(self):
        session = _make_inv_session(_CONCENTRATED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        assert len(result["top_10_positions"]) <= 10

    @pytest.mark.asyncio
    async def test_pct_of_portfolio_sums_to_100(self):
        session = _make_inv_session(_DIVERSIFIED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        total_pct = sum(p["pct_of_portfolio"] for p in result["top_10_positions"])
        assert 95 <= total_pct <= 105  # allow rounding

    @pytest.mark.asyncio
    async def test_all_required_keys_present(self):
        session = _make_inv_session(_DIVERSIFIED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        for key in ("total_portfolio_value", "diversification_grade", "concentrated_positions",
                    "top_10_positions", "asset_type_breakdown", "recommendations"):
            assert key in result, f"Missing key: {key}"

    @pytest.mark.asyncio
    async def test_recommendations_not_empty(self):
        session = _make_inv_session(_DIVERSIFIED_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        assert isinstance(result["recommendations"], list)
        assert len(result["recommendations"]) >= 1


# ===========================================================================
# get_net_worth_velocity
# ===========================================================================

# Card 8 returns a list of net worth values, newest first
_NW_HISTORY_GROWING = [
    1_100_000, 1_080_000, 1_060_000, 1_040_000,
    1_020_000, 1_000_000, 980_000,  960_000,
    940_000,   920_000,   900_000,  880_000,
]  # consistent +$20k/month

_NW_HISTORY_FLAT = [500_000] * 12


class TestGetNetWorthVelocity:

    @pytest.mark.asyncio
    async def test_growing_portfolio_positive_velocity(self):
        session = make_mock_http_session(card_responses={8: "card8_history"})
        with patch("emoney_mcp.scrapers.portfolio._get_card", return_value=_NW_HISTORY_GROWING):
            from emoney_mcp.scrapers.portfolio import get_net_worth_velocity
            result = await get_net_worth_velocity(session, months=12)
        assert result["avg_monthly_gain"] > 0

    @pytest.mark.asyncio
    async def test_current_net_worth_is_last_in_history(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.portfolio._get_card", return_value=_NW_HISTORY_GROWING):
            from emoney_mcp.scrapers.portfolio import get_net_worth_velocity
            result = await get_net_worth_velocity(session, months=12)
        assert result["current_net_worth"] == _NW_HISTORY_GROWING[0]

    @pytest.mark.asyncio
    async def test_monthly_history_length(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.portfolio._get_card", return_value=_NW_HISTORY_GROWING):
            from emoney_mcp.scrapers.portfolio import get_net_worth_velocity
            result = await get_net_worth_velocity(session, months=12)
        assert len(result["monthly_history"]) == min(12, len(_NW_HISTORY_GROWING))

    @pytest.mark.asyncio
    async def test_trend_accelerating_for_growing(self):
        # Second half should grow faster than first half for 'accelerating'
        accel_history = list(range(900_000, 900_000 + 20_000 * 12, 20_000))[::-1]
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.portfolio._get_card", return_value=accel_history):
            from emoney_mcp.scrapers.portfolio import get_net_worth_velocity
            result = await get_net_worth_velocity(session, months=12)
        assert result["trend"] in ("stable", "accelerating")

    @pytest.mark.asyncio
    async def test_card8_unavailable_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.portfolio._get_card", return_value=None):
            from emoney_mcp.scrapers.portfolio import get_net_worth_velocity
            result = await get_net_worth_velocity(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_all_required_keys_present(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.portfolio._get_card", return_value=_NW_HISTORY_GROWING):
            from emoney_mcp.scrapers.portfolio import get_net_worth_velocity
            result = await get_net_worth_velocity(session, months=12)
        for key in ("current_net_worth", "avg_monthly_gain", "trend",
                    "projected_net_worth_12mo", "monthly_history"):
            assert key in result, f"Missing key: {key}"


# ===========================================================================
# get_tax_drag_analysis
# ===========================================================================

_BOND_IN_TAXABLE_DATA = _make_investment_data([
    _make_account("Drew Brokerage", [   # taxable
        _make_holding("BND",  "Vanguard Total Bond Market ETF",   50_000),  # bond in taxable → drag
        _make_holding("VNQ",  "Vanguard Real Estate ETF (REIT)",  20_000),  # reit in taxable → drag
        _make_holding("VTI",  "Vanguard Total Stock Market ETF",  80_000),  # equity, efficient
    ]),
    _make_account("Drew IRA Roth", [    # tax-free — no drag
        _make_holding("BND", "Vanguard Total Bond Market ETF",    40_000),
    ]),
])

_TYPE_MAP = {"drew brokerage": "Taxable", "drew ira roth": "Tax-Free"}


class TestGetTaxDragAnalysis:

    @pytest.mark.asyncio
    async def test_misplaced_bonds_generate_drag(self):
        session = _make_inv_session(_BOND_IN_TAXABLE_DATA)
        with patch("emoney_mcp.scrapers.portfolio._build_account_type_map", return_value=_TYPE_MAP):
            from emoney_mcp.scrapers.portfolio import get_tax_drag_analysis
            result = await get_tax_drag_analysis(session)
        assert result["total_annual_tax_drag_est"] > 0

    @pytest.mark.asyncio
    async def test_well_placed_bonds_in_ira_no_drag(self):
        ira_only = _make_investment_data([
            _make_account("Drew IRA", [
                _make_holding("BND", "Vanguard Total Bond ETF", 50_000),
            ])
        ])
        ira_type_map = {"drew ira": "Tax-Deferred"}
        session = _make_inv_session(ira_only)
        with patch("emoney_mcp.scrapers.portfolio._build_account_type_map", return_value=ira_type_map):
            from emoney_mcp.scrapers.portfolio import get_tax_drag_analysis
            result = await get_tax_drag_analysis(session)
        assert result["total_annual_tax_drag_est"] == 0
        assert result["misplaced_position_count"] == 0

    @pytest.mark.asyncio
    async def test_priority_swaps_capped_at_5(self):
        session = _make_inv_session(_BOND_IN_TAXABLE_DATA)
        with patch("emoney_mcp.scrapers.portfolio._build_account_type_map", return_value=_TYPE_MAP):
            from emoney_mcp.scrapers.portfolio import get_tax_drag_analysis
            result = await get_tax_drag_analysis(session)
        assert len(result["priority_swaps"]) <= 5

    @pytest.mark.asyncio
    async def test_higher_marginal_rate_increases_drag(self):
        session = _make_inv_session(_BOND_IN_TAXABLE_DATA)
        with patch("emoney_mcp.scrapers.portfolio._build_account_type_map", return_value=_TYPE_MAP):
            from emoney_mcp.scrapers.portfolio import get_tax_drag_analysis
            r22 = await get_tax_drag_analysis(session, marginal_rate=0.22)
            r37 = await get_tax_drag_analysis(session, marginal_rate=0.37)
        assert r37["total_annual_tax_drag_est"] >= r22["total_annual_tax_drag_est"]

    @pytest.mark.asyncio
    async def test_all_required_keys_present(self):
        session = _make_inv_session(_BOND_IN_TAXABLE_DATA)
        with patch("emoney_mcp.scrapers.portfolio._build_account_type_map", return_value=_TYPE_MAP):
            from emoney_mcp.scrapers.portfolio import get_tax_drag_analysis
            result = await get_tax_drag_analysis(session)
        for key in ("total_annual_tax_drag_est", "misplaced_positions",
                    "priority_swaps", "assumptions"):
            assert key in result, f"Missing key: {key}"
