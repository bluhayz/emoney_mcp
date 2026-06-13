"""
Tests for scrapers/reports.py: get_reports and get_report_url.

HTML responses are mocked at the HTTP layer — no live network calls.
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# HTML fixtures
# ---------------------------------------------------------------------------

def _make_reports_html(families: list[dict]) -> str:
    """Build a minimal Reports page HTML with embedded JSON families."""
    blobs = []
    for family in families:
        reports_json = json.dumps(family["reports"])
        blobs.append(f'"Name": "{family["name"]}", "Reports": {reports_json}')
    inner = ", ".join(f"{{{b}}}" for b in blobs)
    return f"<html><body><script>var config = [{inner}];</script></body></html>"


_FAMILY_INVESTMENTS = {
    "name": "Investments",
    "reports": [
        {"ReportID": "LiquidityReport",    "Name": "Liquidity Report",    "ShortName": "Liquidity",    "Description": "Shows liquid assets."},
        {"ReportID": "AssetTaxTypeReport", "Name": "Asset Tax Type",      "ShortName": "Asset Tax",    "Description": "Assets by tax treatment."},
    ],
}

_FAMILY_NET_WORTH = {
    "name": "Net Worth",
    "reports": [
        {"ReportID": "NetWorthReport", "Name": "Net Worth Over Time", "ShortName": "Net Worth", "Description": "Historical net worth."},
    ],
}

_REPORTS_HTML = _make_reports_html([_FAMILY_INVESTMENTS, _FAMILY_NET_WORTH])


def _make_html_session(html: str, status: int = 200):
    """Return a mock session whose GET returns the given HTML string."""
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": "text/html"}
    resp.text = html

    http = AsyncMock()
    http.get = AsyncMock(return_value=resp)

    session = make_mock_http_session()
    session.get_http = AsyncMock(return_value=http)
    return session


def _make_report_url_session(response_data, status: int = 200):
    """Return a mock session whose POST returns JSON for GetReportUrl."""
    get_resp = MagicMock()
    get_resp.status_code = 404
    get_resp.headers = {"content-type": "text/html"}
    get_resp.text = ""

    post_resp = MagicMock()
    post_resp.status_code = status
    post_resp.headers = {"content-type": "application/json"}
    post_resp.json.return_value = response_data
    post_resp.text = json.dumps(response_data)

    http = AsyncMock()
    http.get = AsyncMock(return_value=get_resp)
    http.post = AsyncMock(return_value=post_resp)

    session = make_mock_http_session()
    session.get_http = AsyncMock(return_value=http)
    return session


# ===========================================================================
# get_reports
# ===========================================================================

class TestGetReports:

    @pytest.mark.asyncio
    async def test_families_parsed(self):
        session = _make_html_session(_REPORTS_HTML)
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        assert "families" in result
        assert result["total_reports"] >= 3
        family_names = [f["family"] for f in result["families"]]
        assert "Investments" in family_names
        assert "Net Worth" in family_names

    @pytest.mark.asyncio
    async def test_report_fields_present(self):
        session = _make_html_session(_REPORTS_HTML)
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        for family in result["families"]:
            for report in family["reports"]:
                assert "report_id" in report
                assert "name" in report
                assert "short_name" in report
                assert "description" in report

    @pytest.mark.asyncio
    async def test_known_report_ids_found(self):
        session = _make_html_session(_REPORTS_HTML)
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        all_ids = {r["report_id"] for f in result["families"] for r in f["reports"]}
        assert "LiquidityReport" in all_ids
        assert "AssetTaxTypeReport" in all_ids
        assert "NetWorthReport" in all_ids

    @pytest.mark.asyncio
    async def test_no_duplicate_report_ids_within_a_family(self):
        """Within a single family block, each report_id should appear only once."""
        session = _make_html_session(_REPORTS_HTML)
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        for family in result["families"]:
            ids_in_family = [r["report_id"] for r in family["reports"]]
            assert len(ids_in_family) == len(set(ids_in_family)), (
                f"Duplicate report IDs in family '{family['family']}': {ids_in_family}"
            )

    @pytest.mark.asyncio
    async def test_note_key_present(self):
        session = _make_html_session(_REPORTS_HTML)
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        assert "note" in result

    @pytest.mark.asyncio
    async def test_http_error_returns_error(self):
        session = _make_html_session("", status=403)
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        assert "error" in result
        assert "403" in result["error"]

    @pytest.mark.asyncio
    async def test_empty_page_returns_zero_reports(self):
        """A page with no embedded report JSON should return 0 reports, not crash."""
        session = _make_html_session("<html><body>No reports here.</body></html>")
        from emoney_mcp.scrapers.reports import get_reports
        result = await get_reports(session)
        assert "families" in result
        assert result["total_reports"] == 0


# ===========================================================================
# get_report_url
# ===========================================================================

class TestGetReportUrl:

    @pytest.mark.asyncio
    async def test_url_from_Url_key(self):
        session = _make_report_url_session({"Url": "https://wealth.emaplan.com/report/abc123"})
        from emoney_mcp.scrapers.reports import get_report_url
        result = await get_report_url(session, report_id="LiquidityReport")
        assert result["report_id"] == "LiquidityReport"
        assert result["url"] == "https://wealth.emaplan.com/report/abc123"

    @pytest.mark.asyncio
    async def test_url_from_lowercase_url_key(self):
        session = _make_report_url_session({"url": "https://wealth.emaplan.com/report/xyz"})
        from emoney_mcp.scrapers.reports import get_report_url
        result = await get_report_url(session, report_id="NetWorthReport")
        assert "url" in result

    @pytest.mark.asyncio
    async def test_raw_string_response(self):
        """API may return a plain JSON string (the URL itself)."""
        url_str = "https://wealth.emaplan.com/report/raw-url"
        session = _make_report_url_session(url_str)
        from emoney_mcp.scrapers.reports import get_report_url
        result = await get_report_url(session, report_id="AnyReport")
        # Should return a url key or a raw key, not crash
        assert "report_id" in result

    @pytest.mark.asyncio
    async def test_http_error_returns_error(self):
        session = _make_report_url_session({}, status=404)
        from emoney_mcp.scrapers.reports import get_report_url
        result = await get_report_url(session, report_id="BadReport")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_non_json_response_returns_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "text/html"}
        resp.text = "<html>Not JSON</html>"

        get_resp = MagicMock()
        get_resp.status_code = 404
        get_resp.headers = {"content-type": "text/html"}
        get_resp.text = ""

        http = AsyncMock()
        http.get = AsyncMock(return_value=get_resp)
        http.post = AsyncMock(return_value=resp)

        session = make_mock_http_session()
        session.get_http = AsyncMock(return_value=http)

        from emoney_mcp.scrapers.reports import get_report_url
        result = await get_report_url(session, report_id="SomeReport")
        assert "error" in result

    @pytest.mark.asyncio
    async def test_success_false_in_response_returns_error(self):
        session = _make_report_url_session({"Success": False, "Message": "Report not available"})
        from emoney_mcp.scrapers.reports import get_report_url
        result = await get_report_url(session, report_id="SomeReport")
        assert "error" in result
        assert "not available" in result["error"]
