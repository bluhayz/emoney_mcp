"""
Spending, transaction, and cash-flow scraping.

Data sources
------------
Card 13  — Simple cash-flow summary (income / expenses / net + 5 recent txns).
            Used by get_spending() and get_budget_vs_actual().

SNB API  — api.emoneyadvisor.com/snb-api.  Provides full transaction history
            with category labels.  Requires a JWT token + API key extracted
            from the Spending/Transactions page HTML on each call.  Used by
            get_spending_transactions(), _fetch_snb_data(), and all analytics
            built on top of it.

Caching
-------
_fetch_snb_raw() caches the full SNB dataset (all transactions + category map)
for _SNB_CACHE_TTL seconds (default 5 minutes).  All SNB-based tools call
_fetch_snb_data() which internally calls _fetch_snb_raw(), so within a single
conversation turn the SNB API is hit only once regardless of how many spending
tools are called.  Call clear_snb_cache() on session reset to invalidate.

Public functions
----------------
get_spending(http_session, months=1)
    Quick cash-flow summary from card 13. Limited detail — use
    get_spending_transactions for the full categorised list.

get_spending_transactions(http_session, days=30, max_transactions=100)
    Full transaction list from the SNB API with category labels, top-10
    categories, and top-15 merchants (after normalizing merchant names).
    Pass max_transactions=0 to return all transactions (may be large).

get_spending_trends(http_session, months=3)
    Month-over-month spending by category — which categories are trending up,
    down, or stable, plus monthly income vs. spending summary.

get_income_summary(http_session, days=90)
    Income sources (paychecks, dividends, interest, ACH deposits) grouped by
    normalized payee name, with a monthly income trend.

get_savings_rate(http_session, months=6)
    Month-by-month savings rate: (income − spending) / income × 100.

search_transactions(http_session, query, category, days, min_amount,
                    max_amount, max_results=100)
    Keyword / category / amount filter over SNB transaction history.
    Pass max_results=0 to return all matches.

get_recurring_charges(http_session)
    Subscription and recurring charge detection over 120 days.  Groups
    transactions by normalized merchant, computes inter-charge gaps, and
    matches known cadences (weekly / biweekly / monthly / quarterly / annual).

get_budget_vs_actual(http_session, months_avg=3)
    Compares this month's actual spending (SNB) against rolling N-month
    category averages and any total budget configured in Emoney (Card 13).
    Flags categories that are tracking above their rolling average.

get_year_over_year(http_session)
    Compares this year's spending and income to the same calendar year-to-date
    period last year, and compares the last two full 12-month windows.

get_cash_flow_projection(http_session, months_ahead=6)
    Projects future monthly cash flow using actual average income and spending
    from the last 90 days, layering in known recurring charges.

Internal helpers
----------------
_normalize_merchant(raw)         — Strips POS prefixes, location suffixes, and
                                   reference numbers to produce a stable key.
_get_snb_credentials(http_session) — Extracts the JWT and API key from the
                                     Spending page HTML.
_fetch_snb_raw(http_session)     — Fetches and caches the full SNB dataset.
_fetch_snb_data(http_session, days) — Shared SNB fetch/filter used by all
                                      analytics functions above.
clear_snb_cache()                — Purges the SNB in-memory cache.
"""

import asyncio
import calendar
import logging
import re
import time
from datetime import datetime, timedelta

from ._helpers import BASE_URL, _SNB_API, _get_card, _month_offset

_log = logging.getLogger("emoney_mcp.scrapers.spending")


# ---------------------------------------------------------------------------
# US state abbreviations — used by _normalize_merchant to strip trailing
# "CITY STATE" suffixes that appear in bank transaction descriptions.
# Only a fixed, known set is used to avoid accidentally stripping real words.
# ---------------------------------------------------------------------------

_US_STATES = frozenset({
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC","PR","GU","VI",
})

# ---------------------------------------------------------------------------
# Regex patterns used by _normalize_merchant()
# ---------------------------------------------------------------------------

# Payment-network prefixes that appear before the real merchant name.
# Examples: "APLPAY FOOD LION" → "FOOD LION", "SQ *BLUE BOTTLE COFFEE" → "BLUE BOTTLE COFFEE"
_POS_PREFIXES = re.compile(
    r"^(?:APLPAY\s+|SQ\s*\*\s*|TST\*?\s+|PP\*\s*|PAYPAL\s*\*\s*|SP\s+|"
    r"AMZN\s+MKTP\s+US\*?\s*|GOOGLE\s*\*\s*|APPLE\.COM/\s*)",
    re.IGNORECASE,
)

# Trailing asterisk reference codes appended by some processors: *XYZ123
_ASTERISK_REF = re.compile(r"\*[A-Z0-9]{4,}$")

# Store location/register numbers like " #1234" or " 123456" at the end
_STORE_NUMBER = re.compile(r"\s+\#?\d{4,}$")

# US ZIP codes at end of description: " 20166" or " 20166-1234"
_ZIP_CODE = re.compile(r"\s+\d{5}(?:-\d{4})?$")

# ---------------------------------------------------------------------------
# Category classification sets
# ---------------------------------------------------------------------------

# Categories that represent internal financial flows rather than real merchants.
# Merchant-level rollups (top_merchants) skip these categories entirely.
_NON_MERCHANT_CATEGORIES = {
    # Internal flows
    "Transfers",           # 63
    "Credit Card Payment", # 9
    # Income
    "Paycheck/Salary",     # 36
    "Net Salary",          # 37
    "Bonus",               # 31
    "Income",              # 30
    "Dividend",            # 32
    "Interest Income",     # 34
    "Investment Income",   # 35
    "Other Income",        # 33
    "Tax Refund",          # 38
    # Tax payments
    "Federal Tax",         # 77
    "State Tax",           # 62
    "Local Tax",           # 78
    "Taxes",               # 76
    "Social Security Tax", # 61
    "Medicare Tax",        # 79
    "SDI Tax",             # 60
    # Savings / investment contributions
    "Savings",             # 56
    "Investment Savings",  # 57
    "Retirement Savings",  # 58
    "College Savings",     # 110
}

# Categories that count as income (credits into the account).
# These name-based sets are used by planning.py and other callers that work with
# normalized transaction dicts.  _fetch_snb_data itself uses the ID-based sets
# below (more robust to category renames).
_INCOME_CATEGORIES = frozenset({
    "Paycheck/Salary",   # 36
    "Net Salary",        # 37
    "Bonus",             # 31
    "Income",            # 30
    "Dividend",          # 32
    "Interest Income",   # 34
    "Investment Income", # 35
    "Other Income",      # 33
    "Tax Refund",        # 38
})

# Pure internal flows excluded from both the income and spending totals.
# Including these would double-count money moving between your own accounts.
_EXCLUDE_CATEGORIES = frozenset({
    "Transfers",           # 63
    "Credit Card Payment", # 9
    "Excluded",            # -1  (manually hidden in Emoney)
})

# ---------------------------------------------------------------------------
# ID-based classification sets (Phase 2)
# ---------------------------------------------------------------------------
# These are the authoritative sets used by _fetch_snb_data.  Matching on the
# numeric categoryId is immune to category rename, which breaks string matching.
# The string-based sets above are kept for callers (planning.py, 50/30/20, etc.)
# that work with the normalised category name field.
_INCOME_CATEGORY_IDS = frozenset({
    30,   # Income
    31,   # Bonus
    32,   # Dividend
    33,   # Other Income
    34,   # Interest Income
    35,   # Investment Income
    36,   # Paycheck/Salary
    37,   # Net Salary
    38,   # Tax Refund
})

_EXCLUDE_CATEGORY_IDS = frozenset({
    -1,   # Excluded (manually hidden in Emoney)
    9,    # Credit Card Payment
    63,   # Transfers
})

# ---------------------------------------------------------------------------
# Recurring-charge detection constants
# ---------------------------------------------------------------------------

# Each tuple is (label, expected_gap_days, tolerance_days).
# A merchant is assigned a cadence when its average inter-charge gap falls
# within ±tolerance days of the expected gap.
_CADENCES = [
    ("weekly",     7,   4),
    ("biweekly",   14,  4),
    ("monthly",    30,  6),
    ("quarterly",  91, 10),
    ("annual",    365, 20),
]

# Multiplier to convert a per-occurrence amount to an estimated monthly cost.
_CADENCE_TO_MONTHLY = {
    "weekly":    30 / 7,   # ~4.3 charges per month
    "biweekly":  30 / 14,  # ~2.1 charges per month
    "monthly":   1.0,
    "quarterly": 1 / 3,    # 1 charge covers 3 months
    "annual":    1 / 12,   # 1 charge covers 12 months
}


