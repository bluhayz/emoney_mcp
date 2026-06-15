"""
Tests for v1.0.2 live-endpoint discoveries:
  - get_client_profile  (Profile/GetProfileData)
  - get_aggregation_status (Card 20)
  - get_spending_by_account — SNB GetAccounts integration (account name resolution)
  - get_portfolio_concentration — Card 6 top-holdings supplement
  - get_home_equity — Card 10 liquid cash / credit
"""

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_http_resp(data, status=200, content_type="application/json"):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"content-type": content_type}
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


def _make_session_with_get(responses: dict):
    """
    Return a mock session whose http.get dispatches by URL substring.
    responses: {url_substring: response_data_or_mock}
    """
    async def mock_get(url, **kwargs):
        for fragment, data in responses.items():
            if fragment in url:
                if isinstance(data, MagicMock):
                    return data
                return _make_http_resp(data)
        # Default 404
        resp = MagicMock()
        resp.status_code = 404
        resp.headers = {"content-type": "text/html"}
        resp.text = ""
        return resp

    http = AsyncMock()
    http.get = mock_get

    session = make_mock_http_session()
    session.get_http = AsyncMock(return_value=http)
    return session


# ---------------------------------------------------------------------------
# Profile/GetProfileData response fixture
# ---------------------------------------------------------------------------

_PROFILE_RESP = {
    "Clients": [
        {"ID": "uuid-1", "IsSpouse": False, "Name": "Drew Hayes",
         "DateOfBirth": "12/4/1984", "EmailAddress": "drew@example.com"},
        {"ID": "uuid-1", "IsSpouse": True,  "Name": "Lacey Hayes",
         "DateOfBirth": "7/5/1981",  "EmailAddress": ""},
    ],
    "People": {
        "People": [
            {"Name": "Parker", "DateOfBirth": "9/19/2018 12:00:00 AM",
             "FactID": "fact-parker", "IsSpouse": False},
        ],
        "ClientHasSpouse": True,
    },
    "Property": {
        "Properties": [
            {"Name": "47721 Allegheny Cir", "FactID": "fact-home"},
        ]
    },
    "OtherSections": [],
    "IsOrganizationClient": False,
}

# ---------------------------------------------------------------------------
# Card 20 fixture
# ---------------------------------------------------------------------------

_CARD20_HEALTHY = {
    "CardId": 20,
    "BrokenConnections": [],
    "Accounts": [],
    "ItemStatus": 0,
}

_CARD20_BROKEN = {
    "CardId": 20,
    "BrokenConnections": [
        {
            "ConnectionID": {"Value": "33550423"},
            "Name": "Prudential (Client Access)",
            "ConnectionStatusName": "Disconnected",
            "ConnectionStatusLevel": "UnhealthyActionable",
            "ConnectionStatusDescription": "Disconnected for having no accounts with minimum age of 60 days",
        }
    ],
    "Accounts": [],
    "ItemStatus": 2,
}


# ===========================================================================
# get_client_profile
# ===========================================================================

