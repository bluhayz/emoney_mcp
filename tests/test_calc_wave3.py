"""
Tests for calculator wave 3 (#85, #94, #98):
  - get_income_sources_timeline          (retirement.py)
  - get_sequence_of_returns_stress_test  (retirement.py)
  - get_portfolio_risk_metrics           (portfolio.py)
  - get_benchmark_comparison             (portfolio.py)

Pure calculators — no live network. Account/holdings/card dependencies are
patched at the scraper-module level.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import emoney_mcp.scrapers.retirement as ret
import emoney_mcp.scrapers.portfolio as pf


_RET = {
    "retirement_accounts": [{"name": "Traditional IRA", "type": "IRA", "balance": 800_000}],
    "retirement_breakdown": {},
}
_ACCTS = {"total_assets": 1_500_000, "total_liabilities": 0, "net_worth": 1_500_000, "account_groups": []}
_AA = {"asset_classes": [
    {"name": "US Equity", "percent": 55},
    {"name": "International Equity", "percent": 15},
    {"name": "Bonds", "percent": 25},
    {"name": "Cash", "percent": 5},
]}
# A rising 13-point monthly value series with one dip (for max-drawdown).
_SERIES = [100_000, 103_000, 99_000, 107_000, 110_000, 104_000, 112_000,
           118_000, 115_000, 121_000, 119_000, 128_000, 133_000]


# ---------------------------------------------------------------------------
# get_income_sources_timeline  (#85)
# ---------------------------------------------------------------------------

class TestIncomeSourcesTimeline:

    @pytest.mark.asyncio
    async def test_events_sorted_and_complete(self):
        with patch.object(ret, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await ret.get_income_sources_timeline(
                AsyncMock(), birth_year=1962, retirement_age=63,
                social_security_annual=40_000, ss_start_age=70,
                pension_annual=12_000, pension_start_age=65,
                mortgage_payment_monthly=2_000, mortgage_payoff_age=68)
        ages = [e["age"] for e in r["timeline"]]
        assert ages == sorted(ages)
        events = {e["event"] for e in r["timeline"]}
        assert "Social Security begins" in events
        assert "Pension begins" in events
        assert "Required Minimum Distributions begin" in events
        assert any("Mortgage" in e for e in events)

    @pytest.mark.asyncio
    async def test_bridge_gap_detected(self):
        # Retire at 63, first income (pension) at 65 → 2-year bridge.
        with patch.object(ret, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await ret.get_income_sources_timeline(
                AsyncMock(), birth_year=1962, retirement_age=63,
                pension_annual=12_000, pension_start_age=65)
        assert r["bridge_gap"] is not None
        assert r["bridge_gap"]["gap_years"] == 2

    @pytest.mark.asyncio
    async def test_rmd_estimated_from_pretax(self):
        with patch.object(ret, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await ret.get_income_sources_timeline(AsyncMock(), birth_year=1962)
        assert r["first_year_rmd_estimate"] is not None
        assert r["first_year_rmd_estimate"] > 0

    @pytest.mark.asyncio
    async def test_no_rmd_when_no_pretax(self):
        empty = {"retirement_accounts": [], "retirement_breakdown": {}}
        with patch.object(ret, "get_retirement_accounts", AsyncMock(return_value=empty)):
            r = await ret.get_income_sources_timeline(AsyncMock(), birth_year=1962)
        assert r["first_year_rmd_estimate"] is None
        assert r["rmd_note"]

    @pytest.mark.asyncio
    async def test_cash_flow_freed_type(self):
        with patch.object(ret, "get_retirement_accounts", AsyncMock(return_value=_RET)):
            r = await ret.get_income_sources_timeline(
                AsyncMock(), birth_year=1962,
                mortgage_payment_monthly=2_000, mortgage_payoff_age=68)
        mort = next(e for e in r["timeline"] if "Mortgage" in e["event"])
        assert mort["type"] == "cash_flow_freed"
        assert mort["annual_amount"] == 24_000


# ---------------------------------------------------------------------------
# get_sequence_of_returns_stress_test  (#98)
# ---------------------------------------------------------------------------

class TestSequenceOfReturnsStressTest:

    @pytest.mark.asyncio
    async def test_all_scenarios_present(self):
        with patch.object(ret, "get_accounts", AsyncMock(return_value=_ACCTS)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            r = await ret.get_sequence_of_returns_stress_test(
                AsyncMock(), years=30, withdrawal_rate=0.05)
        assert set(r["scenarios"]) == {"average", "adverse_2000", "adverse_2008", "favorable"}

    @pytest.mark.asyncio
    async def test_sequence_risk_visible(self):
        # adverse_2000 and favorable share identical returns in opposite order;
        # the bad-order path must end with less money than the good-order path.
        with patch.object(ret, "get_accounts", AsyncMock(return_value=_ACCTS)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            r = await ret.get_sequence_of_returns_stress_test(
                AsyncMock(), years=30, withdrawal_rate=0.05)
        adv = r["scenarios"]["adverse_2000"]["ending_balance"]
        fav = r["scenarios"]["favorable"]["ending_balance"]
        assert fav > adv
        assert r["sequence_risk"]["identical_returns_outcome_gap"] == pytest.approx(fav - adv, abs=1)
        # identical return SETS → near-identical average return
        assert r["scenarios"]["adverse_2000"]["avg_annual_return_pct"] == pytest.approx(
            r["scenarios"]["favorable"]["avg_annual_return_pct"], abs=0.01)

    @pytest.mark.asyncio
    async def test_low_withdrawal_survives_average(self):
        with patch.object(ret, "get_accounts", AsyncMock(return_value=_ACCTS)), \
             patch.object(ret, "_fetch_snb_data", AsyncMock(return_value=([], False))):
            r = await ret.get_sequence_of_returns_stress_test(
                AsyncMock(), years=30, withdrawal_rate=0.03)
        assert r["scenarios"]["average"]["depleted"] is False

    @pytest.mark.asyncio
    async def test_no_portfolio_errors(self):
        with patch.object(ret, "get_accounts",
                          AsyncMock(return_value={"total_assets": 0, "total_liabilities": 0, "net_worth": 0, "account_groups": []})):
            r = await ret.get_sequence_of_returns_stress_test(AsyncMock(), withdrawal_rate=0.04)
        assert "error" in r


# ---------------------------------------------------------------------------
# get_portfolio_risk_metrics  (#94)
# ---------------------------------------------------------------------------

def _card3_only(series):
    card3 = {"History": series, "ValueChange": {"CurrentValue": series[-1]}}
    async def _fake(http, cid):
        return card3 if cid == 3 else None
    return _fake


class TestPortfolioRiskMetrics:

    @pytest.mark.asyncio
    async def test_metrics_computed(self):
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only(_SERIES))), \
             patch.object(pf, "get_asset_allocation", AsyncMock(return_value=_AA)):
            r = await pf.get_portfolio_risk_metrics(AsyncMock())
        assert r["months_of_history"] == len(_SERIES)
        assert r["annualized_volatility_pct"] > 0
        assert r["max_drawdown_pct"] > 0          # the series has a dip
        assert r["sharpe_ratio"] is not None
        # 70% equity → beta heuristic ~0.7
        assert r["estimated_beta"] == pytest.approx(0.7, abs=0.01)
        assert r["equity_weight_pct"] == pytest.approx(70.0, abs=0.1)

    @pytest.mark.asyncio
    async def test_insufficient_history_errors(self):
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only([100, 101]))):
            r = await pf.get_portfolio_risk_metrics(AsyncMock())
        assert "error" in r

    @pytest.mark.asyncio
    async def test_missing_card_errors(self):
        with patch.object(pf, "_get_card", AsyncMock(return_value=None)):
            r = await pf.get_portfolio_risk_metrics(AsyncMock())
        assert "error" in r

    @pytest.mark.asyncio
    async def test_max_drawdown_on_declining_series(self):
        decline = [100_000, 90_000, 80_000, 70_000, 60_000]
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only(decline))), \
             patch.object(pf, "get_asset_allocation", AsyncMock(return_value=_AA)):
            r = await pf.get_portfolio_risk_metrics(AsyncMock())
        # peak 100k → trough 60k = 40% drawdown
        assert r["max_drawdown_pct"] == pytest.approx(40.0, abs=0.1)


# ---------------------------------------------------------------------------
# get_benchmark_comparison  (#94)
# ---------------------------------------------------------------------------

class TestBenchmarkComparison:

    @pytest.mark.asyncio
    async def test_comparison_and_excess(self):
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only(_SERIES))):
            r = await pf.get_benchmark_comparison(AsyncMock(), benchmark="60/40")
        assert r["benchmark"] == "60/40"
        assert r["benchmark_expected_return_pct"] == pytest.approx(7.6, abs=0.01)
        assert r["excess_return_pct"] == pytest.approx(
            r["portfolio_annualized_return_pct"] - r["benchmark_expected_return_pct"], abs=0.01)
        assert len(r["all_benchmarks"]) == 8

    @pytest.mark.asyncio
    async def test_whitespace_benchmark_normalized(self):
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only(_SERIES))):
            r = await pf.get_benchmark_comparison(AsyncMock(), benchmark=" 80 / 20 ")
        assert r["benchmark"] == "80/20"

    @pytest.mark.asyncio
    async def test_unknown_benchmark_errors(self):
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only(_SERIES))):
            r = await pf.get_benchmark_comparison(AsyncMock(), benchmark="90/10")
        assert "error" in r

    @pytest.mark.asyncio
    async def test_insufficient_history_errors(self):
        with patch.object(pf, "_get_card", AsyncMock(side_effect=_card3_only([100, 101]))):
            r = await pf.get_benchmark_comparison(AsyncMock(), benchmark="60/40")
        assert "error" in r
