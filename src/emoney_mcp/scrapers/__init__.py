"""
emoney_mcp.scrapers
===================
Domain-split scraping modules.  This package re-exports every public function
so that the legacy ``emoney_mcp.scraper`` shim (and any other caller) can
continue to do ``from .scrapers import *`` without knowing the internal layout.

Module layout
-------------
_helpers.py   — URL constants, _get_card (TTL-cached), _fmt_dollars,
                 clear_card_cache
accounts.py   — get_accounts, get_retirement_accounts, get_net_worth_breakdown,
                 get_debt_payoff_plan,
                 _build_account_type_map, _match_tax_bucket
investments.py — get_holdings, get_asset_allocation, get_net_worth_history,
                 get_performance, get_transactions, get_capital_gains
spending.py   — get_spending, get_spending_transactions, get_spending_trends,
                 get_income_summary, get_savings_rate, search_transactions,
                 get_recurring_charges, get_budget_vs_actual, get_year_over_year,
                 get_cash_flow_projection, get_unusual_transactions,
                 get_merchant_spending, get_cash_flow_forecast,
                 explore_snb_write_endpoints,
                 _normalize_merchant, _fetch_snb_data, _fetch_snb_raw,
                 clear_snb_cache
goals.py      — get_goals, get_financial_summary, get_financial_health_score,
                 get_quick_status, get_college_savings_gap, get_monthly_review
tax.py        — get_tax_loss_harvesting, get_contribution_room,
                 get_roth_conversion_analysis, get_capital_gains_exposure,
                 get_rmd_estimate, get_tax_bracket_headroom,
                 get_social_security_optimizer, get_quarterly_estimated_taxes,
                 get_year_end_checklist
retirement.py — get_retirement_runway, get_withdrawal_rate_analysis,
                 get_net_worth_projection, run_monte_carlo_retirement,
                 get_dynamic_withdrawal_guardrails, run_scenario
portfolio.py  — get_asset_location_efficiency, get_rebalancing_targets,
                 explore_emoney_cards, get_available_cards, _classify_asset
planning.py   — get_insurance_gap_analysis

Cache management
----------------
clear_caches()          — purges both card and SNB caches (called on session reset).
clear_cache(module)     — purge a specific cache: 'cards', 'spending', or 'all'.
"""

from ._helpers import BASE_URL, _get_card, _fmt_dollars, clear_card_cache

from .accounts import (
    get_accounts,
    get_retirement_accounts,
    get_net_worth_breakdown,
    get_debt_payoff_plan,
    get_debt_overview,
    get_client_profile,
    get_aggregation_status,
    _calc_investable_assets,
    _build_account_type_map,
    _match_tax_bucket,
    _TAX_BUCKET,
)

from .investments import (
    get_holdings,
    get_asset_allocation,
    get_net_worth_history,
    get_performance,
    get_transactions,
    get_capital_gains,
)

from .spending import (
    get_spending,
    get_spending_transactions,
    get_spending_trends,
    get_income_summary,
    get_savings_rate,
    search_transactions,
    get_recurring_charges,
    get_budget_vs_actual,
    get_year_over_year,
    get_cash_flow_projection,
    get_unusual_transactions,
    get_merchant_spending,
    get_cash_flow_forecast,
    explore_snb_write_endpoints,
    get_categories,
    get_50_30_20_analysis,
    get_spending_by_account,
    get_upcoming_bills,
    _normalize_merchant,
    _fetch_snb_data,
    _fetch_snb_raw,
    clear_snb_cache,
    _sum_income_spending,
    _fetch_snb_account_map,
    _INCOME_CATEGORIES,
    _EXCLUDE_CATEGORIES,
    _NON_MERCHANT_CATEGORIES,
    _INCOME_CATEGORY_IDS,
    _EXCLUDE_CATEGORY_IDS,
)

from .goals import (
    get_goals,
    get_financial_summary,
    get_financial_health_score,
    get_quick_status,
    get_college_savings_gap,
    get_monthly_review,
    _goal_type_label,
)

from .tax import (
    get_tax_loss_harvesting,
    get_contribution_room,
    get_roth_conversion_analysis,
    get_capital_gains_exposure,
    get_rmd_estimate,
    get_multi_year_tax_projection,
    get_roth_conversion_ladder,
    get_irmaa_analysis,
    get_tax_bracket_headroom,
    get_social_security_optimizer,
    get_quarterly_estimated_taxes,
    get_year_end_checklist,
    get_annual_tax_advantaged_summary,
    _compute_tax,
    _marginal_rate,
    _ltcg_rate,
)

from .retirement import (
    get_retirement_runway,
    get_withdrawal_rate_analysis,
    get_net_worth_projection,
    run_monte_carlo_retirement,
    get_dynamic_withdrawal_guardrails,
    run_scenario,
    get_financial_independence_roadmap,
)

from .portfolio import (
    get_asset_location_efficiency,
    get_rebalancing_targets,
    explore_emoney_cards,
    get_available_cards,
    get_portfolio_concentration,
    get_net_worth_velocity,
    get_tax_drag_analysis,
    _classify_asset,
    _ASSET_EFFICIENCY,
)

