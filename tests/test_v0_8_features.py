"""Tests for v0.8.0 new features."""

import pytest
from unittest.mock import patch
from datetime import datetime, timedelta

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from helpers import make_mock_http_session


# ---------------------------------------------------------------------------
# Shared SNB fixture data
# ---------------------------------------------------------------------------

_CATEGORIES = {
    "1": "Groceries",
    "2": "Dining",
    "3": "Paycheck/Salary",
    "4": "Transfers",
    "5": "Shopping",
    "6": "Entertainment",
}

def _make_txn(date_str: str, desc: str, cat_id: str, value: float, pending: bool = False) -> dict:
    return {
        "date":            date_str,
        "userDescription": desc,
        "categoryId":      cat_id,
        "value":           value,
        "isDeleted":       False,
        "isPending":       pending,
    }

def _norm_txn(date_str: str, desc: str, category: str, amount: float,
              is_income: bool = False, is_excluded: bool = False) -> dict:
    """Return a normalized txn dict matching what _fetch_snb_data returns."""
    return {
        "date":        date_str,
        "description": desc,
        "category":    category,
        "amount":      amount,
        "is_income":   is_income,
        "is_excluded": is_excluded,
        "is_pending":  False,
    }

def _recent_date(days_ago: int = 10) -> str:
    return (datetime.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")

def _last_month_date(day: int = 5) -> str:
    now = datetime.now()
    month = now.month - 1 or 12
    year  = now.year if now.month > 1 else now.year - 1
    return f"{year}-{month:02d}-{day:02d}"

def _two_months_ago_date(day: int = 5) -> str:
    now = datetime.now()
    month = now.month - 2
    year  = now.year
    while month <= 0:
        month += 12
        year  -= 1
    return f"{year}-{month:02d}-{day:02d}"


_RAW_TXNS = [
    # This month — groceries
    _make_txn(_recent_date(3),  "COSTCO WHOLESALE",       "1", -120.50),
    _make_txn(_recent_date(5),  "WHOLE FOODS MARKET",     "1",  -85.00),
    # This month — dining
    _make_txn(_recent_date(2),  "CHIPOTLE GRILL",         "2",  -22.50),
    # This month — huge anomaly
    _make_txn(_recent_date(1),  "COSTCO WHOLESALE",       "1", -500.00),  # unusual
    # This month — income
    _make_txn(_recent_date(15), "ACME CORP PAYROLL",      "3",  5000.00),
    # Last month — groceries (for baseline)
    _make_txn(_last_month_date(5),  "COSTCO WHOLESALE",   "1", -115.00),
    _make_txn(_last_month_date(12), "WHOLE FOODS MARKET", "1",  -80.00),
    # Last month — dining
    _make_txn(_last_month_date(8),  "CHIPOTLE GRILL",     "2",  -20.00),
    _make_txn(_last_month_date(20), "CHIPOTLE GRILL",     "2",  -18.50),
    # Last month — income
    _make_txn(_last_month_date(15), "ACME CORP PAYROLL",  "3",  5000.00),
    # Two months ago
    _make_txn(_two_months_ago_date(5),  "COSTCO WHOLESALE",   "1", -110.00),
    _make_txn(_two_months_ago_date(15), "ACME CORP PAYROLL",  "3",  5000.00),
    _make_txn(_two_months_ago_date(20), "CHIPOTLE GRILL",     "2",  -19.00),
    # Transfer (should be excluded)
    _make_txn(_recent_date(4),  "BANK TRANSFER",          "4",  -500.00),
    # Shopping
    _make_txn(_last_month_date(10), "AMAZON",             "5",  -45.00),
    _make_txn(_two_months_ago_date(10), "AMAZON",         "5",  -38.00),
]

# Normalized txns (for functions that receive _fetch_snb_data output)
_NORM_TXNS = [
    _norm_txn(_recent_date(3),  "COSTCO WHOLESALE",  "Groceries",      120.50),
    _norm_txn(_recent_date(5),  "WHOLE FOODS",       "Groceries",       85.00),
    _norm_txn(_recent_date(2),  "CHIPOTLE GRILL",    "Dining",          22.50),
    _norm_txn(_recent_date(1),  "COSTCO WHOLESALE",  "Groceries",      500.00),  # anomaly
    _norm_txn(_recent_date(15), "ACME CORP PAYROLL", "Paycheck/Salary", 5000.00, is_income=True),
    _norm_txn(_last_month_date(5),  "COSTCO WHOLESALE",   "Groceries", 115.00),
    _norm_txn(_last_month_date(12), "WHOLE FOODS",        "Groceries",  80.00),
    _norm_txn(_last_month_date(8),  "CHIPOTLE GRILL",     "Dining",     20.00),
    _norm_txn(_last_month_date(20), "CHIPOTLE GRILL",     "Dining",     18.50),
    _norm_txn(_last_month_date(15), "ACME CORP PAYROLL",  "Paycheck/Salary", 5000.00, is_income=True),
    _norm_txn(_two_months_ago_date(5),  "COSTCO WHOLESALE",   "Groceries", 110.00),
    _norm_txn(_two_months_ago_date(15), "ACME CORP PAYROLL",  "Paycheck/Salary", 5000.00, is_income=True),
    _norm_txn(_two_months_ago_date(20), "CHIPOTLE GRILL",     "Dining", 19.00),
    _norm_txn(_recent_date(4),  "BANK TRANSFER", "Transfers", 500.00, is_excluded=True),
    _norm_txn(_last_month_date(10),      "AMAZON", "Shopping", 45.00),
    _norm_txn(_two_months_ago_date(10),  "AMAZON", "Shopping", 38.00),
]


# ===========================================================================
# get_unusual_transactions
# ===========================================================================

class TestGetUnusualTransactions:

    @pytest.mark.asyncio
    async def test_flags_large_merchant_charge(self):
        from emoney_mcp.scrapers.spending import get_unusual_transactions

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_unusual_transactions(session, days=90, threshold_pct=150)

        assert "unusual_transactions" in result
        # The $500 Costco charge should be flagged (avg is ~$115)
        flagged_descs = [t["merchant"] for t in result["unusual_transactions"]]
        assert any("COSTCO" in d for d in flagged_descs)

    @pytest.mark.asyncio
    async def test_returns_summary_fields(self):
        from emoney_mcp.scrapers.spending import get_unusual_transactions

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_unusual_transactions(session)

        assert "unusual_count" in result
        assert "total_flagged_amount" in result
        assert "period_days" in result

    @pytest.mark.asyncio
    async def test_snb_failure_returns_error(self):
        from emoney_mcp.scrapers.spending import get_unusual_transactions

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(False, [], {})):
            result = await get_unusual_transactions(session)

        assert "error" in result


