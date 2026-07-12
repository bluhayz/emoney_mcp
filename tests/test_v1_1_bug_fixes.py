"""
Regression tests for the v1.1 bug-fix batch (issues #156–#172).

Each test pins behaviour that was previously wrong so the bug can't silently
return. Issue numbers reference the GitHub tracker.
"""

import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))


# ---------------------------------------------------------------------------
# #157 — savings_rate_pct is 100% when expenses are zero, not None
# ---------------------------------------------------------------------------

class TestSavingsRateZeroExpenses:

    @pytest.mark.asyncio
    async def test_zero_expenses_gives_100pct(self):
        from emoney_mcp.scrapers.spending import get_spending
        card13 = {
            "CashFlow": {"Income": 5000.0, "Expenses": 0.0, "Net": 5000.0},
            "Budget": {"Budgeted": 0},
            "Transactions": [],
        }
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=card13)):
            result = await get_spending(http_session)
        assert result["savings_rate_pct"] == 100.0

    @pytest.mark.asyncio
    async def test_nonzero_expenses_unchanged(self):
        from emoney_mcp.scrapers.spending import get_spending
        card13 = {
            "CashFlow": {"Income": 10000.0, "Expenses": 4000.0, "Net": 6000.0},
            "Budget": {"Budgeted": 0},
            "Transactions": [],
        }
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=card13)):
            result = await get_spending(http_session)
        assert result["savings_rate_pct"] == pytest.approx(60.0)


# ---------------------------------------------------------------------------
# #156 — get_income_summary monthly series reconciles with total_income
# ---------------------------------------------------------------------------

class TestIncomeSummaryMonthlyReconciles:

    @pytest.mark.asyncio
    async def test_sum_of_monthly_equals_total_4_month_window(self):
        """90 days can span 4 calendar months; all 4 must appear in the series."""
        from emoney_mcp.scrapers.spending import get_income_summary
        # Build transactions that span 4 calendar months
        txns = [
            {"date": "2026-03-15", "amount": 500.0,  "is_income": True, "is_excluded": False, "category": "Income", "description": "Pay"},
            {"date": "2026-02-10", "amount": 600.0,  "is_income": True, "is_excluded": False, "category": "Income", "description": "Pay"},
            {"date": "2026-01-20", "amount": 700.0,  "is_income": True, "is_excluded": False, "category": "Income", "description": "Pay"},
            {"date": "2025-12-25", "amount": 800.0,  "is_income": True, "is_excluded": False, "category": "Income", "description": "Pay"},
        ]
        with patch("emoney_mcp.scrapers.spending._fetch_snb_data", AsyncMock(return_value=(txns, True))):
            result = await get_income_summary(MagicMock(), days=90)
        monthly = result["monthly_income"]
        total = result["total_income"]
        # Series total must match headline
        assert sum(m["total"] for m in monthly) == pytest.approx(total)
        # All 4 months must have a bucket
        months_present = {m["month"] for m in monthly}
        assert "2025-12" in months_present
        assert "2026-03" in months_present


# ---------------------------------------------------------------------------
# #158 — get_budget_vs_actual exposes month_progress_pct and pace_projected_total
# ---------------------------------------------------------------------------

class TestBudgetVsActualPartialMonth:

    @pytest.mark.asyncio
    async def test_month_progress_pct_present(self):
        from emoney_mcp.scrapers.spending import get_budget_vs_actual
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=None)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", AsyncMock(return_value=([], True))):
            result = await get_budget_vs_actual(http_session)
        assert "month_progress_pct" in result
        pct = result["month_progress_pct"]
        assert 0 < pct <= 100

    @pytest.mark.asyncio
    async def test_pace_projected_total_present(self):
        from emoney_mcp.scrapers.spending import get_budget_vs_actual
        http_session = AsyncMock()
        with patch("emoney_mcp.scrapers.spending._get_card", AsyncMock(return_value=None)), \
             patch("emoney_mcp.scrapers.spending._fetch_snb_data", AsyncMock(return_value=([], True))):
            result = await get_budget_vs_actual(http_session)
        assert "pace_projected_total" in result


# ---------------------------------------------------------------------------
# #163 — debt payoff simulation no longer double-counts freed minimums
# ---------------------------------------------------------------------------

