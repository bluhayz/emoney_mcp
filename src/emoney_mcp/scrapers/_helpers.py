"""Shared URL constants and low-level helpers used across scraper modules."""

import os
import time

from ..browser import BASE_URL  # single source of truth for the subdomain

_CARD_URL  = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"
_INV_URL   = f"{BASE_URL}/ema/CS/Investments"
_SPEND_URL = f"{BASE_URL}/ema/CS/Spending"
_SNB_API   = "https://api.emoneyadvisor.com/snb-api"


async def _get_card(http, card_id: int) -> dict | None:
    """Fetch a CardSwitcher card and return its Data dict, or None on failure."""
    ts = int(time.time() * 1000)
    resp = await http.get(f"{_CARD_URL}/{card_id}?_={ts}", timeout=20)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return None
    return resp.json().get("Data")


def _fmt_dollars(v) -> str | None:
    if v is None:
        return None
    return f"${v:,.2f}"
