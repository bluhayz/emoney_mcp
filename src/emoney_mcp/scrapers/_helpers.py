"""
Shared URL constants and low-level HTTP helpers used across all scraper modules.

Every module in this package imports its URL constants from here so that the
base subdomain (controlled by the EMONEY_SUBDOMAIN env variable) is defined
in a single place.  ``BASE_URL`` itself comes from ``browser.py``, which reads
the env variable at import time.

Constants
---------
BASE_URL   — https://<subdomain>.emaplan.com
_CARD_URL  — CardSwitcher endpoint (used by almost every module)
_INV_URL   — Investments endpoint (holdings, transactions, performance)
_SPEND_URL — Spending page (used only to scrape the JWT token for the SNB API)
_SNB_API   — External SNB (Spending/Net Banking) API base URL

Functions
---------
_get_card(http, card_id)  — Fetch a named CardSwitcher card; returns its Data
                            dict or None if the request fails.
_fmt_dollars(v)           — Format a numeric value as "$1,234.56" for display.
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
# Shared HTTP helpers
# ---------------------------------------------------------------------------

async def _get_card(http, card_id: int) -> dict | None:
    """
    Fetch a single CardSwitcher card by ID and return its ``Data`` dict.

    Emoney's dashboard is powered by numbered "cards" (e.g. card 9 = net worth,
    card 1 = account groups, card 3 = portfolio performance).  Each card
    endpoint returns ``{"Data": {...}, "Status": "Success"}``.

    A cache-busting ``_=<timestamp>`` query param is appended so Emoney never
    returns a stale cached response.

    Returns ``None`` if the HTTP status is not 200 or the response is not JSON.
    """
    ts = int(time.time() * 1000)
    resp = await http.get(f"{_CARD_URL}/{card_id}?_={ts}", timeout=20)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return None
    return resp.json().get("Data")


def _fmt_dollars(v) -> str | None:
    """
    Format a numeric value as a dollar string (e.g. 1234.5 → "$1,234.50").

    Returns ``None`` if ``v`` is ``None``, preserving the absence of data
    rather than showing "$0.00" for missing fields.
    """
    if v is None:
        return None
    return f"${v:,.2f}"