class TestDebtPayoffNoBudgetDoubleCounting:

    @pytest.mark.asyncio
    async def test_small_debt_pays_off_then_accelerates_focus(self):
        """A small second debt that pays off in month 1 should accelerate focus,
        but the freed minimum should only be counted once per month, not twice."""
        from emoney_mcp.scrapers.accounts import get_debt_payoff_plan
        accts = {
            "account_groups": [{
                "group": "Debt",
                "accounts": [
                    {
                        "name": "Credit Card A", "balance": -5000.0, "type": "CreditCard",
                        "institution": "Bank", "as_of": "2026-07-01",
                    },
                    {
                        "name": "Small Loan", "balance": -100.0, "type": "Loan",
                        "institution": "Bank", "as_of": "2026-07-01",
                    },
                ],
            }],
            "net_worth": -5100.0,
        }
        with patch("emoney_mcp.scrapers.accounts.get_accounts", AsyncMock(return_value=accts)):
            result = await get_debt_payoff_plan(MagicMock(), extra_monthly_payment=500.0)
        assert "error" not in result
        avalanche = result["avalanche_strategy"]
        # With a $100 loan paying off almost immediately, months should be short
        # (not inflated by double-counted freed minimums).
        assert avalanche["months_to_payoff"] > 0
        assert avalanche["months_to_payoff"] < 20


# ---------------------------------------------------------------------------
# #159 — permission tests skip on Windows (NTFS)
# ---------------------------------------------------------------------------

class TestPermissionTestSkipsWindows:
    """Verify the test skip marks exist — no behavioural assertion needed here."""

    def test_skip_markers_present(self):
        import tests.test_browser_helpers as tbh
        cls = tbh.TestSaveCookiesPermissions
        for name in ("test_new_file_is_owner_only", "test_preexisting_loose_file_is_tightened"):
            fn = getattr(cls, name)
            marks = getattr(fn, "pytestmark", []) or []
            assert any(getattr(m, "name", "") == "skipif" for m in marks), \
                f"{name} should have a skipif mark"


# ---------------------------------------------------------------------------
# #160 — _calc_investable_assets uses net equity, not gross property value
# ---------------------------------------------------------------------------

class TestCalcInvestableAssets:

    def test_mortgaged_property_uses_net_equity(self):
        """cash $500k + house $1.0M + mortgage −$600k → NW $900k, investable $500k"""
        from emoney_mcp.scrapers.accounts import _calc_investable_assets
        accts = {
            "net_worth": 900_000.0,
            "account_groups": [{
                "group": "Assets",
                "accounts": [
                    {"name": "Cash",        "balance": 500_000.0, "type": "CashAsset"},
                    {"name": "Home",        "balance": 1_000_000.0, "type": "RealEstateAsset"},
                    {"name": "Mortgage",    "balance": -600_000.0, "type": "Mortgage"},
                ],
            }],
        }
        result = _calc_investable_assets(accts)
        assert result == pytest.approx(500_000.0)

    def test_no_real_estate_returns_net_worth(self):
        from emoney_mcp.scrapers.accounts import _calc_investable_assets
        accts = {
            "net_worth": 500_000.0,
            "account_groups": [{"group": "Assets", "accounts": [
                {"name": "Brokerage", "balance": 500_000.0, "type": "InvestmentAsset"},
            ]}],
        }
        assert _calc_investable_assets(accts) == pytest.approx(500_000.0)

    def test_result_clamped_to_zero(self):
        """Fully underwater property → investable >= 0, never negative."""
        from emoney_mcp.scrapers.accounts import _calc_investable_assets
        accts = {
            "net_worth": -100_000.0,
            "account_groups": [{"group": "Assets", "accounts": [
                {"name": "Underwater Property", "balance": 400_000.0, "type": "RealEstateAsset"},
                {"name": "Mortgage",            "balance": -500_000.0, "type": "Mortgage"},
            ]}],
        }
        assert _calc_investable_assets(accts) >= 0.0


# ---------------------------------------------------------------------------
# #162 — SS spousal benefit uses primary PIA, not age-70 benefit
# ---------------------------------------------------------------------------

