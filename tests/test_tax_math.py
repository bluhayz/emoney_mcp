"""Tests for tax math helpers and get_rmd_estimate (including v0.7.3 Roth IRA exclusion fix)."""

import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.scrapers.tax import _compute_tax, _marginal_rate, _ltcg_rate


# ---------------------------------------------------------------------------
# _compute_tax  (taxable income = income after standard deduction)
# ---------------------------------------------------------------------------

class TestComputeTax:
    """Verify federal income tax calculation against 2026 IRS brackets."""

    def test_zero_income_returns_zero(self):
        assert _compute_tax(0, "mfj") == 0.0

    def test_negative_income_returns_zero(self):
        assert _compute_tax(-5_000, "mfj") == 0.0

    # MFJ brackets: 10% to $23,850 | 12% to $96,950 | 22% to $206,700 | ...
    def test_mfj_entirely_in_10pct_bracket(self):
        # $10,000 × 10% = $1,000
        assert _compute_tax(10_000, "mfj") == pytest.approx(1_000.0)

    def test_mfj_straddles_10_and_12_pct_brackets(self):
        # $23,850 × 10% + ($50,000 - $23,850) × 12%
        expected = 24_800 * 0.10 + (50_000 - 24_800) * 0.12
        assert _compute_tax(50_000, "mfj") == pytest.approx(expected, rel=1e-6)

    def test_mfj_straddles_12_and_22_pct_brackets(self):
        # $23,850 × 10% + ($96,950 - $23,850) × 12% + ($120,000 - $96,950) × 22%
        expected = (
            24_800 * 0.10
            + (100_800 - 24_800) * 0.12
            + (120_000 - 100_800) * 0.22
        )
        assert _compute_tax(120_000, "mfj") == pytest.approx(expected, rel=1e-6)

    def test_mfj_at_top_of_10pct_bracket(self):
        # Exactly at first bracket ceiling — all taxed at 10%
        assert _compute_tax(24_800, "mfj") == pytest.approx(24_800 * 0.10)

    def test_single_entirely_in_10pct_bracket(self):
        # single: 10% to $11,925
        assert _compute_tax(8_000, "single") == pytest.approx(800.0)

    def test_single_straddles_10_and_12_pct(self):
        expected = 12_400 * 0.10 + (30_000 - 12_400) * 0.12
        assert _compute_tax(30_000, "single") == pytest.approx(expected, rel=1e-6)

    def test_hoh_bracket(self):
        # hoh: 10% to $17,000
        assert _compute_tax(17_700, "hoh") == pytest.approx(17_700 * 0.10)

    def test_unknown_filing_status_falls_back_to_mfj(self):
        """Invalid filing_status should silently use mfj brackets."""
        assert _compute_tax(50_000, "invalid") == _compute_tax(50_000, "mfj")

    def test_high_income_reaches_37pct_bracket(self):
        """Income above $751,600 (mfj) should push into 37% bracket."""
        tax = _compute_tax(1_000_000, "mfj")
        # Quick sanity: marginal dollar above $751,600 should be taxed at 37%
        tax_plus_one = _compute_tax(1_000_001, "mfj")
        assert pytest.approx(tax_plus_one - tax, abs=0.01) == 0.37

    def test_returns_rounded_to_cents(self):
        """Return value must be rounded to 2 decimal places."""
        result = _compute_tax(12_345.67, "single")
        assert result == round(result, 2)


# ---------------------------------------------------------------------------
# _marginal_rate
# ---------------------------------------------------------------------------

class TestMarginalRate:
    """Verify marginal bracket rate lookups."""

    def test_zero_income_is_10pct(self):
        assert _marginal_rate(0, "mfj") == 0.10

    def test_income_in_10pct_bracket(self):
        assert _marginal_rate(10_000, "mfj") == 0.10

    def test_income_exactly_at_bracket_ceiling_uses_that_brackets_rate(self):
        # At $23,850 (top of mfj 10% bracket) the rate is still 10%
        assert _marginal_rate(24_800, "mfj") == 0.10

    def test_income_just_above_first_bracket_ceiling(self):
        assert _marginal_rate(24_801, "mfj") == 0.12

    def test_income_in_22pct_bracket(self):
        assert _marginal_rate(150_000, "mfj") == 0.22

    def test_income_in_24pct_bracket(self):
        assert _marginal_rate(300_000, "mfj") == 0.24

    def test_income_in_37pct_bracket(self):
        assert _marginal_rate(800_000, "mfj") == 0.37

    def test_single_brackets_differ_from_mfj(self):
        # At $15,000 single is in 12% bracket, mfj is still 10%
        assert _marginal_rate(15_000, "single") == 0.12
        assert _marginal_rate(15_000, "mfj") == 0.10

    def test_unknown_status_falls_back_to_mfj(self):
        assert _marginal_rate(100_000, "bad") == _marginal_rate(100_000, "mfj")


