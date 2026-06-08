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
)
