"""
Regression tests for the v1.0.15 issue-fix batch.

Each test pins behaviour that was previously wrong so the bug can't silently
return. Issue numbers reference the GitHub tracker.
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# #37 — net-worth-history month labels are drift-free (no 28-day arithmetic)
# ---------------------------------------------------------------------------

class TestCard8HistoryLabels:

    def test_labels_are_calendar_months_not_28_day_drift(self):
        from emoney_mcp.scrapers._helpers import _parse_card8_history
        now = datetime(2026, 6, 16)
        pts = _parse_card8_history({"History": list(range(13))}, months=13, now=now)
        # Newest point is the current month; oldest is exactly 12 months back.
        assert pts[-1]["month"] == "2026-06"
        assert pts[0]["month"] == "2025-06"
        # Each step is exactly one calendar month — 28-day math would drift here.
        assert [p["month"] for p in pts[-4:]] == ["2026-03", "2026-04", "2026-05", "2026-06"]

    def test_empty_history_returns_empty_list(self):
        from emoney_mcp.scrapers._helpers import _parse_card8_history
        assert _parse_card8_history({"History": []}, months=12) == []
        assert _parse_card8_history({}, months=12) == []


# ---------------------------------------------------------------------------
# #40 — get_retirement_runway must not crash on $0 investable assets
# ---------------------------------------------------------------------------

class TestRetirementRunwayZeroAssets:

    @pytest.mark.asyncio
    async def test_zero_investable_does_not_raise(self):
        accts = {
            "total_assets": 300_000, "total_liabilities": 300_000,
            "net_worth": 0, "account_groups": [],
        }
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.retirement.get_accounts", return_value=accts):
            from emoney_mcp.scrapers.retirement import get_retirement_runway
            result = await get_retirement_runway(session, annual_spending=40_000)
        # Previously raised ZeroDivisionError; must now return a clean dict.
        assert "error" not in result
        assert result["investable_assets"] == 0
        for scenario in result["scenarios"]:
            assert scenario["sustainable"] is False


# ---------------------------------------------------------------------------
# #55 — unrecognized holdings classified "unknown", not the best tax score
# ---------------------------------------------------------------------------

class TestClassifyAssetUnknownDefault:

    def test_unrecognized_ticker_is_unknown(self):
        from emoney_mcp.scrapers.portfolio import _classify_asset
        assert _classify_asset("ZZZZ", "Totally Unrecognized Fund") == "unknown"

    def test_unknown_score_is_neutral_not_best(self):
        from emoney_mcp.scrapers.portfolio import _ASSET_EFFICIENCY
        assert _ASSET_EFFICIENCY["unknown"] == 5
        # Must not inherit the most tax-efficient rating (which understated drag).
        assert _ASSET_EFFICIENCY["unknown"] < _ASSET_EFFICIENCY["domestic_equity_index"]

    def test_known_classifications_still_work(self):
        from emoney_mcp.scrapers.portfolio import _classify_asset
        assert _classify_asset("VNQ", "Vanguard REIT") == "reit"
        assert _classify_asset("BND", "Total Bond Market") == "bond_fund"
        assert _classify_asset("VTI", "Total Market Index") == "domestic_equity_index"


# ---------------------------------------------------------------------------
# #41 — get_features advertises the real tool count, derived from _DISPATCH
# ---------------------------------------------------------------------------

class TestGetFeaturesCount:

    def test_total_tools_matches_dispatch(self):
        from emoney_mcp.server import _get_features, _DISPATCH
        feats = _get_features()
        assert feats["total_tools"] == len(_DISPATCH)
        assert feats["total_tools"] != 51  # the old hardcoded value

    def test_every_tool_is_categorized_or_flagged(self):
        from emoney_mcp.server import _get_features, _DISPATCH
        feats = _get_features()
        # Coverage accounting must reconcile exactly with the registry.
        assert feats["categorized_tools"] + len(feats["uncategorized_tools"]) == len(_DISPATCH)
