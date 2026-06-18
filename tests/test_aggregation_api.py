"""
Tests for refresh_account_aggregation (#103) — aggapi refresh control.

The tool resolves aggapi credentials (Bearer token + aggApiKey + user GUID),
GETs the connections list, then POSTs a refresh per target connection. Tests
patch the credential helper and mock the GET/POST. No live network.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.scrapers import aggregation_api

_CONNS = [
    {"id": 111, "name": "Ally", "status": "UpToDate", "statusLevel": "Healthy",
     "statusDescription": "", "lastSuccessfulUpdate": "2026-06-18"},
    {"id": 222, "name": "Truist", "status": "Error", "statusLevel": "UnhealthyActionable",
     "statusDescription": "Disconnected", "lastSuccessfulUpdate": "2026-04-25"},
    {"id": 333, "name": "Fidelity - via Direct Web API", "status": "UpToDate",
     "statusLevel": "Healthy", "statusDescription": "", "lastSuccessfulUpdate": "2026-06-18"},
]


def _resp(body, status=200, ctype="application/json; charset=utf-8"):
    m = MagicMock()
    m.status_code = status
    m.headers = {"content-type": ctype}
    m.json.return_value = body
    m.text = "{}"
    return m


def _make_session(conns=_CONNS, conns_status=200, refresh_status=202, post_recorder=None):
    async def mock_get(url, **kwargs):
        if "/connections" in url:
            return _resp(conns, status=conns_status,
                         ctype="application/json" if conns_status == 200 else "text/html")
        return _resp(None, 404, "text/html")

    async def mock_post(url, **kwargs):
        if post_recorder is not None:
            post_recorder.append(url)
        return _resp({"activityId": "act-" + url.rstrip("/").split("/")[-2]},
                     status=refresh_status)

    http = AsyncMock()
    http.get = mock_get
    http.post = mock_post
    session = AsyncMock()
    session.get_http = AsyncMock(return_value=http)
    return session


def _patch_creds(ok=True):
    if ok:
        return patch.object(aggregation_api, "_get_agg_credentials",
                            AsyncMock(return_value=("tok", "aggkey", "user-guid", None)))
    return patch.object(aggregation_api, "_get_agg_credentials",
                        AsyncMock(return_value=(None, None, None, {"error": "Session expired"})))


class TestRefreshAccountAggregation:
    @pytest.mark.asyncio
    async def test_refresh_all(self):
        posts = []
        session = _make_session(post_recorder=posts)
        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session)
        assert r["scope"] == "all connections"
        assert r["queued_count"] == 3
        assert r["failed_count"] == 0
        assert len(posts) == 3
        assert all(p.endswith("/refresh") for p in posts)
        assert r["refreshed"][0]["activity_id"]

    @pytest.mark.asyncio
    async def test_filter_by_institution(self):
        posts = []
        session = _make_session(post_recorder=posts)
        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session, institution="truist")
        assert "truist" in r["scope"]
        assert r["queued_count"] == 1
        assert len(posts) == 1
        assert "/connections/222/refresh" in posts[0]

    @pytest.mark.asyncio
    async def test_filter_by_connection_id(self):
        posts = []
        session = _make_session(post_recorder=posts)
        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session, connection_id="333")
        assert r["queued_count"] == 1
        assert "/connections/333/refresh" in posts[0]

    @pytest.mark.asyncio
    async def test_unknown_institution_errors(self):
        session = _make_session()
        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session, institution="nonexistent")
        assert "error" in r

    @pytest.mark.asyncio
    async def test_unknown_connection_id_errors(self):
        session = _make_session()
        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session, connection_id="999")
        assert "error" in r

    @pytest.mark.asyncio
    async def test_credential_error_propagates(self):
        session = _make_session()
        with _patch_creds(ok=False):
            r = await aggregation_api.refresh_account_aggregation(session)
        assert r.get("error") == "Session expired"

    @pytest.mark.asyncio
    async def test_connections_http_error(self):
        session = _make_session(conns_status=500)
        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session)
        assert "error" in r

    @pytest.mark.asyncio
    async def test_partial_refresh_failure_recorded(self):
        """A non-2xx on one connection lands in failures, not refreshed."""
        async def mock_get(url, **kwargs):
            return _resp(_CONNS, 200)

        async def mock_post(url, **kwargs):
            # connection 222 fails, others succeed
            if "/222/" in url:
                return _resp({"message": "bad"}, status=409)
            return _resp({"activityId": "ok"}, status=202)

        http = AsyncMock()
        http.get = mock_get
        http.post = mock_post
        session = AsyncMock()
        session.get_http = AsyncMock(return_value=http)

        with _patch_creds():
            r = await aggregation_api.refresh_account_aggregation(session)
        assert r["queued_count"] == 2
        assert r["failed_count"] == 1
        assert r["failures"][0]["connection_id"] == 222


class TestJwtUserGuid:
    def test_extracts_user_id_claim(self):
        import base64, json
        payload = base64.urlsafe_b64encode(
            json.dumps({"userId": "abc-123"}).encode()).decode().rstrip("=")
        token = f"header.{payload}.sig"
        assert aggregation_api._jwt_user_guid(token) == "abc-123"

    def test_malformed_token_returns_none(self):
        assert aggregation_api._jwt_user_guid("not-a-jwt") is None
