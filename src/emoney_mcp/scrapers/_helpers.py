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
_SNB_API           — External SNB (Spending/Net Banking) API base URL
_CARD_CACHE_TTL    — Card response TTL in seconds (default 300 = 5 minutes)

Functions
---------
_get_card(http, card_id)  — Fetch a named CardSwitcher card; returns its Data
                            dict or None if the request fails.  Results are
                            cached for _CARD_CACHE_TTL seconds so parallel tool
                            calls within one conversation turn share one HTTP
                            request per card.
_get_investment_data(http_session) — Fetch GetInvestmentData (holdings JSON)
                            with the same 5-minute TTL cache as cards.  Shared
                            by investments.py, tax.py, and portfolio.py so a
                            single conversation turn fires at most one request.
_fmt_dollars(v)           — Format a numeric value as "$1,234.56" for display.
clear_card_cache()        — Purge both the card cache and investment data cache
                            (called on session reset).
"""

import time
from datetime import datetime

from ..browser import BASE_URL  # single source of truth for the subdomain

# ---------------------------------------------------------------------------
# API endpoint roots — built once at import time from BASE_URL
# ---------------------------------------------------------------------------

_CARD_URL = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"
_INV_URL  = f"{BASE_URL}/ema/CS/Investments"

# The SNB API lives on a separate host — always https regardless of subdomain
_SNB_API  = "https://api.emoneyadvisor.com/snb-api"

# ---------------------------------------------------------------------------
# In-memory TTL caches for CardSwitcher cards and GetInvestmentData
# ---------------------------------------------------------------------------
# Cards:      {card_id: (fetch_unix_timestamp, data_dict_or_None)}
# Investment: (fetch_unix_timestamp, data_dict_or_None) | None
# The 5-minute TTL is intentionally aligned with the Anthropic prompt-cache
# window so warm-cache conversation turns don't trigger redundant fetches.
_card_cache: dict[int, tuple[float, dict | None]] = {}
_inv_cache: tuple[float, dict | None] | None = None
_CARD_CACHE_TTL: int = 300       # seconds — successful responses
_CARD_ERROR_TTL: int = 30        # seconds — failed/None responses (shorter so transient errors don't block for 5 min)


def clear_card_cache() -> None:
    """
    Purge both the CardSwitcher card cache and the GetInvestmentData cache.

    Called automatically by ``reset_session`` so that session resets never
    return cached data from the previous authenticated user.
    """
    global _inv_cache
    _card_cache.clear()
    _inv_cache = None


# ---------------------------------------------------------------------------
# Shared HTTP helpers
# ---------------------------------------------------------------------------

async def _get_investment_data(http_session) -> tuple[dict | None, dict | None]:
    """
    Fetch the Emoney GetInvestmentData endpoint and return (data, error).

    On success: (data_dict, None).  On failure: (None, {"error": "..."}).

    Results are cached for ``_CARD_CACHE_TTL`` seconds (5 minutes) so that
    multiple tools within one conversation turn share a single HTTP request.
    Failures are cached for ``_CARD_ERROR_TTL`` seconds (30 s) so a transient
    server error doesn't block every investment tool for 5 minutes.
    """
    global _inv_cache
    now = time.time()
    if _inv_cache is not None:
        ts, data = _inv_cache
        ttl = _CARD_CACHE_TTL if data is not None else _CARD_ERROR_TTL
        if now - ts < ttl:
            if data is None:
                return None, {"error": "GetInvestmentData unavailable (cached error). Session may have expired."}
            return data, None
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={int(now * 1000)}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        _inv_cache = (now, None)
        return None, {"error": f"GetInvestmentData returned {resp.status_code}. Session may have expired."}
    payload = resp.json()
    if not isinstance(payload, dict):
        _inv_cache = (now, None)
        return None, {"error": "GetInvestmentData returned an unexpected (non-object) body."}
    _inv_cache = (now, payload)
    return payload, None


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
    # Card IDs index a fixed set of numeric Emoney endpoints. Coerce to int so a
    # crafted value (e.g. via explore_emoney_cards' user/model-supplied card_ids
    # list) can't inject path or query segments into the request against the
    # authenticated host (e.g. "8/../SignOut" or "8?foo=bar").
    try:
        card_id = int(card_id)
    except (TypeError, ValueError):
        return None

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
        # Cache failures with a short TTL so a transient server error doesn't block tools for 5 minutes
        _card_cache[card_id] = (now - _CARD_CACHE_TTL + _CARD_ERROR_TTL, None)
        return None
    # Some (often undocumented) cards return a JSON `null` or a non-object body.
    # Guard against that so a probe of unknown card IDs can't crash the caller.
    payload = resp.json()
    data = payload.get("Data") if isinstance(payload, dict) else None
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


def _month_offset(base_date, months_back: int):
    """Return the first day of the month that is ``months_back`` calendar months before base_date."""
    month = base_date.month - months_back
    year  = base_date.year + (month - 1) // 12
    month = ((month - 1) % 12) + 1
    return base_date.replace(year=year, month=month, day=1)


def _parse_card8_history(card8, months: int, now=None) -> list[dict]:
    """
    Slice and month-label Card 8's net-worth ``History`` array.

    Card 8's ``History`` is **oldest-first** (the newest element is the current
    month). This returns the most recent ``months`` points as
    ``[{"month": "YYYY-MM", "net_worth": <value>}, ...]`` (oldest-first), with
    drift-free calendar-month labels derived from :func:`_month_offset` — the
    newest point is ``months_ago = 0``.

    Shared by ``get_net_worth_history`` (investments.py) and
    ``get_net_worth_velocity`` (portfolio.py) so their parsing/labelling can't
    diverge. Accepts either the card dict (``{"History": [...], ...}``) or a
    bare list. Returns ``[]`` for missing/empty history; callers decide whether
    an empty result is an error.
    """
    raw = (card8.get("History") if isinstance(card8, dict) else card8) or []
    raw = raw[-months:]                      # keep the most recent N months
    now = now or datetime.now()
    total = len(raw)
    points = []
    for i, val in enumerate(raw):
        months_ago = total - 1 - i           # newest element → 0 months ago
        dt = _month_offset(now, months_ago)
        points.append({"month": dt.strftime("%Y-%m"), "net_worth": val})
    return points