# ===========================================================================
# get_merchant_spending
# ===========================================================================

class TestGetMerchantSpending:

    @pytest.mark.asyncio
    async def test_returns_merchant_totals(self):
        from emoney_mcp.scrapers.spending import get_merchant_spending

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_merchant_spending(session, days=365)

        assert "merchants" in result
        assert len(result["merchants"]) > 0
        # Costco should appear
        merchants = [m["merchant"] for m in result["merchants"]]
        assert any("COSTCO" in m for m in merchants)

    @pytest.mark.asyncio
    async def test_merchant_filter(self):
        from emoney_mcp.scrapers.spending import get_merchant_spending

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_merchant_spending(session, merchant="COSTCO")

        assert "merchants" in result
        for m in result["merchants"]:
            assert "COSTCO" in m["merchant"]

    @pytest.mark.asyncio
    async def test_structure_fields_present(self):
        from emoney_mcp.scrapers.spending import get_merchant_spending

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_merchant_spending(session)

        assert result["total_tracked"] >= 0
        if result["merchants"]:
            m = result["merchants"][0]
            assert "total" in m
            assert "transaction_count" in m
            assert "avg_transaction" in m


# ===========================================================================
# get_cash_flow_forecast
# ===========================================================================

class TestGetCashFlowForecast:

    @pytest.mark.asyncio
    async def test_returns_forecast_rows(self):
        from emoney_mcp.scrapers.spending import get_cash_flow_forecast

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_cash_flow_forecast(session, months=3)

        assert "forecast" in result
        assert len(result["forecast"]) == 3

    @pytest.mark.asyncio
    async def test_forecast_structure(self):
        from emoney_mcp.scrapers.spending import get_cash_flow_forecast

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.spending._fetch_snb_raw",
                   return_value=(True, _RAW_TXNS, _CATEGORIES)):
            result = await get_cash_flow_forecast(session, months=2)

        assert "recurring_fixed_monthly" in result
        assert "avg_monthly_income" in result
        for row in result["forecast"]:
            assert "month" in row
            assert "projected_income" in row
            assert "projected_expenses" in row
            assert "projected_net" in row


