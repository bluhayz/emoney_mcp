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


# ===========================================================================
# IRS CONSTANTS  (2025 — update annually)
# ===========================================================================

_TAX_YEAR = 2025
_IRS_CAVEAT = (
    "Figures use 2025 IRS limits and tax brackets. "
    "Consult a qualified tax professional before acting on any estimates."
)

_CONTRIBUTION_LIMITS = {
    "401k_403b":              23_500,
    "401k_403b_catchup_50":   31_000,   # age 50-59 and 64+
    "401k_403b_catchup_60":   34_750,   # SECURE 2.0 super catch-up age 60-63
    "ira":                     7_000,
    "ira_catchup":             8_000,   # age 50+
    "hsa_individual":          4_300,
    "hsa_family":              8_550,
    "hsa_catchup":             1_000,   # age 55+
    "simple_ira":             16_500,
    "simple_ira_catchup":     20_000,   # age 50+
    "sep_ira_pct":             0.25,
    "sep_ira_max":            70_000,
    "gift_tax_exclusion":     19_000,   # per beneficiary (529 / gifting)
}

_STD_DEDUCTION = {"single": 15_000, "mfj": 30_000, "hoh": 22_500}

# Ordinary income brackets — (upper bound of bracket, rate)
_BRACKETS: dict[str, list[tuple[float, float]]] = {
    "single": [
        (11_925,       0.10),
        (48_475,       0.12),
        (103_350,      0.22),
        (197_300,      0.24),
        (250_525,      0.32),
        (626_350,      0.35),
        (float("inf"), 0.37),
    ],
    "mfj": [
        (23_850,       0.10),
        (96_950,       0.12),
        (206_700,      0.22),
        (394_600,      0.24),
        (501_050,      0.32),
        (751_600,      0.35),
        (float("inf"), 0.37),
    ],
    "hoh": [
        (17_000,       0.10),
        (64_850,       0.12),
        (103_350,      0.22),
        (197_300,      0.24),
        (250_500,      0.32),
        (626_350,      0.35),
        (float("inf"), 0.37),
    ],
}

# LTCG thresholds — (upper bound of 0% / 15% bracket, rate)
_LTCG_THRESHOLDS: dict[str, list[tuple[float, float]]] = {
    "single": [(48_350,  0.0), (533_400,  0.15), (float("inf"), 0.20)],
    "mfj":    [(96_700,  0.0), (600_050,  0.15), (float("inf"), 0.20)],
    "hoh":    [(64_750,  0.0), (566_700,  0.15), (float("inf"), 0.20)],
}

# NIIT (3.8%) kicks in above these thresholds
_NIIT_THRESHOLD = {"single": 200_000, "mfj": 250_000, "hoh": 200_000}

# IRS Uniform Lifetime Table — age → distribution period
_RMD_TABLE: dict[int, float] = {
    72: 27.4, 73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7,
    77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2, 81: 19.4,
    82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2,
    87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5,
    92: 10.8, 93: 10.1, 94:  9.5, 95:  8.9, 96:  8.4,
    97:  7.8, 98:  7.3, 99:  6.8, 100: 6.4,
}

# Account-type → tax bucket mapping (from Emoney MajorType strings)
_TAX_BUCKET: dict[str, str] = {
    "InvestmentAsset":           "Taxable",
    "CashAsset":                 "Taxable",
    "RealEstateAsset":           "Taxable",
    "OptionPlan":                "Taxable",
    "OtherAsset":                "Taxable",
    "PreTaxSavingsAsset":        "Tax-Deferred",
    "AnnuityAsset":              "Tax-Deferred",
    "TaxFreeRothSavingsAsset":   "Tax-Free",
    "TaxFree529SavingsAsset":    "Tax-Free",
    "TaxFreeHealthSavingsAsset": "Tax-Free",
}

# Tax-efficiency score for asset classes: higher = more tax-efficient
# (best placed in taxable; low-efficiency = prefer tax-deferred)
_ASSET_EFFICIENCY: dict[str, int] = {
    # High efficiency (good in taxable)
    "domestic_equity_index": 9,
    "international_equity":  8,
    "growth_equity":         7,
    "muni_bond":             9,
    # Medium
    "dividend_equity":       5,
    "balanced":              4,
    # Low efficiency (prefer tax-deferred/free)
    "reit":                  2,
    "bond_fund":             2,
    "tips":                  1,
    "high_yield_bond":       1,
    "money_market":          3,
}


# ---------------------------------------------------------------------------
# Internal tax helpers
# ---------------------------------------------------------------------------

def _compute_tax(taxable_income: float, filing_status: str) -> float:
    """Federal income tax on taxable income (post-deduction)."""
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    tax = 0.0
    prev = 0.0
    for ceiling, rate in _BRACKETS[fs]:
        if taxable_income <= prev:
            break
        tax += (min(taxable_income, ceiling) - prev) * rate
        prev = ceiling
    return round(tax, 2)


def _marginal_rate(taxable_income: float, filing_status: str) -> float:
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    prev = 0.0
    for ceiling, rate in _BRACKETS[fs]:
        if taxable_income <= ceiling:
            return rate
        prev = ceiling
    return 0.37


def _ltcg_rate(taxable_income: float, filing_status: str) -> float:
    fs = filing_status if filing_status in _LTCG_THRESHOLDS else "mfj"
    for ceiling, rate in _LTCG_THRESHOLDS[fs]:
        if taxable_income <= ceiling:
            return rate
    return 0.20


