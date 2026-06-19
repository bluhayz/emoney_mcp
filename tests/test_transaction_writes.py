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


def _make_snb_post_mock(return_value=None):
    """Patch _snb_post (the modern SNB write path) to return a fixed value.

    Defaults to a success envelope, matching ``_snb_post``'s ``{"ok": True,
    "data": ...}`` shape on a 2xx with no JSON body.
    """
    if return_value is None:
        return_value = {"ok": True}
    return patch(
        "emoney_mcp.scrapers.transactions._snb_post",
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

    def _patch_raw(self, post=None):
        """Patch _fetch_snb_raw with a 2-call sequence: the pre-write read (current
        values, for the merge) then the post-write read-back (#126 verification).

        ``post`` is the transaction list the read-back sees; defaults to the
        pre-write state (i.e. nothing changed → simulates a no-op write)."""
        post_state = self._TXNS if post is None else post
        return patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                     new=AsyncMock(side_effect=[(True, self._TXNS, {}),
                                                (True, post_state, {})]))

    @pytest.mark.asyncio
    async def test_update_category_preserves_description(self):
        session = make_mock_http_session()
        captured = {}

        async def snb(http_session, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"ok": True}

        post = [{"id": "txn-123", "categoryId": "42", "userDescription": "Old desc", "notes": None}]
        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=snb), self._patch_raw(post):
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", category_id="42")
        assert result["success"] is True
        assert result["verified"] is True
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

        post = [{"id": "txn-123", "categoryId": "7", "userDescription": "Coffee run", "notes": None}]
        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=snb), self._patch_raw(post):
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", description="Coffee run")
        assert result["updated"]["description"] == "Coffee run"
        assert captured["payload"]["userDescription"] == "Coffee run"
        assert captured["payload"]["categoryId"] == "7"  # preserved from current

    @pytest.mark.asyncio
    async def test_update_both_fields(self):
        session = make_mock_http_session()
        post = [{"id": "txn-123", "categoryId": "10", "userDescription": "Lunch", "notes": None}]
        with patch("emoney_mcp.scrapers.transactions._snb_post",
                   new=AsyncMock(return_value={"ok": True})), self._patch_raw(post):
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(
                session, transaction_id="txn-123", category_id="10", description="Lunch")
        assert result["success"] is True
        assert result["verified"] is True
        assert "category_id" in result["updated"]
        assert "description" in result["updated"]

    @pytest.mark.asyncio
    async def test_no_op_write_reports_error_not_false_success(self):
        """#126: a 200 from SNB that does NOT persist must surface as an error,
        not a false-positive success. Read-back still shows the old category."""
        session = make_mock_http_session()
        # post-write read-back returns the UNCHANGED transaction (no-op write)
        with patch("emoney_mcp.scrapers.transactions._snb_post",
                   new=AsyncMock(return_value={"ok": True})), self._patch_raw():
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", category_id="42")
        assert "success" not in result
        assert "error" in result
        assert "did not persist" in result["error"]
        assert result["actual"]["category_id"] == {"expected": "42", "actual": "7"}

    @pytest.mark.asyncio
    async def test_unverifiable_write_is_flagged_not_claimed(self):
        """#126: if the read-back itself fails, don't claim a verified success —
        return success with verified=False and a warning."""
        session = make_mock_http_session()
        raw = patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                    new=AsyncMock(side_effect=[(True, self._TXNS, {}), (False, [], {})]))
        with patch("emoney_mcp.scrapers.transactions._snb_post",
                   new=AsyncMock(return_value={"ok": True})), raw:
            from emoney_mcp.scrapers.transactions import update_transaction
            result = await update_transaction(session, transaction_id="txn-123", category_id="42")
        assert result["success"] is True
        assert result["verified"] is False
        assert "warning" in result

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
    async def test_hide_posts_to_snb_toggle_visibility(self):
        """Must hit SNB ToggleTransactionVisibility with {hideTransaction, transactionId}."""
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
            from emoney_mcp.scrapers.transactions import hide_transaction
            result = await hide_transaction(session, transaction_id="txn-001", hidden=True)
        assert result["success"] is True and result["hidden"] is True
        assert captured["action"] == "ToggleTransactionVisibility"
        assert captured["payload"] == {"hideTransaction": True, "transactionId": "txn-001"}

    @pytest.mark.asyncio
    async def test_unhide_sends_false(self):
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
            from emoney_mcp.scrapers.transactions import hide_transaction
            result = await hide_transaction(session, transaction_id="txn-001", hidden=False)
        assert result["hidden"] is False
        assert captured["payload"]["hideTransaction"] is False

    @pytest.mark.asyncio
    async def test_snb_error_propagates(self):
        session = make_mock_http_session()
        with _make_snb_post_mock({"error": "ToggleTransactionVisibility returned HTTP 500"}):
            from emoney_mcp.scrapers.transactions import hide_transaction
            result = await hide_transaction(session, transaction_id="txn-001")
        assert "error" in result