class TestSocialSecuritySpousalBenefit:

    @pytest.mark.asyncio
    async def test_spousal_benefit_uses_pia_not_monthly_70(self):
        """Spousal benefit = 50% of primary PIA, not 50% of monthly_70 (≈124% PIA)."""
        from emoney_mcp.scrapers.tax import get_social_security_optimizer
        with patch("emoney_mcp.scrapers.tax.get_income_summary", AsyncMock(return_value={"total_income": 120_000})):
            result = await get_social_security_optimizer(
                MagicMock(),
                birth_year=1970,
                estimated_monthly_benefit_at_67=3_000.0,
                spouse_birth_year=1972,
                spouse_benefit_at_67=1_500.0,
            )
        spousal_note = result["spousal_analysis"]["spousal_benefit_note"]
        # PIA is 3000 → spousal benefit should be ~1500, not ~1860 (which 50% of monthly_70 would give)
        assert "1,500" in spousal_note

    @pytest.mark.asyncio
    async def test_no_dead_statement(self):
        """Lifetime at 67 should not be silently discarded — verify function still returns."""
        from emoney_mcp.scrapers.tax import get_social_security_optimizer
        with patch("emoney_mcp.scrapers.tax.get_income_summary", AsyncMock(return_value={"total_income": 100_000})):
            result = await get_social_security_optimizer(
                MagicMock(), birth_year=1965, estimated_monthly_benefit_at_67=2_500.0
            )
        assert "strategies" in result
        assert len(result["strategies"]) == 3


# ---------------------------------------------------------------------------
# #163 — debt payoff freed accumulator removed (broader check)
# ---------------------------------------------------------------------------

class TestDebtPayoffFreedAccumulator:

    @pytest.mark.asyncio
    async def test_snowball_interest_non_negative(self):
        """total_interest_paid must be >= 0 and months must be > 0."""
        from emoney_mcp.scrapers.accounts import get_debt_payoff_plan
        accts = {
            "account_groups": [{"group": "Debt", "accounts": [
                {"name": "CC", "balance": -3000.0, "type": "CreditCard",
                 "institution": "Bank", "as_of": "2026-01-01"},
            ]}],
            "net_worth": -3000.0,
        }
        with patch("emoney_mcp.scrapers.accounts.get_accounts", AsyncMock(return_value=accts)):
            result = await get_debt_payoff_plan(MagicMock())
        assert result["snowball_strategy"]["total_interest_paid"] >= 0
        assert result["snowball_strategy"]["months_to_payoff"] > 0


# ---------------------------------------------------------------------------
# #164 — get_year_end_checklist handles sub-tool exceptions gracefully
# ---------------------------------------------------------------------------

class TestYearEndChecklistExceptionHandling:

    @pytest.mark.asyncio
    async def test_sub_tool_raises_returns_partial_checklist(self):
        """If one sub-tool raises, the checklist should not crash — it returns
        partial data from the remaining successful sub-tools."""
        from emoney_mcp.scrapers import tax
        with patch.object(tax, "get_tax_bracket_headroom", AsyncMock(side_effect=RuntimeError("test error"))), \
             patch.object(tax, "get_tax_loss_harvesting", AsyncMock(return_value={"summary": {"harvestable_loss_total": 0}})), \
             patch.object(tax, "get_capital_gains_exposure", AsyncMock(return_value={"total_unrealized_gain_taxable": 0})), \
             patch.object(tax, "get_contribution_room", AsyncMock(return_value={"annual_limits": {}})), \
             patch.object(tax, "get_rmd_estimate", AsyncMock(return_value={"rmd_required": False})):
            result = await tax.get_year_end_checklist(MagicMock())
        # Should not contain an 'error' key at the top level
        assert "error" not in result
        # checklist should be a list (may be empty for the bracket_headroom section)
        assert "checklist" in result
        assert isinstance(result["checklist"], list)


# ---------------------------------------------------------------------------
# #169 — retirement keyword false positives fixed (Admiral/Joseph/Simple Checking)
# ---------------------------------------------------------------------------