# ---------------------------------------------------------------------------
# _ltcg_rate
# ---------------------------------------------------------------------------

class TestLtcgRate:
    """Verify long-term capital gains rate lookups."""

    # MFJ LTCG thresholds: 0% to $96,700 | 15% to $600,050 | 20% above
    def test_mfj_income_below_0pct_threshold(self):
        assert _ltcg_rate(50_000, "mfj") == 0.0

    def test_mfj_income_exactly_at_0pct_threshold(self):
        assert _ltcg_rate(98_900, "mfj") == 0.0

    def test_mfj_income_just_above_0pct_threshold(self):
        assert _ltcg_rate(98_901, "mfj") == 0.15

    def test_mfj_income_in_15pct_band(self):
        assert _ltcg_rate(300_000, "mfj") == 0.15

    def test_mfj_income_above_20pct_threshold(self):
        assert _ltcg_rate(700_000, "mfj") == 0.20

    # Single thresholds: 0% to $48,350 | 15% to $533,400 | 20% above
    def test_single_0pct_threshold_lower_than_mfj(self):
        # At $60,000 single is 15% but mfj is still 0%
        assert _ltcg_rate(60_000, "single") == 0.15
        assert _ltcg_rate(60_000, "mfj") == 0.0

    def test_unknown_status_falls_back_to_mfj(self):
        assert _ltcg_rate(100_000, "bad") == _ltcg_rate(100_000, "mfj")


# ---------------------------------------------------------------------------
# get_rmd_estimate — Roth IRA exclusion (v0.7.3 bug fix)
# ---------------------------------------------------------------------------