def _detect_cadence(records_sorted: list[dict]) -> dict | None:
    """Detect a recurring cadence from charge records sorted oldest-first.

    Shared by get_recurring_charges and get_upcoming_bills so the gap/cadence
    math stays in one place. Each record needs a "date" (YYYY-MM-DD) and an
    "amount". Returns the matched cadence plus the derived stats, or None if
    there are fewer than 2 records or no cadence in _CADENCES matches.

    Returned dict: cadence, avg_gap_days, avg_amount, gap_variance, tolerance,
    dates, amounts.
    """
    if len(records_sorted) < 2:
        return None
    dates   = [r["date"] for r in records_sorted]
    amounts = [r["amount"] for r in records_sorted]

    gaps = []
    for i in range(1, len(dates)):
        d1 = datetime.strptime(dates[i - 1], "%Y-%m-%d")
        d2 = datetime.strptime(dates[i],     "%Y-%m-%d")
        gaps.append((d2 - d1).days)

    avg_gap      = sum(gaps) / len(gaps)
    avg_amount   = sum(amounts) / len(amounts)
    gap_variance = sum(abs(g - avg_gap) for g in gaps) / len(gaps)

    for cadence_name, cadence_days, tolerance in _CADENCES:
        if abs(avg_gap - cadence_days) <= tolerance:
            return {
                "cadence":      cadence_name,
                "avg_gap_days": avg_gap,
                "avg_amount":   avg_amount,
                "gap_variance": gap_variance,
                "tolerance":    tolerance,
                "dates":        dates,
                "amounts":      amounts,
            }
    return None

# ---------------------------------------------------------------------------
# In-memory TTL cache for SNB API responses
# ---------------------------------------------------------------------------
# _snb_raw_cache stores (fetch_timestamp, raw_txns_list, categories_dict).
# All SNB-based tools call _fetch_snb_data() → _fetch_snb_raw(), so within a
# single conversation turn the SNB API is hit only once regardless of how many
# tools are chained (e.g., savings_rate + income_summary + spending_trends).
#
# TTL is aligned with _CARD_CACHE_TTL in _helpers.py so both caches expire at
# the same time.  Set to None to force a fresh fetch (e.g., after session reset).
_snb_raw_cache: tuple[float, list, dict] | None = None
_SNB_CACHE_TTL: int = 300  # seconds (5 minutes)


def clear_snb_cache() -> None:
    """
    Purge the in-memory SNB raw-data cache.

    Called automatically by ``reset_session`` so that session resets never
    return cached transactions from a previous authenticated user.
    """
    global _snb_raw_cache
    _snb_raw_cache = None
    clear_snb_account_cache()


async def get_categories(http_session) -> dict:
    """
    Return all SNB spending category names mapped to their numeric IDs.

    Source: SNB API GetCategories endpoint (same cache as get_spending_transactions).
    Use the returned ``id`` values with update_transaction, add_transaction_rule,
    and update_transaction_rule.
    """
    ok, _, categories = await _fetch_snb_raw(http_session)
    if not ok:
        return {"error": "Could not retrieve SNB data. Try re-syncing Chrome session."}
    cats = []
    for k, v in categories.items():
        if not k or not v:
            continue
        try:
            cat_id = int(k)
        except (TypeError, ValueError):
            # Skip a malformed/non-numeric key rather than crashing the whole tool.
            _log.debug("Skipping non-numeric category key: %r", k)
            continue
        cats.append({"id": cat_id, "name": v})
    cats.sort(key=lambda x: x["name"])
    return {"category_count": len(cats), "categories": cats}


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
    """Extract JWT token and API key from the Spending/Transactions page HTML.

    Returns ("", "") on any network error so a transient blip on this one
    request degrades to a clean "could not retrieve" error in callers rather
    than crashing every SNB tool (per the never-raise convention). Mirrors the
    error handling already used by _fetch_snb_account_map.
    """
    try:
        http = await http_session.get_http()
        resp = await http.get(f"{BASE_URL}/ema/CS/Spending/Transactions", timeout=20)
        html = resp.text
    except Exception as e:
        _log.debug("SNB credential fetch failed: %s", type(e).__name__)
        return "", ""
    jwt_match = re.search(r'"JwtToken"\s*:\s*"([^"]+)"', html)
    key_match  = re.search(r'apiKey["\']?\s*:\s*["\']([^"\']+)["\']', html)
    jwt_token = jwt_match.group(1) if jwt_match else ""
    api_key   = key_match.group(1)  if key_match  else ""
    return jwt_token, api_key


def _snb_headers(jwt_token: str, api_key: str) -> dict:
    """Standard request headers for the SNB API (Bearer JWT + apikey)."""
    return {
        "Accept":        "application/json, text/plain, */*",
        "Authorization": f"Bearer {jwt_token}",
        "apikey":        api_key,
        "Origin":        BASE_URL,
    }


async def _fetch_snb_raw(http_session) -> tuple[bool, list, dict]:
    """
    Fetch and cache the complete SNB dataset: all transactions + category map.

    The SNB API returns ALL transactions client-side filtered, so fetching
    once and caching is strictly better than fetching per-tool.  Results are
    cached for ``_SNB_CACHE_TTL`` seconds (default 5 minutes).

    Returns (success, raw_txns_list, categories_dict).
    ``raw_txns_list`` is the full unfiltered list from the SNB API.
    Callers apply their own date/amount/category filters on top.
    """
    global _snb_raw_cache
    now = time.time()

    # Return cached data if still fresh
    if _snb_raw_cache is not None:
        cached_ts, cached_txns, cached_cats = _snb_raw_cache
        if now - cached_ts < _SNB_CACHE_TTL:
            return True, cached_txns, cached_cats

    # Cache miss — fetch credentials and both SNB endpoints
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return False, [], {}

    http = await http_session.get_http()
    snb_headers = _snb_headers(jwt_token, api_key)

    # Fetch category map (114 categories)
    categories: dict[str, str] = {}
    cat_resp = await http.get(f"{_SNB_API}/api/values/GetCategories",
                              headers=snb_headers, timeout=20)
    if cat_resp.status_code == 200 and "json" in cat_resp.headers.get("content-type", ""):
        for cat in cat_resp.json():
            categories[str(cat.get("id", ""))] = cat.get("name", "")

    # Fetch all transactions (SNB returns the full history; we filter client-side)
    txn_resp = await http.get(f"{_SNB_API}/api/values/GetFilteredTransactions",
                              headers=snb_headers, timeout=30)
    if txn_resp.status_code != 200 or "json" not in txn_resp.headers.get("content-type", ""):
        return False, [], {}

    # Strip deleted transactions at fetch time to keep the cache lean
    raw_txns = [t for t in txn_resp.json() if not t.get("isDeleted", False)]

    _snb_raw_cache = (now, raw_txns, categories)
    return True, raw_txns, categories


# ---------------------------------------------------------------------------
# SNB account map cache  (id → name)
# ---------------------------------------------------------------------------
_snb_account_cache: tuple[float, dict] | None = None


async def _fetch_snb_account_map(http_session) -> dict[str, str]:
    """
    Fetch the SNB account list and return a dict mapping account_id → account_name.

    The SNB ``GetAccounts`` endpoint returns every linked account with a stable
    numeric ``id`` that matches the ``accountId`` field on raw transactions.
    Results are cached for ``_SNB_CACHE_TTL`` seconds alongside the transaction cache.

    Returns an empty dict on failure (callers fall back to the raw accountId string).
    """
    global _snb_account_cache
    now = time.time()

    if _snb_account_cache is not None:
        cached_ts, cached_map = _snb_account_cache
        if now - cached_ts < _SNB_CACHE_TTL:
            return cached_map

    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return {}

    http = await http_session.get_http()
    snb_headers = _snb_headers(jwt_token, api_key)
    try:
        resp = await http.get(f"{_SNB_API}/api/values/GetAccounts",
                              headers=snb_headers, timeout=20)
        if resp.status_code == 200 and "json" in resp.headers.get("content-type", ""):
            account_map = {
                str(acct.get("id", "")): acct.get("name", "")
                for acct in resp.json()
                if acct.get("id")
            }
            _snb_account_cache = (now, account_map)
            return account_map
    except Exception as e:
        _log.debug("SNB account-map fetch failed: %s", type(e).__name__)
    return {}


def clear_snb_account_cache() -> None:
    """Purge the SNB account map cache."""
    global _snb_account_cache
    _snb_account_cache = None


