"""Scraping logic — calls Emoney's internal CardSwitcher JSON API.

Card 9  → net worth, total assets, total liabilities
Card 1  → account groups with individual balances and institutions

This module is hot-reloaded on every tool call so changes take
effect without restarting the MCP server.
"""

import os
import time

_SUBDOMAIN = os.getenv("EMONEY_SUBDOMAIN", "wealth")
BASE_URL = f"https://{_SUBDOMAIN}.emaplan.com"
_CARD_URL = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"


async def get_accounts(http_session) -> dict:
    """Fetch net worth and all accounts via the CardSwitcher API."""
    ts = int(time.time() * 1000)
    http = await http_session.get_http()

    # Card 9: net worth summary
    r9 = await http.get(f"{_CARD_URL}/9?_={ts}", timeout=20)
    if r9.status_code != 200 or "json" not in r9.headers.get("content-type", ""):
        return {"error": f"Card 9 returned {r9.status_code}. Session may have expired — call reset_session."}

    nw_data = r9.json().get("Data", {})
    net_worth   = nw_data.get("NetWorth")
    total_assets = nw_data.get("Assets")
    total_liab   = nw_data.get("Liabilities")

    # Card 1: full account list
    r1 = await http.get(f"{_CARD_URL}/1?_={ts}", timeout=20)
    if r1.status_code != 200 or "json" not in r1.headers.get("content-type", ""):
        return {
            "net_worth": net_worth,
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "error": f"Card 1 (accounts) returned {r1.status_code}",
        }

    card1 = r1.json().get("Data", {})
    account_groups = card1.get("AccountGroups", [])

    groups = []
    for grp in account_groups:
        title = grp.get("Title", "Unknown")
        accounts = []
        for acct in grp.get("Accounts", []):
            accounts.append({
                "name":        acct.get("AccountName"),
                "balance":     acct.get("Balance"),
                "institution": acct.get("Institution"),
                "type":        acct.get("MajorType"),
                "as_of":       acct.get("AsOfDate", "")[:10],
            })
        group_total = sum(a["balance"] for a in accounts if a["balance"] is not None)
        groups.append({
            "group":    title,
            "total":    group_total,
            "accounts": accounts,
        })

    return {
        "net_worth":         net_worth,
        "total_assets":      total_assets,
        "total_liabilities": total_liab,
        "account_groups":    groups,
        "account_count":     sum(len(g["accounts"]) for g in groups),
    }
