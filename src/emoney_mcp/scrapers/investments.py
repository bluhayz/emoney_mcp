"""Investment holdings, allocation, performance, and transaction scraping."""

import json
import time
from datetime import datetime, timedelta

from ._helpers import _get_card, _INV_URL


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


async def get_asset_allocation(http_session) -> dict:
    """
    Return the portfolio asset allocation breakdown by asset class.

    Pulls the rich AssetAllocation object from GetInvestmentData and
    supplements with any model targets from CardSwitcher card 4.
    """
    ts = int(time.time() * 1000)
    http = await http_session.get_http()

    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}. Session may have expired."}

    data = resp.json()
    total_portfolio = (data.get("Holdings") or 0) + (data.get("Cash") or 0)

    aa = data.get("AssetAllocation") or {}

    classes = []
    for bucket in aa.get("Buckets", []):
        name  = bucket.get("Name") or bucket.get("AssetClass") or "Unknown"
        value = bucket.get("Value") or bucket.get("MarketValue") or 0.0
        pct   = bucket.get("Percent") or bucket.get("Percentage")
        if pct is None and total_portfolio:
            pct = round(value / total_portfolio * 100, 2)
        sub_classes = []
        for sub in bucket.get("SubBuckets", []) or bucket.get("Children", []):
            sv  = sub.get("Value") or sub.get("MarketValue") or 0.0
            sp  = sub.get("Percent") or sub.get("Percentage")
            if sp is None and total_portfolio:
                sp = round(sv / total_portfolio * 100, 2)
            sub_classes.append({
                "name":    sub.get("Name") or sub.get("AssetClass"),
                "value":   sv,
                "percent": sp,
            })
        classes.append({
            "name":        name,
            "value":       value,
            "percent":     pct,
            "sub_classes": sub_classes,
        })

    card4 = await _get_card(http, 4)
    model_target = None
    if card4:
        model_target = card4.get("ModelName") or card4.get("TargetName")
        if not classes:
            for item in card4.get("AssetClasses", []) or card4.get("Allocations", []) or []:
                name  = item.get("Name") or item.get("AssetClass")
                value = item.get("Value") or item.get("MarketValue") or 0.0
                pct   = item.get("Percent") or item.get("Percentage") or item.get("ActualPercent")
                classes.append({"name": name, "value": value, "percent": pct, "sub_classes": []})

    concentration = []
    for acct in data.get("Accounts", []):
        for h in acct.get("Holdings", []):
            val = h.get("Value") or 0
            if val and total_portfolio:
                concentration.append({
                    "ticker":      h.get("Ticker") or "",
                    "description": (h.get("Description") or "")[:40],
                    "value":       val,
                    "percent":     round(val / total_portfolio * 100, 2),
                })
    concentration.sort(key=lambda x: x["value"], reverse=True)

    return {
        "total_portfolio_value": total_portfolio,
        "asset_classes":         classes,
        "model_target":          model_target,
        "top_10_holdings":       concentration[:10],
        "note": (
            "asset_classes shows allocation by asset type. "
            "top_10_holdings shows largest single-stock concentration risks."
        ),
    }


async def get_net_worth_history(http_session, months: int = 12) -> dict:
    """
    Return monthly net worth trend for the last `months` months (default 12).

    Card 8 returns a bare History array of net worth values (newest last) plus
    ChangeThisMonth.  We label each point "Month N" since the API does not
    return dates alongside the values.
    """
    months = min(max(months, 1), 60)
    http = await http_session.get_http()

    card8 = await _get_card(http, 8)
    if not card8:
        return {"error": "Could not retrieve net worth history (Card 8 unavailable)."}

    raw_history = card8.get("History") or []
    current_nw  = card8.get("NetWorth")
    mtd         = card8.get("ChangeThisMonth") or {}
    ytd         = card8.get("ChangeThisYear")  or {}

    raw_history = raw_history[-months:]

    now = datetime.now()
    points = []
    total  = len(raw_history)
    for i, val in enumerate(raw_history):
        months_ago = total - 1 - i
        dt = (now.replace(day=1) - timedelta(days=months_ago * 28)).replace(day=1)
        points.append({
            "month":     dt.strftime("%Y-%m"),
            "net_worth": val,
        })

    change_dollar = None
    change_pct    = None
    if len(points) >= 2:
        first = points[0]["net_worth"] or 0
        last  = points[-1]["net_worth"] or 0
        if first:
            change_dollar = round(last - first, 2)
            change_pct    = round((last - first) / first * 100, 2)

    return {
        "current_net_worth":    current_nw,
        "months_shown":         len(points),
        "change_over_period":   {"dollar": change_dollar, "percent": change_pct},
        "this_month":           {
            "change_dollar":  mtd.get("Change"),
            "change_percent": round((mtd.get("ChangePercent") or 0) * 100, 2),
        },
        "this_year":            {
            "change_dollar":  ytd.get("Change"),
            "change_percent": round((ytd.get("ChangePercent") or 0) * 100, 2) if ytd.get("ChangePercent") else None,
        } if ytd else None,
        "history":              points,
    }