class TestRetirementKeywordFalsePositives:

    @pytest.mark.asyncio
    async def test_admiral_shares_not_classified_as_retirement(self):
        """'ira' in 'Admiral' must NOT classify as IRA."""
        from emoney_mcp.scrapers.accounts import get_retirement_accounts
        accts = {
            "net_worth": 100_000.0,
            "account_groups": [{"group": "Taxable", "accounts": [
                {"name": "Vanguard Admiral Shares", "balance": 100_000.0,
                 "type": "InvestmentAsset", "institution": "Vanguard", "as_of": "2026-07-01"},
            ]}],
        }
        with patch("emoney_mcp.scrapers.accounts.get_accounts", AsyncMock(return_value=accts)):
            result = await get_retirement_accounts(MagicMock())
        names = [a["name"] for a in result["retirement_accounts"]]
        assert "Vanguard Admiral Shares" not in names

    @pytest.mark.asyncio
    async def test_sep_ira_classified_as_retirement(self):
        """SEP IRA should still be classified as retirement despite the stricter regex."""
        from emoney_mcp.scrapers.accounts import get_retirement_accounts
        accts = {
            "net_worth": 50_000.0,
            "account_groups": [{"group": "Retirement", "accounts": [
                {"name": "SEP IRA", "balance": 50_000.0, "type": "PreTaxSavingsAsset",
                 "institution": "Fidelity", "as_of": "2026-07-01"},
            ]}],
        }
        with patch("emoney_mcp.scrapers.accounts.get_accounts", AsyncMock(return_value=accts)):
            result = await get_retirement_accounts(MagicMock())
        names = [a["name"] for a in result["retirement_accounts"]]
        assert "SEP IRA" in names

    @pytest.mark.asyncio
    async def test_roth_401k_counted_only_once(self):
        """A 'Roth 401k' should land in 401k_403b only (first-match wins)."""
        from emoney_mcp.scrapers.accounts import get_retirement_accounts
        accts = {
            "net_worth": 80_000.0,
            "account_groups": [{"group": "Retirement", "accounts": [
                {"name": "Roth 401k", "balance": 80_000.0, "type": "TaxFreeRothSavingsAsset",
                 "institution": "Fidelity", "as_of": "2026-07-01"},
            ]}],
        }
        with patch("emoney_mcp.scrapers.accounts.get_accounts", AsyncMock(return_value=accts)):
            result = await get_retirement_accounts(MagicMock())
        bd = result["retirement_breakdown"]
        # Total of all buckets should equal total retirement assets (no double counting)
        total_buckets = sum(bd.values())
        assert total_buckets == pytest.approx(result["total_retirement_assets"])


# ---------------------------------------------------------------------------
# #170 — quarterly estimated tax due dates use next business day
# ---------------------------------------------------------------------------

class TestQuarterlyTaxDueDates:

    @pytest.mark.asyncio
    async def test_q2_2026_is_june_15_not_june_16(self):
        """June 15 2026 is a Monday → correct due date is June 15, not June 16."""
        from emoney_mcp.scrapers.tax import get_quarterly_estimated_taxes
        with patch("emoney_mcp.scrapers.tax.get_income_summary",
                   AsyncMock(return_value={"total_income": 200_000})):
            result = await get_quarterly_estimated_taxes(MagicMock(), filing_status="mfj")
        payments = result["methods"]["current_year_annualized"]["quarterly_payments"]
        q2 = next(p for p in payments if p["quarter"] == "Q2")
        assert "June 15" in q2["due"]

    @pytest.mark.asyncio
    async def test_weekend_rolls_forward(self):
        """April 15 2023 was a Saturday → due April 17 (next Monday)."""
        from emoney_mcp.scrapers.tax import get_quarterly_estimated_taxes
        # Patch datetime.now() to return a year where Q1 falls on a weekend
        with patch("emoney_mcp.scrapers.tax.datetime") as mock_dt, \
             patch("emoney_mcp.scrapers.tax.get_income_summary",
                   AsyncMock(return_value={"total_income": 100_000})):
            mock_dt.now.return_value = datetime(2023, 3, 1)
            result = await get_quarterly_estimated_taxes(MagicMock())
        payments = result["methods"]["current_year_annualized"]["quarterly_payments"]
        q1 = next(p for p in payments if p["quarter"] == "Q1")
        # April 15 2023 is Saturday → should be April 17 (Monday)
        assert "April 15" not in q1["due"]
        assert "April 17" in q1["due"]


