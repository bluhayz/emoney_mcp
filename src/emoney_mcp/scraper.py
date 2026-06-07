"""Scraping logic — calls Emoney's internal JSON APIs.

Endpoints used
--------------
CardSwitcher/GetCard/9  → net worth, total assets, total liabilities
CardSwitcher/GetCard/1  → account groups with balances
Investments/GetInvestmentData          → per-account holdings (positions)
Investments/GetInvestmentTransactions  → transaction history (POST, needs CSRF)

This module is hot-reloaded on every tool call so changes take
effect without restarting the MCP server.
"""

import os
import time
from datetime import datetime, timedelta

_SUBDOMAIN = os.getenv("EMONEY_SUBDOMAIN", "wealth")
BASE_URL = f"https://{_SUBDOMAIN}.emaplan.com"
_CARD_URL = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"
_INV_URL  = f"{BASE_URL}/ema/CS/Investments"


# ---------------------------------------------------------------------------
# Accounts + net worth  (existing)
# ---------------------------------------------------------------------------

async def get_accounts(http_session) -> dict:
    """Fetch net worth and all accounts via the CardSwitcher API."""
    ts = int(time.time() * 1000)
    http = await http_session.get_http()

    r9 = await http.get(f"{_CARD_URL}/9?_={ts}", timeout=20)
    if r9.status_code != 200 or "json" not in r9.headers.get("content-type", ""):
        return {"error": f"Card 9 returned {r9.status_code}. Session may have expired — call reset_session."}

    nw_data = r9.json().get("Data", {})
    net_worth    = nw_data.get("NetWorth")
    total_assets = nw_data.get("Assets")
    total_liab   = nw_data.get("Liabilities")

    r1 = await http.get(f"{_CARD_URL}/1?_={ts}", timeout=20)
    if r1.status_code != 200 or "json" not in r1.headers.get("content-type", ""):
        return {
            "net_worth": net_worth,
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "error": f"Card 1 (accounts) returned {r1.status_code}",
        }

    card1 = r1.json().get("Data", {})
    groups = []
    for grp in card1.get("AccountGroups", []):
        accounts = []
        for acct in grp.get("Accounts", []):
            accounts.append({
                "name":        acct.get("AccountName"),
                "balance":     acct.get("Balance"),
                "institution": acct.get("Institution"),
                "type":        acct.get("MajorType"),
                "as_of":       (acct.get("AsOfDate") or "")[:10],
            })
        groups.append({
            "group":    grp.get("Title", "Unknown"),
            "total":    sum(a["balance"] for a in accounts if a["balance"] is not None),
            "accounts": accounts,
        })

    return {
        "net_worth":         net_worth,
        "total_assets":      total_assets,
        "total_liabilities": total_liab,
        "account_groups":    groups,
        "account_count":     sum(len(g["accounts"]) for g in groups),
    }


# ---------------------------------------------------------------------------
# Holdings (investment positions)
# ---------------------------------------------------------------------------