from .planning import (
    get_insurance_gap_analysis,
    get_home_equity,
    get_fire_number,
    get_gifting_and_estate_strategy,
)

from .explore import (
    explore_emoney_site,
)

from .transactions import (
    update_transaction,
    hide_transaction,
    get_transaction_splits,
    update_transaction_splits,
    get_transaction_rules,
    add_transaction_rule,
    update_transaction_rule,
    apply_transaction_rule,
)

from .reports import (
    get_reports,
    get_report_url,
)


def clear_caches() -> None:
    """
    Purge all in-memory TTL caches (cards + SNB).

    Call this on session reset so that a new authenticated user never
    receives stale data cached from a previous session.
    """
    clear_card_cache()
    clear_snb_cache()


def clear_cache(module: str = "all") -> dict:
    """
    Selectively purge in-memory TTL caches.

    Parameters
    ----------
    module : 'cards' (card responses only), 'spending' (SNB only), or 'all' (both)

    Returns a dict confirming what was cleared.
    """
    module = (module or "all").lower().strip()
    cleared = []
    if module in ("cards", "all"):
        clear_card_cache()
        cleared.append("card_cache")
    if module in ("spending", "all"):
        clear_snb_cache()
        cleared.append("snb_cache")
    if not cleared:
        return {
            "success": False,
            "error":   f"Unknown module '{module}'. Use 'cards', 'spending', or 'all'.",
        }
    return {
        "success":  True,
        "cleared":  cleared,
        "message":  f"Cleared {', '.join(cleared)}. Next tool call will fetch fresh data.",
    }


__all__ = [
    # helpers
    "BASE_URL", "_get_card", "_fmt_dollars", "clear_card_cache",
    "clear_caches", "clear_cache",
    # accounts
    "get_accounts", "get_retirement_accounts", "get_net_worth_breakdown",
    "get_debt_payoff_plan", "get_debt_overview",
    "get_client_profile", "get_aggregation_status",
    "_calc_investable_assets", "_build_account_type_map", "_match_tax_bucket", "_TAX_BUCKET",
    # investments
    "get_holdings", "get_asset_allocation", "get_net_worth_history",
    "get_performance", "get_transactions", "get_capital_gains",
    # spending
    "get_spending", "get_spending_transactions", "get_spending_trends",
    "get_income_summary", "get_savings_rate", "search_transactions",
    "get_recurring_charges", "get_budget_vs_actual", "get_year_over_year",
    "get_cash_flow_projection", "get_unusual_transactions",
    "get_merchant_spending", "explore_snb_write_endpoints", "get_cash_flow_forecast",
    "get_categories", "get_50_30_20_analysis", "get_spending_by_account", "get_upcoming_bills",
    "_normalize_merchant", "_fetch_snb_data", "_fetch_snb_raw", "clear_snb_cache",
    "_sum_income_spending", "_fetch_snb_account_map",
    "_INCOME_CATEGORIES", "_EXCLUDE_CATEGORIES", "_NON_MERCHANT_CATEGORIES",
    "_INCOME_CATEGORY_IDS", "_EXCLUDE_CATEGORY_IDS",
    # goals
    "get_goals", "get_financial_summary", "get_financial_health_score",
    "get_quick_status", "get_college_savings_gap", "get_monthly_review",
    "_goal_type_label",
    # tax
    "get_tax_loss_harvesting", "get_contribution_room",
    "get_roth_conversion_analysis", "get_capital_gains_exposure",
    "get_rmd_estimate", "get_multi_year_tax_projection", "get_roth_conversion_ladder",
    "get_irmaa_analysis", "get_tax_bracket_headroom",
    "get_social_security_optimizer", "get_quarterly_estimated_taxes",
    "get_year_end_checklist", "get_annual_tax_advantaged_summary",
    "_compute_tax", "_marginal_rate", "_ltcg_rate",
    # retirement
    "get_retirement_runway", "get_withdrawal_rate_analysis",
    "get_net_worth_projection", "run_monte_carlo_retirement",
    "get_dynamic_withdrawal_guardrails", "run_scenario",
    "get_financial_independence_roadmap",
    # portfolio
    "get_asset_location_efficiency", "get_rebalancing_targets",
    "explore_emoney_cards", "get_available_cards",
    "get_portfolio_concentration", "get_net_worth_velocity", "get_tax_drag_analysis",
    "_classify_asset", "_ASSET_EFFICIENCY",
    # planning
    "get_insurance_gap_analysis",
    "get_home_equity", "get_fire_number", "get_gifting_and_estate_strategy",
    # explore
    "explore_emoney_site",
    # transactions (write)
    "update_transaction", "hide_transaction",
    "get_transaction_splits", "update_transaction_splits",
    "get_transaction_rules", "add_transaction_rule",
    "update_transaction_rule", "apply_transaction_rule",
    # reports
    "get_reports", "get_report_url",
]