async def _fetch_snb_data(http_session, days: int) -> tuple[list, bool]:
    """
    Return normalized SNB transactions filtered to the last ``days`` days.

    Internally calls ``_fetch_snb_raw()`` which caches the full dataset,
    so multiple tools calling this function within one conversation turn
    share a single HTTP round-trip to the SNB API.

    Each returned transaction dict has:
      date, description, category, category_id, amount,
      is_income, is_excluded, is_pending
    Returns ``([], False)`` on auth failure.
    """
    ok, raw_txns, categories = await _fetch_snb_raw(http_session)
    if not ok:
        return [], False

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    result = []
    for t in raw_txns:
        date_str = (t.get("date") or "")[:10]
        if date_str < cutoff:
            continue
        desc        = t.get("userDescription") or t.get("cleanDescription") or t.get("description", "")
        cat_id_str  = str(t.get("categoryId") or "")
        cat_id_int  = int(cat_id_str) if cat_id_str else 0
        category    = categories.get(cat_id_str, "Uncategorized") if cat_id_str else "Uncategorized"
        result.append({
            "date":        date_str,
            "description": desc,
            "category":    category,
            "category_id": cat_id_int or None,
            "amount":      abs(t.get("value", 0) or 0),
            # ID-based classification — immune to category renames
            "is_income":   cat_id_int in _INCOME_CATEGORY_IDS,
            "is_excluded": cat_id_int in _EXCLUDE_CATEGORY_IDS,
            "is_pending":  t.get("isPending", False),
        })

    result.sort(key=lambda t: t["date"], reverse=True)
    return result, True


# ---------------------------------------------------------------------------
# Shared SNB analytics helpers
# ---------------------------------------------------------------------------

def _sum_income_spending(txns: list) -> tuple[float, float]:
    """
    Sum income and spending amounts from a list of normalized SNB transactions.

    Skips excluded transactions (transfers, credit card payments).
    Returns (annual_income, annual_spending) both as positive floats.
    """
    income   = 0.0
    spending = 0.0
    for t in txns:
        if t.get("is_excluded"):
            continue
        if t.get("is_income"):
            income   += t.get("amount", 0)
        else:
            spending += t.get("amount", 0)
    return round(income, 2), round(spending, 2)


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
    if income and income > 0 and expenses is not None:
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

