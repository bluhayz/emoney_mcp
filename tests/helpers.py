"""Shared test helpers — importable from any test module."""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

# Ensure src/ is on the path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file by name (without .json extension)."""
    return json.loads((FIXTURES_DIR / f"{name}.json").read_text())


def make_mock_response(data: dict, status: int = 200, content_type: str = "application/json"):
    """Build a mock curl_cffi response object."""
    mock = MagicMock()
    mock.status_code = status
    mock.headers = {"content-type": content_type}
    mock.json.return_value = data
    mock.text = json.dumps(data)
    return mock


def make_mock_http_session(card_responses: dict = None, endpoint_responses: dict = None):
    """
    Build a mock EmoneyHttpSession whose get/post return fixture data.

    card_responses:    {card_id: fixture_name}     e.g. {8: "card8_history"}
    endpoint_responses:{url_fragment: fixture_name} e.g. {"GetInvestmentData": "investment_data"}
    """
    card_responses     = card_responses or {}
    endpoint_responses = endpoint_responses or {}

    http = AsyncMock()

    async def mock_get(url, **kwargs):
        for card_id, fixture_name in card_responses.items():
            if f"/GetCard/{card_id}" in url:
                return make_mock_response({"Data": load_fixture(fixture_name)})
        for fragment, fixture_name in endpoint_responses.items():
            if fragment in url:
                return make_mock_response(load_fixture(fixture_name))
        not_found = MagicMock()
        not_found.status_code = 404
        not_found.headers = {"content-type": "text/html"}
        return not_found

    async def mock_post(url, **kwargs):
        for fragment, fixture_name in endpoint_responses.items():
            if fragment in url:
                return make_mock_response(load_fixture(fixture_name))
        not_found = MagicMock()
        not_found.status_code = 404
        not_found.headers = {"content-type": "text/html"}
        return not_found

    http.get  = mock_get
    http.post = mock_post

    session = AsyncMock()
    session.get_http          = AsyncMock(return_value=http)
    session.get_csrf_token    = AsyncMock(return_value="test-csrf-token-abc123")
    session._csrf_token       = "test-csrf-token-abc123"
    return session
