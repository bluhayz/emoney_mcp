"""
Characterization + drift tests for the server tool-dispatch layer.

These were written against the pre-registry if/elif dispatch to lock in its
exact behavior (which scraper function each tool calls, and with which
converted kwargs), then kept as the contract for the registry refactor (#23).

No network: the scraper module and session getter are mocked.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import emoney_mcp.server as server  # noqa: E402


# Required args per tool, so the route-all guard doesn't KeyError.
MINIMAL_ARGS = {
    "get_roth_conversion_analysis": {"conversion_amount": "100", "current_income": "200"},
    "get_rmd_estimate": {"birth_year": "1980"},
    "get_social_security_optimizer": {"birth_year": "1980"},
    "update_transaction": {"transaction_id": "t1", "category_id": "5"},
    "hide_transaction": {"transaction_id": "t1"},
    "get_transaction_splits": {"transaction_id": "t1"},
    "update_transaction_splits": {"transaction_splits": []},
    "add_transaction_rule": {"description_contains": "X", "category_id": "5"},
    "update_transaction_rule": {"rule_id": "9"},
    "apply_transaction_rule": {"rule_id": "9"},
    "get_report_url": {"report_id": "LiquidityReport"},
}

# Tools with bespoke handlers (no scraper passthrough / side effects) — exercised
# separately, excluded from the route-all scraper-mock guard.
SPECIAL = {"get_features", "get_version", "sync_chrome_session", "reset_session"}


class _FakeScraper:
    """Every attribute is an AsyncMock returning {} — except the synchronous
    cache helpers, which must stay regular callables."""
    _SYNC = {"clear_cache", "clear_caches"}

    def __getattr__(self, name):
        m = MagicMock(return_value={}) if name in self._SYNC else AsyncMock(return_value={})
        object.__setattr__(self, name, m)
        return m


async def _all_tool_names():
    return [t.name for t in await server.list_tools()]


# ---------------------------------------------------------------------------
# Drift guard: every advertised tool must be dispatchable (no "Unknown tool").
# ---------------------------------------------------------------------------

class TestEveryToolRoutes:

    @pytest.mark.asyncio
    async def test_listed_tools_match_registry_keys(self):
        """list_tools() and the _DISPATCH registry must be exactly in sync —
        no advertised tool without a handler, no orphan handler."""
        names = set(await _all_tool_names())
        registry = set(server._DISPATCH)
        assert names == registry, (
            f"advertised-but-unhandled: {names - registry}; "
            f"handler-but-unadvertised: {registry - names}"
        )

    @pytest.mark.asyncio
    async def test_all_tools_dispatch_without_unknown(self):
        names = await _all_tool_names()
        assert len(names) >= 80

        fake = _FakeScraper()
        with patch.object(server, "scraper", fake), \
             patch.object(server, "_get_session_or_err", AsyncMock(return_value=("SESS", None))):
            for name in names:
                if name in SPECIAL:
                    continue
                args = MINIMAL_ARGS.get(name, {})
                out = await server._call_tool_inner(name, args)
                assert isinstance(out, list) and out and out[0].type == "text"
                payload = json.loads(out[0].text)
                # A routing miss surfaces as {"error": "Unknown tool: ..."} only via
                # call_tool; _call_tool_inner raises ValueError — assert it didn't.
                assert "Unknown tool" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_unknown_tool_raises(self):
        with patch.object(server, "_get_session_or_err", AsyncMock(return_value=("SESS", None))):
            with pytest.raises(ValueError):
                await server._call_tool_inner("does_not_exist", {})


# ---------------------------------------------------------------------------
# Argument conversion contract — one case per distinct pattern.
# ---------------------------------------------------------------------------

# (tool, args, scraper_attr, expected_kwargs)
ARG_CASES = [
    # no-arg passthrough
    ("get_accounts", {}, "get_accounts", {}),
    ("get_holdings", {}, "get_holdings", {}),
    # int with default
    ("get_net_worth_history", {}, "get_net_worth_history", {"months": 12}),
    ("get_net_worth_history", {"months": "6"}, "get_net_worth_history", {"months": 6}),
    # two ints + optional str passthrough
    ("get_transactions", {"days": "7", "account_id": "abc"},
     "get_transactions", {"days": 7, "account_id": "abc"}),
    ("get_transactions", {}, "get_transactions", {"days": 30, "account_id": None}),
    # optional int (absent -> None, present -> int)
    ("get_capital_gains", {}, "get_capital_gains", {"year": None}),
    ("get_capital_gains", {"year": "2025"}, "get_capital_gains", {"year": 2025}),
    # required floats + default str + optional int
    ("get_roth_conversion_analysis",
     {"conversion_amount": "100", "current_income": "200"},
     "get_roth_conversion_analysis",
     {"conversion_amount": 100.0, "current_income": 200.0, "filing_status": "mfj", "age": None}),
    # bool default True
    ("hide_transaction", {"transaction_id": "t1"},
     "hide_transaction", {"transaction_id": "t1", "hidden": True}),
    # required str-coerced category_id + optional floats omitted -> None
    ("add_transaction_rule", {"description_contains": "X", "category_id": 5},
     "add_transaction_rule",
     {"description_contains": "X", "category_id": "5", "user_description": None,
      "transaction_id": None, "min_amount": None, "max_amount": None}),
    # list-of-int conversion, present
    ("get_available_cards", {"card_ids": ["5", "6"]},
     "get_available_cards", {"card_ids": [5, 6]}),
    # list conversion, absent -> None
    ("explore_emoney_cards", {}, "explore_emoney_cards", {"card_ids": None}),
    # mixed: default-0 float, optional float, default ints/strs
    ("search_transactions", {"max_amount": "50"},
     "search_transactions",
     {"query": "", "category": "", "days": 365, "min_amount": 0.0,
      "max_amount": 50.0, "max_results": 100}),
    # float with default
    ("get_tax_drag_analysis", {}, "get_tax_drag_analysis",
     {"marginal_rate": 0.32, "ltcg_rate": 0.15}),
    ("get_fire_number", {"swr": "0.05"}, "get_fire_number",
     {"swr": 0.05, "annual_return": 0.07}),
]


class TestArgConversion:

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool,args,attr,expected", ARG_CASES)
    async def test_arg_conversion(self, tool, args, attr, expected):
        mock = AsyncMock(return_value={"ok": True})
        with patch.object(server, "_get_session_or_err", AsyncMock(return_value=("SESS", None))), \
             patch.object(server.scraper, attr, mock):
            await server._call_tool_inner(tool, args)
        assert mock.await_args == call("SESS", **expected)


# ---------------------------------------------------------------------------
# Special handlers.
# ---------------------------------------------------------------------------

class TestSpecialHandlers:

    @pytest.mark.asyncio
    async def test_get_net_worth_derives_subset(self):
        accts = {"net_worth": 100, "total_assets": 150, "total_liabilities": -50, "extra": "x"}
        with patch.object(server, "_get_session_or_err", AsyncMock(return_value=("SESS", None))), \
             patch.object(server.scraper, "get_accounts", AsyncMock(return_value=accts)):
            out = await server._call_tool_inner("get_net_worth", {})
        payload = json.loads(out[0].text)
        assert payload == {"net_worth": 100, "total_assets": 150, "total_liabilities": -50}

    @pytest.mark.asyncio
    async def test_get_net_worth_propagates_error(self):
        with patch.object(server, "_get_session_or_err", AsyncMock(return_value=("SESS", None))), \
             patch.object(server.scraper, "get_accounts", AsyncMock(return_value={"error": "boom"})):
            out = await server._call_tool_inner("get_net_worth", {})
        assert json.loads(out[0].text) == {"error": "boom"}

    @pytest.mark.asyncio
    async def test_clear_cache_calls_scraper(self):
        m = MagicMock(return_value={"cleared": ["card_cache"]})
        with patch.object(server.scraper, "clear_cache", m):
            out = await server._call_tool_inner("clear_cache", {"module": "cards"})
        m.assert_called_once_with(module="cards")
        assert "cleared" in json.loads(out[0].text)

    @pytest.mark.asyncio
    async def test_get_version_returns_version(self):
        out = await server._call_tool_inner("get_version", {})
        payload = json.loads(out[0].text)
        assert "version" in payload

    @pytest.mark.asyncio
    async def test_get_features_returns_tool_listing(self):
        out = await server._call_tool_inner("get_features", {})
        payload = json.loads(out[0].text)
        assert isinstance(payload, dict) and payload  # non-empty


# ---------------------------------------------------------------------------
# Signature-drift guard (#67): every _A(...) arg on a _passthru handler must
# name a real parameter on its scraper function. The route-all test above uses
# a permissive mock that swallows any kwarg, so it can't catch this — this test
# checks against the *real* scraper signatures via inspect.
# ---------------------------------------------------------------------------

class TestDispatchSignatureParity:

    def test_passthru_arg_names_exist_on_scraper(self):
        import inspect
        from emoney_mcp import scraper

        for tool_name, handler in server._DISPATCH.items():
            fn_name = getattr(handler, "_scraper_fn", None)
            specs   = getattr(handler, "_specs", None)
            if fn_name is None or specs is None:
                continue  # bespoke lambda handler, not a _passthru

            fn = getattr(scraper, fn_name)
            params = inspect.signature(fn).parameters
            accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in params.values())

            for spec in specs:
                arg_name = spec[0]
                assert arg_name in params or accepts_kwargs, (
                    f"{tool_name}: _A({arg_name!r}) does not match any parameter of "
                    f"scraper.{fn_name}{tuple(params)}"
                )
