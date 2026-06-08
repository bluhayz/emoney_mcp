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
CS/CardSwitcher/GetCard/13             → cash flow summary (income / expenses / net)
SNB API (api.emoneyadvisor.com/snb-api)
  api/values/GetFilteredTransactions   → bank/CC transactions with categoryId
  api/values/GetCategories             → 114 category names (id → name)

This module is hot-reloaded on every tool call so changes take
effect without restarting the MCP server.
"""

import json as _json
import os
import time
from datetime import datetime, timedelta

import re as _re

_SUBDOMAIN = os.getenv("EMONEY_SUBDOMAIN", "wealth")
BASE_URL = f"https://{_SUBDOMAIN}.emaplan.com"
_CARD_URL  = f"{BASE_URL}/ema/CS/CardSwitcher/GetCard"
_INV_URL   = f"{BASE_URL}/ema/CS/Investments"
_SPEND_URL = f"{BASE_URL}/ema/CS/Spending"
_SNB_API   = "https://api.emoneyadvisor.com/snb-api"


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


# ---------------------------------------------------------------------------
# Spending transactions with categories  (SNB API)
# ---------------------------------------------------------------------------

# Known US state + territory abbreviations — only these are stripped as trailing tokens
_US_STATES = frozenset({
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","GU","VI",
})

# Common POS / payment-system prefixes to strip from transaction descriptions
_POS_PREFIXES = _re.compile(
    r"^(?:APLPAY\s+|SQ\s*\*\s*|TST\*?\s+|PP\*\s*|PAYPAL\s*\*\s*|SP\s+|"
    r"AMZN\s+MKTP\s+US\*?\s*|GOOGLE\s*\*\s*|APPLE\.COM/\s*)",
    _re.IGNORECASE,
)

# Trailing asterisk transaction reference codes like  *XYZ123
_ASTERISK_REF = _re.compile(r"\*[A-Z0-9]{4,}$")

# Store / transaction reference numbers like  #1234  or  1234567
_STORE_NUMBER = _re.compile(r"\s+\#?\d{4,}$")

# ZIP codes at end: " 20166" or " 20166-1234"
_ZIP_CODE = _re.compile(r"\s+\d{5}(?:-\d{4})?$")

# Categories that represent internal financial flows, not real merchant spending
_NON_MERCHANT_CATEGORIES = {
    "Transfers", "Credit Card Payment", "Paycheck/Salary",
    "Income", "ACH Transfer", "Internal Transfer", "Investment",
    "Dividend & Cap Gains", "Interest Income",
}


def _normalize_merchant(raw: str) -> str:
    """
    Normalize a transaction description into a stable merchant key so that
    location-suffix variants of the same merchant group together.

    Strategy (safe only — avoids stripping actual merchant name words):
      1. Strip known POS / payment-network prefixes (APLPAY, SQ *, TST, etc.)
      2. Strip trailing asterisk ref codes (*XYZ123)
      3. Strip trailing ZIP codes and store reference numbers
      4. Strip trailing "US" / "USA" country suffix
      5. Strip trailing KNOWN US state abbreviation (from a fixed set)
         — after stripping the state, also strip the preceding word if it
           looks like a city (all-caps, 3–12 letters, not at start of string)

    Deliberately does NOT try to guess city names from a general pattern,
    which caused false positives like "FOOD LION" → "FOOD".

    Examples
    --------
    "APLPAY FOOD LION VA"                → "FOOD LION"
    "COSTCO WHSE STERLING US"            → "COSTCO WHSE"
    "COSTCO WHSE RESTON VA"              → "COSTCO WHSE"
    "UNITED AIRLINES HOUSTON TX"         → "UNITED AIRLINES"
    "TST AUSTIN GRILL VA"                → "AUSTIN GRILL"
    "SQ *BLUE BOTTLE COFFEE"             → "BLUE BOTTLE COFFEE"
    "ANTHROPIC CLAUDE SUB SAN FRANCISCO CA" → "ANTHROPIC CLAUDE SUB"
    "WAYMO CA"                           → "WAYMO"
    """
    s = raw.strip().upper()

    # 1. Strip POS / payment-network prefixes
    s = _POS_PREFIXES.sub("", s).strip()

    # 2. Strip asterisk ref codes
    s = _ASTERISK_REF.sub("", s).strip()

    # 3. Strip ZIP codes and store numbers (up to 2 passes)
    for _ in range(2):
        prev = s
        s = _ZIP_CODE.sub("", s).strip()
        s = _STORE_NUMBER.sub("", s).strip()
        if s == prev:
            break

    # Business/merchant words that should NEVER be treated as city names
    _NOT_CITY = frozenset({
        "TIMES", "MARKET", "MARKETS", "STORE", "STORES", "SHOP", "SHOPS",
        "PLACE", "PLAZA", "PARK", "SQUARE", "CENTER", "CENTRE", "POINT",
        "GROUP", "CORP", "INC", "LLC", "LTD", "AUTO", "HOME", "CARE",
        "HEALTH", "CLUB", "GYM", "CAFE", "GRILL", "BAR", "PUB",
    })

    def _strip_city_after_location(parts: list[str]) -> list[str]:
        """
        Strip one trailing city-name word after a state/country has been removed.
        Keeps at least 2 words and never strips known business/merchant words.
        """
        if len(parts) >= 3:
            candidate = parts[-1]
            if (candidate.isalpha()
                    and 3 <= len(candidate) <= 12
                    and candidate not in _NOT_CITY):
                parts = parts[:-1]
        return parts

    # 4. Strip trailing "US" or "USA" country suffix, then strip trailing city word(s)
    if s.endswith(" US") or s.endswith(" USA"):
        s = s.rsplit(" ", 1)[0].strip()
        parts = _strip_city_after_location(s.split())
        s = " ".join(parts)

    # 5a. Strip trailing known US state abbreviation
    parts = s.split()
    if len(parts) >= 2 and parts[-1] in _US_STATES:
        parts = parts[:-1]
        # 5b. Strip trailing city word(s) after state removal
        parts = _strip_city_after_location(parts)
        s = " ".join(parts)

    # Collapse multiple spaces
    s = _re.sub(r"\s{2,}", " ", s)

    return s or raw.upper()


# Income-generating categories (credits into the account)
_INCOME_CATEGORIES = frozenset({
    "Paycheck/Salary", "Income", "Dividend & Cap Gains", "Interest Income",
    "ACH Transfer",   # often direct deposit — treated as income
})

# Pure internal flows — exclude from both income and spending
_EXCLUDE_CATEGORIES = frozenset({
    "Transfers", "Credit Card Payment", "Internal Transfer",
})


async def _get_snb_credentials(http_session) -> tuple[str, str]:
    """Extract JWT token and API key from the Spending/Transactions page HTML."""
    http = await http_session.get_http()
    resp = await http.get(f"{BASE_URL}/ema/CS/Spending/Transactions", timeout=20)
    html = resp.text
    jwt_match = _re.search(r'"JwtToken"\s*:\s*"([^"]+)"', html)
    key_match  = _re.search(r'apiKey["\']?\s*:\s*["\']([^"\']+)["\']', html)
    jwt_token = jwt_match.group(1) if jwt_match else ""
    api_key   = key_match.group(1)  if key_match  else ""
    return jwt_token, api_key


async def get_spending_transactions(http_session, days: int = 30) -> dict:
    """
    Return bank/credit card transactions with category labels for the last `days` days.

    Source: SNB API (api.emoneyadvisor.com/snb-api)
      GET api/values/GetFilteredTransactions  → transactions with categoryId
      GET api/values/GetCategories            → 114 category names

    The JWT token needed to call the SNB API is extracted from the
    CS/Spending/Transactions page on each call.  Transactions with
    isDeleted=true are excluded; pending transactions are included
    with a flag.
    """
    days = min(max(days, 1), 365)
    jwt_token, api_key = await _get_snb_credentials(http_session)

    if not jwt_token:
        return {"error": "Could not extract JWT token from Spending page. Try re-syncing Chrome session."}

    http = await http_session.get_http()
    snb_headers = {
        "Accept":        "application/json, text/plain, */*",
        "Authorization": f"Bearer {jwt_token}",
        "apikey":        api_key,
        "Origin":        BASE_URL,
    }

    # Fetch category map
    cat_resp = await http.get(f"{_SNB_API}/api/values/GetCategories",
                              headers=snb_headers, timeout=20)
    categories: dict[str, str] = {}
    if cat_resp.status_code == 200 and "json" in cat_resp.headers.get("content-type", ""):
        for cat in cat_resp.json():
            categories[str(cat.get("id", ""))] = cat.get("name", "")

    # Fetch all transactions
    txn_resp = await http.get(f"{_SNB_API}/api/values/GetFilteredTransactions",
                              headers=snb_headers, timeout=30)
    if txn_resp.status_code != 200 or "json" not in txn_resp.headers.get("content-type", ""):
        return {"error": f"GetFilteredTransactions returned {txn_resp.status_code}."}

    all_txns = txn_resp.json()

    # Client-side date filter
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    filtered = [
        t for t in all_txns
        if (t.get("date") or "")[:10] >= cutoff and not t.get("isDeleted", False)
    ]

    # Sort newest first
    filtered.sort(key=lambda t: t.get("date", ""), reverse=True)

    transactions = []
    for t in filtered:
        desc = t.get("userDescription") or t.get("cleanDescription") or t.get("description", "")
        cat_id = str(t.get("categoryId") or "")
        cat_name = categories.get(cat_id, "Uncategorized") if cat_id else "Uncategorized"
        transactions.append({
            "date":        (t.get("date") or "")[:10],
            "description": desc,
            "category":    cat_name,
            "amount":      t.get("value", 0),
            "is_pending":  t.get("isPending", False),
            "is_split":    t.get("isSplit", False),
        })

    # Summarize by category
    cat_totals: dict[str, float] = {}
    for t in transactions:
        cat = t["category"]
        cat_totals[cat] = round(cat_totals.get(cat, 0) + abs(t["amount"]), 2)

    top_categories = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:10]

    # Summarize by merchant (normalized to dedup location variants).
    # Exclude internal financial flows (transfers, payroll, credit card payments)
    # so the list reflects actual spending merchants.
    merchant_data: dict[str, dict] = {}
    for t in transactions:
        if t["category"] in _NON_MERCHANT_CATEGORIES:
            continue
        raw = t["description"]
        key = _normalize_merchant(raw)
        if key not in merchant_data:
            # Use the normalized key as the display name (cleaned-up merchant name)
            merchant_data[key] = {"display": key, "total": 0.0, "count": 0}
        entry = merchant_data[key]
        entry["total"] = round(entry["total"] + abs(t["amount"]), 2)
        entry["count"] += 1

    top_merchants = sorted(merchant_data.values(), key=lambda x: x["total"], reverse=True)[:15]

    cutoff_display = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    return {
        "period_days":       days,
        "start_date":        cutoff_display,
        "end_date":          datetime.now().strftime("%Y-%m-%d"),
        "transaction_count": len(transactions),
        "top_categories":    [{"category": c, "total": v} for c, v in top_categories],
        "top_merchants":     [
            {"merchant": e["display"], "total": e["total"], "transactions": e["count"]}
            for e in top_merchants
        ],
        "transactions":      transactions,
    }


# ---------------------------------------------------------------------------
# Shared SNB fetch helper
# ---------------------------------------------------------------------------

async def _fetch_snb_data(http_session, days: int) -> tuple[list, bool]:
    """
    Fetch and normalize SNB transactions for the last `days` days.
    Returns (transactions, success) where each transaction has:
      date, description, category, amount, is_income, is_pending
    Returns ([], False) on auth failure.
    """
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return [], False

    http = await http_session.get_http()
    snb_headers = {
        "Accept":        "application/json, text/plain, */*",
        "Authorization": f"Bearer {jwt_token}",
        "apikey":        api_key,
        "Origin":        BASE_URL,
    }

    categories: dict[str, str] = {}
    cat_resp = await http.get(f"{_SNB_API}/api/values/GetCategories",
                              headers=snb_headers, timeout=20)
    if cat_resp.status_code == 200 and "json" in cat_resp.headers.get("content-type", ""):
        for cat in cat_resp.json():
            categories[str(cat.get("id", ""))] = cat.get("name", "")

    txn_resp = await http.get(f"{_SNB_API}/api/values/GetFilteredTransactions",
                              headers=snb_headers, timeout=30)
    if txn_resp.status_code != 200 or "json" not in txn_resp.headers.get("content-type", ""):
        return [], False

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    result = []
    for t in txn_resp.json():
        date_str = (t.get("date") or "")[:10]
        if date_str < cutoff or t.get("isDeleted", False):
            continue
        desc = t.get("userDescription") or t.get("cleanDescription") or t.get("description", "")
        cat_id   = str(t.get("categoryId") or "")
        category = categories.get(cat_id, "Uncategorized") if cat_id else "Uncategorized"
        result.append({
            "date":        date_str,
            "description": desc,
            "category":    category,
            "amount":      abs(t.get("value", 0) or 0),
            "is_income":   category in _INCOME_CATEGORIES,
            "is_excluded": category in _EXCLUDE_CATEGORIES,
            "is_pending":  t.get("isPending", False),
        })

    result.sort(key=lambda t: t["date"], reverse=True)
    return result, True


# ---------------------------------------------------------------------------
# Spending trends  (month-over-month by category)
# ---------------------------------------------------------------------------

async def get_spending_trends(http_session, months: int = 3) -> dict:
    """
    Return month-over-month spending trends by category for the last `months` months.

    For each spending category shows monthly totals and whether spending is
    trending up, down, or stable vs. the prior month.  Also returns monthly
    totals for income and expenses so savings rate is visible per month.
    """
    months = min(max(months, 2), 12)
    days   = months * 31 + 5   # a little padding

    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    # Build ordered month labels (oldest → newest)
    now = datetime.now()
    month_labels = []
    for i in range(months - 1, -1, -1):
        dt = (now.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        month_labels.append(dt.strftime("%Y-%m"))

    month_set = set(month_labels)

    # Bucket transactions by month
    monthly_income:   dict[str, float] = {m: 0.0 for m in month_labels}
    monthly_spending: dict[str, float] = {m: 0.0 for m in month_labels}
    # cat_monthly[category][month] = total
    cat_monthly: dict[str, dict[str, float]] = {}

    for t in txns:
        month = t["date"][:7]
        if month not in month_set:
            continue
        if t["is_excluded"]:
            continue
        if t["is_income"]:
            monthly_income[month] = round(monthly_income[month] + t["amount"], 2)
        else:
            monthly_spending[month] = round(monthly_spending[month] + t["amount"], 2)
            cat = t["category"]
            if cat not in cat_monthly:
                cat_monthly[cat] = {m: 0.0 for m in month_labels}
            cat_monthly[cat][month] = round(cat_monthly[cat][month] + t["amount"], 2)

    # Build monthly summary
    monthly_summary = []
    for m in month_labels:
        income   = monthly_income[m]
        spending = monthly_spending[m]
        net      = round(income - spending, 2)
        rate     = round(net / income * 100, 1) if income > 0 else None
        monthly_summary.append({
            "month":            m,
            "income":           round(income, 2),
            "spending":         round(spending, 2),
            "net":              net,
            "savings_rate_pct": rate,
        })

    # Build category trends
    last_month = month_labels[-1]
    prev_month = month_labels[-2] if len(month_labels) >= 2 else None

    category_trends = []
    for cat, by_month in cat_monthly.items():
        total_all = sum(by_month.values())
        if total_all < 1:
            continue
        last  = by_month.get(last_month, 0)
        prev  = by_month.get(prev_month, 0) if prev_month else 0
        if prev > 0:
            pct_change = round((last - prev) / prev * 100, 1)
        else:
            pct_change = None
        if pct_change is not None:
            trend = "up" if pct_change > 10 else ("down" if pct_change < -10 else "stable")
        else:
            trend = "new" if last > 0 else "none"
        category_trends.append({
            "category":          cat,
            "monthly_totals":    [{"month": m, "total": round(by_month[m], 2)} for m in month_labels],
            "this_month":        round(last, 2),
            "prior_month":       round(prev, 2),
            "change_pct":        pct_change,
            "trend":             trend,
        })

    # Sort by this month's spending descending
    category_trends.sort(key=lambda x: x["this_month"], reverse=True)

    biggest_increases = sorted(
        [c for c in category_trends if c["trend"] == "up"],
        key=lambda x: x["change_pct"] or 0, reverse=True
    )[:5]
    biggest_decreases = sorted(
        [c for c in category_trends if c["trend"] == "down"],
        key=lambda x: x["change_pct"] or 0
    )[:5]

    return {
        "months_shown":      months,
        "month_labels":      month_labels,
        "monthly_summary":   monthly_summary,
        "category_trends":   category_trends,
        "biggest_increases": biggest_increases,
        "biggest_decreases": biggest_decreases,
    }


# ---------------------------------------------------------------------------
# Income summary
# ---------------------------------------------------------------------------

async def get_income_summary(http_session, days: int = 90) -> dict:
    """
    Return income sources and monthly income trend for the last `days` days.

    Identifies income transactions by category (Paycheck/Salary, Income,
    ACH Transfer, Dividend & Cap Gains, Interest Income) and groups them
    by normalized source description.
    """
    days = min(max(days, 7), 365)

    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    income_txns = [t for t in txns if t["is_income"]]

    if not income_txns:
        return {
            "period_days": days,
            "total_income": 0,
            "message": "No income transactions found in this period.",
        }

    total_income = round(sum(t["amount"] for t in income_txns), 2)

    # Group by normalized source description
    sources: dict[str, dict] = {}
    for t in income_txns:
        key = _normalize_merchant(t["description"])
        if key not in sources:
            sources[key] = {
                "source":   key,
                "category": t["category"],
                "count":    0,
                "total":    0.0,
                "dates":    [],
            }
        sources[key]["count"] += 1
        sources[key]["total"]  = round(sources[key]["total"] + t["amount"], 2)
        sources[key]["dates"].append(t["date"])

    # Compute average and most-recent date per source
    source_list = []
    for s in sources.values():
        s["average"]     = round(s["total"] / s["count"], 2)
        s["most_recent"] = max(s["dates"])
        del s["dates"]
        source_list.append(s)
    source_list.sort(key=lambda x: x["total"], reverse=True)

    # Monthly income trend
    now = datetime.now()
    months_back = max(1, days // 30)
    month_labels = []
    for i in range(months_back - 1, -1, -1):
        dt = (now.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        month_labels.append(dt.strftime("%Y-%m"))

    monthly: dict[str, float] = {m: 0.0 for m in month_labels}
    month_set = set(month_labels)
    for t in income_txns:
        m = t["date"][:7]
        if m in month_set:
            monthly[m] = round(monthly[m] + t["amount"], 2)

    return {
        "period_days":    days,
        "start_date":     (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
        "end_date":       datetime.now().strftime("%Y-%m-%d"),
        "total_income":   total_income,
        "income_sources": source_list,
        "monthly_income": [{"month": m, "total": monthly[m]} for m in month_labels],
    }


# ---------------------------------------------------------------------------
# Savings rate
# ---------------------------------------------------------------------------

async def get_savings_rate(http_session, months: int = 6) -> dict:
    """
    Return month-by-month savings rate for the last `months` months.

    Savings rate = (income - spending) / income * 100
    Excludes internal transfers, credit card payments, and other non-cash-flow items.
    """
    months = min(max(months, 1), 12)
    days   = months * 31 + 5

    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    now = datetime.now()
    month_labels = []
    for i in range(months - 1, -1, -1):
        dt = (now.replace(day=1) - timedelta(days=i * 28)).replace(day=1)
        month_labels.append(dt.strftime("%Y-%m"))

    month_set = set(month_labels)
    monthly_income:   dict[str, float] = {m: 0.0 for m in month_labels}
    monthly_spending: dict[str, float] = {m: 0.0 for m in month_labels}

    for t in txns:
        m = t["date"][:7]
        if m not in month_set or t["is_excluded"]:
            continue
        if t["is_income"]:
            monthly_income[m]   = round(monthly_income[m]   + t["amount"], 2)
        else:
            monthly_spending[m] = round(monthly_spending[m] + t["amount"], 2)

    monthly_rows = []
    total_income   = 0.0
    total_spending = 0.0
    for m in month_labels:
        inc  = monthly_income[m]
        exp  = monthly_spending[m]
        net  = round(inc - exp, 2)
        rate = round(net / inc * 100, 1) if inc > 0 else None
        total_income   += inc
        total_spending += exp
        monthly_rows.append({
            "month":            m,
            "income":           round(inc, 2),
            "spending":         round(exp, 2),
            "net":              net,
            "savings_rate_pct": rate,
        })

    avg_rate = None
    if total_income > 0:
        avg_rate = round((total_income - total_spending) / total_income * 100, 1)

    return {
        "months_shown":          months,
        "average_savings_rate":  avg_rate,
        "total_income":          round(total_income, 2),
        "total_spending":        round(total_spending, 2),
        "total_net":             round(total_income - total_spending, 2),
        "monthly":               monthly_rows,
        "note": (
            "Income = Paycheck/Salary, Income, ACH Transfer, Dividends, Interest. "
            "Transfers and credit card payments are excluded as internal flows."
        ),
    }


# ---------------------------------------------------------------------------
# Transaction search
# ---------------------------------------------------------------------------

async def search_transactions(
    http_session,
    query: str = "",
    category: str = "",
    days: int = 365,
    min_amount: float = 0.0,
    max_amount: float | None = None,
) -> dict:
    """
    Search spending transactions by keyword, category, and/or amount range.

    Parameters
    ----------
    query       : substring match against transaction description (case-insensitive)
    category    : substring match against category name (e.g. "Grocer", "Dining")
    days        : how far back to search (default 365, max 365)
    min_amount  : only include transactions >= this amount
    max_amount  : only include transactions <= this amount (omit for no upper limit)
    """
    days = min(max(days, 1), 365)
    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    q          = query.strip().upper()
    cat_filter = category.strip().lower()

    results = []
    for t in txns:
        if t["is_excluded"]:
            continue
        if q and q not in t["description"].upper():
            continue
        if cat_filter and cat_filter not in t["category"].lower():
            continue
        if t["amount"] < min_amount:
            continue
        if max_amount is not None and t["amount"] > max_amount:
            continue
        results.append(t)

    total = round(sum(t["amount"] for t in results), 2)

    # Merchant rollup (spending only)
    by_merchant: dict[str, dict] = {}
    for t in results:
        if t["is_income"]:
            continue
        key = _normalize_merchant(t["description"])
        if key not in by_merchant:
            by_merchant[key] = {"merchant": key, "total": 0.0, "count": 0}
        by_merchant[key]["total"] = round(by_merchant[key]["total"] + t["amount"], 2)
        by_merchant[key]["count"] += 1

    merchant_summary = sorted(by_merchant.values(), key=lambda x: x["total"], reverse=True)[:10]

    # Category rollup
    by_cat: dict[str, float] = {}
    for t in results:
        by_cat[t["category"]] = round(by_cat.get(t["category"], 0) + t["amount"], 2)
    category_summary = sorted(
        [{"category": k, "total": v} for k, v in by_cat.items()],
        key=lambda x: x["total"], reverse=True,
    )

    return {
        "query":            query or "(all)",
        "category_filter":  category or "(all)",
        "period_days":      days,
        "start_date":       (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
        "end_date":         datetime.now().strftime("%Y-%m-%d"),
        "match_count":      len(results),
        "total_amount":     total,
        "merchant_summary": merchant_summary,
        "category_summary": category_summary,
        "transactions":     results,
    }


# ---------------------------------------------------------------------------
# Recurring charges / subscription detection
# ---------------------------------------------------------------------------

_CADENCES = [
    ("weekly",     7,   4),
    ("biweekly",   14,  4),
    ("monthly",    30,  6),
    ("quarterly",  91, 10),
    ("annual",    365, 20),
]

_CADENCE_TO_MONTHLY = {
    "weekly":    30 / 7,
    "biweekly":  30 / 14,
    "monthly":   1.0,
    "quarterly": 1 / 3,
    "annual":    1 / 12,
}


async def get_recurring_charges(http_session) -> dict:
    """
    Detect recurring and subscription charges from the last 120 days.

    Groups transactions by normalized merchant name, computes gaps between
    consecutive charges, and identifies those matching known cadences:
    weekly, biweekly, monthly, quarterly, or annual.

    Returns a list of detected recurring charges sorted by monthly cost,
    plus a total estimated monthly recurring spend.
    """
    txns, ok = await _fetch_snb_data(http_session, days=120)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    # Group spending transactions by normalized merchant
    merchant_records: dict[str, list[dict]] = {}
    for t in txns:
        if t["is_excluded"] or t["is_income"] or t["is_pending"]:
            continue
        key = _normalize_merchant(t["description"])
        if key not in merchant_records:
            merchant_records[key] = []
        merchant_records[key].append(t)

    recurring = []
    for merchant, records in merchant_records.items():
        if len(records) < 2:
            continue

        records_sorted = sorted(records, key=lambda r: r["date"])
        dates = [r["date"] for r in records_sorted]
        amounts = [r["amount"] for r in records_sorted]

        # Compute day-gaps between consecutive transactions
        gaps = []
        for i in range(1, len(dates)):
            d1 = datetime.strptime(dates[i - 1], "%Y-%m-%d")
            d2 = datetime.strptime(dates[i],     "%Y-%m-%d")
            gaps.append((d2 - d1).days)

        avg_gap = sum(gaps) / len(gaps)
        avg_amount = sum(amounts) / len(amounts)
        # Consistency: how much do gaps vary from the average?
        gap_variance = sum(abs(g - avg_gap) for g in gaps) / len(gaps)

        for cadence_name, cadence_days, tolerance in _CADENCES:
            if abs(avg_gap - cadence_days) <= tolerance:
                monthly_cost = round(avg_amount * _CADENCE_TO_MONTHLY[cadence_name], 2)
                recurring.append({
                    "merchant":          merchant,
                    "cadence":           cadence_name,
                    "avg_amount":        round(avg_amount, 2),
                    "monthly_cost_est":  monthly_cost,
                    "occurrences":       len(records),
                    "last_charge":       dates[-1],
                    "consistent":        gap_variance < tolerance,
                    "avg_gap_days":      round(avg_gap, 1),
                })
                break

    recurring.sort(key=lambda x: x["monthly_cost_est"], reverse=True)

    total_monthly_est = round(sum(r["monthly_cost_est"] for r in recurring), 2)
    total_annual_est  = round(total_monthly_est * 12, 2)

    by_cadence: dict[str, list] = {}
    for r in recurring:
        by_cadence.setdefault(r["cadence"], []).append(r)

    return {
        "detection_window_days":  120,
        "recurring_count":        len(recurring),
        "total_monthly_est":      total_monthly_est,
        "total_annual_est":       total_annual_est,
        "by_cadence":             {k: v for k, v in by_cadence.items()},
        "all_recurring":          recurring,
        "note": (
            "Detection looks for merchants with 2+ charges at consistent intervals "
            "over the last 120 days. Annual subscriptions may not appear if charged "
            "only once in this window."
        ),
    }


# ---------------------------------------------------------------------------
# Net worth breakdown  (by person / liquidity / tax treatment)
# ---------------------------------------------------------------------------

async def get_net_worth_breakdown(http_session) -> dict:
    """
    Break down net worth by three lenses:
      1. By person   — Drew, Lacey, Joint, or Other
      2. By liquidity — Liquid (cash/checking/savings), Semi-liquid (brokerage),
                        Illiquid (real estate, annuities, options/RSU)
      3. By tax treatment — Taxable, Tax-Deferred (traditional 401k/IRA/annuity),
                            Tax-Free (Roth, HSA, 529)

    Source: get_accounts (CardSwitcher cards 9 + 1)
    """
    accounts_result = await get_accounts(http_session)
    if "error" in accounts_result:
        return accounts_result

    net_worth    = accounts_result["net_worth"]
    total_assets = accounts_result["total_assets"]

    # Flatten all accounts
    all_accts = []
    for group in accounts_result.get("account_groups", []):
        for acct in group["accounts"]:
            all_accts.append({**acct, "group": group["group"]})

    # ── 1. By person ──────────────────────────────────────────────────────
    def _person(name: str) -> str:
        n = (name or "").lower()
        has_drew  = "drew" in n
        has_lacey = "lacey" in n
        has_joint = "joint" in n or "parker" in n  # joint includes kid accounts
        if has_drew and not has_lacey:
            return "Drew"
        if has_lacey and not has_drew:
            return "Lacey"
        if has_joint or (has_drew and has_lacey):
            return "Joint / Family"
        return "Other"

    by_person: dict[str, float] = {}
    for a in all_accts:
        bal = a.get("balance") or 0
        if bal <= 0:
            continue
        p = _person(a["name"])
        by_person[p] = round(by_person.get(p, 0) + bal, 2)

    # ── 2. By liquidity ───────────────────────────────────────────────────
    _LIQUID_TYPES = {"CashAsset"}
    _SEMI_LIQUID_TYPES = {"InvestmentAsset", "TaxFreeRothSavingsAsset",
                           "TaxFree529SavingsAsset", "TaxFreeHealthSavingsAsset",
                           "PreTaxSavingsAsset"}
    _ILLIQUID_TYPES = {"AnnuityAsset", "RealEstateAsset", "OptionPlan",
                        "OtherAsset", "Mortgage"}

    by_liquidity: dict[str, float] = {"Liquid": 0.0, "Semi-liquid": 0.0, "Illiquid": 0.0}
    for a in all_accts:
        bal  = a.get("balance") or 0
        atyp = a.get("type") or ""
        if bal <= 0:
            continue
        if atyp in _LIQUID_TYPES:
            by_liquidity["Liquid"] = round(by_liquidity["Liquid"] + bal, 2)
        elif atyp in _SEMI_LIQUID_TYPES:
            by_liquidity["Semi-liquid"] = round(by_liquidity["Semi-liquid"] + bal, 2)
        else:
            by_liquidity["Illiquid"] = round(by_liquidity["Illiquid"] + bal, 2)

    # ── 3. By tax treatment ───────────────────────────────────────────────
    _TAX_FREE_TYPES = {"TaxFreeRothSavingsAsset", "TaxFree529SavingsAsset",
                        "TaxFreeHealthSavingsAsset"}
    _TAX_DEFERRED_TYPES = {"PreTaxSavingsAsset", "AnnuityAsset"}
    _TAXABLE_TYPES = {"InvestmentAsset", "CashAsset", "RealEstateAsset",
                       "OptionPlan", "OtherAsset"}

    by_tax: dict[str, float] = {"Taxable": 0.0, "Tax-Deferred": 0.0, "Tax-Free": 0.0}
    for a in all_accts:
        bal  = a.get("balance") or 0
        atyp = a.get("type") or ""
        if bal <= 0:
            continue
        if atyp in _TAX_FREE_TYPES:
            by_tax["Tax-Free"] = round(by_tax["Tax-Free"] + bal, 2)
        elif atyp in _TAX_DEFERRED_TYPES:
            by_tax["Tax-Deferred"] = round(by_tax["Tax-Deferred"] + bal, 2)
        else:
            by_tax["Taxable"] = round(by_tax["Taxable"] + bal, 2)

    def _pct(val):
        return round(val / total_assets * 100, 1) if total_assets else 0

    return {
        "net_worth":    net_worth,
        "total_assets": total_assets,
        "by_person": [
            {"person": k, "value": v, "percent": _pct(v)}
            for k, v in sorted(by_person.items(), key=lambda x: x[1], reverse=True)
        ],
        "by_liquidity": [
            {"bucket": k, "value": v, "percent": _pct(v)}
            for k, v in by_liquidity.items()
        ],
        "by_tax_treatment": [
            {"bucket": k, "value": v, "percent": _pct(v)}
            for k, v in sorted(by_tax.items(), key=lambda x: x[1], reverse=True)
        ],
        "note": (
            "Person attribution based on account name keywords (Drew/Lacey/Joint). "
            "Liabilities (mortgage, credit cards) excluded from asset breakdowns."
        ),
    }


# ---------------------------------------------------------------------------
# Financial summary  (executive dashboard — single call)
# ---------------------------------------------------------------------------

async def get_financial_summary(http_session) -> dict:
    """
    Return a compact executive summary of the complete financial picture.

    Combines net worth, portfolio performance, this month's cash flow,
    top spending categories, and goal status into a single response.
    Designed as the first tool to call for broad questions like
    'How are my finances looking?' or 'Give me a financial overview.'
    """
    http = await http_session.get_http()

    # ── Net worth + portfolio (Cards 9, 11, 3) ────────────────────────────
    ts = int(time.time() * 1000)
    card9  = await _get_card(http, 9)
    card11 = await _get_card(http, 11)
    card3  = await _get_card(http, 3)
    card2  = await _get_card(http, 2)

    net_worth = (card9 or {}).get("NetWorth")
    assets    = (card9 or {}).get("Assets")
    liab      = (card9 or {}).get("Liabilities")

    nw_mtd = (card11 or {}).get("ChangeThisMonth") or {}
    nw_ytd = (card11 or {}).get("ChangeThisYear")  or {}

    inv_vc     = (card3 or {}).get("ValueChange") or {}
    inv_value  = inv_vc.get("CurrentValue")
    inv_today  = inv_vc.get("Change")
    inv_pct    = inv_vc.get("ChangePercent")

    # ── Goals summary ─────────────────────────────────────────────────────
    goals_raw = (card2 or {}).get("Goals") or []
    goals_summary = []
    for g in goals_raw:
        proj = g.get("Projection") or {}
        pct  = proj.get("PercentFunded")
        goals_summary.append({
            "name":           g.get("Name"),
            "percent_funded": pct,
            "on_track":       (pct or 0) >= 100,
        })

    # ── This month's cash flow (SNB — last 35 days) ───────────────────────
    txns, snb_ok = await _fetch_snb_data(http_session, days=35)
    this_month = datetime.now().strftime("%Y-%m")

    month_income   = 0.0
    month_spending = 0.0
    cat_totals: dict[str, float] = {}

    if snb_ok:
        for t in txns:
            if t["date"][:7] != this_month or t["is_excluded"]:
                continue
            if t["is_income"]:
                month_income = round(month_income + t["amount"], 2)
            else:
                month_spending = round(month_spending + t["amount"], 2)
                cat = t["category"]
                cat_totals[cat] = round(cat_totals.get(cat, 0) + t["amount"], 2)

    savings_rate = None
    if month_income > 0:
        savings_rate = round((month_income - month_spending) / month_income * 100, 1)

    top_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:5]

    return {
        "as_of": datetime.now().strftime("%Y-%m-%d"),
        "net_worth": {
            "current":             net_worth,
            "total_assets":        assets,
            "total_liabilities":   liab,
            "change_this_month":   nw_mtd.get("Change"),
            "change_this_month_pct": round((nw_mtd.get("ChangePercent") or 0) * 100, 2),
            "change_this_year":    nw_ytd.get("Change") if nw_ytd else None,
        },
        "investment_portfolio": {
            "current_value":     inv_value,
            "today_change":      round(inv_today or 0, 2),
            "today_change_pct":  round((inv_pct or 0) * 100, 2),
        },
        "this_month_cash_flow": {
            "income":           round(month_income, 2),
            "spending":         round(month_spending, 2),
            "net":              round(month_income - month_spending, 2),
            "savings_rate_pct": savings_rate,
            "top_categories":   [{"category": c, "total": round(v, 2)} for c, v in top_cats],
        },
        "goals": goals_summary,
        "all_goals_on_track": all(g["on_track"] for g in goals_summary) if goals_summary else None,
    }