# ===========================================================================
# get_transaction_splits
# ===========================================================================

def _snb_get_session(body, status=200, ctype="application/json; charset=utf-8"):
    """Session whose http.get returns a fixed JSON response — for the SNB GET path
    (GetBankTransactionSplits). Also patch _get_snb_credentials when using it."""
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": ctype}
    resp.json.return_value = body
    http = AsyncMock()
    http.get = AsyncMock(return_value=resp)
    session = AsyncMock()
    session.get_http = AsyncMock(return_value=http)
    return session


def _patch_snb_creds():
    return patch("emoney_mcp.scrapers.spending._get_snb_credentials",
                 new=AsyncMock(return_value=("jwt-token", "api-key")))


class TestGetTransactionSplits:
    # Live SNB GetBankTransactionSplits shape: camelCase, wrapped {value} ids.
    @pytest.mark.asyncio
    async def test_returns_splits_list(self):
        body = [
            {"categoryID": {"value": "1"}, "splitAmount": 50.0, "userDescription": "A"},
            {"categoryID": {"value": "2"}, "splitAmount": 35.0, "userDescription": "B"},
        ]
        session = _snb_get_session(body)
        with _patch_snb_creds():
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-split")
        assert result["transaction_id"] == "txn-split"
        assert result["split_count"] == 2
        assert result["is_split"] is True
        assert result["total_amount"] == 85.0
        assert result["splits"][0]["category_id"] == "1"
        assert result["splits"][1]["category_id"] == "2"

    @pytest.mark.asyncio
    async def test_single_split_not_flagged(self):
        body = [
            {"categoryID": {"value": "21"}, "splitAmount": -40.01,
             "description": "TST* PURA VIDA", "userDescription": None},
        ]
        session = _snb_get_session(body)
        with _patch_snb_creds():
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-list")
        assert result["split_count"] == 1
        assert result["is_split"] is False
        assert result["total_amount"] == -40.01
        assert result["splits"][0]["category_id"] == "21"

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        session = _snb_get_session(None, status=404, ctype="text/html")
        with _patch_snb_creds():
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-x")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_missing_credentials_errors(self):
        session = _snb_get_session([])
        with patch("emoney_mcp.scrapers.spending._get_snb_credentials",
                   new=AsyncMock(return_value=(None, None))):
            from emoney_mcp.scrapers.transactions import get_transaction_splits
            result = await get_transaction_splits(session, transaction_id="txn-x")
        assert "error" in result


# ===========================================================================
# update_transaction_splits
# ===========================================================================

