"""Spending, transaction, and cash-flow scraping (SNB API + Card 13)."""

import re
from datetime import datetime, timedelta

from ._helpers import BASE_URL, _get_card, _CARD_URL

_SNB_API = "https://api.emoneyadvisor.com/snb-api"

# ---------------------------------------------------------------------------
# US state abbreviations — used by _normalize_merchant
# ---------------------------------------------------------------------------

_US_STATES = frozenset({
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","GU","VI",
})

# Common POS / payment-system prefixes to strip from transaction descriptions
_POS_PREFIXES = re.compile(
    r"^(?:APLPAY\s+|SQ\s*\*\s*|TST\*?\s+|PP\*\s*|PAYPAL\s*\*\s*|SP\s+|"
    r"AMZN\s+MKTP\s+US\*?\s*|GOOGLE\s*\*\s*|APPLE\.COM/\s*)",
    re.IGNORECASE,
)

# Trailing asterisk transaction reference codes like  *XYZ123
_ASTERISK_REF = re.compile(r"\*[A-Z0-9]{4,}$")

# Store / transaction reference numbers like  #1234  or  1234567
_STORE_NUMBER = re.compile(r"\s+\#?\d{4,}$")

# ZIP codes at end: " 20166" or " 20166-1234"
_ZIP_CODE = re.compile(r"\s+\d{5}(?:-\d{4})?$")

# Categories that represent internal financial flows, not real merchant spending
_NON_MERCHANT_CATEGORIES = {
    "Transfers", "Credit Card Payment", "Paycheck/Salary",
    "Income", "ACH Transfer", "Internal Transfer", "Investment",
    "Dividend & Cap Gains", "Interest Income",
}

# Income-generating categories (credits into the account)
_INCOME_CATEGORIES = frozenset({
    "Paycheck/Salary", "Income", "Dividend & Cap Gains", "Interest Income",
    "ACH Transfer",   # often direct deposit — treated as income
})

# Pure internal flows — exclude from both income and spending
_EXCLUDE_CATEGORIES = frozenset({
    "Transfers", "Credit Card Payment", "Internal Transfer",
})

# Recurring cadence detection
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


# ---------------------------------------------------------------------------
# Merchant name normalization
# ---------------------------------------------------------------------------

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
    s = re.sub(r"\s{2,}", " ", s)

    return s or raw.upper()


# ---------------------------------------------------------------------------
# SNB API helpers
# ---------------------------------------------------------------------------

async def _get_snb_credentials(http_session) -> tuple[str, str]:
    """Extract JWT token and API key from the Spending/Transactions page HTML."""
    http = await http_session.get_http()
    resp = await http.get(f"{BASE_URL}/ema/CS/Spending/Transactions", timeout=20)
    html = resp.text
    jwt_match = re.search(r'"JwtToken"\s*:\s*"([^"]+)"', html)
    key_match  = re.search(r'apiKey["\']?\s*:\s*["\']([^"\']+)["\']', html)
    jwt_token = jwt_match.group(1) if jwt_match else ""
    api_key   = key_match.group(1)  if key_match  else ""
    return jwt_token, api_key


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
# get_spending  (Card 13 — simple cash flow summary)
# ---------------------------------------------------------------------------

async def get_spending(http_session, months: int = 1) -> dict:
    """
    Return cash flow summary and recent transactions for the last 30 days.

    Source: CardSwitcher/GetCard/13 — contains Income, Expenses, Net cash flow
    and the 5 most recent spending transactions.
    """
    http = await http_session.get_http()

    card13 = await _get_card(http, 13)
    if not card13:
        return {"error": "Could not retrieve spending data (Card 13 unavailable). Session may have expired."}

    cf  = card13.get("CashFlow")   or {}
    bud = card13.get("Budget")     or {}
    rt  = card13.get("RecentTransactions") or {}

    dr = cf.get("DataSourceRoute") or {}
    rvd = dr.get("RouteValueDictionary") or {}
    period_start = rvd.get("startDate", "")
    period_end   = rvd.get("endDate",   "")

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
# get_spending_transactions  (SNB API — full transaction list with categories)
# ---------------------------------------------------------------------------

async def get_spending_transactions(http_session, days: int = 30) -> dict:
    """
    Return bank/credit card transactions with category labels for the last `days` days.

    Source: SNB API (api.emoneyadvisor.com/snb-api)
      GET api/values/GetFilteredTransactions  → transactions with categoryId
      GET api/values/GetCategories            → 114 category names
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

    # Summarize by merchant
    merchant_data: dict[str, dict] = {}
    for t in transactions:
        if t["category"] in _NON_MERCHANT_CATEGORIES:
            continue
        raw = t["description"]
        key = _normalize_merchant(raw)
        if key not in merchant_data:
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
# get_spending_trends  (month-over-month by category)
# ---------------------------------------------------------------------------

async def get_spending_trends(http_session, months: int = 3) -> dict:
    """
    Return month-over-month spending trends by category for the last `months` months.
    """
    months = min(max(months, 2), 12)
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
# get_income_summary
# ---------------------------------------------------------------------------

async def get_income_summary(http_session, days: int = 90) -> dict:
    """
    Return income sources and monthly income trend for the last `days` days.
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

    source_list = []
    for s in sources.values():
        s["average"]     = round(s["total"] / s["count"], 2)
        s["most_recent"] = max(s["dates"])
        del s["dates"]
        source_list.append(s)
    source_list.sort(key=lambda x: x["total"], reverse=True)

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
# get_savings_rate
# ---------------------------------------------------------------------------

async def get_savings_rate(http_session, months: int = 6) -> dict:
    """
    Return month-by-month savings rate for the last `months` months.

    Savings rate = (income - spending) / income * 100
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
# search_transactions
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
# get_recurring_charges
# ---------------------------------------------------------------------------

async def get_recurring_charges(http_session) -> dict:
    """
    Detect recurring and subscription charges from the last 120 days.
    """
    txns, ok = await _fetch_snb_data(http_session, days=120)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

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

        gaps = []
        for i in range(1, len(dates)):
            d1 = datetime.strptime(dates[i - 1], "%Y-%m-%d")
            d2 = datetime.strptime(dates[i],     "%Y-%m-%d")
            gaps.append((d2 - d1).days)

        avg_gap = sum(gaps) / len(gaps)
        avg_amount = sum(amounts) / len(amounts)
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
