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

    @pytest.mark.asyncio
    async def test_update_category_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", category_id="42")
        assert result["success"] is True
        assert result["transaction_id"] == "txn-123"
        assert result["updated"]["category_id"] == "42"

    @pytest.mark.asyncio
    async def test_update_description_success(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-456", description="Coffee run")
        assert result["success"] is True
        assert result["updated"]["description"] == "Coffee run"

    @pytest.mark.asyncio
    async def test_update_both_fields(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True}):
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(
                session, transaction_id="txn-789",
                category_id="10", description="Lunch"
            )
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
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "UpdateTransaction returned HTTP 403", "response_body": "Forbidden"}):
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
        assert len(result["splits"]) == 2
        assert result["total"] == 85.0

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

# Canonical wrapped-object rule shape (Emoney's normal format)
_RULE_WRAPPED = {
    "1": {
        "RuleID":             {"Value": "1", "IsValid": True},
        "DescriptionContains": "COSTCO",
        "CategoryID":          {"Value": "12", "IsValid": True},
        "UserDescription":     "Costco → Groceries",
        "MinAmount":           None,
        "MaxAmount":           None,
        "StartDay":            None,
        "EndDay":              None,
    }
}

# List-shaped response (alternate format the API may return)
_RULE_LIST = [
    {
        "RuleID":             {"Value": "42", "IsValid": True},
        "DescriptionContains": "AMAZON",
        "CategoryID":          {"Value": "5", "IsValid": True},
        "UserDescription":     "Amazon → Shopping",
        "MinAmount":           10.0,
        "MaxAmount":           None,
        "StartDay":            None,
        "EndDay":              None,
    }
]

# Scalar (plain) rule shape — no wrapper objects
_RULE_SCALAR = [
    {
        "RuleID":             "99",
        "DescriptionContains": "STARBUCKS",
        "CategoryID":          "3",
        "UserDescription":     "Starbucks → Coffee",
        "MinAmount":           None,
        "MaxAmount":           None,
        "StartDay":            None,
        "EndDay":              None,
    }
]


class TestGetTransactionRules:

    @pytest.mark.asyncio
    async def test_dict_response_parsed(self):
        """API returns a dict keyed by rule_id — should be normalised to a list."""
        session = make_mock_http_session()
        with _make_csrf_mock(_RULE_WRAPPED):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 1
        rule = result["rules"][0]
        assert rule["rule_id"] == "1"
        assert rule["description_contains"] == "COSTCO"
        assert rule["category_id"] == "12"
        assert rule["user_description"] == "Costco → Groceries"

    @pytest.mark.asyncio
    async def test_list_response_parsed(self):
        """API may return a list — should be handled without crashing."""
        session = make_mock_http_session()
        with _make_csrf_mock(_RULE_LIST):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 1
        rule = result["rules"][0]
        assert rule["rule_id"] == "42"
        assert rule["description_contains"] == "AMAZON"
        assert rule["category_id"] == "5"

    @pytest.mark.asyncio
    async def test_scalar_rule_id_and_category_id(self):
        """Plain scalar RuleID/CategoryID (not wrapped objects) must be extracted correctly."""
        session = make_mock_http_session()
        with _make_csrf_mock(_RULE_SCALAR):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 1
        rule = result["rules"][0]
        assert rule["rule_id"] == "99"
        assert rule["category_id"] == "3"

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_no_rules(self):
        session = make_mock_http_session()
        with _make_csrf_mock({}):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert result["count"] == 0
        assert result["rules"] == []

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with _make_csrf_mock({"error": "GetRules returned HTTP 500", "response_body": "<html>Error</html>"}):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_unexpected_type_returns_error(self):
        """A non-list, non-dict response (e.g. raw string) should return an error dict."""
        session = make_mock_http_session()
        with _make_csrf_mock("unexpected string"):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            result = await get_transaction_rules(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_filter_param_sent_in_payload(self):
        """GetRules POST must include filter='' to avoid server-side 500 (Bug 2 fix)."""
        session = make_mock_http_session()
        captured_data = {}

        async def capture_csrf_post(http_session, path, data):
            captured_data.update(data)
            return {}

        with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture_csrf_post):
            from emoney_mcp.scrapers.transactions import get_transaction_rules
            await get_transaction_rules(session)

        assert "filter" in captured_data, "GetRules must send filter param to avoid server 500"

    @pytest.mark.asyncio
    async def test_all_rule_fields_present(self):
        session = make_mock_http_session()
        with _make_csrf_mock(_RULE_LIST):
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

    @pytest.mark.asyncio
    async def test_apply_rule_success(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_EXISTING_RULES_RESPONSE)):
            with _make_csrf_mock({"Success": True, "Updated": 7}):
                from emoney_mcp.scrapers.transactions import apply_transaction_rule
                result = await apply_transaction_rule(session, rule_id="10")
        assert result["success"] is True
        assert result["rule_id"] == "10"
        assert "COSTCO" in result["description"]

    @pytest.mark.asyncio
    async def test_apply_with_transaction_id(self):
        session = make_mock_http_session()
        captured_data = {}

        async def capture(http_session, path, data):
            captured_data.update(data)
            return {"Success": True}

        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_EXISTING_RULES_RESPONSE)):
            with patch("emoney_mcp.scrapers.transactions._csrf_post", side_effect=capture):
                from emoney_mcp.scrapers.transactions import apply_transaction_rule
                await apply_transaction_rule(session, rule_id="10", transaction_id="txn-555")

        assert captured_data.get("transactionID") == "txn-555"

    @pytest.mark.asyncio
    async def test_rule_not_found_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value={"rules": [], "count": 0})):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            result = await apply_transaction_rule(session, rule_id="999")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_csrf_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_EXISTING_RULES_RESPONSE)):
            with _make_csrf_mock({"error": "ApplyRule returned HTTP 500", "response_body": ""}):
                from emoney_mcp.scrapers.transactions import apply_transaction_rule
                result = await apply_transaction_rule(session, rule_id="10")
        assert "error" in result