class TestUpdateTransactionSplits:
    # Existing-split metadata returned by GetBankTransactionSplits (carried over).
    _EXISTING = [{
        "categoryID": {"value": "103"}, "splitAmount": -80.0,
        "cleanDescription": "VENMO PAYMENT", "description": "VENMO PAYMENT",
        "postDate": "2026-06-18T00:00:00Z", "transactionDate": "2026-06-18T00:00:00Z",
        "transactionID": {"value": "txn-1"}, "parentTransactionID": None,
    }]

    @pytest.mark.asyncio
    async def test_two_way_split_builds_parent_and_child(self):
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._fetch_splits_raw",
                   new=AsyncMock(return_value=(self._EXISTING, None))):
            with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
                from emoney_mcp.scrapers.transactions import update_transaction_splits
                result = await update_transaction_splits(session, transaction_id="txn-1", splits=[
                    {"category_id": "103", "amount": -40.00},
                    {"category_id": "103", "amount": -40.00},
                ])
        assert result["success"] is True and result["splits_written"] == 2 and result["is_split"] is True
        assert captured["action"] == "updateTransactionSplits"
        body = captured["payload"]
        assert isinstance(body, list) and len(body) == 2
        # parent: transactionID set, parentTransactionID null; amount is a string
        assert body[0]["transactionID"] == {"value": "txn-1"}
        assert body[0]["parentTransactionID"] is None
        assert body[0]["categoryID"] == {"value": "103"}
        assert body[0]["splitAmount"] == "-40.00"
        # child: transactionID null, parentTransactionID set, identity present
        assert body[1]["transactionID"] is None
        assert body[1]["parentTransactionID"] == {"value": "txn-1"}
        assert body[1]["identity"] == 1
        # metadata carried over from the existing record
        assert body[0]["description"] == "VENMO PAYMENT"
        assert body[0]["transactionDate"] == "2026-06-18T00:00:00Z"

    @pytest.mark.asyncio
    async def test_single_split_unsplits(self):
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._fetch_splits_raw",
                   new=AsyncMock(return_value=(self._EXISTING, None))):
            with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
                from emoney_mcp.scrapers.transactions import update_transaction_splits
                result = await update_transaction_splits(session, transaction_id="txn-1", splits=[
                    {"category_id": "103", "amount": -80.00},
                ])
        assert result["is_split"] is False
        assert len(captured["payload"]) == 1
        assert captured["payload"][0]["parentTransactionID"] is None

    @pytest.mark.asyncio
    async def test_validation_errors(self):
        session = make_mock_http_session()
        from emoney_mcp.scrapers.transactions import update_transaction_splits
        assert "error" in await update_transaction_splits(session, transaction_id="t", splits=[])
        assert "error" in await update_transaction_splits(session, transaction_id="t",
                                                          splits=[{"amount": -10}])  # no category_id
        assert "error" in await update_transaction_splits(session, transaction_id="t",
                                                          splits=[{"category_id": "5"}])  # no amount

    @pytest.mark.asyncio
    async def test_snb_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions._fetch_splits_raw",
                   new=AsyncMock(return_value=(self._EXISTING, None))):
            with patch("emoney_mcp.scrapers.transactions._snb_post",
                       new=AsyncMock(return_value={"error": "updateTransactionSplits returned HTTP 400"})):
                from emoney_mcp.scrapers.transactions import update_transaction_splits
                result = await update_transaction_splits(session, transaction_id="txn-1",
                                                         splits=[{"category_id": "5", "amount": -80}])
        assert "error" in result


# ===========================================================================
# get_transaction_rules  (Bug 2 fix)
# ===========================================================================

