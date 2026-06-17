"""
Regression tests for the v1.0.17 Tier 3 issue-fix batch (hardening, semantics).

Issue numbers reference the GitHub tracker.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# #43 — _bool converter doesn't treat the string "false" as True
# ---------------------------------------------------------------------------

class TestBoolConverter:

    def test_native_bools_pass_through(self):
        from emoney_mcp.server import _bool
        assert _bool(True) is True
        assert _bool(False) is False

    def test_stringly_typed_falsey_values(self):
        from emoney_mcp.server import _bool
        assert _bool("false") is False
        assert _bool("0") is False
        assert _bool("no") is False
        assert _bool("") is False

    def test_stringly_typed_truthy_values(self):
        from emoney_mcp.server import _bool
        assert _bool("true") is True
        assert _bool("1") is True
        assert _bool("yes") is True


# ---------------------------------------------------------------------------
# #62 / #64 — host validation only trusts emaplan.com
# ---------------------------------------------------------------------------

class TestIsEmoneyHost:

    def test_trusted_hosts(self):
        from emoney_mcp.browser import is_emoney_host
        assert is_emoney_host("https://wealth.emaplan.com/ema/CS/Home")
        assert is_emoney_host("https://auth.wealth.emaplan.com/oauth")
        assert is_emoney_host("https://emaplan.com/x")

    def test_untrusted_hosts(self):
        from emoney_mcp.browser import is_emoney_host
        assert not is_emoney_host("https://evil.com/emaplan.com")        # path, not host
        assert not is_emoney_host("https://emaplan.com.evil.com/")       # suffix spoof
        assert not is_emoney_host("https://notemaplan.com/")
        assert not is_emoney_host("")


# ---------------------------------------------------------------------------
# #45 — top-level handler surfaces error_type and keeps the tool name
# ---------------------------------------------------------------------------

class TestCallToolErrorType:

    @pytest.mark.asyncio
    async def test_error_type_included(self):
        import emoney_mcp.server as server

        async def boom(name, args):
            raise RuntimeError("kaboom")

        with patch.object(server, "_call_tool_inner", new=boom):
            out = await server.call_tool("get_accounts", {})
        payload = json.loads(out[0].text)
        assert payload["error_type"] == "RuntimeError"
        assert payload["tool"] == "get_accounts"
        assert "kaboom" in payload["error"]


# ---------------------------------------------------------------------------
# #56 — update_transaction_splits only emits allowlisted form fields
# ---------------------------------------------------------------------------

class TestSplitFieldAllowlist:

    @pytest.mark.asyncio
    async def test_unexpected_keys_dropped(self):
        from emoney_mcp.scrapers.transactions import update_transaction_splits

        captured: dict = {}

        async def fake_post(session, path, data):
            captured.update(data)
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", new=fake_post):
            await update_transaction_splits(AsyncMock(), transaction_splits=[
                {
                    "TransactionSplitID": "ts1",
                    "CategoryID": {"Value": "5"},
                    "SplitAmount": 10.0,
                    "UserDescription": "ok",
                    "injected": "evil",          # must be dropped
                    "CategoryID_typo": {"x": 1},  # must be dropped
                },
            ])

        keys = set(captured)
        assert "transactionSplits[0][SplitAmount]" in keys
        assert "transactionSplits[0][CategoryID][Value]" in keys
        assert "transactionSplits[0][UserDescription]" in keys
        assert not any("injected" in k or "evil" in k or "typo" in k for k in keys)


# ---------------------------------------------------------------------------
# #38 — short generic CC keywords match on word boundaries, not substrings
# ---------------------------------------------------------------------------

class TestDebtClassificationWordBoundary:

    @pytest.mark.asyncio
    async def test_comcast_not_a_credit_card(self):
        from emoney_mcp.scrapers.accounts import get_debt_payoff_plan
        accts = {"account_groups": [
            {"group": "Loans", "accounts": [
                # "Comcast" contains "mc" as an incidental substring.
                {"name": "Comcast Financing", "balance": -5_000, "type": "Loan"},
                {"name": "Chase Visa Card",   "balance": -2_000, "type": "CreditCard"},
            ]},
        ]}
        with patch("emoney_mcp.scrapers.accounts.get_accounts", return_value=accts):
            result = await get_debt_payoff_plan(AsyncMock())

        by_name = {d["name"]: d for d in result["debt_accounts"]}
        assert by_name["Comcast Financing"]["type"] == "loan"
        assert by_name["Chase Visa Card"]["type"] == "credit_card"
