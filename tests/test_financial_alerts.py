"""Tests for the consolidated alerts orchestrator (#105)."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

_MOD = "emoney_mcp.scrapers"


def _patches(**overrides):
    """Patch every orchestrated tool; defaults return a clean/no-alert result."""
    defaults = {
        "accounts.get_aggregation_status": {"broken_count": 0, "broken_connections": []},
        "spending.get_unusual_transactions": {"unusual_count": 0, "unusual_transactions": []},
        "spending.get_upcoming_bills": {"overdue_count": 0, "upcoming": [], "total_expected_amount": 0},
        "spending.get_budget_vs_actual": {"over_budget_count": 0},
        "goals.get_emergency_fund_analysis": {"status": "funded", "months_covered": 8, "surplus_or_shortfall": 1000},
        "portfolio.get_portfolio_concentration": {"concentrated_positions": [], "diversification_grade": "A"},
    }
    defaults.update(overrides)
    return [patch(f"{_MOD}.{path}", return_value=val) for path, val in defaults.items()]


async def _run(**overrides):
    patches = _patches(**overrides)
    for p in patches:
        p.start()
    try:
        from emoney_mcp.scrapers.goals import get_financial_alerts
        return await get_financial_alerts(AsyncMock())
    finally:
        for p in patches:
            p.stop()


class TestFinancialAlerts:

    @pytest.mark.asyncio
    async def test_all_clear(self):
        r = await _run()
        assert r["alert_count"] == 0
        assert all(r["sources_checked"].values())

    @pytest.mark.asyncio
    async def test_high_priority_sorted_first(self):
        r = await _run(**{
            "accounts.get_aggregation_status": {"broken_count": 2, "broken_connections": [{"institution": "Chase"}]},
            "goals.get_emergency_fund_analysis": {"status": "underfunded", "months_covered": 1.0, "surplus_or_shortfall": -20000},
            "spending.get_budget_vs_actual": {"over_budget_count": 3, "top_overspend": []},
        })
        assert r["alert_count"] == 3
        assert r["high_priority_count"] == 2
        # High-severity alerts must come before the low-severity budget one.
        assert r["alerts"][0]["severity"] == "high"
        assert r["alerts"][-1]["category"] == "budget"

    @pytest.mark.asyncio
    async def test_errored_source_skipped_not_fatal(self):
        # One source returns an error dict; the orchestrator still returns and
        # marks that source false.
        r = await _run(**{
            "spending.get_unusual_transactions": {"error": "SNB unavailable"},
            "accounts.get_aggregation_status": {"broken_count": 1, "broken_connections": []},
        })
        assert r["sources_checked"]["unusual_transactions"] is False
        assert r["sources_checked"]["aggregation"] is True
        assert r["high_priority_count"] == 1

    @pytest.mark.asyncio
    async def test_concentration_and_unusual(self):
        r = await _run(**{
            "spending.get_unusual_transactions": {"unusual_count": 2, "total_flagged_amount": 5000,
                                                  "unusual_transactions": [{"x": 1}]},
            "portfolio.get_portfolio_concentration": {"concentrated_positions": [{"ticker": "AAPL"}],
                                                      "diversification_grade": "C"},
        })
        cats = {a["category"] for a in r["alerts"]}
        assert "spending" in cats and "portfolio" in cats
