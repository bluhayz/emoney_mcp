"""Scraping logic — calls Emoney's internal JSON APIs.

Endpoints used
--------------
CardSwitcher/GetCard/9   → net worth, total assets, total liabilities
CardSwitcher/GetCard/1   → account groups with balances
CardSwitcher/GetCard/3   → portfolio value change / performance
CardSwitcher/GetCard/4   → asset allocation model summary
CardSwitcher/GetCard/8   → net worth history (monthly trend)
Investments/GetInvestmentData          → per-account holdings + asset allocation + history
Investments/GetInvestmentTransactions  → transaction history (POST, needs CSRF)
CS/Spending/GetSpendingData            → spending by category

This module is hot-reloaded on every tool call so changes take
effect without restarting the MCP server.
"""

import json as _json
import os
import time
from datetime import datetime, timedelta

_SUBDOMAIN = os.getenv("EMONEY_SUBDOMAIN", "wealth")
BASE_URL = f"https://{_SUBDOMAIN}.emaplan.com"
_CARD_URL = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"
_INV_URL  = f"{BASE_URL}/ema/CS/Investments"
_SPEND_URL = f"{BASE_URL}/ema/CS/Spending"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
                "id":          acct.get("AccountID"),
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
# Asset Allocation
# ---------------------------------------------------------------------------

async def get_asset_allocation(http_session) -> dict:
    """
    Return the portfolio asset allocation breakdown by asset class.

    Pulls the rich AssetAllocation object from GetInvestmentData and
    supplements with any model targets from CardSwitcher card 4.

    Returns a breakdown by major class (Equities, Fixed Income, Cash, etc.)
    and, where available, by sub-class (Large Cap Growth, etc.).
    """
    ts = int(time.time() * 1000)
    http = await http_session.get_http()

    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}. Session may have expired."}

    data = resp.json()
    total_portfolio = (data.get("Holdings") or 0) + (data.get("Cash") or 0)

    # --- Parse AssetAllocation from GetInvestmentData ---
    aa = data.get("AssetAllocation") or {}

    # Try top-level buckets
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

    # If GetInvestmentData didn't have Buckets, try CardSwitcher card 4
    card4 = await _get_card(http, 4)
    model_target = None
    if card4:
        model_target = card4.get("ModelName") or card4.get("TargetName")
        # If no classes from GetInvestmentData, try to extract from card 4
        if not classes:
            for item in card4.get("AssetClasses", []) or card4.get("Allocations", []) or []:
                name  = item.get("Name") or item.get("AssetClass")
                value = item.get("Value") or item.get("MarketValue") or 0.0
                pct   = item.get("Percent") or item.get("Percentage") or item.get("ActualPercent")
                classes.append({"name": name, "value": value, "percent": pct, "sub_classes": []})

    # Build concentration risk: top positions by value
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
    top_holdings = concentration[:10]

    return {
        "total_portfolio_value": total_portfolio,
        "asset_classes":         classes,
        "model_target":          model_target,
        "top_10_holdings":       top_holdings,
        "note": (
            "asset_classes shows allocation by asset type. "
            "top_10_holdings shows largest single-stock concentration risks."
        ),
    }


# ---------------------------------------------------------------------------
# Net Worth History
# ---------------------------------------------------------------------------

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

    # The History array is bare floats, newest last
    # Trim to requested months
    raw_history = raw_history[-months:]

    # Build labelled points — we know the last entry is current month
    now = datetime.now()
    points = []
    total  = len(raw_history)
    for i, val in enumerate(raw_history):
        # Work backwards from current month
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


# ---------------------------------------------------------------------------
# Portfolio Performance
# ---------------------------------------------------------------------------