def _make_retirement_result(accounts: list) -> dict:
    """Build a mock get_retirement_accounts return value from a list of account dicts."""
    total = sum(a["balance"] for a in accounts)

    def bucket(kws):
        return round(sum(
            a["balance"] for a in accounts
            if any(kw in (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()
                   for kw in kws)
        ), 2)

    return {
        "total_retirement_assets": round(total, 2),
        "retirement_breakdown": {
            "401k_403b":     bucket(["401", "403"]),
            "ira_roth":      bucket(["ira", "roth"]),  # combined — the old bug was using this
            "annuities":     bucket(["annuit"]),
            "hsa":           bucket(["hsa"]),
            "education_529": bucket(["529", "education"]),
            "other":         bucket(["pension", "sep", "simple", "deferred"]),
        },
        "retirement_accounts": accounts,
    }


class TestGetRmdEstimateRothExclusion:
    """Verify get_rmd_estimate correctly excludes Roth IRA balances (v0.7.3 fix)."""

    def _make_session(self, accounts: list):
        """Build a mock http_session whose get_retirement_accounts returns the given accounts."""
        mock = AsyncMock()
        retirement_result = _make_retirement_result(accounts)

        async def mock_get_accounts(_sess):
            return retirement_result

        import unittest.mock
        patcher = unittest.mock.patch(
            "emoney_mcp.scrapers.tax.get_retirement_accounts",
            return_value=retirement_result,
        )
        patcher.start()
        self._patcher = patcher
        return mock

    def teardown_method(self):
        if hasattr(self, "_patcher"):
            self._patcher.stop()

    @pytest.mark.asyncio
    async def test_roth_ira_excluded_from_rmd_base(self):
        """Roth IRA balance must NOT be counted in the RMD pretax base."""
        accounts = [
            {"name": "Traditional IRA", "type": "IRA",      "balance": 200_000},
            {"name": "Roth IRA",        "type": "Roth IRA", "balance": 150_000},
        ]
        session = self._make_session(accounts)
        from emoney_mcp.scrapers.tax import get_rmd_estimate
        result = await get_rmd_estimate(session, birth_year=1950)

        # Pre-tax balance should be $200,000 (traditional only), NOT $350,000
        assert result["current_pretax_balance"] == pytest.approx(200_000.0)

    @pytest.mark.asyncio
    async def test_401k_included_in_rmd_base(self):
        """401k balance must be included."""
        accounts = [
            {"name": "401k Plan",  "type": "401k",     "balance": 300_000},
            {"name": "Roth IRA",   "type": "Roth IRA", "balance": 100_000},
        ]
        session = self._make_session(accounts)
        from emoney_mcp.scrapers.tax import get_rmd_estimate
        result = await get_rmd_estimate(session, birth_year=1950)

        assert result["current_pretax_balance"] == pytest.approx(300_000.0)

    @pytest.mark.asyncio
    async def test_traditional_and_401k_both_included(self):
        """Both 401k and traditional IRA should be summed, Roth excluded."""
        accounts = [
            {"name": "401k",            "type": "401k",     "balance": 250_000},
            {"name": "Traditional IRA", "type": "IRA",      "balance": 100_000},
            {"name": "Roth IRA",        "type": "Roth IRA", "balance": 80_000},
        ]
        session = self._make_session(accounts)
        from emoney_mcp.scrapers.tax import get_rmd_estimate
        result = await get_rmd_estimate(session, birth_year=1950)

        # 250k + 100k = 350k, NOT 430k
        assert result["current_pretax_balance"] == pytest.approx(350_000.0)

    @pytest.mark.asyncio
    async def test_age_below_73_no_current_rmd(self):
        """Under age 73 (SECURE 2.0), current RMD should not be required."""
        accounts = [
            {"name": "401k", "type": "401k", "balance": 500_000},
        ]
        session = self._make_session(accounts)
        from emoney_mcp.scrapers.tax import get_rmd_estimate
        result = await get_rmd_estimate(session, birth_year=1960)

        assert result["rmd_required_this_year"] is False
        assert result["current_rmd_estimate"] is None

    @pytest.mark.asyncio
    async def test_age_73_triggers_rmd(self):
        """At age 73 RMD is required."""
        from datetime import datetime
        birth_year = datetime.now().year - 73
        accounts = [
            {"name": "Traditional IRA", "type": "IRA", "balance": 400_000},
        ]
        session = self._make_session(accounts)
        from emoney_mcp.scrapers.tax import get_rmd_estimate
        result = await get_rmd_estimate(session, birth_year=birth_year)

        assert result["rmd_required_this_year"] is True
        assert result["current_rmd_estimate"] is not None
        assert result["current_rmd_estimate"] > 0

    @pytest.mark.asyncio
    async def test_rmd_schedule_has_ten_years(self):
        """The projected RMD schedule must always have exactly 10 entries."""
        accounts = [
            {"name": "401k", "type": "401k", "balance": 500_000},
        ]
        session = self._make_session(accounts)
        from emoney_mcp.scrapers.tax import get_rmd_estimate
        result = await get_rmd_estimate(session, birth_year=1955)

        assert len(result["projected_rmd_schedule"]) == 10


# ---------------------------------------------------------------------------
# Tax-table staleness nag (#27)
# ---------------------------------------------------------------------------

class TestTaxYearFreshness:
    """The IRS brackets/limits in tax.py are hardcoded per year (_TAX_YEAR) and
    must be refreshed each January. This test fails once the tables fall more
    than one year behind the wall clock — a loud, well-timed reminder to update
    _BRACKETS, _CONTRIBUTION_LIMITS, _STD_DEDUCTION, _LTCG_THRESHOLDS, _NIIT_THRESHOLD.
    A one-year grace is allowed so a not-yet-updated January doesn't break CI."""

    def test_tax_tables_not_more_than_one_year_stale(self):
        from datetime import datetime
        from emoney_mcp.scrapers.tax import _TAX_YEAR

        current_year = datetime.now().year
        assert _TAX_YEAR >= current_year - 1, (
            f"Tax tables in scrapers/tax.py are for {_TAX_YEAR}, but it is "
            f"{current_year}. Update _TAX_YEAR and the IRS constants "
            f"(_BRACKETS, _CONTRIBUTION_LIMITS, _STD_DEDUCTION, _LTCG_THRESHOLDS, "
            f"_NIIT_THRESHOLD) to {current_year}."
        )
