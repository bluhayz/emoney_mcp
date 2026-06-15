"""
Tests for v1.0.0 spending tools:
  - get_50_30_20_analysis
  - get_spending_by_account
  - get_upcoming_bills
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def _days_ago(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")

def _last_month_date(day: int = 10) -> str:
    now = datetime.now()
    m   = now.month - 1 or 12
    y   = now.year if now.month > 1 else now.year - 1
    return f"{y}-{m:02d}-{day:02d}"

def _two_months_ago(day: int = 10) -> str:
    now = datetime.now()
    m   = now.month - 2
    y   = now.year
    while m <= 0:
        m += 12
        y -= 1
    return f"{y}-{m:02d}-{day:02d}"


_CATEGORIES = {
    "22": "Groceries",          # real Emoney ID
    "21": "Restaurants/Dining", # real Emoney ID
    "36": "Paycheck/Salary",    # real Emoney ID
    "63": "Transfers",          # real Emoney ID
    "11": "Entertainment",      # real Emoney ID
    "70": "Bills & Utilities",  # real Emoney ID
    "84": "Shopping",           # real Emoney ID
}

# Normalized txns for functions that use _fetch_snb_data
def _norm(date, desc, cat, amount, is_income=False, is_excluded=False):
    return {"date": date, "description": desc, "category": cat,
            "amount": amount, "is_income": is_income, "is_excluded": is_excluded, "is_pending": False}

_NORM_TXNS = [
    # Needs
    _norm(_days_ago(5),             "WHOLE FOODS",    "Groceries",      200.0),
    _norm(_last_month_date(5),      "WHOLE FOODS",    "Groceries",      210.0),
    _norm(_two_months_ago(5),       "WHOLE FOODS",    "Groceries",      190.0),
    _norm(_days_ago(3),             "COMCAST",        "Utilities",      100.0),
    _norm(_last_month_date(3),      "COMCAST",        "Utilities",      100.0),
    _norm(_two_months_ago(3),       "COMCAST",        "Utilities",      100.0),
    # Wants
    _norm(_days_ago(4),             "CHIPOTLE",       "Dining",          50.0),
    _norm(_last_month_date(4),      "CHIPOTLE",       "Dining",          45.0),
    _norm(_two_months_ago(4),       "CHIPOTLE",       "Dining",          55.0),
    _norm(_days_ago(6),             "NETFLIX",        "Entertainment",   16.0),
    _norm(_last_month_date(6),      "NETFLIX",        "Entertainment",   16.0),
    _norm(_two_months_ago(6),       "NETFLIX",        "Entertainment",   16.0),
    # Income
    _norm(_days_ago(15),            "PAYCHECK",       "Paycheck/Salary", 5000.0, is_income=True),
    _norm(_last_month_date(15),     "PAYCHECK",       "Paycheck/Salary", 5000.0, is_income=True),
    _norm(_two_months_ago(15),      "PAYCHECK",       "Paycheck/Salary", 5000.0, is_income=True),
    # Excluded
    _norm(_days_ago(2),             "TRANSFER",       "Transfers",       500.0, is_excluded=True),
]

# Raw SNB transactions for get_spending_by_account
_RAW_TXNS_WITH_ACCOUNTS = [
    {"id": "t1", "date": _days_ago(5), "userDescription": "WHOLE FOODS",
     "categoryId": "22", "value": -200.0, "accountId": "acc-001",     # 22 = Groceries
     "accountName": "Drew Visa", "isDeleted": False, "isPending": False},
    {"id": "t2", "date": _days_ago(3), "userDescription": "CHIPOTLE",
     "categoryId": "21", "value": -50.0, "accountId": "acc-001",      # 21 = Restaurants/Dining
     "accountName": "Drew Visa", "isDeleted": False, "isPending": False},
    {"id": "t3", "date": _days_ago(4), "userDescription": "AMAZON",
     "categoryId": "84", "value": -80.0, "accountId": "acc-002",      # 84 = Shopping
     "accountName": "Lacey MC", "isDeleted": False, "isPending": False},
    {"id": "t4", "date": _days_ago(1), "userDescription": "PAYCHECK",
     "categoryId": "36", "value": 5000.0, "accountId": "acc-003",     # 36 = Paycheck/Salary
     "accountName": "Joint Checking", "isDeleted": False, "isPending": False},
]

# Raw txns for upcoming bills (recurring pattern)
def _make_recurring_raw(merchant: str, cat_id: str, amount: float, interval_days: int, num: int = 4):
    txns = []
    for i in range(num, 0, -1):
        d = (datetime.now() - timedelta(days=i * interval_days)).strftime("%Y-%m-%d")
        txns.append({
            "id": f"r-{merchant}-{i}", "date": d,
            "userDescription": merchant,
            "categoryId": cat_id, "value": -amount,
            "isDeleted": False, "isPending": False,
        })
    return txns

_RECURRING_RAW = (
    _make_recurring_raw("NETFLIX", "105", 15.99, 30, 4)   # 105 = Subscriptions
    + _make_recurring_raw("COMCAST", "70", 99.00, 30, 4)  # 70 = Bills & Utilities
)


# ===========================================================================
# get_50_30_20_analysis
# ===========================================================================

class TestGet503020Analysis:

    @pytest.mark.asyncio
    async def test_returns_three_buckets(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session, months=3)
        assert "needs" in result
        assert "wants" in result
        assert "savings" in result

    @pytest.mark.asyncio
    async def test_bucket_totals_are_positive(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session, months=3)
        assert result["needs"]["monthly_avg"] >= 0
        assert result["wants"]["monthly_avg"] >= 0
        assert result["savings"]["monthly_avg"] >= 0

    @pytest.mark.asyncio
    async def test_needs_includes_groceries(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session, months=3)
        need_cats = [c["category"] for c in result["needs"]["top_categories"]]
        assert "Groceries" in need_cats

    @pytest.mark.asyncio
    async def test_wants_includes_dining(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session, months=3)
        want_cats = [c["category"] for c in result["wants"]["top_categories"]]
        assert "Dining" in want_cats

    @pytest.mark.asyncio
    async def test_status_field_valid_values(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session, months=3)
        valid = {"on_track", "slightly_over", "over_target", "unknown"}
        for bucket in ("needs", "wants", "savings"):
            assert result[bucket]["status"] in valid

    @pytest.mark.asyncio
    async def test_recommendations_not_empty(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session, months=3)
        assert isinstance(result["recommendations"], list)
        assert len(result["recommendations"]) >= 1

    @pytest.mark.asyncio
    async def test_snb_failure_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=([], False)):
            from emoney_mcp.scrapers.spending import get_50_30_20_analysis
            result = await get_50_30_20_analysis(session)
        assert "error" in result


# ===========================================================================
# get_spending_by_account
# ===========================================================================

class TestGetSpendingByAccount:

    @pytest.mark.asyncio
    async def test_groups_by_account(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS_WITH_ACCOUNTS, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map", return_value={}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        account_names = [a["account_name"] for a in result["accounts"]]
        assert "Drew Visa" in account_names
        assert "Lacey MC"  in account_names

    @pytest.mark.asyncio
    async def test_income_excluded_from_accounts(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS_WITH_ACCOUNTS, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map", return_value={}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        # Joint Checking only has paycheck (income) — should not appear
        account_names = [a["account_name"] for a in result["accounts"]]
        assert "Joint Checking" not in account_names

    @pytest.mark.asyncio
    async def test_top_categories_per_account(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS_WITH_ACCOUNTS, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map", return_value={}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        for acct in result["accounts"]:
            assert "top_categories" in acct
            for cat in acct["top_categories"]:
                assert "category" in cat
                assert "total" in cat

    @pytest.mark.asyncio
    async def test_accounts_sorted_by_spend_desc(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS_WITH_ACCOUNTS, _CATEGORIES)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map", return_value={}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session, days=30)
        totals = [a["total_spent"] for a in result["accounts"]]
        assert totals == sorted(totals, reverse=True)

    @pytest.mark.asyncio
    async def test_snb_failure_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(False, [], {})), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_account_map", return_value={}):
            from emoney_mcp.scrapers.spending import get_spending_by_account
            result = await get_spending_by_account(session)
        assert "error" in result


# ===========================================================================
# get_upcoming_bills
# ===========================================================================

class TestGetUpcomingBills:

    @pytest.mark.asyncio
    async def test_detects_recurring_merchants(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RECURRING_RAW, _CATEGORIES)):
            from emoney_mcp.scrapers.spending import get_upcoming_bills
            result = await get_upcoming_bills(session, days_ahead=60)
        merchant_names = [u["merchant"] for u in result["upcoming"]]
        assert any("NETFLIX" in m for m in merchant_names)

    @pytest.mark.asyncio
    async def test_expected_amount_populated(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RECURRING_RAW, _CATEGORIES)):
            from emoney_mcp.scrapers.spending import get_upcoming_bills
            result = await get_upcoming_bills(session, days_ahead=60)
        for bill in result["upcoming"]:
            assert bill["expected_amount"] > 0
            assert "cadence" in bill
            assert "expected_date" in bill

    @pytest.mark.asyncio
    async def test_total_expected_excludes_overdue(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RECURRING_RAW, _CATEGORIES)):
            from emoney_mcp.scrapers.spending import get_upcoming_bills
            result = await get_upcoming_bills(session, days_ahead=60)
        non_overdue_sum = round(sum(u["expected_amount"] for u in result["upcoming"] if not u["overdue"]), 2)
        assert result["total_expected_amount"] == non_overdue_sum

    @pytest.mark.asyncio
    async def test_snb_failure_returns_error(self):
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(False, [], {})):
            from emoney_mcp.scrapers.spending import get_upcoming_bills
            result = await get_upcoming_bills(session)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_non_recurring_merchants_excluded(self):
        one_off = [
            {"id": "x1", "date": _days_ago(10), "userDescription": "RANDOM VENDOR",
             "categoryId": "2", "value": -50.0, "isDeleted": False, "isPending": False},
        ]
        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, one_off, _CATEGORIES)):
            from emoney_mcp.scrapers.spending import get_upcoming_bills
            result = await get_upcoming_bills(session, days_ahead=30)
        assert result["upcoming_count"] == 0
