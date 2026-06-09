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
                 get_cash_flow_projection,
                 _normalize_merchant, _fetch_snb_data, _fetch_snb_raw,
                 clear_snb_cache
goals.py      — get_goals, get_financial_summary, get_financial_health_score,
                 get_quick_status, get_college_savings_gap
tax.py        — get_tax_loss_harvesting, get_contribution_room,
                 get_roth_conversion_analysis, get_capital_gains_exposure,
                 get_rmd_estimate, get_tax_bracket_headroom,
                 get_social_security_optimizer, get_quarterly_estimated_taxes
retirement.py — get_retirement_runway, get_withdrawal_rate_analysis,
                 get_net_worth_projection, run_monte_carlo_retirement,
                 get_dynamic_withdrawal_guardrails
portfolio.py  — get_asset_location_efficiency, get_rebalancing_targets,
                 explore_emoney_cards, _classify_asset

Cache management
----------------
clear_caches() — purges both the card and SNB in-memory caches.  Called
                 automatically when reset_session is invoked from server.py.
"""

from ._helpers import BASE_URL, _get_card, _fmt_dollars, clear_card_cache

from .accounts import (
    get_accounts,
    get_retirement_accounts,
    get_net_worth_breakdown,
    get_debt_payoff_plan,
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
    _normalize_merchant,
    _fetch_snb_data,
    _fetch_snb_raw,
    clear_snb_cache,
    _INCOME_CATEGORIES,
    _EXCLUDE_CATEGORIES,
    _NON_MERCHANT_CATEGORIES,
)

from .goals import (
    get_goals,
    get_financial_summary,
    get_financial_health_score,
    get_quick_status,
    get_college_savings_gap,
    _goal_type_label,
)

from .tax import (
    get_tax_loss_harvesting,
    get_contribution_room,
    get_roth_conversion_analysis,
    get_capital_gains_exposure,
    get_rmd_estimate,
    get_tax_bracket_headroom,
    get_social_security_optimizer,
    get_quarterly_estimated_taxes,
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
)

from .portfolio import (
    get_asset_location_efficiency,
    get_rebalancing_targets,
    explore_emoney_cards,
    _classify_asset,
    _ASSET_EFFICIENCY,
)


def clear_caches() -> None:
    """
    Purge all in-memory TTL caches (cards + SNB).

    Call this on session reset so that a new authenticated user never
    receives stale data cached from a previous session.
    """
    clear_card_cache()
    clear_snb_cache()


__all__ = [
    # helpers
    "BASE_URL", "_get_card", "_fmt_dollars", "clear_card_cache", "clear_caches",
    # accounts
    "get_accounts", "get_retirement_accounts", "get_net_worth_breakdown",
    "get_debt_payoff_plan",
    "_build_account_type_map", "_match_tax_bucket", "_TAX_BUCKET",
    # investments
    "get_holdings", "get_asset_allocation", "get_net_worth_history",
    "get_performance", "get_transactions", "get_capital_gains",
    # spending
    "get_spending", "get_spending_transactions", "get_spending_trends",
    "get_income_summary", "get_savings_rate", "search_transactions",
    "get_recurring_charges", "get_budget_vs_actual", "get_year_over_year",
    "get_cash_flow_projection",
    "_normalize_merchant", "_fetch_snb_data", "_fetch_snb_raw", "clear_snb_cache",
    "_INCOME_CATEGORIES", "_EXCLUDE_CATEGORIES", "_NON_MERCHANT_CATEGORIES",
    # goals
    "get_goals", "get_financial_summary", "get_financial_health_score",
    "get_quick_status", "get_college_savings_gap",
    "_goal_type_label",
    # tax
    "get_tax_loss_harvesting", "get_contribution_room",
    "get_roth_conversion_analysis", "get_capital_gains_exposure",
    "get_rmd_estimate", "get_tax_bracket_headroom",
    "get_social_security_optimizer", "get_quarterly_estimated_taxes",
    "_compute_tax", "_marginal_rate", "_ltcg_rate",
    # retirement
    "get_retirement_runway", "get_withdrawal_rate_analysis",
    "get_net_worth_projection", "run_monte_carlo_retirement",
    "get_dynamic_withdrawal_guardrails",
    # portfolio
    "get_asset_location_efficiency", "get_rebalancing_targets",
    "explore_emoney_cards", "_classify_asset", "_ASSET_EFFICIENCY",
]
