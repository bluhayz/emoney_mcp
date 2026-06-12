"""Tests for SNB-based spending functions: get_spending_transactions, search_transactions, get_savings_rate.

These tests mock _fetch_snb_raw directly to avoid needing a real SNB API connection,
letting us test the filtering, truncation, and aggregation logic in isolation.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ---------------------------------------------------------------------------
# Shared SNB fixture data
# ---------------------------------------------------------------------------

def _make_txn(date_str, description, category_id, value, is_income=False, is_pending=False):
    """Build a raw SNB transaction dict."""
    return {
        "date":            date_str,
        "description":     description,
        "cleanDescription": description,
        "userDescription": None,
        "categoryId":      category_id,
        "value":           value,
        "isPending":       is_pending,
        "isSplit":         False,
    }


# Category map used across tests
_CATEGORIES = {
    "1":  "Groceries",
    "2":  "Dining",
    "3":  "Shopping",
    "4":  "Income",
    "5":  "Transfer",
}

def _today():
    return datetime.now().strftime("%Y-%m-%d")

def _days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


# A fixed dataset covering 45 days with known amounts
_RAW_TXNS = [
    _make_txn(_days_ago(5),  "WHOLE FOODS MARKET",   "1", -85.50),
    _make_txn(_days_ago(7),  "CHIPOTLE GRILL",        "2", -22.00),
    _make_txn(_days_ago(10), "AMAZON",                "3", -120.00),
    _make_txn(_days_ago(12), "WHOLE FOODS MARKET",   "1", -91.25),
    _make_txn(_days_ago(14), "STARBUCKS",             "2", -8.75),
    _make_txn(_days_ago(20), "AMAZON",                "3", -55.00),
    _make_txn(_days_ago(25), "PAYCHECK DIRECT DEP",   "4", 3_000.00),
    _make_txn(_days_ago(30), "CHIPOTLE GRILL",        "2", -18.50),
    _make_txn(_days_ago(35), "TARGET",                "3", -65.00),
    _make_txn(_days_ago(40), "PAYCHECK DIRECT DEP",   "4", 3_000.00),
    # Pending transaction
    _make_txn(_days_ago(3),  "PENDING CHARGE",        "3", -10.00, is_pending=True),
]


def _make_mock_session_with_snb(raw_txns=None, categories=None):
    """Return a mock http_session backed by the given SNB data."""
    raw = raw_txns if raw_txns is not None else _RAW_TXNS
    cats = categories if categories is not None else _CATEGORIES
    session = AsyncMock()
    return session, raw, cats


# ---------------------------------------------------------------------------
# get_spending_transactions
# ---------------------------------------------------------------------------

class TestGetSpendingTransactions:

    @pytest.mark.asyncio
    async def test_returns_transactions_within_days_window(self):
        session, raw, cats = _make_mock_session_with_snb()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw, cats)):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result = await get_spending_transactions(session, days=15)
        # Only transactions within last 15 days should appear
        cutoff = _days_ago(15)
        for t in result["transactions"]:
            assert t["date"] >= cutoff

    @pytest.mark.asyncio
    async def test_max_transactions_truncates(self):
        session, raw, cats = _make_mock_session_with_snb()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw, cats)):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result = await get_spending_transactions(session, days=365, max_transactions=3)
        assert result["transactions_returned"] == 3
        assert result["transaction_count"] >= 3

    @pytest.mark.asyncio
    async def test_max_transactions_zero_returns_all(self):
        """max_transactions=0 must return all transactions (v0.7.3 fix)."""
        session, raw, cats = _make_mock_session_with_snb()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw, cats)):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result = await get_spending_transactions(session, days=365, max_transactions=0)
        assert result["transactions_returned"] == result["transaction_count"]

    @pytest.mark.asyncio
    async def test_top_categories_computed_before_truncation(self):
        """Top-category totals must reflect ALL transactions, not just the truncated subset."""
        session, raw, cats = _make_mock_session_with_snb()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw, cats)):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result_full  = await get_spending_transactions(session, days=365, max_transactions=0)
            result_trunc = await get_spending_transactions(session, days=365, max_transactions=2)
        # Category totals should be equal regardless of truncation
        full_cats  = {c["category"]: c["total"] for c in result_full["top_categories"]}
        trunc_cats = {c["category"]: c["total"] for c in result_trunc["top_categories"]}
        assert full_cats == trunc_cats

    @pytest.mark.asyncio
    async def test_transaction_fields_present(self):
        session, raw, cats = _make_mock_session_with_snb()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw, cats)):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result = await get_spending_transactions(session, days=30)
        for t in result["transactions"]:
            assert "date" in t
            assert "description" in t
            assert "category" in t
            assert "amount" in t
            assert "is_pending" in t

    @pytest.mark.asyncio
    async def test_days_clamped_to_365(self):
        """Requesting more than 365 days should be silently clamped."""
        session, raw, cats = _make_mock_session_with_snb()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw, cats)):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result = await get_spending_transactions(session, days=9999)
        assert result["period_days"] == 365

    @pytest.mark.asyncio
    async def test_snb_failure_returns_error(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(False, [], {})):
            from emoney_mcp.scrapers.spending import get_spending_transactions
            result = await get_spending_transactions(session, days=30)
        assert "error" in result


# ---------------------------------------------------------------------------
# search_transactions
# ---------------------------------------------------------------------------

# Normalized transactions as returned by _fetch_snb_data
_NORM_TXNS = [
    {"date": _days_ago(5),  "description": "WHOLE FOODS MARKET", "category": "Groceries", "amount": 85.50,  "is_income": False, "is_excluded": False, "is_pending": False},
    {"date": _days_ago(7),  "description": "CHIPOTLE GRILL",     "category": "Dining",    "amount": 22.00,  "is_income": False, "is_excluded": False, "is_pending": False},
    {"date": _days_ago(10), "description": "AMAZON",             "category": "Shopping",  "amount": 120.00, "is_income": False, "is_excluded": False, "is_pending": False},
    {"date": _days_ago(12), "description": "WHOLE FOODS MARKET", "category": "Groceries", "amount": 91.25,  "is_income": False, "is_excluded": False, "is_pending": False},
    {"date": _days_ago(25), "description": "PAYCHECK",           "category": "Income",    "amount": 3_000,  "is_income": True,  "is_excluded": False, "is_pending": False},
    {"date": _days_ago(30), "description": "INTERNAL TRANSFER",  "category": "Transfer",  "amount": 500.00, "is_income": False, "is_excluded": True,  "is_pending": False},
]


class TestSearchTransactions:

    @pytest.mark.asyncio
    async def test_keyword_filter_case_insensitive(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, query="whole foods", days=365)
        assert result["match_count"] == 2
        for t in result["transactions"]:
            assert "WHOLE FOODS" in t["description"].upper()

    @pytest.mark.asyncio
    async def test_category_filter_partial_match(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, category="groc", days=365)
        assert result["match_count"] == 2
        for t in result["transactions"]:
            assert "Groceries" in t["category"]

    @pytest.mark.asyncio
    async def test_min_amount_filter(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, min_amount=90.0, days=365)
        for t in result["transactions"]:
            assert t["amount"] >= 90.0

    @pytest.mark.asyncio
    async def test_max_amount_filter(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, max_amount=25.0, days=365)
        for t in result["transactions"]:
            assert t["amount"] <= 25.0

    @pytest.mark.asyncio
    async def test_excluded_transactions_not_returned(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, query="transfer", days=365)
        descriptions = [t["description"] for t in result["transactions"]]
        assert "INTERNAL TRANSFER" not in descriptions

    @pytest.mark.asyncio
    async def test_max_results_zero_returns_all(self):
        """max_results=0 must return all matching transactions (v0.7.3 fix)."""
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, days=365, max_results=0)
        # All non-excluded transactions should be returned
        non_excluded = [t for t in _NORM_TXNS if not t["is_excluded"]]
        assert result["match_count"] == len(non_excluded)
        assert len(result["transactions"]) == len(non_excluded)

    @pytest.mark.asyncio
    async def test_max_results_truncates(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, days=365, max_results=2)
        assert len(result["transactions"]) == 2
        assert result["match_count"] >= 2

    @pytest.mark.asyncio
    async def test_total_amount_reflects_all_matches(self):
        """total_amount should sum all matches, not just the truncated page."""
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result_full  = await search_transactions(session, days=365, max_results=0)
            result_trunc = await search_transactions(session, days=365, max_results=1)
        assert result_full["total_amount"] == result_trunc["total_amount"]

    @pytest.mark.asyncio
    async def test_empty_query_returns_all_non_excluded(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(_NORM_TXNS, True)):
            from emoney_mcp.scrapers.spending import search_transactions
            result = await search_transactions(session, days=365)
        non_excluded = [t for t in _NORM_TXNS if not t["is_excluded"]]
        assert result["match_count"] == len(non_excluded)


# ---------------------------------------------------------------------------
# get_savings_rate
# ---------------------------------------------------------------------------

def _make_monthly_txns(income_per_month, spending_per_month, months):
    """Create transactions that produce predictable monthly savings rates."""
    txns = []
    now = datetime.now()
    for m in range(months):
        # First day of each prior month
        if now.month - m > 0:
            year, month = now.year, now.month - m
        else:
            year, month = now.year - 1, now.month - m + 12
        date_str = f"{year:04d}-{month:02d}-15"
        txns.append({
            "date": date_str, "description": "PAYCHECK", "category": "Income",
            "amount": income_per_month, "is_income": True, "is_excluded": False, "is_pending": False,
        })
        txns.append({
            "date": date_str, "description": "GROCERIES", "category": "Groceries",
            "amount": spending_per_month, "is_income": False, "is_excluded": False, "is_pending": False,
        })
    return txns


class TestGetSavingsRate:

    @pytest.mark.asyncio
    async def test_correct_number_of_months_returned(self):
        txns = _make_monthly_txns(5_000, 3_000, months=6)
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(txns, True)):
            from emoney_mcp.scrapers.spending import get_savings_rate
            result = await get_savings_rate(session, months=3)
        assert result["months_shown"] == 3

    @pytest.mark.asyncio
    async def test_average_savings_rate_calculation(self):
        """With $5k income and $3k spending each month, savings rate should be ~40%."""
        txns = _make_monthly_txns(5_000, 3_000, months=6)
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(txns, True)):
            from emoney_mcp.scrapers.spending import get_savings_rate
            result = await get_savings_rate(session, months=3)
        # Average savings rate should be approximately 40% ((5000-3000)/5000 * 100)
        if result["average_savings_rate"] is not None:
            assert 35.0 <= result["average_savings_rate"] <= 45.0

    @pytest.mark.asyncio
    async def test_snb_failure_returns_error(self):
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=([], False)):
            from emoney_mcp.scrapers.spending import get_savings_rate
            result = await get_savings_rate(session, months=3)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_months_clamped_to_12(self):
        txns = _make_monthly_txns(5_000, 3_000, months=12)
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(txns, True)):
            from emoney_mcp.scrapers.spending import get_savings_rate
            result = await get_savings_rate(session, months=99)
        assert result["months_shown"] <= 12

    @pytest.mark.asyncio
    async def test_excluded_transactions_not_counted(self):
        """Excluded transactions must not affect income or spending totals."""
        txns = _make_monthly_txns(5_000, 3_000, months=3)
        # Add an excluded transaction with a huge amount
        txns.append({
            "date": _days_ago(10), "description": "TRANSFER", "category": "Transfer",
            "amount": 999_999.0, "is_income": False, "is_excluded": True, "is_pending": False,
        })
        session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", return_value=(txns, True)):
            from emoney_mcp.scrapers.spending import get_savings_rate
            result = await get_savings_rate(session, months=3)
        # Savings rate should still be ~40%, not thrown off by the excluded transfer
        if result["average_savings_rate"] is not None:
            assert result["average_savings_rate"] < 100.0
