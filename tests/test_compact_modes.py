"""
Tests for compact/verbose response modes (#182).

Verifies EMONEY_COMPACT=1 triggers payload truncation in the 4 affected tools,
and that the default (unset) returns full verbose output unchanged.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mc_accts(net_worth=1_000_000):
    return {
        "total_assets": net_worth,
        "total_liabilities": 0,
        "net_worth": net_worth,
        "account_groups": [],
    }


def _make_cashflow_rows(n=20):
    """Build n fake projection rows spanning years 2025..2025+n."""
    return [
        {
            "year": 2025 + i,
            "totalCashInflow": 100_000.0,
            "totalCashOutflow": 80_000.0,
            "netCashFlow": 20_000.0 - i * 1_000,  # eventually goes negative
            "withdrawals": {},
            "portfolioValue": {
                "totalPortfolioAssets": 1_000_000.0 + i * 20_000,
                "totalNetWorth": 1_200_000.0 + i * 20_000,
                "portfolioGrowth": 50_000.0,
            },
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# _is_compact helper
# ---------------------------------------------------------------------------

class TestIsCompact:

    def test_unset_returns_false(self, monkeypatch):
        monkeypatch.delenv("EMONEY_COMPACT", raising=False)
        from emoney_mcp.scrapers._helpers import _is_compact
        assert _is_compact() is False

    def test_set_to_1_returns_true(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "1")
        from emoney_mcp.scrapers._helpers import _is_compact
        assert _is_compact() is True

    def test_set_to_true_returns_true(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "true")
        from emoney_mcp.scrapers._helpers import _is_compact
        assert _is_compact() is True

    def test_set_to_yes_returns_true(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "YES")
        from emoney_mcp.scrapers._helpers import _is_compact
        assert _is_compact() is True

    def test_set_to_0_returns_false(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "0")
        from emoney_mcp.scrapers._helpers import _is_compact
        assert _is_compact() is False


# ---------------------------------------------------------------------------
# get_budget_vs_actual — compact mode
# ---------------------------------------------------------------------------

class TestBudgetVsActualCompact:

    def _make_many_txns(self, n_cats=25):
        """Build transaction list with n_cats distinct categories."""
        txns = []
        for i in range(n_cats):
            for day in range(1, 4):
                txns.append({
                    "date": f"2026-07-{day:02d}",
                    "amount": 100.0 + i * 10,
                    "is_income": False,
                    "is_excluded": False,
                    "category": f"Category {i:02d}",
                    "description": "test",
                })
        return txns

    @pytest.mark.asyncio
    async def test_compact_truncates_to_top_10(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "1")
        from emoney_mcp.scrapers.spending import get_budget_vs_actual
        txns = self._make_many_txns(25)
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=None)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", AsyncMock(return_value=(txns, True))):
            result = await get_budget_vs_actual(http_session)
        assert result["output_mode"] == "compact"
        assert result["categories_total"] == 25
        assert result["categories_shown"] == 10
        assert len(result["categories"]) == 10

    @pytest.mark.asyncio
    async def test_verbose_returns_all_categories(self, monkeypatch):
        monkeypatch.delenv("EMONEY_COMPACT", raising=False)
        from emoney_mcp.scrapers.spending import get_budget_vs_actual
        txns = self._make_many_txns(25)
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=None)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", AsyncMock(return_value=(txns, True))):
            result = await get_budget_vs_actual(http_session)
        assert "output_mode" not in result
        assert "categories_total" not in result
        assert len(result["categories"]) == 25

    @pytest.mark.asyncio
    async def test_compact_with_few_cats_no_truncation(self, monkeypatch):
        """With fewer than 11 categories compact mode should not truncate."""
        monkeypatch.setenv("EMONEY_COMPACT", "1")
        from emoney_mcp.scrapers.spending import get_budget_vs_actual
        txns = self._make_many_txns(5)
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=None)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", AsyncMock(return_value=(txns, True))):
            result = await get_budget_vs_actual(http_session)
        # Should NOT set output_mode since no truncation needed
        assert "output_mode" not in result
        assert len(result["categories"]) == 5


# ---------------------------------------------------------------------------
# run_monte_carlo_retirement — compact mode
# ---------------------------------------------------------------------------

class TestMonteCarloCompact:

    @pytest.mark.asyncio
    async def test_compact_omits_year_by_year(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "1")
        from emoney_mcp.scrapers import retirement as ret
        accts = _make_mc_accts()
        with patch.object(ret, "get_accounts", AsyncMock(return_value=accts)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            result = await ret.run_monte_carlo_retirement(
                AsyncMock(), years=10, simulations=100, annual_spending=40_000
            )
        assert "year_by_year_percentiles" not in result
        assert result["output_mode"] == "compact"
        assert "year_by_year_percentiles_note" in result
        # Summary stats must still be present
        assert "results" in result
        assert "probability_of_success_pct" in result["results"]

    @pytest.mark.asyncio
    async def test_verbose_includes_year_by_year(self, monkeypatch):
        monkeypatch.delenv("EMONEY_COMPACT", raising=False)
        from emoney_mcp.scrapers import retirement as ret
        accts = _make_mc_accts()
        with patch.object(ret, "get_accounts", AsyncMock(return_value=accts)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            result = await ret.run_monte_carlo_retirement(
                AsyncMock(), years=10, simulations=100, annual_spending=40_000
            )
        assert "year_by_year_percentiles" in result
        assert "output_mode" not in result
        assert len(result["year_by_year_percentiles"]) == 10


# ---------------------------------------------------------------------------
# get_lifetime_cash_flow_projection — compact mode
# ---------------------------------------------------------------------------

def _cashflow_session(raw_data: dict):
    """Build an http_session mock that returns raw_data for the BFF endpoint."""
    http = AsyncMock()
    session = AsyncMock()
    session.get_http.return_value = http
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = raw_data
    http.get.return_value = resp
    return session


class TestLifetimeCashFlowCompact:

    @pytest.mark.asyncio
    async def test_compact_reduces_row_count(self, monkeypatch):
        monkeypatch.setenv("EMONEY_COMPACT", "1")
        from emoney_mcp.scrapers import plan_api
        raw_data = {"years": _make_cashflow_rows(20)}
        session = _cashflow_session(raw_data)

        with patch.object(plan_api, "_get_plan_ids",
                          AsyncMock(return_value=("c", "p", None))), \
             patch.object(plan_api, "_get_snb_credentials",
                          AsyncMock(return_value=("jwt", "key"))):
            result = await plan_api.get_lifetime_cash_flow_projection(session)

        assert "error" not in result
        assert result["output_mode"] == "compact"
        assert result["years_total"] == 20
        assert result["years_shown"] < 20
        # All-year summary fields still present
        assert "summary" in result
        assert result["summary"]["peak_portfolio_year"] is not None

    @pytest.mark.asyncio
    async def test_verbose_returns_all_rows(self, monkeypatch):
        monkeypatch.delenv("EMONEY_COMPACT", raising=False)
        from emoney_mcp.scrapers import plan_api
        raw_data = {"years": _make_cashflow_rows(20)}
        session = _cashflow_session(raw_data)

        with patch.object(plan_api, "_get_plan_ids",
                          AsyncMock(return_value=("c", "p", None))), \
             patch.object(plan_api, "_get_snb_credentials",
                          AsyncMock(return_value=("jwt", "key"))):
            result = await plan_api.get_lifetime_cash_flow_projection(session)

        assert "error" not in result
        assert "output_mode" not in result
        assert len(result["years"]) == 20

    @pytest.mark.asyncio
    async def test_compact_with_few_rows_no_truncation(self, monkeypatch):
        """With ≤10 rows compact mode should not truncate."""
        monkeypatch.setenv("EMONEY_COMPACT", "1")
        from emoney_mcp.scrapers import plan_api
        raw_data = {"years": _make_cashflow_rows(5)}
        session = _cashflow_session(raw_data)

        with patch.object(plan_api, "_get_plan_ids",
                          AsyncMock(return_value=("c", "p", None))), \
             patch.object(plan_api, "_get_snb_credentials",
                          AsyncMock(return_value=("jwt", "key"))):
            result = await plan_api.get_lifetime_cash_flow_projection(session)

        assert "error" not in result
        assert "output_mode" not in result
        assert len(result["years"]) == 5


# ---------------------------------------------------------------------------
# Backward compat — default verbose output has no output_mode field
# ---------------------------------------------------------------------------

class TestBackwardCompat:

    def test_is_compact_false_by_default(self, monkeypatch):
        monkeypatch.delenv("EMONEY_COMPACT", raising=False)
        from emoney_mcp.scrapers._helpers import _is_compact
        assert _is_compact() is False

    @pytest.mark.asyncio
    async def test_monte_carlo_no_output_mode_by_default(self, monkeypatch):
        monkeypatch.delenv("EMONEY_COMPACT", raising=False)
        from emoney_mcp.scrapers import retirement as ret
        accts = _make_mc_accts()
        with patch.object(ret, "get_accounts", AsyncMock(return_value=accts)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            result = await ret.run_monte_carlo_retirement(
                AsyncMock(), years=5, simulations=50, annual_spending=30_000
            )
        assert "output_mode" not in result
        assert "year_by_year_percentiles" in result
