"""
Account-aggregation control via the eMoney ``aggapi`` service (#103).

``get_aggregation_status`` (Card 20) only *reports* connection staleness; this
module *triggers* a refresh so a stale/broken feed can be re-pulled without
leaving the assistant.

Aggregation runs on a separate Apigee-gated REST service ("aggapi") at
``https://api.emoneyadvisor.com/aggapi/api/v1`` — NOT the same-origin MVC site
(the legacy ``/ema/CS/Organizer/RefreshConnections`` MVC endpoint returns a
blanket 500 on this Capstone instance and is dead — see #103 discovery pass 1).
Auth is a TWO-part credential, both required:

  - ``Authorization: Bearer <token>`` — minted from ``/ema/CS/Aggregation/GetToken``
    (cookie-authed GET, returns ``{"accessToken": <jwt>}``). The JWT's ``aud`` is
    the aggapi and its ``userId`` claim is the aggregation user GUID used in the
    request path.
  - ``apikey: <aggApiKey>`` — a DISTINCT Apigee product key scraped from the
    Organizer/Accounts page config. It is NOT ``connectionsApiKey`` and NOT the
    SNB ``apikey`` — those return ``401 InvalidApiKeyForGivenResource``.

Endpoints (verified live 2026-06-18, #103):
  GET  /users/<userGuid>/connections                  → [{id, status, statusLevel,
                                                          lastSuccessfulUpdate, name,
                                                          institutionId, ...}, ...]
  POST /users/<userGuid>/connections/<id>/refresh      → 202 {"activityId": "..."}

Public functions
----------------
refresh_account_aggregation(http_session, institution=None, connection_id=None)
    Queue an aggregation re-pull for all connections, one institution (name
    substring), or a single connection id, and report the queued activity.
"""

import base64
import json
import logging
import re

from ._helpers import BASE_URL

_AGG_API      = "https://api.emoneyadvisor.com/aggapi/api/v1"
_ACCOUNTS_URL = f"{BASE_URL}/ema/CS/Organizer/Accounts"
_GETTOKEN_URL = f"{BASE_URL}/ema/CS/Aggregation/GetToken"

_AGGKEY_RE = re.compile(r'aggApiKey["\']?\s*[:=]\s*["\']([A-Za-z0-9]{20,40})["\']')

_log = logging.getLogger("emoney_mcp.scrapers.aggregation_api")