# SNB GetBankTransactionRules shape — the LIVE format (captured 2026-06-18, #121).
# ruleID/categoryID are WCF DataContract complex types, NOT bare strings:
#   {"extensionData": {}, "value": "42"}
# A flat-string fixture is what let the v1.0.31 wrapping bug ship — keep this
# mirroring the real payload (see CLAUDE.md "Fixtures must match the real shape").
_SNB_RULES = [
    {
        "ruleID":             {"extensionData": {}, "value": "42"},
        "categoryID":         {"extensionData": {}, "value": "5"},
        "descriptionContains": "AMAZON",
        "userDescription":    "Amazon → Shopping",
        "minAmount":          10.0,
        "maxAmount":          None,
        "startDay":           None,
        "endDay":             None,
        "extensionData":      {},
    },
    {
        "ruleID":             {"extensionData": {}, "value": "99"},
        "categoryID":         {"extensionData": {}, "value": "3"},
        "descriptionContains": "STARBUCKS",
        "userDescription":    "Starbucks → Coffee",
        "minAmount":          None,
        "maxAmount":          None,
        "startDay":           None,
        "endDay":             None,
        "extensionData":      {},
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
    """add_transaction_rule posts to the SNB CreateRule endpoint
    (``{Rule:{...camelCase...}, TransactionID}``), not the dead legacy AddRule."""

    @pytest.mark.asyncio
    async def test_add_rule_success(self):
        session = make_mock_http_session()
        with _make_snb_post_mock({"ok": True, "data": {"ruleID": "77"}}):
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
    async def test_add_rule_posts_to_snb_createrule(self):
        """Must hit SNB CreateRule with the captured PascalCase Rule shape — and
        MUST omit RuleID on create (sending RuleID:{Value:null} causes HTTP 500)."""
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            await add_transaction_rule(
                session,
                description_contains="NETFLIX",
                category_id="6",
                user_description="Netflix → Subscriptions",
            )

        assert captured["action"] == "CreateRule"
        rule = captured["payload"]["Rule"]
        assert rule["DescriptionContains"] == "NETFLIX"
        # IDs must be wrapped {"Value": ...} — SNB rejects flat strings
        assert rule["CategoryID"] == {"Value": "6"}
        assert rule["UserDescription"] == "Netflix → Subscriptions"
        assert "RuleID" not in rule  # create MUST omit RuleID

    @pytest.mark.asyncio
    async def test_add_rule_with_amount_range(self):
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(
                session,
                description_contains="STARBUCKS",
                category_id="3",
                min_amount=5.0,
                max_amount=20.0,
            )
        assert result["success"] is True
        assert captured["payload"]["Rule"]["MinAmount"] == 5.0
        assert captured["payload"]["Rule"]["MaxAmount"] == 20.0

    @pytest.mark.asyncio
    async def test_add_rule_with_transaction_id(self):
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            await add_transaction_rule(
                session,
                description_contains="AMAZON",
                category_id="5",
                transaction_id="txn-999",
            )

        assert captured["payload"].get("TransactionID") == {"Value": "txn-999"}

    @pytest.mark.asyncio
    async def test_snb_error_propagates(self):
        session = make_mock_http_session()
        with _make_snb_post_mock({"error": "CreateRule returned HTTP 403", "response_body": ""}):
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
    """update_transaction_rule posts the full merged Rule object to SNB UpdateRule."""

    @pytest.mark.asyncio
    async def test_update_category(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_EXISTING_RULES_RESPONSE)):
            with _make_snb_post_mock({"ok": True}):
                from emoney_mcp.scrapers.transactions import update_transaction_rule
                result = await update_transaction_rule(
                    session, rule_id="10", category_id="99"
                )
        assert result["success"] is True
        assert result["rule_id"] == "10"
        assert result["updated"]["category_id"] == "99"

    @pytest.mark.asyncio
    async def test_update_posts_full_merged_rule_to_snb(self):
        """UpdateRule must carry the full Rule object — changed field overridden,
        the rest carried over from the existing rule."""
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, action, payload):
            captured["action"] = action
            captured["payload"] = payload
            return {"ok": True}

        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_EXISTING_RULES_RESPONSE)):
            with patch("emoney_mcp.scrapers.transactions._snb_post", side_effect=cap):
                from emoney_mcp.scrapers.transactions import update_transaction_rule
                await update_transaction_rule(session, rule_id="10", category_id="99")

        assert captured["action"] == "UpdateRule"
        rule = captured["payload"]["Rule"]
        # IDs must be wrapped {"Value": ...} — SNB rejects flat strings
        assert rule["RuleID"] == {"Value": "10"}
        assert rule["CategoryID"] == {"Value": "99"}           # changed
        assert rule["DescriptionContains"] == "COSTCO"          # carried over
        assert rule["UserDescription"] == "Costco → Groceries"  # carried over

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

_DELETE_RULES_RESPONSE = {
    "rules": [
        {"rule_id": "42", "description_contains": "AMAZON", "category_id": "5",
         "user_description": "Amazon", "min_amount": 10.0, "max_amount": None,
         "start_day": None, "end_day": None},
        {"rule_id": "99", "description_contains": "STARBUCKS", "category_id": "3",
         "user_description": "Starbucks", "min_amount": None, "max_amount": None,
         "start_day": None, "end_day": None},
    ],
    "count": 2,
}