async def get_performance(http_session) -> dict:
    """
    Return portfolio performance across available time periods.

    Card 3 → investment portfolio value + today's change (dollar + %)
    Card 11 → net worth change this month (dollar + %)
    """
    http = await http_session.get_http()

    card3  = await _get_card(http, 3)
    card11 = await _get_card(http, 11)

    if not card3 and not card11:
        return {"error": "Could not retrieve performance data from Emoney."}

    result: dict = {}

    if card3:
        vc = card3.get("ValueChange") or {}
        inv_history = card3.get("History") or []
        current_inv = vc.get("CurrentValue")
        result["investment_portfolio"] = {
            "current_value":      current_inv,
            "today_change_dollar":  round(vc.get("Change") or 0, 2),
            "today_change_percent": round((vc.get("ChangePercent") or 0) * 100, 2),
        }
        if inv_history and current_inv:
            periods = []
            labels = [("1 month ago", -2), ("3 months ago", -4), ("5 months ago", -6)]
            for label, idx in labels:
                try:
                    past_val = inv_history[idx]
                    if past_val:
                        chg = current_inv - past_val
                        pct = round(chg / past_val * 100, 2)
                        periods.append({
                            "period":         label,
                            "change_dollars": round(chg, 2),
                            "change_percent": pct,
                            "from_value":     past_val,
                            "to_value":       current_inv,
                        })
                except IndexError:
                    pass
            result["investment_portfolio"]["history_periods"] = periods

    if card11:
        nw = card11.get("NetWorth")
        mtd = card11.get("ChangeThisMonth") or {}
        ytd = card11.get("ChangeThisYear")  or {}
        result["net_worth"] = {
            "current_value": nw,
            "this_month": {
                "change_dollar":  round(mtd.get("Change") or 0, 2),
                "change_percent": round((mtd.get("ChangePercent") or 0) * 100, 2),
            },
        }
        if ytd and ytd.get("Change") is not None:
            result["net_worth"]["this_year"] = {
                "change_dollar":  round(ytd.get("Change") or 0, 2),
                "change_percent": round((ytd.get("ChangePercent") or 0) * 100, 2),
            }

    return result


# Transaction array column indices from GetInvestmentTransactions
_TX_COLS = {
    "date_ms":     0,
    "type":        1,
    "ticker":      2,
    "description": 3,
    "amount":      4,
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
    from ._helpers import _INV_URL
    days = min(max(days, 1), 365)
    end_dt   = datetime.now()
    start_dt = end_dt - timedelta(days=days)
    start_str = start_dt.strftime("%m/%d/%Y")
    end_str   = end_dt.strftime("%m/%d/%Y")

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

    resp = await http.post(
        f"{_INV_URL}/GetInvestmentTransactions",
        headers=headers,
        data=json.dumps(body).encode(),
        timeout=30,
    )

    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentTransactions returned {resp.status_code}."}

    data = resp.json()
    rows = data.get("aaData", [])

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

    transactions.sort(key=lambda x: x["date"], reverse=True)

    return {
        "start_date":        start_str,
        "end_date":          end_str,
        "transaction_count": len(transactions),
        "transactions":      transactions,
    }


async def get_capital_gains(http_session, year: int | None = None) -> dict:
    """
    Summarize realized capital gains/losses from transaction history.

    Fetches all sell transactions for the given year (default: current year).
    Source: POST /ema/CS/Investments/GetInvestmentTransactions
    """
    now = datetime.now()
    if year is None:
        year = now.year

    start_str = f"01/01/{year}"
    end_str   = f"12/31/{year}" if year < now.year else now.strftime("%m/%d/%Y")
    days      = (datetime.strptime(end_str, "%m/%d/%Y") - datetime.strptime(start_str, "%m/%d/%Y")).days + 1

    txns_result = await get_transactions(http_session, days=days)
    if "error" in txns_result:
        return txns_result

    sales = [t for t in txns_result.get("transactions", []) if t.get("type", "").lower() in ("sell", "sold", "redemption")]
    dividends   = [t for t in txns_result.get("transactions", []) if "dividend" in t.get("type", "").lower()]
    interest    = [t for t in txns_result.get("transactions", []) if "interest" in t.get("type", "").lower()]

    total_proceeds  = sum(abs(t.get("amount") or 0) for t in sales)
    total_dividends = sum(abs(t.get("amount") or 0) for t in dividends)
    total_interest  = sum(abs(t.get("amount") or 0) for t in interest)

    return {
        "year":              year,
        "start_date":        start_str,
        "end_date":          end_str,
        "sell_transactions": len(sales),
        "total_proceeds":    round(total_proceeds, 2),
        "total_dividends":   round(total_dividends, 2),
        "total_interest":    round(total_interest, 2),
        "sales_detail":      [
            {
                "date":        t["date"],
                "ticker":      t.get("ticker") or "",
                "description": (t.get("description") or "")[:50],
                "proceeds":    abs(t.get("amount") or 0),
            }
            for t in sorted(sales, key=lambda x: x.get("date") or "", reverse=True)
        ],
        "note": (
            "Proceeds shown from sell transactions. Net gain/loss requires cost basis "
            "data which is available in get_holdings per position. Short-term vs. "
            "long-term split requires holding period data not currently exposed by the API."
        ),
    }
