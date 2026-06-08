"""
emoney_mcp.scrapers
===================
Domain-split scraping modules.  This package re-exports every public function
so that the legacy ``emoney_mcp.scraper`` shim (and any other caller) can
continue to do ``from .scrapers import *`` without knowing the internal layout.

Module layout
-------------
_helpers.py   — URL constants, _get_card, _fmt_dollars
accounts.py   — get_accounts, get_retirement_accounts, get_net_worth_breakdown,
                 _build_account_type_map, _match_tax_bucket
investments.py — get_holdings, get_asset_allocation, get_net_worth_history,
                 get_performance, get_transactions, get_capital_gains
spending.py   — get_spending, get_spending_transactions, get_spending_trends,
                 get_income_summary, get_savings_rate, search_transactions,
                 get_recurring_charges, _normalize_merchant, _fetch_snb_data
goals.py      — get_goals, get_financial_summary, get_financial_health_score
tax.py        — get_tax_loss_harvesting, get_contribution_room,
                 get_roth_conversion_analysis, get_capital_gains_exposure,
                 get_rmd_estimate
retirement.py — get_retirement_runway, get_withdrawal_rate_analysis
portfolio.py  — get_asset_location_efficiency, get_rebalancing_targets,
                 explore_emoney_cards, _classify_asset
"""

from ._helpers import BASE_URL, _get_card, _fmt_dollars

from .accounts import (
    get_accounts,
    get_retirement_accounts,
    get_net_worth_breakdown,
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
    _normalize_merchant,
    _fetch_snb_data,
    _INCOME_CATEGORIES,
    _EXCLUDE_CATEGORIES,
    _NON_MERCHANT_CATEGORIES,
)

from .goals import (
    get_goals,
    get_financial_summary,
    get_financial_health_score,
    _goal_type_label,
)

from .tax import (
    get_tax_loss_harvesting,
    get_contribution_room,
    get_roth_conversion_analysis,
    get_capital_gains_exposure,
    get_rmd_estimate,
    _compute_tax,
    _marginal_rate,
    _ltcg_rate,
)

from .retirement import (
    get_retirement_runway,
    get_withdrawal_rate_analysis,
)

from .portfolio import (
    get_asset_location_efficiency,
    get_rebalancing_targets,
    explore_emoney_cards,
    _classify_asset,
    _ASSET_EFFICIENCY,
)

__all__ = [
    # helpers
    "BASE_URL", "_get_card", "_fmt_dollars",
    # accounts
    "get_accounts", "get_retirement_accounts", "get_net_worth_breakdown",
    "_build_account_type_map", "_match_tax_bucket", "_TAX_BUCKET",
    # investments
    "get_holdings", "get_asset_allocation", "get_net_worth_history",
    "get_performance", "get_transactions", "get_capital_gains",
    # spending
    "get_spending", "get_spending_transactions", "get_spending_trends",
    "get_income_summary", "get_savings_rate", "search_transactions",
    "get_recurring_charges", "_normalize_merchant", "_fetch_snb_data",
    "_INCOME_CATEGORIES", "_EXCLUDE_CATEGORIES", "_NON_MERCHANT_CATEGORIES",
    # goals
    "get_goals", "get_financial_summary", "get_financial_health_score",
    "_goal_type_label",
    # tax
    "get_tax_loss_harvesting", "get_contribution_room",
    "get_roth_conversion_analysis", "get_capital_gains_exposure",
    "get_rmd_estimate", "_compute_tax", "_marginal_rate", "_ltcg_rate",
    # retirement
    "get_retirement_runway", "get_withdrawal_rate_analysis",
    # portfolio
    "get_asset_location_efficiency", "get_rebalancing_targets",
    "explore_emoney_cards", "_classify_asset", "_ASSET_EFFICIENCY",
]
