"""
Backward-compatible re-export shim.

All scraping logic has been moved into the ``emoney_mcp.scrapers`` package.
This module re-exports everything so that existing callers (server.py) work
without modification.

The MCP server hot-reloads this module on every tool call via
``importlib.reload(scraper)``.  Since this is now a thin shim, reloading it
will also pick up any runtime changes in the scrapers sub-package.
"""

from .scrapers import *  # noqa: F401, F403
from .scrapers import (   # make sure private helpers are also importable
    BASE_URL,
    _get_card,
    _fmt_dollars,
    _build_account_type_map,
    _match_tax_bucket,
    _fetch_snb_data,
    _fetch_snb_raw,
    _normalize_merchant,
    _compute_tax,
    _marginal_rate,
    _ltcg_rate,
    _classify_asset,
    _goal_type_label,
    _TAX_BUCKET,
    _ASSET_EFFICIENCY,
    _INCOME_CATEGORIES,
    _EXCLUDE_CATEGORIES,
    _NON_MERCHANT_CATEGORIES,
    clear_card_cache,
    clear_snb_cache,
    clear_caches,
    clear_cache,
    # v0.7.0 additions
    run_monte_carlo_retirement,
    get_dynamic_withdrawal_guardrails,
    get_social_security_optimizer,
    get_quarterly_estimated_taxes,
    # v0.8.x — SNB write endpoint explorer
    explore_snb_write_endpoints,
    # v0.8.x — site explorer
    explore_emoney_site,
    # v0.9.0 — transaction writes, rules, reports
    update_transaction,
    hide_transaction,
    get_transaction_splits,
    update_transaction_splits,
    get_transaction_rules,
    add_transaction_rule,
    update_transaction_rule,
    apply_transaction_rule,
    get_reports,
    get_report_url,
    # v0.9.1 — category lookup
    get_categories,
    # v1.0.0 — family financial planning tools
    get_home_equity,
    get_fire_number,
    get_gifting_and_estate_strategy,
    get_debt_overview,
    get_50_30_20_analysis,
    get_spending_by_account,
    get_upcoming_bills,
    get_portfolio_concentration,
    get_net_worth_velocity,
    get_tax_drag_analysis,
    get_financial_independence_roadmap,
    get_annual_tax_advantaged_summary,
    # v1.0.2 — live endpoint discoveries
    get_client_profile,
    get_aggregation_status,
    _fetch_snb_account_map,
    # v0.8.0 additions
    get_monthly_review,
    get_unusual_transactions,
    get_merchant_spending,
    get_cash_flow_forecast,
    get_year_end_checklist,
    run_scenario,
    get_insurance_gap_analysis,
    get_available_cards,
)