async def get_performance(http_session) -> dict:
    """
    Return portfolio performance across available time periods.

    Card 3 → investment portfolio value + today's change (dollar + %)
    Card 11 → net worth change this month (dollar + %)
    Card 8 → net worth change this month + history for trend
    """
    http = await http_session.get_http()

    card3  = await _get_card(http, 3)
    card11 = await _get_card(http, 11)

    if not card3 and not card11:
        return {"error": "Could not retrieve performance data from Emoney."}

    result: dict = {}

    # --- Investment portfolio (Card 3) ---
    if card3:
        vc = card3.get("ValueChange") or {}
        inv_history = card3.get("History") or []
        current_inv = vc.get("CurrentValue")
        result["investment_portfolio"] = {
            "current_value":      current_inv,
            "today_change_dollar":  round(vc.get("Change") or 0, 2),
            "today_change_percent": round((vc.get("ChangePercent") or 0) * 100, 2),
        }
        # Compute period returns from monthly history
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

    # --- Net worth (Card 11) ---
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


# ---------------------------------------------------------------------------
# Spending
# ---------------------------------------------------------------------------

async def get_spending(http_session, months: int = 1) -> dict:
    """
    Return cash flow summary and recent transactions for the last 30 days.

    Source: CardSwitcher/GetCard/13 — contains Income, Expenses, Net cash flow
    and the 5 most recent spending transactions.

    Note: Emoney's spending category breakdown is only available through the
    SPA (JavaScript-rendered pages) and is not exposed as a JSON API endpoint.
    """
    http = await http_session.get_http()

    card13 = await _get_card(http, 13)
    if not card13:
        return {"error": "Could not retrieve spending data (Card 13 unavailable). Session may have expired."}

    cf  = card13.get("CashFlow")   or {}
    bud = card13.get("Budget")     or {}
    rt  = card13.get("RecentTransactions") or {}

    # Date range is embedded in DataSourceRoute
    dr = cf.get("DataSourceRoute") or {}
    rvd = dr.get("RouteValueDictionary") or {}
    period_start = rvd.get("startDate", "")
    period_end   = rvd.get("endDate",   "")

    # Recent transactions
    transactions = []
    for tx in rt.get("Transactions") or []:
        transactions.append({
            "date":        (tx.get("Date") or "")[:10],
            "description": tx.get("Description") or "",
            "category":    tx.get("Category"),
            "amount":      tx.get("Amount"),
        })

    income   = cf.get("Income")
    expenses = cf.get("Expenses")
    net      = cf.get("Net")

    # Calculate savings rate if both are available
    savings_rate = None
    if income and expenses and income > 0:
        savings_rate = round((income - abs(expenses)) / income * 100, 1)

    return {
        "period":             f"{period_start} to {period_end}" if period_start else "last 30 days",
        "income":             income,
        "expenses":           expenses,
        "net_cash_flow":      net,
        "savings_rate_pct":   savings_rate,
        "budget_set":         (bud.get("Budgeted") or 0) > 0,
        "recent_transactions": transactions,
        "note": (
            "Income and expenses cover all linked bank and credit card accounts. "
            "Category breakdown requires browsing the Emoney Spending page directly."
        ),
    }


# ---------------------------------------------------------------------------
# Financial Goals
# ---------------------------------------------------------------------------

async def get_goals(http_session) -> dict:
    """
    Return financial goals and their funding status from Emoney's plan.

    Source: CardSwitcher/GetCard/2 — contains Goals[] with PercentFunded,
    TotalCost, TotalFunding, and projected dates for each goal.
    """
    http = await http_session.get_http()
    card2 = await _get_card(http, 2)
    if not card2:
        return {"error": "Could not retrieve goals data (Card 2 unavailable). Session may have expired."}

    goals_raw = card2.get("Goals") or []
    goals = []
    for g in goals_raw:
        proj = g.get("Projection") or {}
        goals.append({
            "name":             g.get("Name"),
            "type":             g.get("SubTypeName") or _goal_type_label(g.get("ClientGoalInfoType")),
            "start_year":       g.get("StartYear"),
            "end_year":         g.get("EndYear"),
            "duration":         g.get("Duration"),
            "percent_funded":   proj.get("PercentFunded"),
            "total_cost":       proj.get("TotalCost"),
            "total_funding":    proj.get("TotalFunding"),
            "funding_summary":  proj.get("ProjectedFundingText"),
            "on_track":         (proj.get("PercentFunded") or 0) >= 100,
        })

    # Separate retirement from spending goals
    retirement = [g for g in goals if g["name"] == "Retirement" or g.get("type") == "Retirement"]
    spending   = [g for g in goals if g not in retirement]

    return {
        "goal_count":       len(goals),
        "all_on_track":     all(g["on_track"] for g in goals),
        "retirement_goals": retirement,
        "spending_goals":   spending,
    }