# ===========================================================================
# get_monthly_review
# ===========================================================================

_CARD9_FIXTURE = {
    "NetWorth": 1_200_000,
    "Assets": 1_350_000,
    "Liabilities": 150_000,
}
_CARD11_FIXTURE = {
    "ChangeThisMonth": {"Change": 5000, "ChangePercent": 0.004},
    "ChangeThisYear": {"Change": 45000},
}
_CARD3_FIXTURE = {
    "ValueChange": {"CurrentValue": 800_000, "Change": 1200, "ChangePercent": 0.0015},
}
_CARD2_FIXTURE = {
    "Goals": [
        {"Name": "Retirement", "Projection": {"PercentFunded": 105}},
        {"Name": "College", "Projection": {"PercentFunded": 75}},
    ]
}


class TestGetMonthlyReview:

    @pytest.fixture
    def http_session(self):
        return make_mock_http_session(
            card_responses={
                9: "card9_networth",
                11: "card11_changes",
                3:  "card3_performance",
                2:  "card2_goals",
            }
        )

    @pytest.mark.asyncio
    async def test_returns_all_sections(self, http_session):
        from emoney_mcp.scrapers.goals import get_monthly_review

        with patch("emoney_mcp.scrapers.goals._get_card") as mock_card, \
             patch("emoney_mcp.scrapers.goals._fetch_snb_data",
                   return_value=(_NORM_TXNS, True)):
            mock_card.side_effect = lambda http, cid: _card_side_effect(cid)
            with patch("emoney_mcp.scrapers.goals.get_savings_rate",
                       return_value={"monthly": [{"savings_rate_pct": 18.5}], "average_savings_rate": 18.5}):
                result = await get_monthly_review(http_session)

        assert "net_worth" in result
        assert "investments" in result
        assert "spending" in result
        assert "goals" in result
        assert "action_items" in result
        assert isinstance(result["action_items"], list)

    @pytest.mark.asyncio
    async def test_goals_on_off_track(self, http_session):
        from emoney_mcp.scrapers.goals import get_monthly_review

        with patch("emoney_mcp.scrapers.goals._get_card") as mock_card, \
             patch("emoney_mcp.scrapers.goals._fetch_snb_data",
                   return_value=(_NORM_TXNS[:3], True)), \
             patch("emoney_mcp.scrapers.goals.get_savings_rate",
                   return_value={"monthly": [{"savings_rate_pct": 15}]}):
            mock_card.side_effect = lambda http, cid: _card_side_effect(cid)
            result = await get_monthly_review(http_session)

        assert result["goals"]["total"] == 2
        assert result["goals"]["on_track"] == 1   # only Retirement at 105%
        assert result["goals"]["off_track"] == 1


def _card_side_effect(cid):
    return {
        9:  _CARD9_FIXTURE,
        11: _CARD11_FIXTURE,
        3:  _CARD3_FIXTURE,
        2:  _CARD2_FIXTURE,
    }.get(cid)


# ===========================================================================
# get_year_end_checklist
# ===========================================================================

