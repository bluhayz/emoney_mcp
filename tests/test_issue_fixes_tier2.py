"""
Regression tests for the v1.0.16 Tier 2 issue-fix batch.

Each test pins behaviour that was previously wrong/ambiguous so the bug can't
silently return. Issue numbers reference the GitHub tracker.
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# #42 — missing required argument gives a clear message, not a bare KeyError
# ---------------------------------------------------------------------------

class TestMissingRequiredArg:

    def test_missing_required_raises_clear_value_error(self):
        from emoney_mcp.server import _kwargs, _A
        with pytest.raises(ValueError, match="Missing required argument: 'transaction_id'"):
            _kwargs([_A("transaction_id")], {})

    def test_present_required_arg_converts_normally(self):
        from emoney_mcp.server import _kwargs, _A
        assert _kwargs([_A("days", int, 30)], {"days": "7"}) == {"days": 7}


# ---------------------------------------------------------------------------
# #51 — get_available_cards returns a clean error on an empty card_ids list
# ---------------------------------------------------------------------------

class TestAvailableCardsEmptyList:

    @pytest.mark.asyncio
    async def test_empty_card_ids_returns_error(self):
        from emoney_mcp.scrapers.portfolio import get_available_cards
        result = await get_available_cards(AsyncMock(), card_ids=[])
        assert "error" in result


# ---------------------------------------------------------------------------
# #50 — GetRules maintenance 500 surfaces as an error, not "0 rules"
# ---------------------------------------------------------------------------

class TestGetRulesMaintenance:
    """get_transaction_rules now reads the SNB GetBankTransactionRules endpoint
    (migrated off the dead legacy /ema/CS/Spending/GetRules Nexus path)."""

    @pytest.mark.asyncio
    async def test_snb_error_not_masked_as_empty(self):
        from emoney_mcp.scrapers.transactions import get_transaction_rules
        err = {"error": "GetBankTransactionRules returned HTTP 401"}
        with patch("emoney_mcp.scrapers.transactions._snb_get", new=AsyncMock(return_value=err)):
            result = await get_transaction_rules(AsyncMock())
        assert "error" in result
        assert "rules" not in result   # must NOT be reported as 0 rules

    @pytest.mark.asyncio
    async def test_empty_list_treated_as_no_rules(self):
        from emoney_mcp.scrapers.transactions import get_transaction_rules
        with patch("emoney_mcp.scrapers.transactions._snb_get",
                   new=AsyncMock(return_value={"ok": True, "data": []})):
            result = await get_transaction_rules(AsyncMock())
        assert result["count"] == 0
        assert result["rules"] == []


# ---------------------------------------------------------------------------
# #47 — get_categories skips a non-numeric key instead of crashing
# ---------------------------------------------------------------------------

class TestGetCategoriesBadKey:

    @pytest.mark.asyncio
    async def test_non_numeric_key_skipped(self):
        from emoney_mcp.scrapers.spending import get_categories
        cats = {"22": "Groceries", "not-a-number": "Junk", "5": "Dining"}
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   new=AsyncMock(return_value=(True, [], cats))):
            result = await get_categories(AsyncMock())
        ids = {c["id"] for c in result["categories"]}
        assert ids == {5, 22}
        assert result["category_count"] == 2


# ---------------------------------------------------------------------------
# #61 — missing CSRF token short-circuits with a clear error before POSTing
# ---------------------------------------------------------------------------

class TestCsrfTokenMissing:

    @pytest.mark.asyncio
    async def test_csrf_post_errors_when_token_missing(self):
        from emoney_mcp.scrapers.transactions import _csrf_post
        session = AsyncMock()
        session.get_csrf_token = AsyncMock(return_value=None)
        result = await _csrf_post(session, "UpdateTransaction", {})
        assert "error" in result
        assert "CSRF token" in result["error"]
        # Must not have attempted the POST with an empty token.
        session.get_http.return_value.post.assert_not_called()


# ---------------------------------------------------------------------------
# #53 — get_fire_number returns an explicit fi_status
# ---------------------------------------------------------------------------

class TestFireNumberStatus:

    def _patch(self, investable, income, spending):
        accts = {"total_assets": 1, "total_liabilities": 0, "net_worth": 1, "account_groups": []}
        return [
            patch("emoney_mcp.scrapers.planning.get_accounts", return_value=accts),
            patch("emoney_mcp.scrapers.planning._fetch_snb_data",
                  new=AsyncMock(return_value=([], True))),
            patch("emoney_mcp.scrapers.planning._calc_investable_assets", return_value=investable),
            patch("emoney_mcp.scrapers.planning._sum_income_spending", return_value=(income, spending)),
        ]

    @pytest.mark.asyncio
    async def test_already_fi(self):
        from emoney_mcp.scrapers.planning import get_fire_number
        patches = self._patch(investable=2_000_000, income=100_000, spending=40_000)
        for p in patches:
            p.start()
        try:
            result = await get_fire_number(AsyncMock())
        finally:
            for p in patches:
                p.stop()
        assert result["fi_status"] == "already_fi"
        assert result["years_to_fi_at_current_pace"] is None

    @pytest.mark.asyncio
    async def test_on_track_populates_years(self):
        from emoney_mcp.scrapers.planning import get_fire_number
        patches = self._patch(investable=100_000, income=100_000, spending=40_000)
        for p in patches:
            p.start()
        try:
            result = await get_fire_number(AsyncMock())
        finally:
            for p in patches:
                p.stop()
        assert result["fi_status"] == "on_track"
        assert result["years_to_fi_at_current_pace"] is not None


# ---------------------------------------------------------------------------
# #49 — get_50_30_20_analysis excludes the current partial month
# ---------------------------------------------------------------------------

class TestFiftyThirtyTwentyExcludesCurrentMonth:

    @pytest.mark.asyncio
    async def test_current_month_spike_excluded(self):
        from emoney_mcp.scrapers._helpers import _month_offset
        from emoney_mcp.scrapers.spending import get_50_30_20_analysis

        now = datetime.now()
        curr  = now.strftime("%Y-%m")
        prev1 = _month_offset(now, 1).strftime("%Y-%m")
        prev2 = _month_offset(now, 2).strftime("%Y-%m")

        def txn(date, amount, is_income, category):
            return {"date": date, "amount": amount, "is_income": is_income,
                    "is_excluded": False, "category": category}

        txns = [
            txn(f"{curr}-05",  9_999, False, "Dining"),   # current partial month → excluded
            txn(f"{prev1}-10", 1_000, False, "Dining"),
            txn(f"{prev2}-10", 1_000, False, "Dining"),
            txn(f"{prev1}-01", 4_000, True,  "Paycheck/Salary"),
            txn(f"{prev2}-01", 4_000, True,  "Paycheck/Salary"),
        ]
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data",
                   new=AsyncMock(return_value=(txns, True))):
            result = await get_50_30_20_analysis(AsyncMock(), months=2)

        # "Dining" is a Want; averaged over the two complete months it is 1,000.
        # If the current-month $9,999 spike were included this would be far higher.
        assert result["wants"]["monthly_avg"] == pytest.approx(1_000.0)
        assert result["avg_monthly_income"] == pytest.approx(4_000.0)
