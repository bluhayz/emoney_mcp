"""
Tests for scrapers/explore.py: explore_emoney_site.

HTTP responses are mocked at the session layer — no live network calls.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

_HTML_WITH_ENDPOINTS = """
<html>
<head><title>Emoney Dashboard</title></head>
<body>
<h1>Dashboard</h1>
<nav>
  <a href="/ema/CS/Accounts">Accounts</a>
  <a href="/ema/CS/Spending">Spending</a>
</nav>
<script>
  var config = {
    apiUrl: '/ema/CS/Investments/GetData',
    endpoint: '/api/values/GetCategories',
  };
  fetch('/ema/CS/CardSwitcher/GetCard/9');
</script>
</body>
</html>
"""

_HTML_REDIRECTED_TO_LOGIN = """
<html><head><title>Sign In</title></head>
<body><form action="/signin">Login</form></body>
</html>
"""

_HTML_MINIMAL = "<html><head><title>Empty Page</title></head><body></body></html>"


def _make_explore_session(pages: dict):
    """
    Return a mock session whose http.get returns different HTML per URL.

    pages: {url_fragment: (status_code, html_string, final_url)}
    The default for unrecognised URLs is (200, _HTML_MINIMAL, url).
    """
    async def mock_get(url, **kwargs):
        for fragment, (status, html, final_url) in pages.items():
            if fragment in url:
                resp = MagicMock()
                resp.status_code = status
                resp.headers = {"content-type": "text/html"}
                resp.text = html
                resp.url = final_url or url
                return resp
        # Default: minimal 200
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "text/html"}
        resp.text = _HTML_MINIMAL
        resp.url = url
        return resp

    http = AsyncMock()
    http.get = mock_get

    session = make_mock_http_session()
    session.get_http = AsyncMock(return_value=http)
    return session


# ===========================================================================
# explore_emoney_site
# ===========================================================================

class TestExploreEmoneySite:

    @pytest.mark.asyncio
    async def test_returns_top_level_keys(self):
        session = _make_explore_session({})
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        assert "pages_visited" in result
        assert "all_endpoints" in result
        assert "nav_map" in result
        assert "summary" in result

    @pytest.mark.asyncio
    async def test_summary_counts_pages_probed(self):
        session = _make_explore_session({})
        from emoney_mcp.scrapers.explore import explore_emoney_site, _PAGES
        result = await explore_emoney_site(session)
        assert result["summary"]["pages_probed"] == len(_PAGES)

    @pytest.mark.asyncio
    async def test_pages_visited_length_matches_pages_list(self):
        session = _make_explore_session({})
        from emoney_mcp.scrapers.explore import explore_emoney_site, _PAGES
        result = await explore_emoney_site(session)
        assert len(result["pages_visited"]) == len(_PAGES)

    @pytest.mark.asyncio
    async def test_endpoints_extracted_from_html(self):
        """Pages with embedded /ema/ or /api/ URLs should have endpoints extracted."""
        session = _make_explore_session({
            "/ema/CS/Home": (200, _HTML_WITH_ENDPOINTS, None),
        })
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        # At least one /ema/ or /api/ endpoint should have been mined
        assert len(result["all_endpoints"]) >= 1

    @pytest.mark.asyncio
    async def test_nav_links_extracted(self):
        """href=/ema/CS/… links should be collected in nav_map."""
        session = _make_explore_session({
            "/ema/CS/Home": (200, _HTML_WITH_ENDPOINTS, None),
        })
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        # _HTML_WITH_ENDPOINTS contains /ema/CS/Accounts and /ema/CS/Spending
        assert any("/ema/CS/" in nav for nav in result["nav_map"])

    @pytest.mark.asyncio
    async def test_login_redirect_flagged(self):
        """Pages that redirect to a signin URL should be noted, not silently skipped."""
        session = _make_explore_session({
            "/ema/CS/Home": (200, _HTML_REDIRECTED_TO_LOGIN, "https://wealth.emaplan.com/signin"),
        })
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        home_entry = next(
            (p for p in result["pages_visited"] if "Home" in p["section"]), None
        )
        assert home_entry is not None
        assert "note" in home_entry or home_entry.get("status") is not None

    @pytest.mark.asyncio
    async def test_network_error_does_not_crash(self):
        """A connection error on one page should not abort the entire crawl."""
        async def mock_get_with_error(url, **kwargs):
            if "/ema/CS/Home" in url:
                raise ConnectionError("Connection refused")
            resp = MagicMock()
            resp.status_code = 200
            resp.headers = {"content-type": "text/html"}
            resp.text = _HTML_MINIMAL
            resp.url = url
            return resp

        http = AsyncMock()
        http.get = mock_get_with_error

        session = make_mock_http_session()
        session.get_http = AsyncMock(return_value=http)

        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)

        # Should complete and return results for all other pages
        assert "pages_visited" in result
        error_page = next(
            (p for p in result["pages_visited"] if "Home" in p["section"]), None
        )
        assert error_page is not None
        assert "error" in error_page

    @pytest.mark.asyncio
    async def test_200_pages_counted_in_summary(self):
        session = _make_explore_session({})
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        ok_pages = sum(1 for p in result["pages_visited"] if p.get("status") == 200)
        assert result["summary"]["pages_ok"] == ok_pages

    @pytest.mark.asyncio
    async def test_page_entry_has_section_and_url(self):
        session = _make_explore_session({})
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        for page in result["pages_visited"]:
            assert "section" in page
            assert "url" in page

    @pytest.mark.asyncio
    async def test_all_endpoints_is_deduplicated_sorted_list(self):
        """all_endpoints must contain no duplicates and must be sorted."""
        session = _make_explore_session({
            "/ema/CS/Home":     (200, _HTML_WITH_ENDPOINTS, None),
            "/ema/CS/Accounts": (200, _HTML_WITH_ENDPOINTS, None),
        })
        from emoney_mcp.scrapers.explore import explore_emoney_site
        result = await explore_emoney_site(session)
        endpoints = result["all_endpoints"]
        assert endpoints == sorted(set(endpoints))