async def get_spending_transactions(
    http_session,
    days: int = 30,
    max_transactions: int = 100,
) -> dict:
    """
    Return bank/credit card transactions with category labels for the last ``days`` days.

    Source: SNB API (api.emoneyadvisor.com/snb-api) — uses the shared
    ``_fetch_snb_raw()`` cache so this call is free when preceded by any
    other SNB-based tool in the same conversation turn.

    Parameters
    ----------
    days             : look-back window (default 30, max 365)
    max_transactions : cap on returned transactions to control response size
                       (default 100; pass 0 for all — may be large for long periods)
    """
    days = min(max(days, 1), 365)

    ok, raw_txns, categories = await _fetch_snb_raw(http_session)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    # Client-side date filter
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    filtered = [t for t in raw_txns if (t.get("date") or "")[:10] >= cutoff]
    filtered.sort(key=lambda t: t.get("date", ""), reverse=True)

    transactions = []
    for t in filtered:
        desc    = t.get("userDescription") or t.get("cleanDescription") or t.get("description", "")
        cat_id  = str(t.get("categoryId") or "")
        cat_name = categories.get(cat_id, "Uncategorized") if cat_id else "Uncategorized"
        transactions.append({
            "transaction_id": t.get("id") or t.get("transactionId") or t.get("Id"),
            "date":           (t.get("date") or "")[:10],
            "description":    desc,
            "category":       cat_name,
            "category_id":    int(cat_id) if cat_id else None,
            "amount":         t.get("value", 0),
            "is_pending":     t.get("isPending", False),
            "is_split":       t.get("isSplit", False),
        })

    # Summarize by category (over ALL transactions, before truncation)
    cat_totals: dict[str, float] = {}
    for t in transactions:
        cat = t["category"]
        cat_totals[cat] = round(cat_totals.get(cat, 0) + abs(t["amount"]), 2)
    top_categories = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)[:10]

    # Summarize by merchant (over ALL transactions, before truncation)
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

    total_count = len(transactions)
    truncated   = False
    if max_transactions > 0 and total_count > max_transactions:
        transactions = transactions[:max_transactions]
        truncated    = True

    cutoff_display = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    result: dict = {
        "period_days":           days,
        "start_date":            cutoff_display,
        "end_date":              datetime.now().strftime("%Y-%m-%d"),
        "transaction_count":     total_count,
        "transactions_returned": len(transactions),
        "top_categories":        [{"category": c, "total": v} for c, v in top_categories],
        "top_merchants":         [
            {"merchant": e["display"], "total": e["total"], "transactions": e["count"]}
            for e in top_merchants
        ],
        "transactions":          transactions,
    }
    if truncated:
        result["note"] = (
            f"Showing {len(transactions)} of {total_count} transactions. "
            "Pass max_transactions=0 to retrieve all (response will be larger)."
        )
    return result


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
        dt = _month_offset(now, i)
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
    # Derive month labels from actual transaction span so the series never
    # drops the partial oldest calendar month (#156: days//30 can undercount).
    months_seen: set[str] = {t["date"][:7] for t in income_txns}
    if months_seen:
        month_labels = sorted(months_seen)
    else:
        months_back = max(1, days // 30)
        month_labels = [_month_offset(now, i).strftime("%Y-%m")
                        for i in range(months_back - 1, -1, -1)]

    monthly: dict[str, float] = {m: 0.0 for m in month_labels}
    month_set = set(month_labels)
    for t in income_txns:
        m = t["date"][:7]
        if m in month_set:
            monthly[m] = round(monthly[m] + t["amount"], 2)

    # Recompute total from labeled buckets so sum(monthly) == total_income.
    total_income = round(sum(monthly.values()), 2)

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
        dt = _month_offset(now, i)
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
    max_results: int = 100,
) -> dict:
    """
    Search spending transactions by keyword, category, and/or amount range.

    Parameters
    ----------
    query       : text to match in transaction description (case-insensitive)
    category    : category name filter (partial match, case-insensitive)
    days        : look-back window (default 365, max 365)
    min_amount  : minimum transaction amount
    max_amount  : maximum transaction amount (None = no upper limit)
    max_results : cap on returned transactions (default 100; pass 0 for all)
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

    # Summary stats computed over ALL matches (before truncation)
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

    total_matches = len(results)
    truncated     = False
    if max_results > 0 and total_matches > max_results:
        results   = results[:max_results]
        truncated = True

    out: dict = {
        "query":            query or "(all)",
        "category_filter":  category or "(all)",
        "period_days":      days,
        "start_date":       (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
        "end_date":         datetime.now().strftime("%Y-%m-%d"),
        "match_count":      total_matches,
        "total_amount":     total,
        "merchant_summary": merchant_summary,
        "category_summary": category_summary,
        "transactions":     results,
    }
    if truncated:
        out["note"] = (
            f"Showing {len(results)} of {total_matches} matches. "
            "Pass max_results=0 to retrieve all."
        )
    return out


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
        records_sorted = sorted(records, key=lambda r: r["date"])
        cad = _detect_cadence(records_sorted)
        if cad is None:
            continue

        monthly_cost = round(cad["avg_amount"] * _CADENCE_TO_MONTHLY[cad["cadence"]], 2)
        recurring.append({
            "merchant":          merchant,
            "cadence":           cad["cadence"],
            "avg_amount":        round(cad["avg_amount"], 2),
            "monthly_cost_est":  monthly_cost,
            "occurrences":       len(records),
            "last_charge":       cad["dates"][-1],
            "consistent":        cad["gap_variance"] < cad["tolerance"],
            "avg_gap_days":      round(cad["avg_gap_days"], 1),
        })

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
# get_budget_vs_actual  (Sprint 2)
# ---------------------------------------------------------------------------

async def get_budget_vs_actual(http_session, months_avg: int = 3) -> dict:
    """
    Compare this month's actual spending against rolling averages and any total
    budget configured in Emoney.

    For each spending category shows:
      • This month's actual spend
      • Rolling N-month average (``months_avg``, default 3)
      • Dollar and percent variance vs. the rolling average
      • Status: over_budget / on_track / under_budget

    Also pulls the total monthly budget from Card 13 (if set) and compares
    total actual spending this month against it.

    Parameters
    ----------
    months_avg : number of prior months used to compute the rolling average
                 benchmark (default 3, capped at 12)
    """
    months_avg = min(max(months_avg, 1), 12)
    days       = (months_avg + 1) * 31 + 5  # extra month for current month

    # Fetch data in parallel: card 13 (budget total) + SNB transactions
    http        = await http_session.get_http()
    card13_task = _get_card(http, 13)
    snb_task    = _fetch_snb_data(http_session, days=days)

    card13, (txns, ok) = await asyncio.gather(card13_task, snb_task)

    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    # Pull total budget from Card 13 (if configured)
    configured_budget = None
    if card13:
        bud = card13.get("Budget") or {}
        budgeted = bud.get("Budgeted")
        if budgeted and budgeted > 0:
            configured_budget = round(budgeted, 2)

    now        = datetime.now()
    this_month = now.strftime("%Y-%m")

    # Partial-month progress — used to add pace fields to the comparison (#158).
    days_in_month   = calendar.monthrange(now.year, now.month)[1]
    month_progress  = round(now.day / days_in_month, 4)  # fraction elapsed (0 < x <= 1)

    # Build month labels: [oldest avg month … one month ago] + [this_month]
    avg_months = []
    for i in range(months_avg, 0, -1):
        dt = _month_offset(now, i)
        avg_months.append(dt.strftime("%Y-%m"))
    all_months = avg_months + [this_month]
    month_set  = set(all_months)

    # Accumulate spending by (month, category)
    # Excludes income transactions and internal transfers
    month_cat: dict[str, dict[str, float]] = {m: {} for m in all_months}
    month_total: dict[str, float] = {m: 0.0 for m in all_months}

    for t in txns:
        m = t["date"][:7]
        if m not in month_set or t["is_excluded"] or t["is_income"]:
            continue
        cat = t["category"]
        month_cat[m][cat]   = round(month_cat[m].get(cat, 0)   + t["amount"], 2)
        month_total[m]      = round(month_total[m] + t["amount"], 2)

    # Collect all categories that appear in ANY month
    all_cats = set()
    for m in all_months:
        all_cats.update(month_cat[m].keys())

    # Compute per-category budget vs. actual
    category_comparison = []
    for cat in sorted(all_cats):
        avg_amounts = [month_cat[m].get(cat, 0.0) for m in avg_months]
        avg_spend   = round(sum(avg_amounts) / len(avg_amounts), 2) if avg_amounts else 0.0
        actual      = round(month_cat[this_month].get(cat, 0.0), 2)
        variance    = round(actual - avg_spend, 2)
        pct_var     = round((variance / avg_spend * 100), 1) if avg_spend > 0 else None

        if avg_spend > 0:
            if pct_var is not None and pct_var > 15:
                status = "over_budget"
            elif pct_var is not None and pct_var < -15:
                status = "under_budget"
            else:
                status = "on_track"
        else:
            status = "new_category" if actual > 0 else "no_activity"

        pace_projected = round(actual / month_progress, 2) if month_progress > 0 else None

        category_comparison.append({
            "category":            cat,
            "this_month_actual":   actual,
            "pace_projected_total": pace_projected,
            "rolling_avg":         avg_spend,
            "variance":            variance,
            "variance_pct":        pct_var,
            "status":              status,
        })

    # Sort by absolute variance descending (biggest overspend first)
    category_comparison.sort(key=lambda x: abs(x["variance"]), reverse=True)

    over_budget_cats  = [c for c in category_comparison if c["status"] == "over_budget"]
    under_budget_cats = [c for c in category_comparison if c["status"] == "under_budget"]

    # Total spending comparison
    actual_total   = round(month_total[this_month], 2)
    avg_total_spend = round(sum(month_total[m] for m in avg_months) / len(avg_months), 2) if avg_months else 0.0
    total_variance = round(actual_total - avg_total_spend, 2)

    budget_status = None
    if configured_budget:
        budget_status = {
            "configured_monthly_budget": configured_budget,
            "actual_this_month":         actual_total,
            "remaining":                 round(configured_budget - actual_total, 2),
            "on_track":                  actual_total <= configured_budget,
        }

    return {
        "as_of":               now.strftime("%Y-%m-%d"),
        "this_month":          this_month,
        "month_progress_pct":  round(month_progress * 100, 1),
        "benchmark_months":    avg_months,
        "total_this_month":    actual_total,
        "pace_projected_total": round(actual_total / month_progress, 2) if month_progress > 0 else None,
        "total_avg":           avg_total_spend,
        "total_variance":      total_variance,
        "configured_budget":   budget_status,
        "categories":          category_comparison,
        "over_budget_count":   len(over_budget_cats),
        "under_budget_count":  len(under_budget_cats),
        "top_overspend":       over_budget_cats[:5],
        "note": (
            f"Benchmark is the {months_avg}-month rolling average (prior months only). "
            "Over/under defined as >15% variance from the benchmark. "
            "Excludes transfers and credit card payments as internal flows. "
            f"this_month_actual and pace_projected_total reflect "
            f"{now.day} of {days_in_month} days elapsed ({round(month_progress*100,1)}% of month); "
            "compare pace_projected_total against rolling_avg for an apples-to-apples pace view."
        ),
    }


# ---------------------------------------------------------------------------
# get_year_over_year  (Sprint 2)
# ---------------------------------------------------------------------------

async def get_year_over_year(http_session) -> dict:
    """
    Compare this calendar year's spending and income to the same period last year.

    Uses up to 730 days of SNB transaction history to produce:
      • Year-to-date comparison: this year vs. last year through today's date
      • Per-category breakdown showing absolute and percent change
      • Income comparison year-over-year

    Useful for answering questions like:
      "Am I spending more this year than last year?"
      "How has my grocery spending changed year-over-year?"
    """
    txns, ok = await _fetch_snb_data(http_session, days=730)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    now          = datetime.now()
    this_year    = now.year
    last_year    = this_year - 1

    # YTD cutoff: same month-day last year (e.g., if today is Jun 9, compare Jan-Jun 9)
    ytd_cutoff   = now.strftime("%m-%d")  # "06-09"

    def _in_ytd(date_str: str, year: int) -> bool:
        """True if date_str falls within Jan 1 – today's calendar date in `year`."""
        if not date_str or len(date_str) < 10:
            return False
        d_year = date_str[:4]
        d_mmdd = date_str[5:10]
        return d_year == str(year) and d_mmdd <= ytd_cutoff

    # Accumulate per category for this_year YTD and last_year YTD
    this_ytd_cat:  dict[str, float] = {}
    last_ytd_cat:  dict[str, float] = {}
    this_ytd_income = 0.0
    last_ytd_income = 0.0
    this_ytd_spend  = 0.0
    last_ytd_spend  = 0.0

    for t in txns:
        date_str = t["date"]
        if t["is_excluded"]:
            continue
        is_this = _in_ytd(date_str, this_year)
        is_last = _in_ytd(date_str, last_year)
        if not is_this and not is_last:
            continue

        if t["is_income"]:
            if is_this:
                this_ytd_income = round(this_ytd_income + t["amount"], 2)
            else:
                last_ytd_income = round(last_ytd_income + t["amount"], 2)
        else:
            cat = t["category"]
            if is_this:
                this_ytd_cat[cat] = round(this_ytd_cat.get(cat, 0) + t["amount"], 2)
                this_ytd_spend    = round(this_ytd_spend + t["amount"], 2)
            else:
                last_ytd_cat[cat] = round(last_ytd_cat.get(cat, 0) + t["amount"], 2)
                last_ytd_spend    = round(last_ytd_spend + t["amount"], 2)

    # Build category-level comparison
    all_cats = set(this_ytd_cat) | set(last_ytd_cat)
    cat_rows = []
    for cat in sorted(all_cats):
        this_amt = this_ytd_cat.get(cat, 0.0)
        last_amt = last_ytd_cat.get(cat, 0.0)
        change   = round(this_amt - last_amt, 2)
        pct_chg  = round(change / last_amt * 100, 1) if last_amt > 0 else None
        cat_rows.append({
            "category":   cat,
            "this_year":  round(this_amt, 2),
            "last_year":  round(last_amt, 2),
            "change":     change,
            "change_pct": pct_chg,
            "direction":  "up" if change > 0 else ("down" if change < 0 else "flat"),
        })
    cat_rows.sort(key=lambda x: abs(x["change"]), reverse=True)

    spend_change = round(this_ytd_spend - last_ytd_spend, 2)
    spend_pct    = round(spend_change / last_ytd_spend * 100, 1) if last_ytd_spend > 0 else None
    income_change = round(this_ytd_income - last_ytd_income, 2)
    income_pct   = round(income_change / last_ytd_income * 100, 1) if last_ytd_income > 0 else None

    return {
        "comparison_type":       "year-to-date",
        "this_year":             this_year,
        "last_year":             last_year,
        "ytd_through":           now.strftime("%Y-%m-%d"),
        "spending": {
            "this_year":         round(this_ytd_spend, 2),
            "last_year":         round(last_ytd_spend, 2),
            "change":            spend_change,
            "change_pct":        spend_pct,
            "direction":         "up" if spend_change > 0 else ("down" if spend_change < 0 else "flat"),
        },
        "income": {
            "this_year":         round(this_ytd_income, 2),
            "last_year":         round(last_ytd_income, 2),
            "change":            income_change,
            "change_pct":        income_pct,
            "direction":         "up" if income_change > 0 else ("down" if income_change < 0 else "flat"),
        },
        "category_breakdown":    cat_rows,
        "biggest_increases":     [c for c in cat_rows if c["direction"] == "up"][:5],
        "biggest_decreases":     [c for c in cat_rows if c["direction"] == "down"][:5],
        "note": (
            "Compares Jan 1 – today for each year. Requires 2 years of SNB history; "
            "if last year's data is unavailable those totals will show $0."
        ),
    }