class TestGetYearEndChecklist:

    @pytest.mark.asyncio
    async def test_returns_checklist_structure(self):
        from emoney_mcp.scrapers.tax import get_year_end_checklist

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.tax.get_tax_bracket_headroom",
                   return_value={"ordinary_income_headroom": {"headroom_to_next_bracket": 20000},
                                  "ltcg_headroom": {"headroom_in_0pct_ltcg_bracket": 5000}}), \
             patch("emoney_mcp.scrapers.tax.get_tax_loss_harvesting",
                   return_value={"summary": {"harvestable_loss_total": -8000,
                                              "potential_tax_savings_20pct": 1600}}), \
             patch("emoney_mcp.scrapers.tax.get_capital_gains_exposure",
                   return_value={"total_unrealized_gain_taxable": 50000, "estimated_total_tax": 7500}), \
             patch("emoney_mcp.scrapers.tax.get_contribution_room",
                   return_value={"accounts": [{"account_type": "401k", "limit": 23500, "remaining_room": 5000}]}):
            result = await get_year_end_checklist(session)

        assert "checklist" in result
        assert "tax_year" in result
        assert "action_items_count" in result
        assert isinstance(result["checklist"], list)
        assert len(result["checklist"]) > 0

    @pytest.mark.asyncio
    async def test_tlh_opportunity_flagged(self):
        from emoney_mcp.scrapers.tax import get_year_end_checklist

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.tax.get_tax_bracket_headroom",
                   return_value={"ordinary_income_headroom": {"headroom_to_next_bracket": 0},
                                  "ltcg_headroom": {"headroom_in_0pct_ltcg_bracket": 0}}), \
             patch("emoney_mcp.scrapers.tax.get_tax_loss_harvesting",
                   return_value={"summary": {"harvestable_loss_total": -12000,
                                              "potential_tax_savings_20pct": 2400}}), \
             patch("emoney_mcp.scrapers.tax.get_capital_gains_exposure",
                   return_value={}), \
             patch("emoney_mcp.scrapers.tax.get_contribution_room",
                   return_value={"accounts": []}):
            result = await get_year_end_checklist(session)

        items = [c["item"] for c in result["checklist"]]
        assert any("harvesting" in i.lower() for i in items)
        # TLH with a loss should be action_needed
        tlh_item = next(c for c in result["checklist"] if "harvesting" in c["item"].lower())
        assert tlh_item["status"] == "action_needed"


# ===========================================================================
# run_scenario
# ===========================================================================

class TestRunScenario:

    @pytest.mark.asyncio
    async def test_returns_comparison(self):
        from emoney_mcp.scrapers.retirement import run_scenario

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts",
                   return_value={"net_worth": 500_000, "total_assets": 600_000,
                                  "total_liabilities": 100_000}), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate",
                   return_value={"total_net": 24000, "months_shown": 6}), \
             patch("emoney_mcp.scrapers.retirement.get_goals",
                   return_value={"retirement_goals": [{"start_year": 2045}], "spending_goals": []}):
            result = await run_scenario(session, monthly_savings_delta=500)

        assert "baseline" in result
        assert "scenario" in result
        assert result["scenario"]["monthly_savings_delta"] == 500
        assert result["scenario"]["monthly_savings"] == result["baseline"]["monthly_savings"] + 500

    @pytest.mark.asyncio
    async def test_milestone_comparison_present(self):
        from emoney_mcp.scrapers.retirement import run_scenario

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.retirement.get_accounts",
                   return_value={"net_worth": 800_000, "total_assets": 900_000,
                                  "total_liabilities": 100_000}), \
             patch("emoney_mcp.scrapers.retirement.get_savings_rate",
                   return_value={"total_net": 36000, "months_shown": 6}), \
             patch("emoney_mcp.scrapers.retirement.get_goals",
                   return_value={"retirement_goals": [], "spending_goals": []}):
            result = await run_scenario(session)

        assert "milestone_comparison" in result
        assert len(result["milestone_comparison"]) > 0


# ===========================================================================
# get_insurance_gap_analysis
# ===========================================================================

