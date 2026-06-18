"""
Tests for scrapers/transactions.py:
  - update_transaction
  - hide_transaction
  - get_transaction_splits / update_transaction_splits
  - get_transaction_rules (Bug 2 fix: filter param, list/dict response, scalar RuleID)
  - add_transaction_rule
  - update_transaction_rule
  - apply_transaction_rule
  - _csrf_post error surfacing (response_body in error dict)

All tests mock _csrf_post or the underlying HTTP layer — no live network calls.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_csrf_mock(return_value):
    """Patch _csrf_post to return a fixed value."""
    return patch(
        "emoney_mcp.scrapers.transactions._csrf_post",
        new=AsyncMock(return_value=return_value),
    )


# ===========================================================================
# _csrf_post error surfacing (v0.9.1 fix)
# ===========================================================================

class TestCsrfPostErrorSurfacing:

    @pytest.mark.asyncio
    async def test_error_includes_response_body_on_http_failure(self):
        """_csrf_post must include response_body in error dict on non-2xx status."""
        session = make_mock_http_session()
        # Patch http.post to return a 500 with a body
        bad_resp = MagicMock()
        bad_resp.status_code = 500
        bad_resp.headers = {"content-type": "text/html"}
        bad_resp.text = "<html>Internal Server Error — NullReferenceException</html>"

        from emoney_mcp.scrapers.transactions import _csrf_post
        with patch.object(
            (await session.get_http()),
            "post",
            new=AsyncMock(return_value=bad_resp),
        ):
            result = await _csrf_post(session, "SomeEndpoint", {})

        assert "error" in result
        assert "500" in result["error"]
        assert "response_body" in result
        assert "NullReference" in result["response_body"]

    @pytest.mark.asyncio
    async def test_error_body_truncated_to_400_chars(self):
        """response_body must be truncated to first 400 characters."""
        session = make_mock_http_session()
        long_body = "X" * 1000

        bad_resp = MagicMock()
        bad_resp.status_code = 400
        bad_resp.headers = {"content-type": "text/html"}
        bad_resp.text = long_body

        from emoney_mcp.scrapers.transactions import _csrf_post
        with patch.object(
            (await session.get_http()),
            "post",
            new=AsyncMock(return_value=bad_resp),
        ):
            result = await _csrf_post(session, "SomeEndpoint", {})

        assert len(result["response_body"]) <= 400


# ===========================================================================
# update_transaction
# ===========================================================================

class TestUpdateTransaction:
    """update_transaction POSTs JSON to the SNB UpdateTransaction endpoint,
    merging the requested change over the transaction's CURRENT values so an
    unspecified field is never wiped (mirrors the live web UI)."""

    _TXNS = [{"id": "txn-123", "categoryId": "7", "userDescription": "Old desc", "notes": None}]

    def _patch_raw(self):
        return patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                     new=AsyncMock(return_value=(True, self._TXNS, {})))

    @pytest.mark.asyncio
    async def test_update_category_preserves_description(self):
        session = make_mock_http_session()
        captured = {}

        async def snb(http_session, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=snb), self._patch_raw():
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", category_id="42")
        assert result["success"] is True
        assert result["transaction_id"] == "txn-123"
        assert result["updated"]["category_id"] == "42"
        # merge: category changed, description preserved (not nulled)
        assert captured["action"] == "UpdateTransaction"
        assert captured["payload"]["transactionId"] == "txn-123"
        assert captured["payload"]["categoryId"] == "42"
        assert captured["payload"]["userDescription"] == "Old desc"

    @pytest.mark.asyncio
    async def test_update_description_preserves_category(self):
        session = make_mock_http_session()
        captured = {}

        async def snb(http_session, action, payload):
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=snb), self._patch_raw():
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", description="Coffee run")
        assert result["updated"]["description"] == "Coffee run"
        assert captured["payload"]["userDescription"] == "Coffee run"
        assert captured["payload"]["categoryId"] == "7"  # preserved from current

    @pytest.mark.asyncio
    async def test_update_both_fields(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions._snb_post",
                   new=AsyncMock(return_value={"ok": True})), self._patch_raw():
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(
                session, transaction_id="txn-123", category_id="10", description="Lunch")
        assert result["success"] is True
        assert "category_id" in result["updated"]
        assert "description" in result["updated"]

    @pytest.mark.asyncio
    async def test_neither_field_provided_returns_error(self):
        session = make_mock_http_session()
        from emoney_mcp.scrapers.transactions import update_transaction
        result = await update_transaction(session, transaction_id="txn-001")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_snb_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions._snb_post",
                   new=AsyncMock(return_value={"error": "UpdateTransaction returned HTTP 403"})), self._patch_raw():
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", category_id="5")
        assert "error" in result


# ===========================================================================
# hide_transaction
# ===========================================================================

class TestHideTransaction:

    @pytest.mark.asyncio
    async def test_hide_returns_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import hide_transaction
            result = await hide_transaction(session, transaction_id="txn-001", hidden=True)
        assert result["success"] is True
        assert result["hidden"] is True

    @pytest.mark.asyncio
    async def test_unhide_returns_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import hide_transaction
            result = await hide_transaction(session, transaction_id="txn-001", hidden=False)
        assert result["success"] is True
        assert result["hidden"] is False

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "UpdateTransactionHiddenStatus returned HTTP 500", "response_body": ""}):
            from emoney_mcp.scrapers.transactions import hide_transaction
            result = await hide_transaction(session, transaction_id="txn-001")
        assert "error" in result


# ===========================================================================
# get_transaction_splits
# ===========================================================================

class TestGetTransactionSplits:

    @pytest.mark.asyncio
    async def test_returns_splits_list(self):
        session = make_mock_http_session()
        # API may return dict-wrapped or list directly; test both shapes via the dict form
        api_response = {
            "Splits": [
                {"TransactionSplitID": "s1", "CategoryID": {"Value": "1"}, "SplitAmount": 50.0},
                {"TransactionSplitID": "s2", "CategoryID": {"Value": "2"}, "SplitAmount": 35.0},
            ],
            "Total": 85.0,
        }
        with _make_csrf_mock(api_response):
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-split")
        assert result["transaction_id"] == "txn-split"
        assert result["split_count"] == 2
        assert result["is_split"] is True
        assert result["total_amount"] == 85.0
        assert result["splits"][0]["category_id"] == "1"
        assert result["splits"][1]["category_id"] == "2"

    @pytest.mark.asyncio
    async def test_list_response_shape(self):
        """API returns a bare list — code must handle it without crashing."""
        session = make_mock_http_session()
        api_response = [
            {"CategoryID": {"Value": "21"}, "SplitAmount": -40.01,
             "Description": "TST* PURA VIDA", "UserDescription": None},
        ]
        with _make_csrf_mock(api_response):
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-list")
        assert result["split_count"] == 1
        assert result["is_split"] is False
        assert result["total_amount"] == -40.01
        assert result["splits"][0]["category_id"] == "21"

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "GetAllBankTransactionSplits returned HTTP 404", "response_body": ""}):
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-x")
        assert "error" in result


# ===========================================================================
# update_transaction_splits
# ===========================================================================

class TestUpdateTransactionSplits:

    @pytest.mark.asyncio
    async def test_success(self):
        session = make_mock_http_session()
        splits = [
            {"TransactionSplitID": None, "CategoryID": {"Value": "1"}, "SplitAmount": 30.0, "UserDescription": ""},
            {"TransactionSplitID": None, "CategoryID": {"Value": "2"}, "SplitAmount": 55.0, "UserDescription": ""},
        ]
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import update_transaction_splits
            result = await update_transaction_splits(session, transaction_splits=splits)
        assert result["success"] is True
        assert result["splits_updated"] == 2

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "UpdateTransactionSplits returned HTTP 400", "response_body": "Bad Request"}):
            from emoney_mcp.scrapers.transactions import update_transaction_splits
            result = await update_transaction_splits(session, transaction_splits=[])
        assert "error" in result


# ===========================================================================
# get_transaction_rules  (Bug 2 fix)
# ===========================================================================

# SNB GetBankTransactionRules shape — flat camelCase objects (the live format)
_SNB_RULES = [
    {
        "ruleID":             "42",
        "categoryID":         "5",
        "descriptionContains": "AMAZON",
        "userDescription":    "Amazon → Shopping",
        "minAmount":          10.0,
        "maxAmount":          None,
        "startDay":           None,
        "endDay":             None,
        "extensionData":      None,
    },
    {
        "ruleID":             "99",
        "categoryID":         "3",
        "descriptionContains": "STARBUCKS",
        "userDescription":    "Starbucks → Coffee",
        "minAmount":          None,
        "maxAmount":          None,
        "startDay":           None,
        "endDay":             None,
        "extensionData":      None,
    },
]


class TestGetTransactionRules:
    """get_transaction_rules reads the SNB GetBankTransactionRules endpoint
    (the dead legacy /ema/CS/Spending/GetRules path is no longer used)."""

    @staticmethod
    def _snb_get_mock(data):
        return patch("emoney_mcp.scrapers.transactions._snb_get",
                     new=AsyncMock(return_value={"ok": True, "data": data}))

    @pytest.mark.asyncio
    async def test_list_response_parsed(self):
        session = make_mock_http_session()
        with self._snb_get_mock(_SNB_RULES):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 2
        rule = result["rules"][0]
        assert rule["rule_id"] == "42"
        assert rule["description_contains"] == "AMAZON"
        assert rule["category_id"] == "5"
        assert rule["user_description"] == "Amazon → Shopping"
        assert rule["min_amount"] == 10.0

    @pytest.mark.asyncio
    async def test_wrapped_rules_key_tolerated(self):
        """Tolerate a {Rules:[...]} wrapper as well as a bare list."""
        session = make_mock_http_session()
        with self._snb_get_mock({"Rules": _SNB_RULES}):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 2

    @pytest.mark.asyncio
    async def test_empty_list(self):
        session = make_mock_http_session()
        with self._snb_get_mock([]):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 0
        assert result["rules"] == []

    @pytest.mark.asyncio
    async def test_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions._snb_get",
                   new=AsyncMock(return_value={"error": "GetBankTransactionRules returned HTTP 401"})):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_reads_snb_endpoint_not_legacy(self):
        """Must hit the SNB GetBankTransactionRules action, not legacy GetRules."""
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action):
            captured["action"] = action
            return {"ok": True, "data": []}

        with patch("emoney_mcp.scrapers.transactions._snb_get", side_effect=cap):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            await get_transaction_rules(session)
        assert captured["action"] == "GetBankTransactionRules"

    @pytest.mark.asyncio
    async def test_all_rule_fields_present(self):
        session = make_mock_http_session()
        with self._snb_get_mock(_SNB_RULES):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        rule = result["rules"][0]
        for field in ("rule_id", "description_contains", "category_id",
                      "user_description", "min_amount", "max_amount",
                      "start_day", "end_day"):
            assert field in rule, f"Missing field: {field}"


# ===========================================================================
# add_transaction_rule
# ===========================================================================

class TestAddTransactionRule:

    @pytest.mark.asyncio
    async def test_add_rule_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True, "RuleID": {"Value": "77"}}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(
                session,
                description_contains="NETFLIX",
                category_id="6",
                user_description="Netflix → Subscriptions",
            )
        assert result["success"] is True
        assert result["description_contains"] == "NETFLIX"
        assert result["category_id"] == "6"

    @pytest.mark.asyncio
    async def test_add_rule_with_amount_range(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(
                session,
                description_contains="STARBUCKS",
                category_id="3",
                min_amount=5.0,
                max_amount=20.0,
            )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_rule_with_transaction_id(self):
        session = make_mock_http_session()
        captured_data = {}

        async def capture(http_session, path, data):
            captured_data.update(data)
            return {"Success": True}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            await add_transaction_rule(
                session,
                description_contains="AMAZON",
                category_id="5",
                transaction_id="txn-999",
            )

        assert captured_data.get("transactionID") == "txn-999"

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "AddRule returned HTTP 403", "response_body": ""}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(session, description_contains="X", category_id="1")
        assert "error" in result


# ===========================================================================
# update_transaction_rule
# ===========================================================================

_EXISTING_RULES_RESPONSE = {
    "rules": [
        {
            "rule_id": "10",
            "description_contains": "COSTCO",
            "category_id": "1",
            "user_description": "Costco → Groceries",
            "min_amount": None,
            "max_amount": None,
            "start_day": None,
            "end_day": None,
        }
    ],
    "count": 1,
}


class TestUpdateTransactionRule:

    @pytest.mark.asyncio
    async def test_update_category(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_EXISTING_RULES_RESPONSE)):
            with _make_csrf_mock({"Success": True}):
                from emoney_mcp.scrapers.transactions import update_transaction_rule
                result = await update_transaction_rule(
                    session, rule_id="10", category_id="99"
                )
        assert result["success"] is True
        assert result["rule_id"] == "10"
        assert result["updated"]["category_id"] == "99"

    @pytest.mark.asyncio
    async def test_rule_not_found_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value={"rules": [], "count": 0})):
            from emoney_mcp.scrapers.transactions import update_transaction_rule
            result = await update_transaction_rule(
                session, rule_id="999", category_id="1"
            )
        assert "error" in result
        assert "999" in result["error"]

    @pytest.mark.asyncio
    async def test_get_rules_failure_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value={"error": "GetRules returned HTTP 500", "response_body": ""})):
            from emoney_mcp.scrapers.transactions import update_transaction_rule
            result = await update_transaction_rule(session, rule_id="10")
        assert "error" in result


# ===========================================================================
# apply_transaction_rule
# ===========================================================================

class TestApplyTransactionRule:
    """
    apply_transaction_rule sends {ruleID, transactionID} directly to ApplyRule.
    It does NOT look up the rule first — that is intentional (matches JS signature).
    """

    @pytest.mark.asyncio
    async def test_apply_rule_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True, "Updated": 7}):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            result = await apply_transaction_rule(session, rule_id="10")
        assert result["success"] is True
        assert result["rule_id"] == "10"

    @pytest.mark.asyncio
    async def test_apply_sends_rule_id_in_payload(self):
        """ApplyRule POST must contain ruleID (not a full rule object)."""
        session = make_mock_http_session()
        captured_data = {}

        async def capture(http_session, path, data):
            captured_data.update(data)
            return {"Success": True}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            await apply_transaction_rule(session, rule_id="10")

        assert captured_data.get("ruleID") == "10"
        assert "rule[RuleID][Value]" not in captured_data, "Must not send full rule object"

    @pytest.mark.asyncio
    async def test_apply_with_transaction_id(self):
        session = make_mock_http_session()
        captured_data = {}

        async def capture(http_session, path, data):
            captured_data.update(data)
            return {"Success": True}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            await apply_transaction_rule(session, rule_id="10", transaction_id="txn-555")

        assert captured_data.get("ruleID") == "10"
        assert captured_data.get("transactionID") == "txn-555"

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "ApplyRule returned HTTP 500", "response_body": ""}):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            result = await apply_transaction_rule(session, rule_id="10")
        assert "error" in result


# ===========================================================================
# delete_transaction_rule (#19)
# ===========================================================================

class TestDeleteTransactionRule:
    """
    delete_transaction_rule sends {ruleID} directly to RemoveRule
    (JS signature: RemoveRule(ruleID)).
    """

    @pytest.mark.asyncio
    async def test_delete_rule_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import delete_transaction_rule
            result = await delete_transaction_rule(session, rule_id="42")
        assert result["success"] is True
        assert result["deleted"] is True
        assert result["rule_id"] == "42"

    @pytest.mark.asyncio
    async def test_delete_sends_rule_id_to_removerule(self):
        """RemoveRule POST must send ruleID — not a full rule object."""
        session = make_mock_http_session()
        captured = {"path": None, "data": {}}

        async def capture(http_session, path, data):
            captured["path"] = path
            captured["data"].update(data)
            return {"Success": True}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture):
            from emoney_mcp.scrapers.transactions import delete_transaction_rule
            await delete_transaction_rule(session, rule_id="42")

        assert captured["path"] == "RemoveRule"
        assert captured["data"].get("ruleID") == "42"
        assert "rule[RuleID][Value]" not in captured["data"], "Must not send full rule object"

    @pytest.mark.asyncio
    async def test_missing_rule_id_returns_error_without_posting(self):
        session = make_mock_http_session()
        called = False

        async def capture(http_session, path, data):
            nonlocal called
            called = True
            return {"Success": True}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture):
            from emoney_mcp.scrapers.transactions import delete_transaction_rule
            result = await delete_transaction_rule(session, rule_id="")
        assert "error" in result
        assert called is False, "Must not POST when rule_id is empty"

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "RemoveRule returned HTTP 500", "response_body": ""}):
            from emoney_mcp.scrapers.transactions import delete_transaction_rule
            result = await delete_transaction_rule(session, rule_id="42")
        assert "error" in result


# ===========================================================================
# raw passthrough gating (EMONEY_DEV) — issue #24
# ===========================================================================

class TestRawGating:

    @pytest.mark.asyncio
    async def test_add_rule_omits_raw_by_default(self, monkeypatch):
        monkeypatch.delenv("EMONEY_DEV", raising=False)
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True, "RuleID": {"Value": "77"}}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(session, description_contains="X", category_id="1")
        assert "raw" not in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_rule_includes_raw_in_dev(self, monkeypatch):
        monkeypatch.setenv("EMONEY_DEV", "1")
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True, "RuleID": {"Value": "77"}}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(session, description_contains="X", category_id="1")
        assert result.get("raw") == {"Success": True, "RuleID": {"Value": "77"}}

    @pytest.mark.asyncio
    async def test_apply_rule_omits_raw_by_default(self, monkeypatch):
        monkeypatch.delenv("EMONEY_DEV", raising=False)
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True, "Updated": 7}):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            result = await apply_transaction_rule(session, rule_id="10")
        assert "raw" not in result
        assert result["success"] is True