def _classify_asset(ticker: str, description: str) -> str:
    """Heuristically classify a holding into an asset class."""
    t = (ticker or "").upper()
    d = (description or "").upper()
    combined = t + " " + d

    # Munis
    if any(x in combined for x in ("MUNI", "TAX-EXEMPT", "TAX EXEMPT")):
        return "muni_bond"
    # TIPS / inflation
    if any(x in combined for x in ("TIPS", "INFLATION", "INFL-PROT", "TREASURY INFLATION")):
        return "tips"
    # High-yield bonds
    if any(x in combined for x in ("HIGH YIELD", "JUNK", "HYG", "JNK", "HYLD")):
        return "high_yield_bond"
    # REITs
    if any(x in combined for x in ("REIT", "REAL ESTATE", "VNQ", "IYR", "SCHH")):
        return "reit"
    # Bond funds (broad)
    if any(x in combined for x in (
        "BOND", "FIXED INCOME", "INCOME FUND", "AGGREGATE", "TREASURY",
        "GOVT", "CORPORATE BOND", "AGG", "BND", "VBTLX", "TLT", "IEF", "SHY",
    )):
        return "bond_fund"
    # Money market / cash
    if any(x in combined for x in ("MONEY MARKET", "MMKT", "CASH", "TREASURY BILL", "T-BILL")):
        return "money_market"
    # International equity
    if any(x in combined for x in (
        "INTERNATIONAL", "INTL", "FOREIGN", "EMERGING", "EUROPE", "PACIFIC",
        "VXUS", "VEA", "VWO", "EFA", "EEM", "IXUS",
    )):
        return "international_equity"
    # Dividend-focused
    if any(x in combined for x in ("DIVIDEND", "INCOME EQUITY", "VALUE", "DVY", "VYM", "SCHD")):
        return "dividend_equity"
    # Index / passive domestic equity
    if any(x in combined for x in (
        "INDEX", "TOTAL MARKET", "S&P", "500", "VTSAX", "VTI", "SPY", "IVV", "SCHB", "FXAIX",
    )):
        return "domestic_equity_index"
    # Broad equity catch-all
    if any(x in combined for x in ("GROWTH", "EQUITY", "STOCK", "LARGE CAP", "SMALL CAP", "MID CAP")):
        return "growth_equity"

    return "domestic_equity_index"   # conservative default


async def _build_account_type_map(http_session) -> dict[str, str]:
    """Return {account_name_lower: tax_bucket} from card 1."""
    accts = await get_accounts(http_session)
    mapping: dict[str, str] = {}
    for grp in accts.get("account_groups", []):
        for a in grp.get("accounts", []):
            name  = (a.get("name") or "").strip()
            atype = a.get("type") or ""
            bucket = _TAX_BUCKET.get(atype, "Unknown")
            mapping[name.lower()] = bucket
    return mapping


def _match_tax_bucket(account_name: str, type_map: dict[str, str]) -> str:
    """Fuzzy-match an account name from holdings to its tax bucket."""
    key = account_name.lower()
    if key in type_map:
        return type_map[key]
    # substring match
    for mapped_name, bucket in type_map.items():
        if mapped_name in key or key in mapped_name:
            return bucket
    return "Unknown"


# ===========================================================================
# NEW TOOL: Tax-Loss Harvesting
# ===========================================================================