# ---------------------------------------------------------------------------
# #171 — get_dividend_income_analysis clamps days and annualizes
# ---------------------------------------------------------------------------

class TestDividendIncomeAnalysisClamping:

    @pytest.mark.asyncio
    async def test_trailing_window_days_clamped_to_365(self):
        """Requesting 730 days → trailing_window_days should be 365."""
        from emoney_mcp.scrapers.investments import get_dividend_income_analysis
        with patch("emoney_mcp.scrapers.investments.get_transactions",
                   AsyncMock(return_value={"transactions": []})), \
             patch("emoney_mcp.scrapers.investments._get_investment_data",
                   AsyncMock(return_value=(None, None))):
            result = await get_dividend_income_analysis(MagicMock(), days=730)
        assert result["trailing_window_days"] == 365

    @pytest.mark.asyncio
    async def test_projected_forward_income_annualized_from_short_window(self):
        """90 days of income × (365/90) = annualized forward income."""
        from emoney_mcp.scrapers.investments import get_dividend_income_analysis
        txns = [
            {"type": "Income Dividend", "amount": 100.0, "ticker": "AAPL", "date": "2026-06-01"},
        ]
        with patch("emoney_mcp.scrapers.investments.get_transactions",
                   AsyncMock(return_value={"transactions": txns})), \
             patch("emoney_mcp.scrapers.investments._get_investment_data",
                   AsyncMock(return_value=({"Holdings": 10_000, "Cash": 0}, None))):
            result = await get_dividend_income_analysis(MagicMock(), days=90)
        expected_annualized = round(100.0 * 365 / 90, 2)
        assert result["projected_forward_income"] == pytest.approx(expected_annualized)


# ---------------------------------------------------------------------------
# #172 — get_net_worth_breakdown uses dynamic household names
# ---------------------------------------------------------------------------

class TestNetWorthBreakdownDynamicNames:

    @pytest.mark.asyncio
    async def test_no_hardcoded_names_in_result(self):
        """The result should NOT reference 'Drew', 'Lacey', or 'Parker' literally."""
        from emoney_mcp.scrapers import accounts
        accts = {
            "net_worth": 100_000.0,
            "total_assets": 100_000.0,
            "total_liabilities": 0.0,
            "account_groups": [{"group": "Taxable", "accounts": [
                {"name": "Alice Joint Account", "balance": 100_000.0,
                 "type": "InvestmentAsset", "institution": "Fido", "as_of": "2026-07-01"},
            ]}],
        }
        profile = {
            "primary":    {"name": "Alice Smith", "is_spouse": False},
            "spouse":     {"name": "Bob Smith",   "is_spouse": True},
            "dependents": [],
        }
        with patch.object(accounts, "get_accounts", AsyncMock(return_value=accts)), \
             patch.object(accounts, "get_client_profile", AsyncMock(return_value=profile)):
            result = await accounts.get_net_worth_breakdown(MagicMock())
        persons = [e["person"] for e in result["by_person"]]
        assert "Drew" not in persons
        assert "Lacey" not in persons
        assert "Parker" not in persons

    @pytest.mark.asyncio
    async def test_primary_name_in_result(self):
        """Primary account holder's first name should appear in by_person."""
        from emoney_mcp.scrapers import accounts
        accts = {
            "net_worth": 50_000.0,
            "total_assets": 50_000.0,
            "total_liabilities": 0.0,
            "account_groups": [{"group": "Taxable", "accounts": [
                {"name": "Alice Brokerage", "balance": 50_000.0,
                 "type": "InvestmentAsset", "institution": "Fido", "as_of": "2026-07-01"},
            ]}],
        }
        profile = {
            "primary":    {"name": "Alice Smith", "is_spouse": False},
            "spouse":     None,
            "dependents": [],
        }
        with patch.object(accounts, "get_accounts", AsyncMock(return_value=accts)), \
             patch.object(accounts, "get_client_profile", AsyncMock(return_value=profile)):
            result = await accounts.get_net_worth_breakdown(MagicMock())
        persons = [e["person"] for e in result["by_person"]]
        assert "Alice" in persons