# ---------------------------------------------------------------------------
# get_cash_flow_projection  (Sprint 3)
# ---------------------------------------------------------------------------

async def get_cash_flow_projection(http_session, months_ahead: int = 6) -> dict:
    """
    Project future monthly cash flow based on actual income and spending history.

    Uses the last 90 days of SNB transactions to establish baseline monthly
    income and spending, then overlays the detected recurring charges for
    fixed-cost accuracy.  Projects the running cash balance forward assuming
    the current liquid cash balance as the starting point.

    Parameters
    ----------
    months_ahead : number of months to project (default 6, max 24)
    """
    import asyncio
    months_ahead = min(max(months_ahead, 1), 24)

    # Fetch SNB data and card 9 (net worth / liquid assets) in parallel
    snb_task   = _fetch_snb_data(http_session, days=90)
    http       = await http_session.get_http()
    card9_task = _get_card(http, 9)

    (txns, ok), card9 = await asyncio.gather(snb_task, card9_task)

    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    # Derive starting liquid balance from card 9 if available
    starting_cash = None
    if card9:
        # Card 9 returns Assets/Liabilities; we use net worth as a proxy for liquid
        # (a rough approximation — no card directly exposes liquid cash separately)
        starting_cash = card9.get("NetWorth")

    # --- Compute 3-month average monthly income and spending ---
    now = datetime.now()
    month_income:  dict[str, float] = {}
    month_spending: dict[str, float] = {}

    months_seen: set[str] = set()
    for t in txns:
        m = t["date"][:7]
        months_seen.add(m)
        if t["is_excluded"]:
            continue
        if t["is_income"]:
            month_income[m]   = round(month_income.get(m, 0)   + t["amount"], 2)
        else:
            month_spending[m] = round(month_spending.get(m, 0) + t["amount"], 2)

    # Only count complete months (not the current partial month)
    this_month = now.strftime("%Y-%m")
    complete_months = sorted([m for m in months_seen if m < this_month])[-3:]

    if not complete_months:
        return {"error": "Insufficient transaction history for projection (need at least 1 complete month)."}

    avg_income   = round(sum(month_income.get(m, 0)   for m in complete_months) / len(complete_months), 2)
    avg_spending = round(sum(month_spending.get(m, 0) for m in complete_months) / len(complete_months), 2)
    avg_net      = round(avg_income - avg_spending, 2)

    # Build projection months
    projection = []
    running_balance = starting_cash  # None if unavailable

    for i in range(1, months_ahead + 1):
        # Calculate the projected month label
        target_dt = _month_offset(now, -i)
        month_label = target_dt.strftime("%Y-%m")

        proj_income   = avg_income
        proj_spending = avg_spending
        proj_net      = round(proj_income - proj_spending, 2)

        if running_balance is not None:
            running_balance = round(running_balance + proj_net, 2)

        projection.append({
            "month":              month_label,
            "projected_income":   proj_income,
            "projected_spending": proj_spending,
            "projected_net":      proj_net,
            "running_balance":    running_balance,
        })

    savings_rate = round(avg_net / avg_income * 100, 1) if avg_income > 0 else None

    return {
        "as_of":                now.strftime("%Y-%m-%d"),
        "months_ahead":         months_ahead,
        "baseline_months":      complete_months,
        "baseline_avg_monthly": {
            "income":       avg_income,
            "spending":     avg_spending,
            "net":          avg_net,
            "savings_rate_pct": savings_rate,
        },
        "starting_balance":     starting_cash,
        "projection":           projection,
        "note": (
            "Projection uses a flat baseline (3-month average) — it does not model "
            "seasonal variation, planned large expenses, or income changes. "
            "Starting balance is net worth from Emoney (a rough proxy for liquid cash). "
            "For a more precise projection, add planned expenses manually."
        ),
    }


# ---------------------------------------------------------------------------
# get_unusual_transactions  (v0.8.0)
# ---------------------------------------------------------------------------

async def get_unusual_transactions(
    http_session,
    days: int = 90,
    threshold_pct: float = 150.0,
) -> dict:
    """
    Flag transactions that are unusually large compared to the merchant's
    or category's historical average.

    Parameters
    ----------
    days          : look-back window to establish baselines (default 90)
    threshold_pct : flag a transaction when it exceeds this % of the merchant's
                    average amount (default 150 = 50% above average)
    """
    days = min(max(days, 30), 365)
    threshold_pct = max(threshold_pct, 110.0)

    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    spending_txns = [
        t for t in txns
        if not t["is_income"] and not t["is_excluded"] and not t["is_pending"]
    ]

    # --- Per-merchant stats ---
    merchant_totals: dict[str, list[float]] = {}
    for t in spending_txns:
        key = _normalize_merchant(t["description"])
        merchant_totals.setdefault(key, []).append(t["amount"])

    merchant_avg: dict[str, float] = {
        k: sum(v) / len(v) for k, v in merchant_totals.items() if len(v) >= 2
    }

    # --- Per-category monthly averages (over all full months in window) ---
    now = datetime.now()
    this_month = now.strftime("%Y-%m")
    cat_month: dict[str, dict[str, float]] = {}
    for t in spending_txns:
        m = t["date"][:7]
        if m == this_month:
            continue  # exclude current partial month from baseline
        cat = t["category"]
        cat_month.setdefault(cat, {}).setdefault(m, 0.0)
        cat_month[cat][m] += t["amount"]

    cat_monthly_avg: dict[str, float] = {}
    for cat, by_month in cat_month.items():
        if by_month:
            cat_monthly_avg[cat] = sum(by_month.values()) / len(by_month)

    # --- Flag unusual transactions ---
    flagged = []
    for t in spending_txns:
        key = _normalize_merchant(t["description"])
        amount = t["amount"]
        reasons = []

        # Merchant-level check
        if key in merchant_avg:
            avg = merchant_avg[key]
            if avg > 0 and amount >= avg * (threshold_pct / 100):
                pct_above = round((amount / avg - 1) * 100, 1)
                reasons.append(f"{pct_above}% above this merchant's average of ${avg:,.2f}")

        # Category-level check (flag if > 2× monthly average for this category)
        cat = t["category"]
        if cat in cat_monthly_avg:
            cat_avg = cat_monthly_avg[cat]
            if cat_avg > 0 and amount >= cat_avg * 2:
                reasons.append(
                    f"single transaction exceeds 2× the monthly avg for {cat} (${cat_avg:,.2f}/mo)"
                )

        if reasons:
            flagged.append({
                "date":        t["date"],
                "merchant":    key,
                "description": t["description"],
                "category":    t["category"],
                "amount":      round(amount, 2),
                "merchant_avg": round(merchant_avg.get(key, 0), 2) or None,
                "reasons":     reasons,
            })

    flagged.sort(key=lambda x: x["date"], reverse=True)
    total_flagged_amount = round(sum(f["amount"] for f in flagged), 2)

    return {
        "period_days":          days,
        "threshold_pct":        threshold_pct,
        "unusual_count":        len(flagged),
        "total_flagged_amount": total_flagged_amount,
        "unusual_transactions": flagged,
        "note": (
            f"Flags transactions that exceed {threshold_pct:.0f}% of the merchant's "
            "average or are 2× the monthly category average. "
            "Merchants with only one historical charge are excluded from merchant-level checks."
        ),
    }


# ---------------------------------------------------------------------------
# get_merchant_spending  (v0.8.0)
# ---------------------------------------------------------------------------

