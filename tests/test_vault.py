"""
Tests for get_vault_documents (#104) — the first live data-read tool.

The tool makes two GETs: the Vault HTML page (to scrape the per-client API base
URL) and the JSON items endpoint. A small custom mock returns HTML for the page
and JSON for the items call. No live network.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.scrapers.vault import get_vault_documents

_GUID = "17f7a14d-fc83-4ce5-bcc7-ece129485f54"
_BASE = f"/ema/api/v1/vault/{_GUID}"

_VAULT_HTML = (
    '<html><body><script>vaultApi = {"BaseUrl":"' + _BASE + '","HideEmptyFolders":true};'
    '</script></body></html>'
)

_ITEMS_JSON = {
    "metadata": {"name": "Vault", "type": "folder", "fileCount": 26,
                 "sizeInBytes": 16_031_925, "createdDate": "2026-02-23T12:30:51-05:00",
                 "isShared": True},
    "children": [
        {"name": "Shared Documents", "type": "folder", "fileCount": 18,
         "sizeInBytes": 13_847_737, "createdDate": "2026-02-23T12:30:51-05:00",
         "isShared": True, "isClientsPrivateFolder": False},
        {"name": "Investments", "type": "folder", "fileCount": 8,
         "sizeInBytes": 2_184_188, "createdDate": "2026-02-23T12:30:51-05:00",
         "isShared": True, "isClientsPrivateFolder": False},
        {"name": "My Private Documents", "type": "folder", "fileCount": 0,
         "sizeInBytes": 0, "createdDate": "2026-02-23T12:30:51-05:00",
         "isShared": False, "isClientsPrivateFolder": True},
        {"name": "Welcome.pdf", "type": "file", "sizeInBytes": 1000,
         "createdDate": "2026-02-23T12:30:51-05:00", "isShared": True},
    ],
}


def _resp(*, status=200, text="", json_body=None, ctype="text/html", url="https://wealth.emaplan.com/ema/CS/Vault"):
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": ctype}
    m.text = text
    m.url = url
    m.json.return_value = json_body
    return m


def _make_session(*, page_html=_VAULT_HTML, page_status=200, page_url=None,
                  items_json=_ITEMS_JSON, items_status=200, items_ctype="application/json; charset=utf-8"):
    async def mock_get(url, **kwargs):
        if "/items" in url:
            return _resp(status=items_status, json_body=items_json, ctype=items_ctype, url=url)
        # Vault HTML page
        return _resp(status=page_status, text=page_html, ctype="text/html",
                     url=page_url or "https://wealth.emaplan.com/ema/CS/Vault")
    http = AsyncMock()
    http.get = mock_get
    session = AsyncMock()
    session.get_http = AsyncMock(return_value=http)
    return session


class TestGetVaultDocuments:

    @pytest.mark.asyncio
    async def test_parses_folders_and_totals(self):
        r = await get_vault_documents(_make_session())
        assert r["total_files"] == 26
        assert r["total_size_mb"] == pytest.approx(15.29, abs=0.01)
        assert r["folder_count"] == 3                       # files excluded
        # sorted by file_count desc
        assert [f["name"] for f in r["folders"]] == [
            "Shared Documents", "Investments", "My Private Documents"]
        top = r["folders"][0]
        assert top["file_count"] == 18
        assert top["size_mb"] == pytest.approx(13.21, abs=0.01)
        assert top["is_shared"] is True

    @pytest.mark.asyncio
    async def test_files_split_from_folders(self):
        r = await get_vault_documents(_make_session())
        assert len(r["root_files"]) == 1
        assert r["root_files"][0]["name"] == "Welcome.pdf"

    @pytest.mark.asyncio
    async def test_private_folder_flagged(self):
        r = await get_vault_documents(_make_session())
        priv = next(f for f in r["folders"] if f["name"] == "My Private Documents")
        assert priv["is_private"] is True

    @pytest.mark.asyncio
    async def test_session_expired_redirect(self):
        s = _make_session(page_url="https://wealth.emaplan.com/ema/SignIn?ema")
        r = await get_vault_documents(s)
        assert "error" in r and "expired" in r["error"].lower()

    @pytest.mark.asyncio
    async def test_base_url_not_found(self):
        s = _make_session(page_html="<html><body>no config here</body></html>")
        r = await get_vault_documents(s)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_items_non_json_errors(self):
        s = _make_session(items_status=500, items_ctype="text/html")
        r = await get_vault_documents(s)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_page_http_error(self):
        s = _make_session(page_status=503)
        r = await get_vault_documents(s)
        assert "error" in r