def _jwt_user_guid(token: str) -> str | None:
    """Extract the ``userId`` claim (the aggregation user GUID) from the JWT."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part)).get("userId")
    except Exception:
        return None


async def _get_agg_credentials(http_session):
    """
    Resolve ``(bearer_token, agg_api_key, user_guid)`` for the aggapi.

    Scrapes ``aggApiKey`` from the Organizer/Accounts page and mints the Bearer
    token via ``GetToken``. Returns ``(token, apikey, guid, None)`` on success or
    ``(None, None, None, error_dict)``.
    """
    http = await http_session.get_http()

    resp = await http.get(_ACCOUNTS_URL, allow_redirects=True, timeout=25)
    if resp.status_code != 200 or "/ema/SignIn" in str(resp.url):
        return None, None, None, {"error": "Session expired — call sync_chrome_session or reset_session."}
    m = _AGGKEY_RE.search(resp.text)
    if not m:
        return None, None, None, {"error": "Could not locate aggApiKey on the Accounts page — "
                                            "eMoney page layout may have changed."}
    agg_api_key = m.group(1)

    tok_resp = await http.get(_GETTOKEN_URL, headers={"X-Requested-With": "XMLHttpRequest"}, timeout=20)
    try:
        token = tok_resp.json().get("accessToken")
    except Exception:
        token = None
    if not token:
        return None, None, None, {"error": f"Aggregation GetToken returned no token (HTTP {tok_resp.status_code}). "
                                            "Session may be stale — try sync_chrome_session."}

    guid = _jwt_user_guid(token)
    if not guid:
        return None, None, None, {"error": "Could not read the aggregation user id from the token."}
    return token, agg_api_key, guid, None


def _agg_headers(token: str, api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "apikey":        api_key,
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }


def _connection_view(c: dict) -> dict:
    """Trim a raw aggapi connection object to the fields we surface."""
    return {
        "connection_id":        c.get("id"),
        "name":                 c.get("name"),
        "status":               c.get("status"),
        "status_level":         c.get("statusLevel"),
        "status_description":   c.get("statusDescription"),
        "last_successful_update": c.get("lastSuccessfulUpdate"),
    }


async def refresh_account_aggregation(
    http_session,
    institution: str | None = None,
    connection_id: str | None = None,
) -> dict:
    """
    Trigger an account-aggregation refresh (re-pull) of connected institutions.

    ``get_aggregation_status`` reports which feeds are stale or broken; this
    queues a fresh pull so the data updates without visiting eMoney. Refreshes
    every connection by default, or narrow it with ``institution`` (matches the
    connection name, case-insensitive substring) or an exact ``connection_id``.

    Each refresh is asynchronous: eMoney returns an ``activityId`` and updates the
    feed in the background. Re-run ``get_aggregation_status`` shortly after to see
    the new ``last_updated`` dates.

    Parameters
    ----------
    institution   : only refresh connections whose name contains this text
    connection_id : only refresh this exact connection id (overrides institution)
    """
    token, api_key, guid, err = await _get_agg_credentials(http_session)
    if err:
        return err
    headers = _agg_headers(token, api_key)
    http = await http_session.get_http()

    # List connections to resolve targets (and to report names/prior status).
    list_resp = await http.get(f"{_AGG_API}/users/{guid}/connections", headers=headers, timeout=25)
    if list_resp.status_code != 200 or "json" not in list_resp.headers.get("content-type", ""):
        return {"error": f"Could not list account connections (HTTP {list_resp.status_code})."}
    connections = list_resp.json()
    if not isinstance(connections, list):
        return {"error": "Unexpected connections response shape."}

    # Select targets.
    if connection_id is not None:
        targets = [c for c in connections if str(c.get("id")) == str(connection_id)]
        if not targets:
            return {"error": f"No connection with id {connection_id}. "
                             "Call get_aggregation_status to see connections."}
        scope = f"connection {connection_id}"
    elif institution:
        needle = institution.lower()
        targets = [c for c in connections if needle in (c.get("name") or "").lower()]
        if not targets:
            return {"error": f"No connection matching institution '{institution}'. "
                             "Call get_aggregation_status to see institution names."}
        scope = f"institution matching '{institution}'"
    else:
        targets = connections
        scope = "all connections"

    # Queue a refresh per connection.
    refreshed, failures = [], []
    for c in targets:
        cid = c.get("id")
        view = _connection_view(c)
        try:
            r = await http.post(f"{_AGG_API}/users/{guid}/connections/{cid}/refresh",
                                 headers=headers, json={}, timeout=30)
        except Exception as e:
            failures.append({**view, "error": type(e).__name__})
            continue
        if r.status_code in (200, 201, 202, 204):
            activity_id = None
            try:
                activity_id = r.json().get("activityId")
            except Exception:
                pass
            refreshed.append({**view, "queued": True, "activity_id": activity_id})
        else:
            failures.append({**view, "error": f"HTTP {r.status_code}", "response_body": r.text[:200]})

    return {
        "scope":            scope,
        "connections_total": len(connections),
        "queued_count":     len(refreshed),
        "failed_count":     len(failures),
        "refreshed":        refreshed,
        "failures":         failures,
        "note": (
            "Refreshes are asynchronous — each returns an activity_id and updates in the background. "
            "Re-run get_aggregation_status in a minute or two to confirm new last_updated dates. "
            "A connection in a broken/disconnected state may need re-authentication in the eMoney "
            "portal (a refresh alone won't fix a credential or MFA failure)."
        ),
    }