class TestGetClientProfile:

    @pytest.mark.asyncio
    async def test_returns_primary_and_spouse(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        assert result["primary"]["name"] == "Drew Hayes"
        assert result["spouse"]["name"] == "Lacey Hayes"

    @pytest.mark.asyncio
    async def test_age_computed_from_dob(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        # Drew born 1984, so age should be ~40-42
        assert 38 <= result["primary"]["age"] <= 45
        assert result["primary"]["birth_year"] == 1984

    @pytest.mark.asyncio
    async def test_spouse_age_computed(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        assert result["spouse"]["birth_year"] == 1981
        assert result["spouse"]["age"] is not None

    @pytest.mark.asyncio
    async def test_dependents_returned(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        assert len(result["dependents"]) == 1
        assert result["dependents"][0]["name"] == "Parker"

    @pytest.mark.asyncio
    async def test_properties_returned(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        assert len(result["properties"]) == 1
        assert "Allegheny" in result["properties"][0]["name"]

    @pytest.mark.asyncio
    async def test_household_size(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        # 2 clients + 1 dependent
        assert result["household_size"] == 3

    @pytest.mark.asyncio
    async def test_http_error_returns_error(self):
        err_resp = _make_http_resp({}, status=403)
        session = _make_session_with_get({"Profile/GetProfileData": err_resp})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_all_required_keys_present(self):
        session = _make_session_with_get({"Profile/GetProfileData": _PROFILE_RESP})
        from emoney_mcp.scrapers.accounts import get_client_profile
        result = await get_client_profile(session)
        for key in ("primary", "spouse", "dependents", "properties", "household_size", "as_of"):
            assert key in result


# ===========================================================================
# get_aggregation_status
# ===========================================================================

class TestGetAggregationStatus:

    @pytest.mark.asyncio
    async def test_healthy_when_no_broken_connections(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts._get_card", return_value=_CARD20_HEALTHY):
            from emoney_mcp.scrapers.accounts import get_aggregation_status
            result = await get_aggregation_status(session)
        assert result["overall_status"] == "healthy"
        assert result["broken_count"] == 0
        assert result["broken_connections"] == []

    @pytest.mark.asyncio
    async def test_attention_needed_when_broken(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts._get_card", return_value=_CARD20_BROKEN):
            from emoney_mcp.scrapers.accounts import get_aggregation_status
            result = await get_aggregation_status(session)
        assert result["overall_status"] == "attention_needed"
        assert result["broken_count"] == 1

    @pytest.mark.asyncio
    async def test_broken_connection_fields(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts._get_card", return_value=_CARD20_BROKEN):
            from emoney_mcp.scrapers.accounts import get_aggregation_status
            result = await get_aggregation_status(session)
        conn = result["broken_connections"][0]
        assert "institution" in conn
        assert "Prudential" in conn["institution"]
        assert "Disconnected" in conn["status"]

    @pytest.mark.asyncio
    async def test_card20_unavailable_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts._get_card", return_value=None):
            from emoney_mcp.scrapers.accounts import get_aggregation_status
            result = await get_aggregation_status(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_all_required_keys_present(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.accounts._get_card", return_value=_CARD20_HEALTHY):
            from emoney_mcp.scrapers.accounts import get_aggregation_status
            result = await get_aggregation_status(session)
        for key in ("overall_status", "broken_count", "broken_connections", "as_of"):
            assert key in result


# ===========================================================================
# get_spending_by_account — SNB account map resolution
# ===========================================================================

_SNB_ACCOUNTS = [
    {"id": "acct-001", "name": "Drew Visa", "subscriberAccountID": "sub-001", "institutionName": "Chase"},
    {"id": "acct-002", "name": "Lacey MC",  "subscriberAccountID": "sub-002", "institutionName": "Citi"},
]

from datetime import datetime, timedelta

def _dago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

_RAW_TXNS_WITH_IDS = [
    {"id": "t1", "date": _dago(5), "userDescription": "WHOLE FOODS",
     "categoryId": "1", "value": -200.0, "accountId": "acct-001", "isDeleted": False},
    {"id": "t2", "date": _dago(7), "userDescription": "STARBUCKS",
     "categoryId": "2", "value": -8.0, "accountId": "acct-002", "isDeleted": False},
    {"id": "t3", "date": _dago(10), "userDescription": "AMAZON",
     "categoryId": "3", "value": -50.0, "accountId": "acct-001", "isDeleted": False},
]

_CATEGORIES = {"1": "Groceries", "2": "Dining", "3": "Shopping",
               "4": "Paycheck/Salary", "5": "Transfers"}


class TestGetSpendingByAccountWithMap:

    @pytest.mark.asyncio
    async def test_account_names_resolved_from_snb_map(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS_WITH_IDS, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map",
                   return_value={"acct-001": "Drew Visa", "acct-002": "Lacey MC"}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        names = {a["account_name"] for a in result["accounts"]}
        assert "Drew Visa" in names
        assert "Lacey MC" in names

    @pytest.mark.asyncio
    async def test_raw_id_used_when_map_empty(self):
        """When GetAccounts fails and map is empty, falls back to raw accountId."""
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS_WITH_IDS, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map",
                   return_value={}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        # Should still return accounts, using raw IDs as names
        assert result["account_count"] > 0

    @pytest.mark.asyncio
    async def test_map_overrides_embedded_account_name(self):
        """SNB map should take priority over any accountName embedded in transaction."""
        raw_with_names = [
            {**t, "accountName": "Old Stale Name"}  # embedded name should be overridden
            for t in _RAW_TXNS_WITH_IDS
        ]
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, raw_with_names, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map",
                   return_value={"acct-001": "Drew Visa", "acct-002": "Lacey MC"}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        names = {a["account_name"] for a in result["accounts"]}
        assert "Drew Visa" in names
        assert "Old Stale Name" not in names


# ===========================================================================
# get_portfolio_concentration — Card 6 supplement
# ===========================================================================

_CARD6_DATA = {
    "CardId": 6,
    "ValueChange": {"CurrentValue": 8_000_000},
    "Investments": [
        {"Name": "Vanguard Total Stock Market Index Fund;Admiral", "Ticker": "VTSAX", "Value": 1_800_000},
        {"Name": "Snowflake Inc", "Ticker": "SNOW", "Value": 1_500_000},
        {"Name": "Fidelity Government Money Market", "Ticker": "SPAXX", "Value": 700_000},
    ],
}

_INV_DATA_SIMPLE = {
    "Accounts": [
        {
            "AccountName": "Drew Brokerage",
            "MajorType": "InvestmentAsset",
            "Holdings": [
                {"Ticker": "VTSAX", "Description": "Vanguard Total Stock Market Index Fund", "Value": 1_800_000},
                {"Ticker": "SNOW", "Description": "Snowflake Inc", "Value": 1_500_000},
                {"Ticker": "SPAXX", "Description": "Fidelity Govt Money Market Fund", "Value": 700_000},
                {"Ticker": "BND",  "Description": "Vanguard Total Bond Market ETF", "Value": 500_000},
                {"Ticker": "VXUS", "Description": "Vanguard Total International ETF", "Value": 300_000},
            ],
        }
    ]
}


def _make_inv_session_c6(inv_data, card6=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = inv_data

    card6_resp = MagicMock()
    card6_resp.status_code = 200
    card6_resp.headers = {"content-type": "application/json"}
    card6_resp.json.return_value = {"Data": card6} if card6 else {"Data": None}

    async def mock_get(url, **kwargs):
        if "GetInvestmentData" in url:
            return resp
        if "GetCard/6" in url:
            return card6_resp
        r = MagicMock()
        r.status_code = 404
        r.headers = {"content-type": "text/html"}
        return r

    http = AsyncMock()
    http.get = mock_get

    session = make_mock_http_session()
    session.get_http = AsyncMock(return_value=http)
    return session


class TestGetPortfolioConcentrationCard6:

    @pytest.mark.asyncio
    async def test_card6_top_holdings_in_result(self):
        session = _make_inv_session_c6(_INV_DATA_SIMPLE, _CARD6_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        assert "card6_top_holdings" in result

    @pytest.mark.asyncio
    async def test_card6_holdings_have_ticker_and_value(self):
        session = _make_inv_session_c6(_INV_DATA_SIMPLE, _CARD6_DATA)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        for h in result["card6_top_holdings"]:
            assert "ticker" in h
            assert "name" in h
            assert "value" in h

    @pytest.mark.asyncio
    async def test_card6_unavailable_does_not_break_tool(self):
        """If Card 6 is unavailable, get_portfolio_concentration should still succeed."""
        session = _make_inv_session_c6(_INV_DATA_SIMPLE, card6=None)
        from emoney_mcp.scrapers.portfolio import get_portfolio_concentration
        result = await get_portfolio_concentration(session)
        # Should succeed, card6_top_holdings may be empty
        assert "diversification_grade" in result
        assert "card6_top_holdings" in result
        assert isinstance(result["card6_top_holdings"], list)


# ===========================================================================
# get_home_equity — Card 10 liquid cash + credit
# ===========================================================================

_CARD10_DATA = {"CardId": 10, "NetWorth": 8_466_139, "Cash": 182_430, "Credit": -11_772}

_ACCOUNTS_WITH_PROPERTY = {
    "net_worth": 8_466_139,
    "total_assets": 8_912_911,
    "total_liabilities": -446_772,
    "account_groups": [
        {
            "group": "Property",
            "total": 977_000,
            "accounts": [{"name": "47721 Allegheny Cir", "type": "RealEstateAsset", "balance": 977_000}],
        },
        {
            "group": "Loans",
            "total": -435_000,
            "accounts": [{"name": "47721 Mortgage", "type": "Mortgage", "balance": -435_000}],
        },
    ],
}


class TestGetHomeEquityCard10:

    @pytest.mark.asyncio
    async def test_liquid_cash_from_card10(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_ACCOUNTS_WITH_PROPERTY), \
             patch("emoney_mcp.scrapers.planning._get_card", return_value=_CARD10_DATA):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert result["liquid_cash"] == 182_430

    @pytest.mark.asyncio
    async def test_credit_card_balance_from_card10(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_ACCOUNTS_WITH_PROPERTY), \
             patch("emoney_mcp.scrapers.planning._get_card", return_value=_CARD10_DATA):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert result["credit_card_balance"] == -11_772

    @pytest.mark.asyncio
    async def test_card10_unavailable_returns_none_not_error(self):
        """If Card 10 is unavailable, liquid_cash and credit_card_balance should be None."""
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_ACCOUNTS_WITH_PROPERTY), \
             patch("emoney_mcp.scrapers.planning._get_card", return_value=None):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        # Should still succeed with equity data; cash fields are None when Card 10 unavailable
        assert "total_equity" in result
        assert result["liquid_cash"] is None
        assert result["credit_card_balance"] is None

    @pytest.mark.asyncio
    async def test_equity_still_computed_correctly(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts", return_value=_ACCOUNTS_WITH_PROPERTY), \
             patch("emoney_mcp.scrapers.planning._get_card", return_value=_CARD10_DATA):
            from emoney_mcp.scrapers.planning import get_home_equity
            result = await get_home_equity(session)
        assert result["total_equity"] == 977_000 - 435_000


# ===========================================================================
# _fetch_snb_account_map unit tests
# ===========================================================================

class TestFetchSnbAccountMap:

    @pytest.mark.asyncio
    async def test_returns_id_to_name_map(self):
        from emoney_mcp.scrapers.spending import _fetch_snb_account_map
        # Bypass cache by resetting it
        import emoney_mcp.scrapers.spending as sp_mod
        sp_mod._snb_account_cache = None

        with patch("emoney_mcp.scrapers.spending._get_snb_credentials",
                   return_value=("jwt-test", "key-test")):
            http = AsyncMock()
            http.get = AsyncMock(return_value=_make_http_resp(_SNB_ACCOUNTS))
            session2 = make_mock_http_session()
            session2.get_http = AsyncMock(return_value=http)
            result = await _fetch_snb_account_map(session2)

        assert isinstance(result, dict)
        assert "acct-001" in result or len(result) == len(_SNB_ACCOUNTS)

    @pytest.mark.asyncio
    async def test_empty_dict_on_auth_failure(self):
        from emoney_mcp.scrapers.spending import _fetch_snb_account_map
        import emoney_mcp.scrapers.spending as sp_mod
        sp_mod._snb_account_cache = None

        with patch("emoney_mcp.scrapers.spending._get_snb_credentials",
                   return_value=("", "")):
            session = make_mock_http_session()
            result = await _fetch_snb_account_map(session)
        assert result == {}
