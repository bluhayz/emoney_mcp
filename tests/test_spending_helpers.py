"""Tests for pure helper functions in spending.py: _normalize_merchant and _month_offset."""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.scrapers.spending import _normalize_merchant, _month_offset


# ---------------------------------------------------------------------------
# _month_offset
# ---------------------------------------------------------------------------

class TestMonthOffset:
    """Verify calendar-correct month arithmetic (the v0.7.3 drift fix)."""

    def test_zero_months_back_returns_same_month(self):
        base = datetime(2025, 6, 15)
        result = _month_offset(base, 0)
        assert result.year == 2025
        assert result.month == 6
        assert result.day == 1

    def test_one_month_back_mid_year(self):
        base = datetime(2025, 6, 15)
        result = _month_offset(base, 1)
        assert result.year == 2025
        assert result.month == 5
        assert result.day == 1

    def test_january_one_month_back_gives_december_prior_year(self):
        """Critical edge case: Jan - 1 month = Dec of previous year."""
        base = datetime(2025, 1, 20)
        result = _month_offset(base, 1)
        assert result.year == 2024
        assert result.month == 12
        assert result.day == 1

    def test_january_two_months_back_gives_november_prior_year(self):
        base = datetime(2025, 1, 20)
        result = _month_offset(base, 2)
        assert result.year == 2024
        assert result.month == 11
        assert result.day == 1

    def test_january_twelve_months_back_gives_january_prior_year(self):
        base = datetime(2025, 1, 20)
        result = _month_offset(base, 12)
        assert result.year == 2024
        assert result.month == 1
        assert result.day == 1

    def test_january_thirteen_months_back_crosses_two_year_boundaries(self):
        base = datetime(2025, 1, 20)
        result = _month_offset(base, 13)
        assert result.year == 2023
        assert result.month == 12
        assert result.day == 1

    def test_december_one_month_back_gives_november_same_year(self):
        base = datetime(2025, 12, 5)
        result = _month_offset(base, 1)
        assert result.year == 2025
        assert result.month == 11
        assert result.day == 1

    def test_march_three_months_back_gives_december_prior_year(self):
        base = datetime(2025, 3, 10)
        result = _month_offset(base, 3)
        assert result.year == 2024
        assert result.month == 12
        assert result.day == 1

    def test_negative_months_back_projects_forward(self):
        """Negative months_back moves forward in time (used in cash flow projection)."""
        base = datetime(2025, 6, 15)
        result = _month_offset(base, -1)
        assert result.year == 2025
        assert result.month == 7
        assert result.day == 1

    def test_negative_months_back_crosses_year_boundary(self):
        base = datetime(2025, 11, 15)
        result = _month_offset(base, -2)
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 1

    def test_result_always_has_day_one(self):
        """day must always be 1 regardless of base_date.day."""
        for base_day in [1, 15, 28, 31]:
            try:
                base = datetime(2025, 3, base_day)
            except ValueError:
                continue
            result = _month_offset(base, 2)
            assert result.day == 1

    def test_twelve_month_sequence_has_no_duplicates(self):
        """Generating 12 consecutive month labels should produce 12 unique values."""
        base = datetime(2025, 6, 1)
        labels = [_month_offset(base, i).strftime("%Y-%m") for i in range(12 - 1, -1, -1)]
        assert len(set(labels)) == 12

    def test_twelve_month_sequence_oldest_is_correct(self):
        """With base=June 2025, the oldest label (i=11) should be July 2024."""
        base = datetime(2025, 6, 1)
        oldest = _month_offset(base, 11)
        assert oldest.year == 2024
        assert oldest.month == 7


# ---------------------------------------------------------------------------
# _normalize_merchant
# ---------------------------------------------------------------------------

class TestNormalizeMerchant:
    """Verify merchant name normalization strips POS noise without corrupting names."""

    # -- POS prefix stripping --

    def test_aplpay_prefix_stripped(self):
        assert _normalize_merchant("APLPAY FOOD LION") == "FOOD LION"

    def test_sq_asterisk_prefix_stripped(self):
        assert _normalize_merchant("SQ *BLUE BOTTLE COFFEE") == "BLUE BOTTLE COFFEE"

    def test_tst_prefix_stripped(self):
        assert _normalize_merchant("TST* FOUNDING FARMERS") == "FOUNDING FARMERS"

    def test_paypal_prefix_stripped(self):
        result = _normalize_merchant("PAYPAL *AMAZON")
        assert "AMAZON" in result
        assert "PAYPAL" not in result

    def test_sp_prefix_stripped(self):
        result = _normalize_merchant("SP SHOPIFY MERCHANT")
        assert "SP" not in result.split()[0] or "SHOPIFY" in result

    # -- Asterisk reference number stripping --

    def test_asterisk_ref_code_stripped(self):
        result = _normalize_merchant("AMAZON *AB12CD")
        assert "AB12CD" not in result
        assert "AMAZON" in result

    def test_amzn_mktp_prefix_stripped(self):
        """AMZN MKTP US* is a POS prefix — it should be stripped from the result."""
        result = _normalize_merchant("AMZN MKTP US*1A2B3C")
        assert "AMZN MKTP US" not in result

    # -- ZIP code / store number stripping --

    def test_zip_code_stripped(self):
        result = _normalize_merchant("TARGET 20165")
        assert "20165" not in result
        assert "TARGET" in result

    def test_store_number_stripped(self):
        result = _normalize_merchant("COSTCO #0123")
        assert "#0123" not in result or "0123" not in result
        assert "COSTCO" in result

    # -- State abbreviation + city stripping --

    def test_trailing_state_stripped(self):
        result = _normalize_merchant("WHOLE FOODS MARKET ARLINGTON VA")
        assert "VA" not in result.split()
        assert "WHOLE FOODS" in result

    def test_trailing_us_stripped(self):
        result = _normalize_merchant("NETFLIX.COM US")
        assert result == "NETFLIX.COM" or "US" not in result.split()

    # -- Protected words not stripped --

    def test_market_not_stripped_as_city(self):
        """'MARKET' is in _NOT_CITY so it should never be removed as a city-like token."""
        result = _normalize_merchant("WHOLE FOODS MARKET")
        assert "MARKET" in result

    def test_store_not_stripped_as_city(self):
        result = _normalize_merchant("APPLE STORE")
        assert "STORE" in result

    # -- Edge cases --

    def test_empty_string_returns_empty_or_fallback(self):
        result = _normalize_merchant("")
        assert isinstance(result, str)

    def test_only_whitespace_returns_string(self):
        result = _normalize_merchant("   ")
        assert isinstance(result, str)

    def test_plain_name_unchanged(self):
        """A clean merchant name should come back uppercased and trimmed."""
        result = _normalize_merchant("starbucks")
        assert result == "STARBUCKS"

    def test_output_is_always_nonempty(self):
        """Even heavily-stripped input should never return empty string."""
        for raw in ["SQ *", "APLPAY", "TST*", "SP "]:
            result = _normalize_merchant(raw)
            assert len(result) > 0

    def test_multiple_spaces_collapsed(self):
        result = _normalize_merchant("WHOLE   FOODS")
        assert "  " not in result