def _goal_type_label(type_int) -> str:
    return {0: "Education", 1: "Retirement", 2: "Other Spending"}.get(type_int, "Unknown")


# ---------------------------------------------------------------------------
# Capital Gains Summary
# ---------------------------------------------------------------------------

async def get_capital_gains(http_session, year: int | None = None) -> dict:
    """
    Summarize realized capital gains/losses from transaction history.

    Fetches all sell transactions for the given year (default: current year)
    and computes realized gain/loss split into short-term vs. long-term
    (approximated — Emoney doesn't always expose holding period per transaction).

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


# ---------------------------------------------------------------------------
# Retirement Accounts
# ---------------------------------------------------------------------------

async def get_retirement_accounts(http_session) -> dict:
    """
    Aggregate all tax-advantaged retirement and savings accounts.

    Filters account groups for retirement account types (401k, IRA, Roth,
    Annuity, HSA, 403b, SEP, SIMPLE) and returns a summary with totals.
    """
    result = await get_accounts(http_session)
    if "error" in result:
        return result

    _RETIREMENT_KEYWORDS = {
        "401", "ira", "roth", "annuit", "hsa", "403", "sep", "simple",
        "pension", "retirement", "deferred comp", "529", "education",
    }

    retirement_accounts = []
    taxable_accounts    = []
    debt_accounts       = []

    for group in result.get("account_groups", []):
        for acct in group.get("accounts", []):
            name_lower = (acct.get("name") or "").lower()
            type_lower = (acct.get("type") or "").lower()
            combined   = name_lower + " " + type_lower

            bal = acct.get("balance") or 0
            entry = {
                "name":        acct.get("name"),
                "institution": acct.get("institution"),
                "type":        acct.get("type"),
                "balance":     bal,
                "group":       group.get("group"),
                "as_of":       acct.get("as_of"),
            }

            if bal < 0:
                debt_accounts.append(entry)
            elif any(kw in combined for kw in _RETIREMENT_KEYWORDS):
                retirement_accounts.append(entry)
            else:
                taxable_accounts.append(entry)

    retirement_accounts.sort(key=lambda x: x["balance"], reverse=True)

    total_retirement = sum(a["balance"] for a in retirement_accounts)
    total_taxable    = sum(a["balance"] for a in taxable_accounts)

    # Sub-totals by account type keyword
    def _bucket(accounts, keywords):
        return sum(a["balance"] for a in accounts
                   if any(kw in (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()
                          for kw in keywords))

    return {
        "total_retirement_assets": round(total_retirement, 2),
        "total_taxable_assets":    round(total_taxable, 2),
        "retirement_breakdown": {
            "401k_403b":    round(_bucket(retirement_accounts, ["401", "403"]), 2),
            "ira_roth":     round(_bucket(retirement_accounts, ["ira", "roth"]), 2),
            "annuities":    round(_bucket(retirement_accounts, ["annuit"]), 2),
            "hsa":          round(_bucket(retirement_accounts, ["hsa"]), 2),
            "education_529":round(_bucket(retirement_accounts, ["529", "education"]), 2),
            "other":        round(_bucket(retirement_accounts, ["pension", "sep", "simple", "deferred"]), 2),
        },
        "retirement_accounts": retirement_accounts,
        "note": (
            "Retirement assets are identified by keyword matching on account name/type. "
            "Review the list to confirm correct categorization for your accounts."
        ),
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