class TestGetInsuranceGapAnalysis:

    @pytest.mark.asyncio
    async def test_returns_all_sections(self):
        from emoney_mcp.scrapers.planning import get_insurance_gap_analysis

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts",
                   return_value={
                       "net_worth": 500_000,
                       "total_assets": 650_000,
                       "total_liabilities": 150_000,
                       "account_groups": [
                           {"group": "Cash & Banking", "total": 45_000, "accounts": []},
                       ],
                   }), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data",
                   return_value=(_NORM_TXNS, True)):
            result = await get_insurance_gap_analysis(session)

        assert "life_insurance" in result
        assert "disability" in result
        assert "emergency_fund" in result
        assert "income_data" in result

    @pytest.mark.asyncio
    async def test_emergency_fund_status_below_minimum(self):
        from emoney_mcp.scrapers.planning import get_insurance_gap_analysis

        session = make_mock_http_session()
        big_spend_norm = [_norm_txn(_last_month_date(i), f"VENDOR{i}", "Dining", 2000.0) for i in range(1, 10)]
        with patch("emoney_mcp.scrapers.planning.get_accounts",
                   return_value={
                       "net_worth": 5_000,
                       "total_assets": 10_000,
                       "total_liabilities": 5_000,
                       "account_groups": [
                           {"group": "Cash & Banking", "total": 2_000, "accounts": []},
                       ],
                   }), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data",
                   return_value=(big_spend_norm, True)):
            result = await get_insurance_gap_analysis(session)

        assert result["emergency_fund"]["status"] == "below_minimum"

    @pytest.mark.asyncio
    async def test_income_multiple_parameter(self):
        from emoney_mcp.scrapers.planning import get_insurance_gap_analysis

        session = make_mock_http_session()
        with patch("emoney_mcp.scrapers.planning.get_accounts",
                   return_value={"net_worth": 100_000, "total_assets": 150_000,
                                  "total_liabilities": 50_000, "account_groups": []}), \
             patch("emoney_mcp.scrapers.planning._fetch_snb_data",
                   return_value=(_NORM_TXNS, True)):
            r10 = await get_insurance_gap_analysis(session, income_multiple=10)
            r12 = await get_insurance_gap_analysis(session, income_multiple=12)

        # 12× need should be larger than 10×
        assert r12["life_insurance"]["estimated_need"] > r10["life_insurance"]["estimated_need"]


# ===========================================================================
# clear_cache
# ===========================================================================

class TestClearCache:

    def test_clear_all(self):
        from emoney_mcp.scrapers import clear_cache
        result = clear_cache("all")
        assert result["success"] is True
        assert "card_cache" in result["cleared"]
        assert "snb_cache" in result["cleared"]

    def test_clear_cards(self):
        from emoney_mcp.scrapers import clear_cache
        result = clear_cache("cards")
        assert result["success"] is True
        assert "card_cache" in result["cleared"]
        assert "snb_cache" not in result["cleared"]

    def test_clear_spending(self):
        from emoney_mcp.scrapers import clear_cache
        result = clear_cache("spending")
        assert result["success"] is True
        assert "snb_cache" in result["cleared"]
        assert "card_cache" not in result["cleared"]

    def test_invalid_module(self):
        from emoney_mcp.scrapers import clear_cache
        result = clear_cache("bogus")
        assert result["success"] is False
        assert "error" in result


# ===========================================================================
# get_available_cards
# ===========================================================================

class TestGetAvailableCards:

    @pytest.mark.asyncio
    async def test_returns_inventory(self):
        from emoney_mcp.scrapers.portfolio import get_available_cards

        session = make_mock_http_session(card_responses={9: "card9_networth"})
        result = await get_available_cards(session, card_ids=[9, 99])

        assert "inventory" in result
        assert result["available_count"] >= 1
        # card 9 should show available
        card9_entry = next((r for r in result["inventory"] if r["card_id"] == 9), None)
        assert card9_entry is not None
        assert card9_entry["status"] == "available"

    @pytest.mark.asyncio
    async def test_unavailable_cards_listed(self):
        from emoney_mcp.scrapers.portfolio import get_available_cards

        session = make_mock_http_session()
        # Mock explore_emoney_cards to return unavailable results cleanly
        with patch("emoney_mcp.scrapers.portfolio.explore_emoney_cards",
                   return_value={
                       "results": {
                           "card_99":  {"status": "unavailable_or_error"},
                           "card_100": {"status": "unavailable_or_error"},
                       }
                   }):
            result = await get_available_cards(session, card_ids=[99, 100])

        assert result["available_count"] == 0
        for entry in result["inventory"]:
            assert entry["status"] == "unavailable"