class TestDeleteTransactionRule:
    """
    SNB has no single-rule delete — delete_transaction_rule reads the current
    rules and bulk-replaces the collection minus the target via SetRules
    (POST /ema/CS/Spending/SetRules, JSON body, CSRF token in header).
    """

    @pytest.mark.asyncio
    async def test_delete_rule_success(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_DELETE_RULES_RESPONSE)):
            with patch("emoney_mcp.scrapers.transactions._csrf_post_json",
                       new=AsyncMock(return_value={"RuleIDs": [{"Value": "99", "IsValid": True}]})):
                from emoney_mcp.scrapers.transactions import delete_transaction_rule
                result = await delete_transaction_rule(session, rule_id="42")
        assert result["success"] is True
        assert result["deleted"] is True
        assert result["rule_id"] == "42"
        assert result["remaining_rule_count"] == 1

    @pytest.mark.asyncio
    async def test_delete_posts_full_list_minus_target_to_setrules(self):
        """SetRules must receive the whole rules collection EXCEPT the deleted one,
        each in the wrapped PascalCase shape."""
        session = make_mock_http_session()
        captured = {}

        async def cap(http_session, path, body):
            captured["path"] = path
            captured["body"] = body
            return {"RuleIDs": []}

        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_DELETE_RULES_RESPONSE)):
            with patch("emoney_mcp.scrapers.transactions._csrf_post_json", side_effect=cap):
                from emoney_mcp.scrapers.transactions import delete_transaction_rule
                await delete_transaction_rule(session, rule_id="42")

        assert captured["path"] == "SetRules"
        kept = captured["body"]["rules"]
        assert len(kept) == 1
        assert kept[0]["RuleID"] == {"Value": "99"}        # the survivor, wrapped
        assert kept[0]["CategoryID"] == {"Value": "3"}
        assert all(r["RuleID"] != {"Value": "42"} for r in kept)  # target removed

    @pytest.mark.asyncio
    async def test_delete_unknown_rule_errors_without_posting(self):
        session = make_mock_http_session()
        called = False

        async def cap(http_session, path, body):
            nonlocal called
            called = True
            return {}

        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_DELETE_RULES_RESPONSE)):
            with patch("emoney_mcp.scrapers.transactions._csrf_post_json", side_effect=cap):
                from emoney_mcp.scrapers.transactions import delete_transaction_rule
                result = await delete_transaction_rule(session, rule_id="does-not-exist")
        assert "error" in result
        assert called is False, "Must not POST SetRules when the rule isn't found"

    @pytest.mark.asyncio
    async def test_missing_rule_id_returns_error(self):
        session = make_mock_http_session()
        from emoney_mcp.scrapers.transactions import delete_transaction_rule
        result = await delete_transaction_rule(session, rule_id="")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_setrules_error_propagates(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.transactions.get_transaction_rules",
                   new=AsyncMock(return_value=_DELETE_RULES_RESPONSE)):
            with patch("emoney_mcp.scrapers.transactions._csrf_post_json",
                       new=AsyncMock(return_value={"error": "SetRules returned HTTP 500"})):
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
        with _make_snb_post_mock({"ok": True, "data": {"ruleID": "77"}}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(session, description_contains="X", category_id="1")
        assert "raw" not in result
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_add_rule_includes_raw_in_dev(self, monkeypatch):
        monkeypatch.setenv("EMONEY_DEV", "1")
        session = make_mock_http_session()
        with _make_snb_post_mock({"ok": True, "data": {"ruleID": "77"}}):
            from emoney_mcp.scrapers.transactions import add_transaction_rule
            result = await add_transaction_rule(session, description_contains="X", category_id="1")
        assert result.get("raw") == {"ruleID": "77"}

    @pytest.mark.asyncio
    async def test_apply_rule_omits_raw_by_default(self, monkeypatch):
        monkeypatch.delenv("EMONEY_DEV", raising=False)
        session = make_mock_http_session()
        with _make_csrf_mock({"Success": True, "Updated": 7}):
            from emoney_mcp.scrapers.transactions import apply_transaction_rule
            result = await apply_transaction_rule(session, rule_id="10")
        assert "raw" not in result
        assert result["success"] is True
