"""
Tests for the v1.3 data-expansion batch (#176, #178, #179).

get_vault_folder  — vault folder drill-down
get_plan_assumptions, get_plan_expenses  — plan BFF data (#178)
get_official_plan_projection             — official Monte Carlo (#179)
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_vault_page_session(base_path="/ema/api/v1/vault/abc123"):
    """Return a mock http_session whose Vault page response embeds the base URL."""
    page_html = f'someJs.config={{"BaseUrl":"{base_path}","other":"x"}}'
    page_resp = MagicMock()
    page_resp.status_code = 200
    page_resp.url = "https://wealth.emaplan.com/ema/CS/Vault"
    page_resp.text = page_html

    session = AsyncMock()
    http = AsyncMock()
    session.get_http.return_value = http
    http.get.return_value = page_resp
    return session, http, base_path


# ---------------------------------------------------------------------------
# #176 — get_vault_folder
# ---------------------------------------------------------------------------

class TestGetVaultFolder:

    @pytest.mark.asyncio
    async def test_prefix_added_automatically(self):
        """'Tax Documents' should be passed as 'Vault/Tax Documents' to the API."""
        from emoney_mcp.scrapers.vault import get_vault_folder

        folder_resp = MagicMock()
        folder_resp.status_code = 200
        folder_resp.headers = {"content-type": "application/json"}
        folder_resp.json.return_value = {
            "metadata": {"name": "Tax Documents", "fileCount": 2, "sizeInBytes": 204800},
            "children": [
                {"name": "2024 Return.pdf", "type": "file", "sizeInBytes": 102400,
                 "createdDate": "2025-04-01", "isShared": True, "isClientsPrivateFolder": False},
                {"name": "2023 Return.pdf", "type": "file", "sizeInBytes": 102400,
                 "createdDate": "2024-04-01", "isShared": True, "isClientsPrivateFolder": False},
            ],
        }

        session, http, base = _mock_vault_page_session()
        # First call is the Vault page; second is the items endpoint
        http.get.side_effect = [
            MagicMock(status_code=200, url="https://wealth.emaplan.com/ema/CS/Vault",
                      text=f'"BaseUrl":"{base}"'),
            folder_resp,
        ]

        result = await get_vault_folder(session, "Tax Documents")

        assert "error" not in result
        assert result["path"] == "Vault/Tax Documents"
        assert len(result["files"]) == 2
        # Files sorted newest-first
        assert result["files"][0]["name"] == "2024 Return.pdf"

    @pytest.mark.asyncio
    async def test_vault_prefix_not_doubled(self):
        """Passing 'Vault/Tax Documents' should not become 'Vault/Vault/Tax Documents'."""
        from emoney_mcp.scrapers.vault import get_vault_folder

        folder_resp = MagicMock()
        folder_resp.status_code = 200
        folder_resp.headers = {"content-type": "application/json"}
        folder_resp.json.return_value = {
            "metadata": {"name": "Tax Documents", "fileCount": 0, "sizeInBytes": 0},
            "children": [],
        }

        session, http, base = _mock_vault_page_session()
        http.get.side_effect = [
            MagicMock(status_code=200, url="https://wealth.emaplan.com/ema/CS/Vault",
                      text=f'"BaseUrl":"{base}"'),
            folder_resp,
        ]

        result = await get_vault_folder(session, "Vault/Tax Documents")
        assert result["path"] == "Vault/Tax Documents"

    @pytest.mark.asyncio
    async def test_sub_folders_sorted_alphabetically(self):
        from emoney_mcp.scrapers.vault import get_vault_folder

        folder_resp = MagicMock()
        folder_resp.status_code = 200
        folder_resp.headers = {"content-type": "application/json"}
        folder_resp.json.return_value = {
            "metadata": {"name": "Documents", "fileCount": 0, "sizeInBytes": 0},
            "children": [
                {"name": "Z-folder", "type": "folder", "fileCount": 1},
                {"name": "A-folder", "type": "folder", "fileCount": 2},
            ],
        }

        session, http, base = _mock_vault_page_session()
        http.get.side_effect = [
            MagicMock(status_code=200, url="https://wealth.emaplan.com/ema/CS/Vault",
                      text=f'"BaseUrl":"{base}"'),
            folder_resp,
        ]

        result = await get_vault_folder(session, "Documents")
        sub = result["sub_folders"]
        assert sub[0]["name"] == "A-folder"
        assert sub[1]["name"] == "Z-folder"

    @pytest.mark.asyncio
    async def test_session_expired_propagates_error(self):
        from emoney_mcp.scrapers.vault import get_vault_folder

        session = AsyncMock()
        http = AsyncMock()
        session.get_http.return_value = http
        resp = MagicMock()
        resp.status_code = 200
        resp.url = "https://wealth.emaplan.com/ema/SignIn"
        resp.text = "login page"
        http.get.return_value = resp

        result = await get_vault_folder(session, "Tax Documents")
        assert "error" in result
        assert "expired" in result["error"].lower()


# ---------------------------------------------------------------------------
# #178 — get_plan_assumptions
# ---------------------------------------------------------------------------

_PLAN_HTML = 'var cfg = {"clientId":"client-1","planId":"plan-1"};'


class TestGetPlanAssumptions:

    @pytest.mark.asyncio
    async def test_returns_inflation_and_returns(self):
        from emoney_mcp.scrapers.plan_api import get_plan_assumptions

        assumptions_data = {
            "inflationRate": 0.025,
            "equityReturn":  0.07,
            "bondReturn":    0.04,
            "retirementAge": 65,
            "lifeExpectancy": 90,
        }

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("client-1", "plan-1", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt-token", "api-key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(return_value=(assumptions_data, None))):
            result = await get_plan_assumptions(session)

        assert "error" not in result
        assert result["inflation_rate_pct"] == pytest.approx(2.5)
        assert result["equity_return_pct"] == pytest.approx(7.0)
        assert result["bond_return_pct"] == pytest.approx(4.0)
        assert result["retirement_age"] == 65
        assert result["life_expectancy"] == 90

    @pytest.mark.asyncio
    async def test_session_error_propagates(self):
        from emoney_mcp.scrapers.plan_api import get_plan_assumptions

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=(None, None, {"error": "Session expired"}))):
            result = await get_plan_assumptions(session)

        assert "error" in result

    @pytest.mark.asyncio
    async def test_extra_fields_in_additional_assumptions(self):
        from emoney_mcp.scrapers.plan_api import get_plan_assumptions

        assumptions_data = {
            "inflationRate": 0.03,
            "customField1":  "someValue",
            "unknownParam":  42,
        }

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("c", "p", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt", "key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(return_value=(assumptions_data, None))):
            result = await get_plan_assumptions(session)

        assert "additional_assumptions" in result
        assert "customField1" in result["additional_assumptions"]


# ---------------------------------------------------------------------------
# #178 — get_plan_expenses
# ---------------------------------------------------------------------------

class TestGetPlanExpenses:

    @pytest.mark.asyncio
    async def test_returns_expenses_and_education(self):
        from emoney_mcp.scrapers.plan_api import get_plan_expenses

        expenses_data  = [{"name": "Living Expenses", "annualAmount": 80000, "startYear": 2026}]
        edu_data       = [{"name": "College Fund", "annualAmount": 30000, "startYear": 2028, "endYear": 2032}]
        spending_data  = []

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("c", "p", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt", "key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(side_effect=[
                       (expenses_data, None),
                       (edu_data, None),
                       (spending_data, None),
                   ])):
            result = await get_plan_expenses(session)

        assert "error" not in result
        assert len(result["expenses"]) == 1
        assert result["expenses"][0]["name"] == "Living Expenses"
        assert len(result["education_goals"]) == 1
        assert result["education_goals"][0]["name"] == "College Fund"

    @pytest.mark.asyncio
    async def test_all_endpoints_unavailable_returns_error(self):
        from emoney_mcp.scrapers.plan_api import get_plan_expenses

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("c", "p", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt", "key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(side_effect=[
                       (None, "HTTP 404"),
                       (None, "HTTP 404"),
                       (None, "HTTP 404"),
                   ])):
            result = await get_plan_expenses(session)

        assert "error" in result


# ---------------------------------------------------------------------------
# #179 — get_official_plan_projection
# ---------------------------------------------------------------------------

class TestGetOfficialPlanProjection:

    @pytest.mark.asyncio
    async def test_returns_probability_and_asset_spread(self):
        from emoney_mcp.scrapers.plan_api import get_official_plan_projection

        pos_data    = {"probabilityOfSuccess": 0.87}
        spread_data = {
            "years": [
                {"year": 2030, "p10": 500_000, "p50": 900_000, "p90": 1_300_000},
                {"year": 2040, "p10": 300_000, "p50": 700_000, "p90": 1_200_000},
            ]
        }
        ret_data = {"probabilityOfSuccess": 0.87, "hasShortfall": False}

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("c", "p", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt", "key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(side_effect=[
                       (pos_data, None),
                       (spread_data, None),
                       (ret_data, None),
                   ])):
            result = await get_official_plan_projection(session)

        assert "error" not in result
        assert result["probability_of_success_pct"] == pytest.approx(87.0)
        assert result["probability_status"] == "On Track"
        spread = result["asset_spread"]
        assert len(spread) == 2
        assert spread[0]["year"] == 2030
        assert spread[0]["p50"] == pytest.approx(900_000)

    @pytest.mark.asyncio
    async def test_partial_failure_still_returns_data(self):
        """If asset spread fails but probability of success is available, return partial."""
        from emoney_mcp.scrapers.plan_api import get_official_plan_projection

        pos_data = {"probabilityOfSuccess": 0.72}

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("c", "p", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt", "key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(side_effect=[
                       (pos_data, None),          # pos ok
                       (None, "HTTP 404"),         # spread unavailable
                       (None, "HTTP 404"),         # retirement unavailable
                   ])):
            result = await get_official_plan_projection(session)

        assert "error" not in result
        assert result["probability_of_success_pct"] == pytest.approx(72.0)
        assert result["probability_status"] == "Monitor"
        assert "asset_spread_note" in result

    @pytest.mark.asyncio
    async def test_all_unavailable_returns_error(self):
        from emoney_mcp.scrapers.plan_api import get_official_plan_projection

        session = AsyncMock()
        with patch("emoney_mcp.scrapers.plan_api._get_plan_ids",
                   AsyncMock(return_value=("c", "p", None))), \
             patch("emoney_mcp.scrapers.plan_api._get_snb_credentials",
                   AsyncMock(return_value=("jwt", "key"))), \
             patch("emoney_mcp.scrapers.plan_api._bff_get",
                   AsyncMock(side_effect=[
                       (None, "HTTP 503"),
                       (None, "HTTP 503"),
                       (None, "HTTP 503"),
                   ])):
            result = await get_official_plan_projection(session)

        assert "error" in result


# ---------------------------------------------------------------------------
# Dispatch registration
# ---------------------------------------------------------------------------

class TestDispatchRegistration:
    """All 4 new tools must be in the server dispatch table."""

    def test_all_new_tools_registered(self):
        import emoney_mcp.server as srv
        for name in ("get_vault_folder", "get_plan_assumptions",
                     "get_plan_expenses", "get_official_plan_projection"):
            assert name in srv._DISPATCH, f"{name} not in _DISPATCH"