async def get_holdings(http_session) -> dict:
    """
    Return all investment positions across every account that has holdings.
    Source: GET /ema/CS/Investments/GetInvestmentData
    """
    ts = int(time.time() * 1000)
    http = await http_session.get_http()

    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}. Session may have expired."}

    data = resp.json()

    portfolio_value = data.get("Holdings")   # total holdings value (excl. cash)
    portfolio_cash  = data.get("Cash")
    as_of_raw       = data.get("AsOf") or ""

    accounts_out = []
    total_gain_loss = 0.0

    for acct in data.get("Accounts", []):
        holdings = acct.get("Holdings", [])
        if not holdings:
            continue

        positions = []
        acct_gain_loss = 0.0
        for h in holdings:
            value      = h.get("Value") or 0.0
            cost_basis = h.get("CostBasis")
            gain_loss  = (value - cost_basis) if cost_basis is not None else None
            if gain_loss is not None:
                acct_gain_loss += gain_loss

            positions.append({
                "ticker":      h.get("Ticker") or "",
                "description": h.get("Description") or "",
                "units":       h.get("Units"),
                "price":       h.get("Price"),
                "value":       value,
                "cost_basis":  cost_basis,
                "gain_loss":   round(gain_loss, 2) if gain_loss is not None else None,
            })

        total_gain_loss += acct_gain_loss
        accounts_out.append({
            "account":           acct.get("Name"),
            "institution":       acct.get("InstitutionName"),
            "current_value":     acct.get("CurrentValue"),
            "cash":              acct.get("Cash"),
            "holdings_value":    acct.get("HoldingsValue"),
            "gain_loss":         round(acct_gain_loss, 2) if acct_gain_loss else None,
            "has_transactions":  acct.get("HasInvestmentTransactions", False),
            "positions":         positions,
        })

    return {
        "portfolio_holdings_value": portfolio_value,
        "portfolio_cash":           portfolio_cash,
        "total_unrealized_gain_loss": round(total_gain_loss, 2),
        "investment_accounts":      accounts_out,
        "account_count":            len(accounts_out),
        "position_count":           sum(len(a["positions"]) for a in accounts_out),
    }


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

# Transaction array column indices from GetInvestmentTransactions
_TX_COLS = {
    "date_ms":     0,
    "type":        1,   # "Buy", "Sell", "Other", "Dividend", etc.
    "ticker":      2,
    "description": 3,
    "amount":      4,   # positive = credit/sale, negative = debit/purchase
    "cusip":       5,
    "tx_id":       14,
    "conn_acct":   15,
}

async def get_transactions(http_session, days: int = 30, account_id: str | None = None) -> dict:
    """
    Return investment transactions for the last `days` days (default 30, max 365).
    Optionally filter by account_id (Emoney AccountID GUID).
    Source: POST /ema/CS/Investments/GetInvestmentTransactions  (requires CSRF token)
    """
    days = min(max(days, 1), 365)
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    start_str = start_dt.strftime("%m/%d/%Y")
    end_str   = end_dt.strftime("%m/%d/%Y")

    # Get CSRF token (fetches /ema/CS/Investments if not cached)
    token = await http_session.get_csrf_token()
    if not token:
        return {"error": "Could not obtain CSRF token from Emoney Investments page."}

    http = await http_session.get_http()
    headers = {
        "X-Requested-With":          "XMLHttpRequest",
        "Accept":                     "application/json, text/javascript, */*; q=0.01",
        "Content-Type":               "application/json",
        "Referer":                    f"{_INV_URL}/Transactions?startDate={start_str}&endDate={end_str}",
        "__RequestVerificationToken": token,
    }
    body = {
        "AccountID":       account_id,
        "StartDate":       start_str,
        "EndDate":         end_str,
        "TransactionType": "",
        "Search":          None,
    }

    import json as _json
    resp = await http.post(
        f"{_INV_URL}/GetInvestmentTransactions",
        headers=headers,
        data=_json.dumps(body).encode(),
        timeout=30,
    )

    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentTransactions returned {resp.status_code}."}

    data = resp.json()
    rows = data.get("aaData", [])
    total = data.get("Total", len(rows))

    transactions = []
    for row in rows:
        if len(row) < 5:
            continue
        date_ms = row[_TX_COLS["date_ms"]]
        date_str = datetime.fromtimestamp(date_ms / 1000).strftime("%Y-%m-%d") if date_ms else ""
        amount = row[_TX_COLS["amount"]]
        transactions.append({
            "date":        date_str,
            "type":        row[_TX_COLS["type"]],
            "ticker":      row[_TX_COLS["ticker"]],
            "description": row[_TX_COLS["description"]],
            "amount":      amount,
        })

    # Sort newest first
    transactions.sort(key=lambda x: x["date"], reverse=True)

    return {
        "start_date":        start_str,
        "end_date":          end_str,
        "transaction_count": len(transactions),
        "transactions":      transactions,
    }
