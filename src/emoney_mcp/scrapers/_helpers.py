"""
Shared URL constants and low-level HTTP helpers used across all scraper modules.

Every module in this package imports its URL constants from here so that the
base subdomain (controlled by the EMONEY_SUBDOMAIN env variable) is defined
in a single place.  ``BASE_URL`` itself comes from ``browser.py``, which reads
the env variable at import time.

Constants
---------
BASE_URL           — https://<subdomain>.emaplan.com
_CARD_URL          — CardSwitcher endpoint (used by almost every module)
_INV_URL           — Investments endpoint (holdings, transactions, performance)
_SPEND_URL         — Spending page (used only to scrape the JWT token for the SNB API)
_SNB_API           — External SNB (Spending/Net Banking) API base URL
_CARD_CACHE_TTL    — Card response TTL in seconds (default 300 = 5 minutes)

Functions
---------
_get_card(http, card_id)  — Fetch a named CardSwitcher card; returns its Data
                            dict or None if the request fails.  Results are
                            cached for _CARD_CACHE_TTL seconds so parallel tool
                            calls within one conversation turn share one HTTP
                            request per card.
_fmt_dollars(v)           — Format a numeric value as "$1,234.56" for display.
clear_card_cache()        — Purge the in-memory card cache (called on session
                            reset so stale data is never served with new creds).
"""

import time

from ..browser import BASE_URL  # single source of truth for the subdomain

# ---------------------------------------------------------------------------
# API endpoint roots — built once at import time from BASE_URL
# ---------------------------------------------------------------------------

_CARD_URL  = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"
_INV_URL   = f"{BASE_URL}/ema/CS/Investments"
_SPEND_URL = f"{BASE_URL}/ema/CS/Spending"

# The SNB API lives on a separate host — always https regardless of subdomain
_SNB_API   = "https://api.emoneyadvisor.com/snb-api"

# ---------------------------------------------------------------------------
# In-memory TTL cache for CardSwitcher card responses
# ---------------------------------------------------------------------------
# Format: {card_id: (fetch_unix_timestamp, data_dict_or_None)}
# The 5-minute TTL is intentionally aligned with the Anthropic prompt-cache
# window so warm-cache conversation turns don't trigger redundant fetches.
_card_cache: dict[int, tuple[float, dict | None]] = {}
_CARD_CACHE_TTL: int = 300  # seconds


def clear_card_cache() -> None:
    """
    Purge the in-memory CardSwitcher cache.

    Called automatically by ``reset_session`` so that session resets never
    return cached data from the previous authenticated user.
    """
    _card_cache.clear()


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

async def _get_card(http, card_id: int) -> dict | None:
    """
    Fetch a single CardSwitcher card by ID and return its ``Data`` dict.

    Emoney's dashboard is powered by numbered "cards" (e.g. card 9 = net worth,
    card 1 = account groups, card 3 = portfolio performance).  Each card
    endpoint returns ``{"Data": {...}, "Status": "Success"}``.

    Results are cached for ``_CARD_CACHE_TTL`` seconds (default 5 minutes).
    This means multiple tools called within the same conversation turn share a
    single HTTP request per card — e.g., ``get_financial_summary`` and
    ``get_financial_health_score`` both need card 2 (goals) but only one HTTP
    request is made.

    A cache-busting ``_=<timestamp>`` query param is still appended on live
    fetches so Emoney's own server-side cache never returns stale data.

    Returns ``None`` if the HTTP status is not 200 or the response is not JSON.
    """
    now = time.time()

    # Return cached data if still fresh
    if card_id in _card_cache:
        cached_ts, cached_data = _card_cache[card_id]
        if now - cached_ts < _CARD_CACHE_TTL:
            return cached_data

    # Cache miss — fetch from Emoney
    ts = int(now * 1000)
    resp = await http.get(f"{_CARD_URL}/{card_id}?_={ts}", timeout=20)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        _card_cache[card_id] = (now, None)   # cache the miss too (avoids retry storms)
        return None
    data = resp.json().get("Data")
    _card_cache[card_id] = (now, data)
    return data


def _fmt_dollars(v) -> str | None:
    """
    Format a numeric value as a dollar string (e.g. 1234.5 → "$1,234.50").

    Returns ``None`` if ``v`` is ``None``, preserving the absence of data
    rather than showing "$0.00" for missing fields.
    """
    if v is None:
        return None
    return f"${v:,.2f}"