async def get_tax_loss_harvesting(http_session) -> dict:
    """
    Identify positions with unrealized losses suitable for tax-loss harvesting.

    Cross-references holdings against account type so only taxable-account
    losses are flagged as harvestable (losses in IRAs / 401ks have no
    immediate tax benefit).  Returns positions sorted by loss magnitude and
    estimates potential tax savings at the 15% and 20% LTCG rates.
    """
    type_map = await _build_account_type_map(http_session)

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data = resp.json()

    taxable_losses   = []
    deferred_losses  = []
    total_loss_taxable = 0.0
    total_loss_all     = 0.0

    for acct in data.get("Accounts", []):
        acct_name   = acct.get("Name", "")
        tax_bucket  = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            value      = h.get("Value") or 0.0
            cost_basis = h.get("CostBasis")
            if cost_basis is None or value >= cost_basis:
                continue  # no loss

            loss = round(value - cost_basis, 2)   # negative number
            position = {
                "ticker":       h.get("Ticker") or "",
                "description":  (h.get("Description") or "")[:50],
                "account":      acct_name,
                "tax_treatment": tax_bucket,
                "current_value": round(value, 2),
                "cost_basis":   round(cost_basis, 2),
                "unrealized_loss": loss,
                "harvestable":  tax_bucket == "Taxable",
            }

            total_loss_all += loss
            if tax_bucket == "Taxable":
                total_loss_taxable += loss
                taxable_losses.append(position)
            else:
                deferred_losses.append(position)

    taxable_losses.sort(key=lambda x: x["unrealized_loss"])
    deferred_losses.sort(key=lambda x: x["unrealized_loss"])

    potential_savings_15 = round(abs(total_loss_taxable) * 0.15, 2)
    potential_savings_20 = round(abs(total_loss_taxable) * 0.20, 2)
    potential_savings_238 = round(abs(total_loss_taxable) * 0.238, 2)  # 20% + 3.8% NIIT

    return {
        "summary": {
            "harvestable_loss_total":     round(total_loss_taxable, 2),
            "non_harvestable_loss_total": round(total_loss_all - total_loss_taxable, 2),
            "potential_tax_savings_15pct":  potential_savings_15,
            "potential_tax_savings_20pct":  potential_savings_20,
            "potential_tax_savings_238pct": potential_savings_238,
        },
        "harvestable_positions":     taxable_losses,
        "non_harvestable_positions": deferred_losses,
        "note": (
            "Harvestable = taxable brokerage accounts only. "
            "The wash-sale rule prohibits repurchasing substantially identical securities "
            "within 30 days before or after the sale. "
            "Savings estimates assume losses fully offset gains; consult a tax advisor."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ===========================================================================
# NEW TOOL: Contribution Room
# ===========================================================================

async def get_contribution_room(http_session, age: int | None = None,
                                 filing_status: str = "mfj") -> dict:
    """
    Show remaining IRS contribution room across tax-advantaged accounts.

    Uses current account balances as context and displays 2025 IRS annual
    limits.  Because Emoney does not expose year-to-date contribution data,
    this cannot calculate actual remaining room — it shows the annual limits
    alongside current balances so you can cross-reference with your payroll
    records.

    Parameters
    ----------
    age           : your age (determines catch-up eligibility)
    filing_status : 'single', 'mfj' (married filing jointly), or 'hoh'
    """
    retirement = await get_retirement_accounts(http_session)
    if "error" in retirement:
        return retirement

    lim = _CONTRIBUTION_LIMITS
    is_50_plus  = age is not None and age >= 50
    is_55_plus  = age is not None and age >= 55
    is_60_to_63 = age is not None and 60 <= age <= 63

    # 401k limit
    if is_60_to_63:
        k401_limit = lim["401k_403b_catchup_60"]
        k401_label = f"401k/403b (age {age} super catch-up)"
    elif is_50_plus:
        k401_limit = lim["401k_403b_catchup_50"]
        k401_label = f"401k/403b (age {age} catch-up)"
    else:
        k401_limit = lim["401k_403b"]
        k401_label = "401k/403b"

    ira_limit = lim["ira_catchup"] if is_50_plus else lim["ira"]
    hsa_limit = (lim["hsa_family"] if filing_status == "mfj" else lim["hsa_individual"])
    if is_55_plus:
        hsa_limit += lim["hsa_catchup"]

    accounts_summary = {
        "total_retirement_assets": retirement.get("total_retirement_assets"),
        "breakdown": retirement.get("retirement_breakdown"),
    }

    return {
        "age":          age,
        "filing_status": filing_status,
        "tax_year":     _TAX_YEAR,
        "annual_limits": {
            k401_label:           k401_limit,
            "Traditional/Roth IRA": ira_limit,
            "HSA":                hsa_limit,
            "SIMPLE IRA":         lim["simple_ira_catchup"] if is_50_plus else lim["simple_ira"],
            "SEP IRA (max)":      lim["sep_ira_max"],
            "529 (gift exclusion per beneficiary)": lim["gift_tax_exclusion"],
        },
        "current_balances": accounts_summary,
        "catch_up_eligible": {
            "ira_401k_catchup":    is_50_plus,
            "hsa_catchup":         is_55_plus,
            "super_catchup_60_63": is_60_to_63,
        },
        "note": (
            "Emoney does not expose year-to-date contribution amounts, so remaining "
            "room must be calculated manually: (annual limit) − (amount contributed "
            "so far this year from your payroll/brokerage statements)."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ===========================================================================
# NEW TOOL: Roth Conversion Analysis
# ===========================================================================

async def get_roth_conversion_analysis(
    http_session,
    conversion_amount: float,
    current_income: float,
    filing_status: str = "mfj",
    age: int | None = None,
) -> dict:
    """
    Estimate the federal tax cost and break-even of converting pre-tax dollars
    to Roth.

    Parameters
    ----------
    conversion_amount : dollar amount to convert this year
    current_income    : estimated gross ordinary income BEFORE the conversion
                        (wages, RMDs, Social Security, etc.)
    filing_status     : 'single', 'mfj', or 'hoh'
    age               : used to compute standard deduction and RMD context
    """
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    std_ded = _STD_DEDUCTION.get(fs, 30_000)

    taxable_before = max(0.0, current_income - std_ded)
    taxable_after  = max(0.0, current_income + conversion_amount - std_ded)

    tax_before = _compute_tax(taxable_before, fs)
    tax_after  = _compute_tax(taxable_after,  fs)
    marginal   = _marginal_rate(taxable_before, fs)
    effective_rate_on_conversion = (tax_after - tax_before) / conversion_amount if conversion_amount else 0

    # Future tax-free growth estimate (simple)
    future_value_10yr = round(conversion_amount * (1.06 ** 10), 2)
    future_value_20yr = round(conversion_amount * (1.06 ** 20), 2)
    tax_on_conversion = round(tax_after - tax_before, 2)

    # Break-even: years until tax-free compounding saves more than the upfront tax
    # After-Roth: grows tax-free; After-Traditional: grows tax-deferred, taxed on withdrawal
    # Assume withdrawal marginal rate = marginal (simple assumption)
    breakeven_years = None
    if marginal > 0 and effective_rate_on_conversion > 0:
        # Roth after-tax value after N years: (conversion_amount - tax) * 1.06^N
        # Traditional after-tax after N years: conversion_amount * 1.06^N * (1 - marginal)
        # Roth > Trad when: (1 - eff_rate) * 1.06^N > (1 - marginal) * 1.06^N
        # i.e., (1 - eff_rate) > (1 - marginal) only if eff_rate < marginal
        # If conversion rate < withdrawal rate → conversion always wins
        # If conversion rate > withdrawal rate → traditional better
        if effective_rate_on_conversion < marginal:
            breakeven_years = 0   # conversion is immediately better
        else:
            breakeven_years = None   # traditional likely better

    # Current retirement context
    retirement = await get_retirement_accounts(http_session)
    deferred_total = retirement.get("total_retirement_assets", 0) if "error" not in retirement else None

    # Bracket analysis — show which brackets the conversion fills
    bracket_fill = []
    fs_brackets = _BRACKETS[fs]
    remaining = conversion_amount
    income_cursor = taxable_before
    prev = 0.0
    for ceiling, rate in fs_brackets:
        if remaining <= 0:
            break
        if income_cursor < ceiling:
            room = ceiling - max(income_cursor, prev)
            used = min(remaining, room)
            bracket_fill.append({
                "bracket_rate_pct": int(rate * 100),
                "dollars_in_bracket": round(used, 2),
                "tax_in_bracket": round(used * rate, 2),
            })
            remaining -= used
        prev = ceiling

    return {
        "conversion_amount":        round(conversion_amount, 2),
        "current_income":           round(current_income, 2),
        "filing_status":            fs,
        "standard_deduction":       std_ded,
        "taxable_income_before":    round(taxable_before, 2),
        "taxable_income_after":     round(taxable_after, 2),
        "federal_tax_before":       tax_before,
        "federal_tax_after":        tax_after,
        "tax_cost_of_conversion":   tax_on_conversion,
        "effective_rate_on_conversion_pct": round(effective_rate_on_conversion * 100, 2),
        "marginal_rate_entering_pct": int(marginal * 100),
        "bracket_fill":             bracket_fill,
        "projected_roth_value": {
            "10_years_at_6pct":  future_value_10yr,
            "20_years_at_6pct":  future_value_20yr,
        },
        "conversion_favored":       effective_rate_on_conversion <= marginal,
        "breakeven_note": (
            "Conversion is tax-favored when your effective rate on the converted amount "
            "is lower than your expected marginal rate at withdrawal. "
            "This is especially powerful if you expect higher income in retirement "
            "or have significant pre-tax assets that will drive large RMDs."
        ),
        "current_pretax_balance":   deferred_total,
        "caveat": _IRS_CAVEAT,
    }


# ===========================================================================
# NEW TOOL: Capital Gains Exposure
# ===========================================================================

async def get_capital_gains_exposure(
    http_session,
    filing_status: str = "mfj",
    annual_income: float | None = None,
) -> dict:
    """
    Identify embedded (unrealized) capital gains in taxable accounts and
    estimate the tax liability if those positions were sold today.

    Parameters
    ----------
    filing_status : 'single', 'mfj', or 'hoh'
    annual_income : estimated ordinary income (for LTCG rate calculation);
                    if omitted, uses income from get_income_summary
    """
    type_map = await _build_account_type_map(http_session)

    # Try to infer income if not provided
    if annual_income is None:
        inc_result = await get_income_summary(http_session, days=365)
        annual_income = inc_result.get("total_income", 0) if "error" not in inc_result else 0

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data = resp.json()
    fs = filing_status if filing_status in _LTCG_THRESHOLDS else "mfj"

    taxable_gains:   list[dict] = []
    deferred_gains:  list[dict] = []
    total_gain_taxable = 0.0

    for acct in data.get("Accounts", []):
        acct_name  = acct.get("Name", "")
        tax_bucket = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            value      = h.get("Value") or 0.0
            cost_basis = h.get("CostBasis")
            if cost_basis is None or value <= cost_basis:
                continue

            gain = round(value - cost_basis, 2)
            rate = _ltcg_rate(annual_income, fs)
            niit = 0.038 if annual_income > _NIIT_THRESHOLD.get(fs, 250_000) else 0.0
            effective_rate = rate + niit
            est_tax = round(gain * effective_rate, 2)

            position = {
                "ticker":        h.get("Ticker") or "",
                "description":   (h.get("Description") or "")[:50],
                "account":       acct_name,
                "tax_treatment": tax_bucket,
                "current_value": round(value, 2),
                "cost_basis":    round(cost_basis, 2),
                "unrealized_gain": gain,
                "pct_gain":      round((gain / cost_basis) * 100, 1) if cost_basis else None,
                "ltcg_rate_pct": round(effective_rate * 100, 1),
                "estimated_tax_if_sold": est_tax,
            }

            if tax_bucket == "Taxable":
                total_gain_taxable += gain
                taxable_gains.append(position)
            else:
                deferred_gains.append(position)

    taxable_gains.sort(key=lambda x: x["unrealized_gain"], reverse=True)

    rate = _ltcg_rate(annual_income, fs)
    niit = 0.038 if annual_income > _NIIT_THRESHOLD.get(fs, 250_000) else 0.0
    effective_total_rate = rate + niit
    total_tax_exposure = round(total_gain_taxable * effective_total_rate, 2)

    niit_applies = annual_income > _NIIT_THRESHOLD.get(fs, 250_000)

    return {
        "filing_status":            fs,
        "estimated_annual_income":  round(annual_income, 2),
        "ltcg_rate_pct":            round(rate * 100, 1),
        "niit_applies":             niit_applies,
        "effective_rate_pct":       round(effective_total_rate * 100, 1),
        "total_taxable_unrealized_gain": round(total_gain_taxable, 2),
        "total_estimated_tax_exposure":  total_tax_exposure,
        "taxable_account_positions": taxable_gains,
        "deferred_account_positions": deferred_gains,
        "note": (
            "Only taxable brokerage account gains create an immediate tax event on sale. "
            "Gains in IRAs and 401ks are taxed as ordinary income upon withdrawal. "
            "Gains in Roth accounts are tax-free upon qualified withdrawal. "
            "LTCG assumes all positions held > 1 year; short-term gains taxed as ordinary income."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ===========================================================================
# NEW TOOL: RMD Estimate
# ===========================================================================

async def get_rmd_estimate(http_session, birth_year: int) -> dict:
    """
    Estimate Required Minimum Distributions from pre-tax retirement accounts.

    RMDs begin at age 73 (SECURE 2.0).  Uses the IRS Uniform Lifetime Table
    applied to current traditional IRA / 401k balances.

    Parameters
    ----------
    birth_year : your year of birth (e.g. 1955)
    """
    current_year = datetime.now().year
    age = current_year - birth_year
    rmd_start_age = 73   # SECURE 2.0

    retirement = await get_retirement_accounts(http_session)
    if "error" in retirement:
        return retirement

    breakdown = retirement.get("retirement_breakdown", {})
    pretax_balance = (breakdown.get("401k_403b", 0) or 0) + (breakdown.get("ira_roth", 0) or 0)

    # Separate Roth from traditional for IRA bucket (rough — Emoney lumps them)
    # We flag this as approximate
    roth_approx = 0.0
    trad_balance = pretax_balance  # conservative: assume all is pre-tax for RMD

    years_until_rmd = max(0, rmd_start_age - age)
    rmd_age = max(age, rmd_start_age)

    # Project balances at 6% for future years
    future_balance_at_rmd = round(trad_balance * (1.06 ** years_until_rmd), 2) if years_until_rmd > 0 else trad_balance

    # RMD schedule for next 10 years
    rmd_schedule = []
    balance = future_balance_at_rmd
    for yr in range(10):
        calc_age = rmd_age + yr
        factor = _RMD_TABLE.get(calc_age) or _RMD_TABLE.get(min(calc_age, 100), 6.4)
        rmd_amount = round(balance / factor, 2)
        rmd_schedule.append({
            "year":       current_year + years_until_rmd + yr,
            "age":        calc_age,
            "est_balance": round(balance, 2),
            "factor":     factor,
            "rmd_amount": rmd_amount,
        })
        # Reduce balance by RMD then grow by 6%
        balance = round((balance - rmd_amount) * 1.06, 2)

    current_rmd = None
    if age >= rmd_start_age:
        factor = _RMD_TABLE.get(age) or _RMD_TABLE.get(min(age, 100), 6.4)
        current_rmd = round(trad_balance / factor, 2)

    return {
        "birth_year":            birth_year,
        "current_age":           age,
        "rmd_start_age":         rmd_start_age,
        "years_until_rmd":       years_until_rmd,
        "rmd_required_this_year": age >= rmd_start_age,
        "current_pretax_balance": round(trad_balance, 2),
        "current_rmd_estimate":   current_rmd,
        "projected_rmd_schedule": rmd_schedule,
        "roth_conversion_note": (
            "Converting pre-tax balances to Roth before RMD age reduces future mandatory "
            "distributions and creates tax-free growth. This is especially valuable in "
            "low-income years between retirement and RMD start age."
        ),
        "note": (
            "Balances projected at 6% annual growth. IRA and Roth IRA are grouped — "
            "only traditional (pre-tax) balances are subject to RMDs; Roth IRAs have no "
            "RMD requirement during the owner's lifetime. "
            "RMD amounts shown are estimates; always verify with your custodian."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ===========================================================================
# NEW TOOL: Retirement Runway
# ===========================================================================

async def get_retirement_runway(
    http_session,
    annual_spending: float | None = None,
    return_rate: float = 0.06,
) -> dict:
    """
    Model how many years the current portfolio can sustain withdrawals.

    If annual_spending is not provided, uses actual 12-month spending from
    the SNB transaction data.  Models three scenarios: conservative (4%),
    base (6%), and optimistic (8%) portfolio returns.

    Parameters
    ----------
    annual_spending : override spending in dollars (default: actual 12-month spend)
    return_rate     : base-case nominal annual return (default 0.06 = 6%)
    """
    # Get net worth / investable assets
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts
    total_assets     = accts.get("total_assets") or 0
    total_liabilities = accts.get("total_liabilities") or 0
    net_worth         = accts.get("net_worth") or 0

    # Exclude illiquid assets (real estate) — estimate investable as net worth minus debt
    investable = max(0.0, total_assets - total_liabilities)

    # Get spending
    if annual_spending is None:
        txns, ok = await _fetch_snb_data(http_session, days=365)
        if ok:
            annual_spending = round(sum(
                t["amount"] for t in txns
                if not t["is_income"] and not t["is_excluded"]
            ), 2)
        else:
            annual_spending = 0.0

    if annual_spending <= 0:
        return {"error": "Could not determine annual spending. Pass annual_spending explicitly."}

    inflation = 0.03  # 3% inflation assumption

    def _years_to_depletion(portfolio: float, withdrawal: float, ret: float, inf: float) -> float | None:
        """Return years until portfolio hits zero. None if it never depletes."""
        real_return = (1 + ret) / (1 + inf) - 1
        if real_return <= 0:
            if withdrawal <= 0:
                return None
            return portfolio / withdrawal
        # Closed-form solution for years to depletion
        ratio = withdrawal / (portfolio * real_return)
        if ratio >= 1:
            return portfolio / withdrawal   # depletes quickly
        import math
        try:
            years = -math.log(1 - ratio) / math.log(1 + real_return)
            return years if years > 0 else None
        except Exception:
            return None

    scenarios = []
    for label, ret in [("Conservative (4%)", 0.04), ("Base (6%)", 0.06), ("Optimistic (8%)", 0.08)]:
        years = _years_to_depletion(investable, annual_spending, ret, inflation)
        scenarios.append({
            "scenario":          label,
            "return_rate_pct":   int(ret * 100),
            "years_to_depletion": round(years, 1) if years else None,
            "sustainable":        years is None or years > 30,
        })

    # Sustainable withdrawal amounts (SWR) at 3.5%, 4%, 4.5%
    swr = [
        {"swr_pct": 3.5, "annual_amount": round(investable * 0.035, 2), "monthly": round(investable * 0.035 / 12, 2)},
        {"swr_pct": 4.0, "annual_amount": round(investable * 0.040, 2), "monthly": round(investable * 0.040 / 12, 2)},
        {"swr_pct": 4.5, "annual_amount": round(investable * 0.045, 2), "monthly": round(investable * 0.045 / 12, 2)},
    ]

    current_wr = round(annual_spending / investable * 100, 2) if investable > 0 else None

    return {
        "investable_assets":     round(investable, 2),
        "annual_spending":       annual_spending,
        "current_withdrawal_rate_pct": current_wr,
        "spending_covered_by_4pct_rule": annual_spending <= investable * 0.04,
        "inflation_assumption_pct": 3,
        "scenarios":             scenarios,
        "sustainable_withdrawal_amounts": swr,
        "note": (
            "Investable assets = total assets minus liabilities. "
            "Inflation-adjusted real return used for depletion modeling. "
            "'Sustainable' means portfolio survives 30+ years. "
            "Social Security, pensions, and annuities are not factored in — "
            "adding guaranteed income sources would extend runway significantly."
        ),
    }


# ===========================================================================
# NEW TOOL: Withdrawal Rate Analysis
# ===========================================================================

async def get_withdrawal_rate_analysis(http_session) -> dict:
    """
    Analyze safe withdrawal rate in the context of your Emoney financial plan.

    Combines current portfolio value, retirement goal start year, and
    estimated retirement duration to model what various withdrawal rates
    produce in annual income.
    """
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts

    goals_result = await get_goals(http_session)
    retirement_goals = goals_result.get("retirement_goals", []) if "error" not in goals_result else []

    net_worth   = accts.get("net_worth") or 0
    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0
    investable   = max(0.0, total_assets - total_liab)

    current_year = datetime.now().year
    retirement_start = None
    retirement_end   = None
    goal_name        = None

    if retirement_goals:
        g = retirement_goals[0]
        retirement_start = g.get("start_year")
        retirement_end   = g.get("end_year")
        goal_name        = g.get("name")

    years_to_retirement = (retirement_start - current_year) if retirement_start else None
    retirement_duration = (retirement_end - retirement_start) if (retirement_start and retirement_end) else None

    # Project portfolio growth to retirement
    projected_at_retirement = None
    if years_to_retirement and years_to_retirement > 0:
        projected_at_retirement = round(investable * (1.06 ** years_to_retirement), 2)
    else:
        projected_at_retirement = investable

    # Withdrawal rates analysis
    wdl_analysis = []
    for rate in [0.03, 0.035, 0.04, 0.045, 0.05]:
        annual = round((projected_at_retirement or investable) * rate, 2)
        monthly = round(annual / 12, 2)
        # Rough years-to-depletion at this rate, 6% nominal return, 3% inflation
        real_ret = (1.06 / 1.03) - 1
        import math
        ratio = rate / real_ret if real_ret > 0 else float("inf")
        try:
            years = -math.log(1 - min(ratio, 0.9999)) / math.log(1 + real_ret)
        except Exception:
            years = 999
        wdl_analysis.append({
            "rate_pct":           rate * 100,
            "annual_income":      annual,
            "monthly_income":     monthly,
            "est_years_funded":   round(years, 1) if years < 999 else None,
            "covers_30yr_plan":   years >= (retirement_duration or 30),
        })

    return {
        "current_investable_assets": round(investable, 2),
        "retirement_goal":           goal_name,
        "retirement_start_year":     retirement_start,
        "retirement_end_year":       retirement_end,
        "retirement_duration_years": retirement_duration,
        "years_to_retirement":       years_to_retirement,
        "projected_portfolio_at_retirement": projected_at_retirement,
        "projected_growth_assumption": "6% annual nominal return",
        "withdrawal_rate_scenarios": wdl_analysis,
        "rule_of_thumb": {
            "4pct_rule_annual": round((projected_at_retirement or investable) * 0.04, 2),
            "4pct_rule_monthly": round((projected_at_retirement or investable) * 0.04 / 12, 2),
            "summary": (
                "The 4% rule suggests a 30-year retirement is historically well-funded "
                "at this withdrawal rate from a diversified equity/bond portfolio."
            ),
        },
    }


# ===========================================================================
# NEW TOOL: Asset Location Efficiency
# ===========================================================================

async def get_asset_location_efficiency(http_session) -> dict:
    """
    Grade how well your assets are positioned across account types for
    tax efficiency.

    The principle: tax-inefficient assets (bonds, REITs, high-dividend stocks)
    should be sheltered in tax-deferred or tax-free accounts; tax-efficient
    assets (index funds, growth stocks) can sit in taxable accounts.

    Returns a letter grade, position-by-position ratings, and specific
    improvement suggestions.
    """
    type_map = await _build_account_type_map(http_session)

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data  = resp.json()
    total = (data.get("Holdings") or 0) + (data.get("Cash") or 0)

    scored_positions = []
    suggestions = []
    total_weighted_score = 0.0
    total_weight = 0.0

    for acct in data.get("Accounts", []):
        acct_name  = acct.get("Name", "")
        tax_bucket = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            value       = h.get("Value") or 0.0
            if value <= 0:
                continue
            ticker      = h.get("Ticker") or ""
            description = h.get("Description") or ""
            asset_class = _classify_asset(ticker, description)
            efficiency  = _ASSET_EFFICIENCY.get(asset_class, 5)

            # Score the placement: high efficiency in taxable = good;
            # low efficiency in deferred/free = good
            if tax_bucket == "Taxable":
                placement_score = efficiency           # good if efficient
                well_placed     = efficiency >= 6
            elif tax_bucket in ("Tax-Deferred", "Tax-Free"):
                placement_score = 10 - efficiency      # good if inefficient
                well_placed     = efficiency <= 5
            else:
                placement_score = 5
                well_placed     = None

            weight = value / total if total > 0 else 0
            total_weighted_score += placement_score * weight
            total_weight += weight

            entry = {
                "ticker":          ticker,
                "description":     description[:40],
                "account":         acct_name,
                "tax_treatment":   tax_bucket,
                "asset_class":     asset_class,
                "efficiency_score": efficiency,
                "value":           round(value, 2),
                "well_placed":     well_placed,
            }
            scored_positions.append(entry)

            if well_placed is False and value >= 10_000:
                if tax_bucket == "Taxable" and efficiency < 6:
                    suggestions.append(
                        f"Consider moving '{ticker or description[:30]}' (${value:,.0f}, {asset_class}) "
                        f"from taxable '{acct_name}' to a tax-deferred or tax-free account."
                    )
                elif tax_bucket in ("Tax-Deferred", "Tax-Free") and efficiency >= 7:
                    suggestions.append(
                        f"Consider moving '{ticker or description[:30]}' (${value:,.0f}, {asset_class}) "
                        f"to a taxable account to free up tax-sheltered space for less-efficient assets."
                    )

    overall_score = round(total_weighted_score / total_weight, 1) if total_weight > 0 else 5.0
    if overall_score >= 8:
        grade = "A"
    elif overall_score >= 6.5:
        grade = "B"
    elif overall_score >= 5:
        grade = "C"
    elif overall_score >= 3.5:
        grade = "D"
    else:
        grade = "F"

    well_placed_count   = sum(1 for p in scored_positions if p["well_placed"] is True)
    poorly_placed_count = sum(1 for p in scored_positions if p["well_placed"] is False)

    return {
        "overall_grade":       grade,
        "overall_score":       f"{overall_score}/10",
        "well_placed_count":   well_placed_count,
        "poorly_placed_count": poorly_placed_count,
        "suggestions":         suggestions[:10],
        "positions":           sorted(scored_positions, key=lambda x: x["well_placed"] is False, reverse=True),
        "efficiency_guide": {
            "best_in_taxable":   ["index funds", "ETFs", "growth stocks", "municipal bonds"],
            "best_in_deferred":  ["bond funds", "REITs", "TIPS", "high-yield bonds", "high-dividend stocks"],
            "best_in_tax_free":  ["highest-growth assets (Roth)", "bond funds if no deferred space"],
        },
    }


# ===========================================================================
# NEW TOOL: Rebalancing Targets
# ===========================================================================

async def get_rebalancing_targets(
    http_session,
    target_equity_pct: float = 60.0,
    target_bond_pct:   float = 30.0,
    target_cash_pct:   float = 10.0,
) -> dict:
    """
    Compute buy/sell amounts needed to reach a target asset allocation.

    Parameters
    ----------
    target_equity_pct : target percentage in equities (default 60)
    target_bond_pct   : target percentage in bonds/fixed income (default 30)
    target_cash_pct   : target percentage in cash/money market (default 10)
    """
    # Normalize targets to 100%
    total_target = target_equity_pct + target_bond_pct + target_cash_pct
    if abs(total_target - 100) > 0.1:
        target_equity_pct = round(target_equity_pct / total_target * 100, 1)
        target_bond_pct   = round(target_bond_pct   / total_target * 100, 1)
        target_cash_pct   = round(100 - target_equity_pct - target_bond_pct, 1)

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data  = resp.json()
    portfolio_total = (data.get("Holdings") or 0) + (data.get("Cash") or 0)

    # Classify holdings
    equity_value = 0.0
    bond_value   = 0.0
    cash_value   = data.get("Cash") or 0.0
    other_value  = 0.0

    position_details = []
    for acct in data.get("Accounts", []):
        for h in acct.get("Holdings", []):
            value       = h.get("Value") or 0.0
            ticker      = h.get("Ticker") or ""
            description = h.get("Description") or ""
            asset_class = _classify_asset(ticker, description)

            if asset_class in ("domestic_equity_index", "international_equity",
                               "growth_equity", "dividend_equity"):
                bucket = "equity"
                equity_value += value
            elif asset_class in ("bond_fund", "tips", "high_yield_bond", "muni_bond"):
                bucket = "bond"
                bond_value += value
            elif asset_class == "money_market":
                bucket = "cash"
                cash_value += value
            else:
                bucket = "equity"   # default: treat as equity
                equity_value += value

            position_details.append({
                "ticker":      ticker,
                "description": description[:40],
                "asset_class": asset_class,
                "bucket":      bucket,
                "value":       round(value, 2),
            })

    if portfolio_total <= 0:
        return {"error": "No portfolio data found."}

    current_equity_pct = round(equity_value / portfolio_total * 100, 1)
    current_bond_pct   = round(bond_value   / portfolio_total * 100, 1)
    current_cash_pct   = round(cash_value   / portfolio_total * 100, 1)

    target_equity_val = portfolio_total * target_equity_pct / 100
    target_bond_val   = portfolio_total * target_bond_pct   / 100
    target_cash_val   = portfolio_total * target_cash_pct   / 100

    equity_delta = round(target_equity_val - equity_value, 2)
    bond_delta   = round(target_bond_val   - bond_value,   2)
    cash_delta   = round(target_cash_val   - cash_value,   2)

    def _action(delta: float) -> str:
        if delta > 500:
            return f"BUY ${abs(delta):,.0f}"
        elif delta < -500:
            return f"SELL ${abs(delta):,.0f}"
        return "ON TARGET"

    return {
        "portfolio_total": round(portfolio_total, 2),
        "target_allocation": {
            "equity_pct": target_equity_pct,
            "bond_pct":   target_bond_pct,
            "cash_pct":   target_cash_pct,
        },
        "current_allocation": {
            "equity_pct": current_equity_pct,
            "equity_value": round(equity_value, 2),
            "bond_pct":   current_bond_pct,
            "bond_value": round(bond_value, 2),
            "cash_pct":   current_cash_pct,
            "cash_value": round(cash_value, 2),
        },
        "rebalancing_actions": {
            "equity": {"delta": equity_delta, "action": _action(equity_delta)},
            "bonds":  {"delta": bond_delta,   "action": _action(bond_delta)},
            "cash":   {"delta": cash_delta,   "action": _action(cash_delta)},
        },
        "drift_from_target": {
            "equity_drift_pct": round(current_equity_pct - target_equity_pct, 1),
            "bond_drift_pct":   round(current_bond_pct   - target_bond_pct,   1),
            "cash_drift_pct":   round(current_cash_pct   - target_cash_pct,   1),
        },
        "rebalance_needed": any(
            abs(d) >= 5 for d in [
                current_equity_pct - target_equity_pct,
                current_bond_pct   - target_bond_pct,
                current_cash_pct   - target_cash_pct,
            ]
        ),
        "position_breakdown": position_details,
        "note": (
            "Asset class assignment uses ticker/description heuristics. "
            "Verify classification for any position labeled unexpectedly. "
            "Consider executing sells first in tax-advantaged accounts to avoid taxable events."
        ),
    }


# ===========================================================================
# NEW TOOL: Financial Health Score
# ===========================================================================

async def get_financial_health_score(http_session) -> dict:
    """
    Return a single 0-100 composite financial health score with component
    breakdown.  Combines six dimensions: savings rate, goal funding,
    debt-to-asset ratio, emergency fund coverage, diversification, and
    net worth trend.

    Each component is scored 0-100 and weighted to produce the overall score.
    """
    errors = []

    # --- Net worth ---
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts
    net_worth    = accts.get("net_worth") or 0
    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0

    # --- Savings rate (last 3 months) ---
    savings_result = await get_savings_rate(http_session, months=3)
    avg_savings_rate = savings_result.get("average_savings_rate") if "error" not in savings_result else None

    # --- Goals ---
    goals_result = await get_goals(http_session)
    all_goals = []
    if "error" not in goals_result:
        all_goals = goals_result.get("retirement_goals", []) + goals_result.get("spending_goals", [])

    # --- Net worth history (trend) ---
    history_result = await get_net_worth_history(http_session, months=6)
    nw_change_pct = None
    if "error" not in history_result:
        ch = history_result.get("change_over_period", {})
        nw_change_pct = ch.get("percent")

    # --- Spending for emergency fund ---
    txns, snb_ok = await _fetch_snb_data(http_session, days=90)
    monthly_spending = 0.0
    if snb_ok:
        monthly_spending = sum(
            t["amount"] for t in txns
            if not t["is_income"] and not t["is_excluded"]
        ) / 3

    # --- Holdings for diversification ---
    holdings_result = await get_holdings(http_session)
    position_count = holdings_result.get("position_count", 0) if "error" not in holdings_result else 0

    # ── Scoring ────────────────────────────────────────────────────────────

    # 1. Savings rate (weight 25)
    if avg_savings_rate is not None:
        if avg_savings_rate >= 20:
            savings_score = 100
        elif avg_savings_rate >= 15:
            savings_score = 85
        elif avg_savings_rate >= 10:
            savings_score = 70
        elif avg_savings_rate >= 5:
            savings_score = 50
        elif avg_savings_rate > 0:
            savings_score = 30
        else:
            savings_score = 0
    else:
        savings_score = 50   # unknown → neutral
        errors.append("savings_rate unavailable")

    # 2. Goal funding (weight 25)
    if all_goals:
        funded_pcts = [g.get("percent_funded") or 0 for g in all_goals]
        avg_funded  = sum(funded_pcts) / len(funded_pcts)
        goal_score  = min(100, int(avg_funded))
    else:
        goal_score = 50
        errors.append("goals unavailable")

    # 3. Debt-to-asset ratio (weight 20)
    if total_assets > 0:
        dta = total_liab / total_assets
        if dta <= 0.05:
            debt_score = 100
        elif dta <= 0.15:
            debt_score = 85
        elif dta <= 0.30:
            debt_score = 65
        elif dta <= 0.50:
            debt_score = 40
        else:
            debt_score = 15
    else:
        debt_score = 50

    # 4. Emergency fund (weight 15): liquid months of spending
    liquid_group = next(
        (g for g in accts.get("account_groups", []) if "cash" in g.get("group", "").lower()
         or "bank" in g.get("group", "").lower()), None
    )
    liquid_assets = liquid_group["total"] if liquid_group else 0
    if monthly_spending > 0:
        months_covered = liquid_assets / monthly_spending
        if months_covered >= 6:
            emergency_score = 100
        elif months_covered >= 3:
            emergency_score = 70
        elif months_covered >= 1:
            emergency_score = 40
        else:
            emergency_score = 10
    else:
        emergency_score = 60

    # 5. Diversification (weight 10)
    if position_count >= 20:
        diversification_score = 100
    elif position_count >= 10:
        diversification_score = 80
    elif position_count >= 5:
        diversification_score = 55
    elif position_count >= 2:
        diversification_score = 35
    else:
        diversification_score = 10

    # 6. Net worth trend (weight 5)
    if nw_change_pct is not None:
        if nw_change_pct >= 10:
            trend_score = 100
        elif nw_change_pct >= 5:
            trend_score = 80
        elif nw_change_pct >= 0:
            trend_score = 60
        elif nw_change_pct >= -5:
            trend_score = 35
        else:
            trend_score = 10
    else:
        trend_score = 50

    # Weighted composite
    weights = {
        "savings_rate":    0.25,
        "goal_funding":    0.25,
        "debt_to_assets":  0.20,
        "emergency_fund":  0.15,
        "diversification": 0.10,
        "nw_trend":        0.05,
    }
    scores = {
        "savings_rate":    savings_score,
        "goal_funding":    goal_score,
        "debt_to_assets":  debt_score,
        "emergency_fund":  emergency_score,
        "diversification": diversification_score,
        "nw_trend":        trend_score,
    }
    composite = round(sum(scores[k] * weights[k] for k in weights), 1)

    if composite >= 85:
        letter_grade, summary = "A", "Excellent — your finances are in great shape."
    elif composite >= 70:
        letter_grade, summary = "B", "Good — strong fundamentals with room to improve."
    elif composite >= 55:
        letter_grade, summary = "C", "Fair — some important areas need attention."
    elif composite >= 40:
        letter_grade, summary = "D", "Needs work — several key financial metrics are below target."
    else:
        letter_grade, summary = "F", "Urgent attention needed — multiple areas are at risk."

    return {
        "overall_score":  composite,
        "letter_grade":   letter_grade,
        "summary":        summary,
        "components": [
            {
                "name":    k.replace("_", " ").title(),
                "score":   scores[k],
                "weight":  f"{int(weights[k]*100)}%",
                "details": _score_detail(k, scores[k], {
                    "savings_rate":    avg_savings_rate,
                    "goal_funding":    sum(g.get("percent_funded") or 0 for g in all_goals) / max(len(all_goals), 1),
                    "debt_to_assets":  round((total_liab / total_assets * 100) if total_assets else 0, 1),
                    "emergency_fund":  round(liquid_assets / monthly_spending, 1) if monthly_spending > 0 else None,
                    "diversification": position_count,
                    "nw_trend":        nw_change_pct,
                }),
            }
            for k in weights
        ],
        "data_errors": errors if errors else None,
        "note": "Score reflects current snapshot. Improve savings rate and goal funding for the biggest impact.",
    }


def _score_detail(component: str, score: int, values: dict) -> str:
    v = values.get(component)
    if component == "savings_rate":
        return f"{v:.1f}% average savings rate" if v is not None else "Data unavailable"
    if component == "goal_funding":
        return f"{v:.0f}% average goal funding" if v is not None else "No goals found"
    if component == "debt_to_assets":
        return f"{v:.1f}% debt-to-asset ratio" if v is not None else "Unknown"
    if component == "emergency_fund":
        return f"{v:.1f} months of expenses covered" if v is not None else "Unknown"
    if component == "diversification":
        return f"{v} investment positions"
    if component == "nw_trend":
        return f"{v:+.1f}% net worth change over 6 months" if v is not None else "Insufficient history"
    return ""


# ===========================================================================
# NEW TOOL: Explore Emoney Cards
# ===========================================================================

async def explore_emoney_cards(
    http_session,
    card_ids: list[int] | None = None,
) -> dict:
    """
    Probe unexplored Emoney CardSwitcher card endpoints to discover
    what data is available.  Useful for finding insurance, tax projection,
    estate, or other plan data not yet surfaced by the MCP.

    Parameters
    ----------
    card_ids : list of card IDs to probe (default: [5, 6, 7, 10, 12, 14, 15, 16])
    """
    http = await http_session.get_http()

    if card_ids is None:
        card_ids = [5, 6, 7, 10, 12, 14, 15, 16]

    results = {}
    for cid in card_ids:
        data = await _get_card(http, cid)
        if data is None:
            results[f"card_{cid}"] = {"status": "unavailable_or_error"}
        else:
            # Return the top-level keys and a sample so callers can see what's there
            keys = list(data.keys()) if isinstance(data, dict) else []
            results[f"card_{cid}"] = {
                "status":    "available",
                "top_keys":  keys,
                "card_id":   cid,
                "data":      data,   # full payload — useful for discovery
            }

    available = [k for k, v in results.items() if v.get("status") == "available"]
    return {
        "probed_cards":     card_ids,
        "available_cards":  available,
        "unavailable_count": len(card_ids) - len(available),
        "results":          results,
        "note": (
            "Use this tool to discover new Emoney data sources. "
            "If a card returns useful financial data, it can be wrapped into a "
            "dedicated tool in a future update."
        ),
    }
