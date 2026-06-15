"""Tests for _helpers.py: _get_card TTL caching, error TTL (v0.7.3 fix), and _fmt_dollars."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _make_mock_http(data=None, status=200):
    """Build a mock curl_cffi http object that returns the given data."""
    http = AsyncMock()
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = {"Data": data or {"key": "value"}}
    http.get = AsyncMock(return_value=resp)
    return http


def _make_error_http(status=500):
    """Build a mock http that returns a non-200 error response."""
    http = AsyncMock()
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": "text/html"}
    http.get = AsyncMock(return_value=resp)
    return http


# ---------------------------------------------------------------------------
# _fmt_dollars
# ---------------------------------------------------------------------------

class TestFmtDollars:

    def test_none_returns_none(self):
        from emoney_mcp.scrapers._helpers import _fmt_dollars
        assert _fmt_dollars(None) is None

    def test_positive_value_formatted(self):
        from emoney_mcp.scrapers._helpers import _fmt_dollars
        assert _fmt_dollars(1234.5) == "$1,234.50"

    def test_zero_formatted(self):
        from emoney_mcp.scrapers._helpers import _fmt_dollars
        assert _fmt_dollars(0) == "$0.00"

    def test_large_value_has_commas(self):
        from emoney_mcp.scrapers._helpers import _fmt_dollars
        assert _fmt_dollars(1_000_000) == "$1,000,000.00"

    def test_negative_value_formatted(self):
        from emoney_mcp.scrapers._helpers import _fmt_dollars
        result = _fmt_dollars(-500.0)
        assert "500.00" in result

    def test_fractional_cents_rounded(self):
        from emoney_mcp.scrapers._helpers import _fmt_dollars
        result = _fmt_dollars(9.999)
        assert result == "$10.00"


# ---------------------------------------------------------------------------
# _get_card — success caching
# ---------------------------------------------------------------------------

class TestGetCardSuccessCaching:

    @pytest.mark.asyncio
    async def test_cache_miss_makes_http_request(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http(data={"CardId": 99})

        result = await _helpers._get_card(http, 99)
        assert result == {"CardId": 99}
        assert http.get.call_count == 1

    @pytest.mark.asyncio
    async def test_second_call_within_ttl_uses_cache(self):
        """Second call within TTL must not make a second HTTP request."""
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http(data={"cached": True})

        await _helpers._get_card(http, 42)
        await _helpers._get_card(http, 42)
        assert http.get.call_count == 1  # only one HTTP call total

    @pytest.mark.asyncio
    async def test_call_after_ttl_expiry_makes_new_request(self):
        """After TTL expires a fresh HTTP request must be made."""
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http(data={"fresh": True})

        with patch("time.time", return_value=1_000_000.0):
            await _helpers._get_card(http, 55)

        # Simulate time advancing past TTL
        with patch("time.time", return_value=1_000_000.0 + _helpers._CARD_CACHE_TTL + 1):
            await _helpers._get_card(http, 55)

        assert http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_different_card_ids_cached_independently(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http()

        await _helpers._get_card(http, 1)
        await _helpers._get_card(http, 2)
        assert http.get.call_count == 2

    @pytest.mark.asyncio
    async def test_clear_card_cache_forces_fresh_request(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http()

        await _helpers._get_card(http, 7)
        _helpers.clear_card_cache()
        await _helpers._get_card(http, 7)
        assert http.get.call_count == 2


# ---------------------------------------------------------------------------
# _get_card — error TTL (v0.7.3 fix)
# ---------------------------------------------------------------------------

class TestGetCardErrorTTL:
    """Verify that failed responses are cached for _CARD_ERROR_TTL (30s), not the full 5min TTL."""

    @pytest.mark.asyncio
    async def test_error_response_returns_none(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_error_http(status=500)

        result = await _helpers._get_card(http, 88)
        assert result is None

    @pytest.mark.asyncio
    async def test_error_cached_within_error_ttl(self):
        """A second call within the 30s error TTL should NOT make another HTTP request."""
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_error_http(status=404)

        t0 = 2_000_000.0
        with patch("time.time", return_value=t0):
            await _helpers._get_card(http, 77)

        # 10 seconds later — still within 30s error TTL
        with patch("time.time", return_value=t0 + 10):
            await _helpers._get_card(http, 77)

        assert http.get.call_count == 1  # cached — no second request

    @pytest.mark.asyncio
    async def test_error_cache_expires_after_error_ttl(self):
        """After 30s the error entry should expire and a new request should be made."""
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_error_http(status=503)

        t0 = 3_000_000.0
        with patch("time.time", return_value=t0):
            await _helpers._get_card(http, 66)

        # After error TTL, switch to a successful response
        success_resp = MagicMock()
        success_resp.status_code = 200
        success_resp.headers = {"content-type": "application/json"}
        success_resp.json.return_value = {"Data": {"recovered": True}}
        http.get.return_value = success_resp

        with patch("time.time", return_value=t0 + _helpers._CARD_ERROR_TTL + 1):
            result = await _helpers._get_card(http, 66)

        assert http.get.call_count == 2
        assert result == {"recovered": True}

    @pytest.mark.asyncio
    async def test_error_ttl_shorter_than_success_ttl(self):
        """Confirm the error TTL is significantly shorter than the success TTL."""
        from emoney_mcp.scrapers import _helpers
        assert _helpers._CARD_ERROR_TTL < _helpers._CARD_CACHE_TTL
        assert _helpers._CARD_ERROR_TTL == 30

    @pytest.mark.asyncio
    async def test_non_json_response_also_cached_as_error(self):
        """A 200 response with non-JSON content-type should also be treated as an error."""
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "text/html"}  # not JSON
        http = AsyncMock()
        http.get = AsyncMock(return_value=resp)

        t0 = 4_000_000.0
        with patch("time.time", return_value=t0):
            result = await _helpers._get_card(http, 33)

        assert result is None

        # Should still be cached within 30s
        with patch("time.time", return_value=t0 + 5):
            await _helpers._get_card(http, 33)

        assert http.get.call_count == 1


# ---------------------------------------------------------------------------
# _get_card — card_id coercion (request-path injection guard)
# ---------------------------------------------------------------------------

class TestGetCardIdCoercion:
    """card_id must be coerced to int so a crafted value can't inject path/query
    segments into the authenticated request (defense against malicious card_ids
    passed to explore_emoney_cards)."""

    @pytest.mark.asyncio
    async def test_path_injection_card_id_rejected_without_request(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http(data={"should": "not be fetched"})

        result = await _helpers._get_card(http, "8/../SignOut")
        assert result is None
        assert http.get.call_count == 0  # no HTTP request was issued

    @pytest.mark.asyncio
    async def test_query_injection_card_id_rejected(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http()

        result = await _helpers._get_card(http, "8?foo=bar")
        assert result is None
        assert http.get.call_count == 0

    @pytest.mark.asyncio
    async def test_numeric_string_card_id_is_accepted(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http(data={"ok": True})

        result = await _helpers._get_card(http, "13")
        assert result == {"ok": True}
        # The URL must contain the clean integer path segment.
        called_url = http.get.call_args[0][0]
        assert "/GetCard/13?" in called_url

    @pytest.mark.asyncio
    async def test_none_card_id_returns_none(self):
        from emoney_mcp.scrapers import _helpers
        _helpers.clear_card_cache()
        http = _make_mock_http()

        result = await _helpers._get_card(http, None)
        assert result is None
        assert http.get.call_count == 0