async def get_merchant_spending(
    http_session,
    days: int = 365,
    merchant: str = "",
    limit: int = 25,
) -> dict:
    """
    Return spending totals grouped by normalized merchant name.

    Parameters
    ----------
    days     : look-back window (default 365)
    merchant : optional substring filter on merchant name (case-insensitive)
    limit    : number of top merchants to return (default 25)
    """
    days  = min(max(days, 7), 365)
    limit = min(max(limit, 1), 200)

    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    merchant_filter = merchant.strip().upper()

    merchant_data: dict[str, dict] = {}
    for t in txns:
        if t["is_excluded"] or t["is_income"] or t["category"] in _NON_MERCHANT_CATEGORIES:
            continue
        key = _normalize_merchant(t["description"])
        if merchant_filter and merchant_filter not in key:
            continue
        entry = merchant_data.setdefault(key, {
            "merchant":  key,
            "total":     0.0,
            "count":     0,
            "dates":     [],
            "category":  t["category"],
        })
        entry["total"] = round(entry["total"] + t["amount"], 2)
        entry["count"] += 1
        entry["dates"].append(t["date"])

    results = []
    for entry in merchant_data.values():
        results.append({
            "merchant":        entry["merchant"],
            "total":           entry["total"],
            "transaction_count": entry["count"],
            "avg_transaction": round(entry["total"] / entry["count"], 2),
            "last_date":       max(entry["dates"]),
            "first_date":      min(entry["dates"]),
            "category":        entry["category"],
        })

    results.sort(key=lambda x: x["total"], reverse=True)
    total_tracked = round(sum(r["total"] for r in results), 2)
    results = results[:limit]

    return {
        "period_days":   days,
        "start_date":    (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
        "end_date":      datetime.now().strftime("%Y-%m-%d"),
        "merchant_filter": merchant or "(all)",
        "total_tracked": total_tracked,
        "merchant_count_shown": len(results),
        "merchants":     results,
        "note": (
            "Merchant names are normalized (POS prefixes, location suffixes, and ZIP codes stripped). "
            "Excludes transfers, credit card payments, income, and other non-merchant categories."
        ),
    }


# ---------------------------------------------------------------------------
# get_cash_flow_forecast  (v0.8.0)
# ---------------------------------------------------------------------------

async def get_cash_flow_forecast(
    http_session,
    months: int = 3,
) -> dict:
    """
    Project future monthly cash flow broken into recurring vs. discretionary spending.

    Uses actual recurring charges (from get_recurring_charges) for the fixed
    cost component and recent transaction history for the discretionary baseline.

    Parameters
    ----------
    months : number of months to project (1–6, default 3)
    """
    months = min(max(months, 1), 6)

    # Fetch SNB data and recurring charges in parallel
    snb_task   = _fetch_snb_data(http_session, days=90)
    recur_task = get_recurring_charges(http_session)

    (txns, ok), recurring_result = await asyncio.gather(snb_task, recur_task)

    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    now = datetime.now()
    this_month = now.strftime("%Y-%m")

    # --- Build baseline from complete past months ---
    month_income:   dict[str, float] = {}
    month_spending: dict[str, float] = {}
    months_seen: set[str] = set()

    for t in txns:
        m = t["date"][:7]
        months_seen.add(m)
        if t["is_excluded"]:
            continue
        if t["is_income"]:
            month_income[m]   = round(month_income.get(m, 0)   + t["amount"], 2)
        else:
            month_spending[m] = round(month_spending.get(m, 0) + t["amount"], 2)

    complete_months = sorted([m for m in months_seen if m < this_month])[-3:]
    if not complete_months:
        return {"error": "Insufficient history (need at least 1 complete month)."}

    avg_income   = round(sum(month_income.get(m, 0)   for m in complete_months) / len(complete_months), 2)
    avg_spending = round(sum(month_spending.get(m, 0) for m in complete_months) / len(complete_months), 2)

    # --- Recurring fixed costs (from get_recurring_charges result) ---
    recurring_monthly = 0.0
    recurring_items = []
    if "error" not in recurring_result:
        recurring_monthly = recurring_result.get("total_monthly_est", 0.0)
        for r in recurring_result.get("all_recurring", []):
            recurring_items.append({
                "merchant":   r["merchant"],
                "monthly_est": r["monthly_cost_est"],
                "cadence":    r["cadence"],
            })

    # Discretionary = total spending minus recurring fixed costs
    discretionary_avg = max(0.0, round(avg_spending - recurring_monthly, 2))

    # --- Build forecast ---
    forecast = []
    for i in range(1, months + 1):
        target_dt   = _month_offset(now, -i)
        month_label = target_dt.strftime("%Y-%m")
        proj_income = avg_income
        proj_recurring = round(recurring_monthly, 2)
        proj_discretionary = discretionary_avg
        proj_total_spending = round(proj_recurring + proj_discretionary, 2)
        proj_net    = round(proj_income - proj_total_spending, 2)

        forecast.append({
            "month":                month_label,
            "projected_income":     proj_income,
            "projected_expenses": {
                "recurring":        proj_recurring,
                "discretionary":    proj_discretionary,
                "total":            proj_total_spending,
            },
            "projected_net":        proj_net,
            "savings_rate_pct":     round(proj_net / proj_income * 100, 1) if proj_income > 0 else None,
        })

    return {
        "as_of":            now.strftime("%Y-%m-%d"),
        "months_ahead":     months,
        "baseline_months":  complete_months,
        "avg_monthly_income":       avg_income,
        "avg_monthly_spending":     avg_spending,
        "recurring_fixed_monthly":  round(recurring_monthly, 2),
        "discretionary_avg_monthly": discretionary_avg,
        "recurring_items":  sorted(recurring_items, key=lambda x: x["monthly_est"], reverse=True),
        "forecast":         forecast,
        "note": (
            "Recurring costs from 120-day charge pattern detection. "
            "Discretionary = average spending minus recurring. "
            "Income and spending baselines from the most recent 3 complete months."
        ),
    }


# ---------------------------------------------------------------------------
# explore_snb_write_endpoints
# ---------------------------------------------------------------------------

async def explore_snb_write_endpoints(http_session) -> dict:
    """
    Probe the SNB API for writable endpoints that might support transaction
    category updates.

    Strategy
    --------
    1. Fetch a real JWT + apiKey from the Spending page (same as reads).
    2. Pull the first available transaction id and category id from the live
       transaction list — needed to construct realistic probe payloads.
    3. Send OPTIONS requests to candidate write endpoints to check whether
       the server advertises PUT/PATCH/POST methods.
    4. Attempt a dry-run GET on each candidate endpoint to distinguish
       404 (endpoint absent) from 401/403/405 (endpoint exists but blocked).

    No data is modified.  Every request is either OPTIONS or a GET that
    cannot mutate state.
    """
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return {"error": "Could not retrieve SNB credentials. Try sync_chrome_session first."}

    http = await http_session.get_http()
    headers = _snb_headers(jwt_token, api_key)

    # Grab a real transaction id + category id to use in probe URLs
    sample_txn_id  = None
    sample_cat_id  = None
    ok, raw_txns, categories = await _fetch_snb_raw(http_session)
    if ok and raw_txns:
        t = raw_txns[0]
        sample_txn_id = t.get("id") or t.get("transactionId") or t.get("Id")
        sample_cat_id = str(t.get("categoryId") or "")

    # Candidate write endpoints — common REST patterns for transaction management
    base = _SNB_API
    candidates = [
        # Category update patterns
        f"{base}/api/values/UpdateTransactionCategory",
        f"{base}/api/values/SetTransactionCategory",
        f"{base}/api/values/UpdateCategory",
        f"{base}/api/transaction/category",
        f"{base}/api/transactions/category",
        # General transaction update patterns
        f"{base}/api/values/UpdateTransaction",
        f"{base}/api/values/EditTransaction",
        f"{base}/api/values/PatchTransaction",
        f"{base}/api/transaction",
        f"{base}/api/transactions",
        # RESTful with ID
        *(
            [
                f"{base}/api/values/UpdateTransaction/{sample_txn_id}",
                f"{base}/api/transaction/{sample_txn_id}",
                f"{base}/api/transactions/{sample_txn_id}",
                f"{base}/api/transactions/{sample_txn_id}/category",
            ]
            if sample_txn_id else []
        ),
        # Description / label update
        f"{base}/api/values/UpdateTransactionDescription",
        f"{base}/api/values/SetUserDescription",
        # Split transaction
        f"{base}/api/values/SplitTransaction",
        f"{base}/api/values/Split",
        # Delete / exclude
        f"{base}/api/values/DeleteTransaction",
        f"{base}/api/values/ExcludeTransaction",
        f"{base}/api/values/HideTransaction",
    ]

    results = []
    for url in candidates:
        probe = {"url": url, "options_methods": None, "get_status": None, "assessment": ""}

        # OPTIONS probe — does the server advertise write methods?
        try:
            opt_resp = await http.options(url, headers=headers, timeout=10)
            allow = opt_resp.headers.get("Allow", "") or opt_resp.headers.get("allow", "")
            probe["options_status"] = opt_resp.status_code
            probe["options_allow_header"] = allow
            probe["options_methods"] = [m.strip() for m in allow.split(",") if m.strip()]
        except Exception as e:
            probe["options_status"] = f"error: {e}"

        # GET probe — distinguish 404 (missing) from 401/403/405 (present but restricted)
        try:
            get_resp = await http.get(url, headers=headers, timeout=10)
            probe["get_status"] = get_resp.status_code
            if get_resp.status_code == 404:
                probe["assessment"] = "endpoint not found"
            elif get_resp.status_code in (401, 403):
                probe["assessment"] = "endpoint EXISTS — auth/permission issue (promising)"
            elif get_resp.status_code == 405:
                probe["assessment"] = "endpoint EXISTS — GET not allowed, write methods may work"
            elif get_resp.status_code == 200:
                probe["assessment"] = "endpoint EXISTS and readable via GET"
                try:
                    probe["sample_response"] = get_resp.json()
                except Exception:
                    probe["sample_response"] = get_resp.text[:200]
            else:
                probe["assessment"] = f"unexpected status {get_resp.status_code}"
        except Exception as e:
            probe["get_status"] = f"error: {e}"

        results.append(probe)

    promising = [r for r in results if "EXISTS" in str(r.get("assessment", ""))]
    write_capable = [
        r for r in results
        if any(m in (r.get("options_methods") or []) for m in ["PUT", "PATCH", "POST"])
    ]

    return {
        "sample_transaction_id": sample_txn_id,
        "sample_category_id":    sample_cat_id,
        "total_endpoints_probed": len(results),
        "promising_endpoints":   promising,
        "write_capable_by_options": write_capable,
        "all_results":           results,
        "next_steps": (
            "Endpoints marked 'EXISTS' should be probed with POST/PUT/PATCH next. "
            "Use the sample_transaction_id and sample_category_id to build a test payload. "
            "Check options_allow_header on promising endpoints for supported HTTP methods."
        ),
    }


# ---------------------------------------------------------------------------
# Category → 50/30/20 bucket map
# ---------------------------------------------------------------------------
# Every SNB category is classified as "needs", "wants", or "savings".
# Categories not in either explicit set default to "wants".
_NEEDS_CATEGORIES = frozenset({
    # Food & grocery staples
    "Groceries",              # 22
    # Housing
    "Home",                   # 25
    "Mortgage & Rent",        # 50
    "Mortgage Interest",      # 52
    "Mortgage Principal",     # 51
    "Mortgage Escrow",        # 53
    "Household Services",     # 27
    "Home Improvement/Maintenance",  # 28
    # Utilities
    "Bills & Utilities",      # 70
    "Energy, Gas & Electric", # 72
    "Phone, Internet & Cable",# 71
    "Water",                  # 75
    "Garbage & Recycling",    # 73
    "Sewer",                  # 74
    # Insurance
    "Insurance",              # 39
    "Health Insurance",       # 44
    "Auto Insurance",         # 46
    "Homeowner Insurance",    # 41
    "Life Insurance",         # 42
    "Disability Insurance",   # 40
    "LTC Insurance",          # 43
    "Umbrella Insurance",     # 45
    "Whole Life Insurance",   # 101
    # Healthcare
    "Health & Fitness",       # 24
    "Medical",                # 48
    "Doctor",                 # 89
    "Dentist",                # 88
    "Pharmacy",               # 90
    "Vision",                 # 109
    # Transportation (commuting needs)
    "Auto & Transport",       # 64
    "Gas & Fuel",             # 65
    "Auto Service",           # 66
    "Auto Payment",           # 67
    "Auto Registration",      # 68
    "Public Transport",       # 83
    "Parking",                # 111
    "Tolls",                  # 113
    # Family & education
    "Childcare & Daycare",    # 5
    "Education",              # 10
    "Tuition",                # 104
    "Kids",                   # 4
    # Debt minimums (non-mortgage)
    "Loan",                   # 47
    "Student Loan",           # 108
    # Taxes
    "Property Tax",           # 81
    "Federal Tax",            # 77
    "State Tax",              # 62
    "Local Tax",              # 78
    "Taxes",                  # 76
    "Social Security Tax",    # 61
    "Medicare Tax",           # 79
    "SDI Tax",                # 60
})

_SAVINGS_CATEGORIES = frozenset({
    # Direct savings / investment contributions
    "Savings",             # 56
    "Investment Savings",  # 57
    "Retirement Savings",  # 58
    "College Savings",     # 110
})


async def get_50_30_20_analysis(http_session, months: int = 3) -> dict:
    """
    Classify spending into Needs / Wants / Savings buckets and compare against
    the 50/30/20 guideline.

    Needs   (target 50% of income): housing, groceries, utilities, insurance,
                                     healthcare, transportation, minimum debt payments
    Wants   (target 30% of income): dining, entertainment, shopping, travel,
                                     subscriptions, personal care, hobbies
    Savings (target 20% of income): retirement contributions, savings transfers,
                                     extra debt payoff

    Parameters
    ----------
    months : number of complete months to average (default 3, max 12)
    """
    months = min(max(months, 1), 12)
    # Fetch one extra month so we can drop the current partial month and still
    # average over `months` complete months.
    days   = (months + 1) * 31 + 5

    txns, ok = await _fetch_snb_data(http_session, days=days)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    now = datetime.now()
    # Average over the most recent COMPLETE months, excluding the current partial
    # month (consistent with get_cash_flow_projection / get_cash_flow_forecast).
    # Including it would skew percentages by the day of month the tool is called.
    month_labels = []
    for i in range(months, 0, -1):
        dt = _month_offset(now, i)
        month_labels.append(dt.strftime("%Y-%m"))
    month_set = set(month_labels)

    monthly_income:   dict[str, float] = {m: 0.0 for m in month_labels}
    monthly_needs:    dict[str, float] = {m: 0.0 for m in month_labels}
    monthly_wants:    dict[str, float] = {m: 0.0 for m in month_labels}
    monthly_savings:  dict[str, float] = {m: 0.0 for m in month_labels}

    category_buckets: dict[str, dict[str, float]] = {
        "needs": {}, "wants": {}, "savings": {},
    }

    for t in txns:
        m = t["date"][:7]
        if m not in month_set or t["is_excluded"]:
            continue
        cat = t["category"]
        amt = t["amount"]

        if t["is_income"]:
            monthly_income[m] = round(monthly_income[m] + amt, 2)
            continue

        if cat in _SAVINGS_CATEGORIES:
            bucket = "savings"
            monthly_savings[m] = round(monthly_savings[m] + amt, 2)
        elif cat in _NEEDS_CATEGORIES:
            bucket = "needs"
            monthly_needs[m] = round(monthly_needs[m] + amt, 2)
        else:
            bucket = "wants"
            monthly_wants[m] = round(monthly_wants[m] + amt, 2)

        category_buckets[bucket][cat] = round(category_buckets[bucket].get(cat, 0) + amt, 2)

    def _avg(d: dict) -> float:
        vals = list(d.values())
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    avg_income  = _avg(monthly_income)
    avg_needs   = _avg(monthly_needs)
    avg_wants   = _avg(monthly_wants)
    avg_savings = _avg(monthly_savings)

    def _pct(val: float) -> float | None:
        return round(val / avg_income * 100, 1) if avg_income > 0 else None

    def _status(actual_pct: float | None, target_pct: float) -> str:
        if actual_pct is None:
            return "unknown"
        if actual_pct <= target_pct * 1.05:
            return "on_track"
        elif actual_pct <= target_pct * 1.20:
            return "slightly_over"
        return "over_target"

    def _top_cats(bucket: str, n: int = 5) -> list:
        return sorted(
            [{"category": k, "monthly_avg": round(v / months, 2)} for k, v in category_buckets[bucket].items()],
            key=lambda x: x["monthly_avg"], reverse=True
        )[:n]

    needs_pct   = _pct(avg_needs)
    wants_pct   = _pct(avg_wants)
    savings_pct = _pct(avg_savings)

    recommendations = []
    if needs_pct and needs_pct > 55:
        recommendations.append(f"Needs ({needs_pct:.0f}% of income) are above the 50% target — look for ways to reduce fixed costs like housing, insurance, or transportation.")
    if wants_pct and wants_pct > 35:
        recommendations.append(f"Wants ({wants_pct:.0f}% of income) are above the 30% target — top categories: {', '.join(c['category'] for c in _top_cats('wants', 3))}.")
    if savings_pct and savings_pct < 15:
        recommendations.append(f"Savings ({savings_pct:.0f}% of income) are below the 20% target — consider automating a recurring transfer to savings or increasing 401k deferral.")
    if not recommendations:
        recommendations.append("Your 50/30/20 split looks healthy. Keep maintaining the current balance.")

    return {
        "period_months": months,
        "as_of":         now.strftime("%Y-%m-%d"),
        "avg_monthly_income": avg_income,
        "needs": {
            "monthly_avg":   avg_needs,
            "actual_pct":    needs_pct,
            "target_pct":    50,
            "status":        _status(needs_pct, 50),
            "top_categories": _top_cats("needs"),
        },
        "wants": {
            "monthly_avg":   avg_wants,
            "actual_pct":    wants_pct,
            "target_pct":    30,
            "status":        _status(wants_pct, 30),
            "top_categories": _top_cats("wants"),
        },
        "savings": {
            "monthly_avg":   avg_savings,
            "actual_pct":    savings_pct,
            "target_pct":    20,
            "status":        _status(savings_pct, 20),
            "top_categories": _top_cats("savings"),
        },
        "recommendations": recommendations,
        "note": (
            "Category-to-bucket mapping uses standard rules; review top_categories in each bucket "
            "to confirm the classification fits your situation. "
            "Savings bucket includes paycheck deposits captured as income — net savings rate may differ."
        ),
    }


async def get_spending_by_account(http_session, days: int = 30) -> dict:
    """
    Break down spending by which linked bank or credit card account generated it.

    Uses the SNB raw transaction payload which includes an account identifier.
    Useful for families with multiple cards to see which account is being used
    for which spending categories.

    Parameters
    ----------
    days : look-back window (default 30, max 365)
    """
    days = min(max(days, 1), 365)

    # Fetch raw transactions and SNB account map in parallel
    import asyncio as _asyncio
    (ok, raw_txns, categories), account_map = await _asyncio.gather(
        _fetch_snb_raw(http_session),
        _fetch_snb_account_map(http_session),
    )
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    account_data: dict[str, dict] = {}

    for t in raw_txns:
        date_str = (t.get("date") or "")[:10]
        if date_str < cutoff:
            continue
        if t.get("isDeleted", False):
            continue

        acct_id = str(t.get("accountId") or t.get("AccountId") or "unknown")
        # Prefer the authoritative account map from GetAccounts; fall back to
        # any name embedded in the transaction payload, then the raw ID.
        acct_name = (
            account_map.get(acct_id)
            or t.get("accountName") or t.get("AccountName")
            or acct_id
        )

        cat_id   = str(t.get("categoryId") or "")
        cat_name = categories.get(cat_id, "Uncategorized") if cat_id else "Uncategorized"
        amount   = t.get("value", 0) or 0

        # Skip income / excluded categories (ID-based for robustness)
        cat_id_int = int(cat_id) if cat_id else 0
        if cat_id_int in _INCOME_CATEGORY_IDS or cat_id_int in _EXCLUDE_CATEGORY_IDS:
            continue

        if acct_id not in account_data:
            account_data[acct_id] = {
                "account_id":   acct_id,
                "account_name": acct_name,
                "total_spent":  0.0,
                "tx_count":     0,
                "categories":   {},
            }

        entry = account_data[acct_id]
        entry["total_spent"] = round(entry["total_spent"] + abs(amount), 2)
        entry["tx_count"]   += 1
        entry["categories"][cat_name] = round(entry["categories"].get(cat_name, 0) + abs(amount), 2)

    accounts_out = []
    for entry in sorted(account_data.values(), key=lambda x: x["total_spent"], reverse=True):
        top_cats = sorted(
            [{"category": k, "total": v} for k, v in entry["categories"].items()],
            key=lambda x: x["total"], reverse=True
        )[:5]
        accounts_out.append({
            "account_id":        entry["account_id"],
            "account_name":      entry["account_name"],
            "total_spent":       entry["total_spent"],
            "transaction_count": entry["tx_count"],
            "top_categories":    top_cats,
        })

    no_acct_id = len([a for a in accounts_out if a["account_id"] == "unknown"]) > 0

    return {
        "period_days": days,
        "start_date":  (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d"),
        "end_date":    datetime.now().strftime("%Y-%m-%d"),
        "accounts":    accounts_out,
        "account_count": len(accounts_out),
        "note": (
            "Excludes income, transfers, and credit card payments. "
            + ("Some transactions have no account identifier — shown under 'unknown'. " if no_acct_id else "")
        ),
    }


async def get_upcoming_bills(http_session, days_ahead: int = 30) -> dict:
    """
    Project recurring bill charges expected in the next N days.

    Uses the same cadence-detection logic as get_recurring_charges to identify
    recurring merchants, then projects when each charge is next expected based
    on the last observed charge date.

    Parameters
    ----------
    days_ahead : forecast horizon in days (default 30)
    """
    days_ahead = min(max(days_ahead, 7), 90)

    ok, raw_txns, categories = await _fetch_snb_raw(http_session)
    if not ok:
        return {"error": "Could not retrieve SNB transaction data. Try re-syncing Chrome session."}

    now     = datetime.now()
    cutoff  = (now - timedelta(days=120)).strftime("%Y-%m-%d")

    # Build per-merchant charge history (last 120 days, spending only)
    merchant_charges: dict[str, list[dict]] = {}
    for t in raw_txns:
        date_str = (t.get("date") or "")[:10]
        if date_str < cutoff or t.get("isDeleted", False):
            continue
        cat_id     = str(t.get("categoryId") or "")
        cat_name   = categories.get(cat_id, "Uncategorized") if cat_id else "Uncategorized"
        cat_id_int = int(cat_id) if cat_id else 0
        # Skip income / excluded (ID-based for robustness)
        if cat_id_int in _INCOME_CATEGORY_IDS or cat_id_int in _EXCLUDE_CATEGORY_IDS:
            continue
        amount = t.get("value", 0) or 0
        if amount >= 0:
            continue  # only outflows
        key = _normalize_merchant(
            t.get("userDescription") or t.get("cleanDescription") or t.get("description", "")
        )
        if key not in merchant_charges:
            merchant_charges[key] = []
        merchant_charges[key].append({"date": date_str, "amount": abs(amount), "category": cat_name})

    upcoming = []
    today_str = now.strftime("%Y-%m-%d")

    for merchant, charges in merchant_charges.items():
        charges_sorted = sorted(charges, key=lambda c: c["date"])
        cad = _detect_cadence(charges_sorted)
        if cad is None:
            continue
        dates        = cad["dates"]
        avg_gap      = cad["avg_gap_days"]
        avg_amount   = cad["avg_amount"]
        cadence_name = cad["cadence"]

        last_date     = datetime.strptime(dates[-1], "%Y-%m-%d")
        next_expected = last_date + timedelta(days=round(avg_gap))
        next_str      = next_expected.strftime("%Y-%m-%d")
        days_until    = (next_expected - now).days

        overdue = next_str < today_str  # charge expected but not yet seen
        horizon = (now + timedelta(days=days_ahead)).strftime("%Y-%m-%d")

        if overdue or next_str <= horizon:
            upcoming.append({
                "merchant":        merchant,
                "category":        charges_sorted[-1].get("category", "Uncategorized"),
                "expected_date":   next_str,
                "expected_amount": round(avg_amount, 2),
                "cadence":         cadence_name,
                "days_until":      days_until,
                "overdue":         overdue,
                "last_charge_date": dates[-1],
            })

    upcoming.sort(key=lambda x: x["expected_date"])
    total_expected = round(sum(u["expected_amount"] for u in upcoming if not u["overdue"]), 2)
    overdue_count  = sum(1 for u in upcoming if u["overdue"])

    return {
        "as_of":                  today_str,
        "days_ahead":             days_ahead,
        "upcoming_count":         len(upcoming),
        "overdue_count":          overdue_count,
        "total_expected_amount":  total_expected,
        "upcoming":               upcoming,
        "note": (
            "Based on 120 days of charge history. 'Overdue' means the charge was expected "
            "but has not yet appeared in the transaction feed — it may be processing or "
            "the subscription may have been cancelled."
        ),
    }
