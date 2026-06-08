"""Portfolio analysis — asset location efficiency, rebalancing, card exploration."""

import time

from ._helpers import _get_card, _INV_URL
from .accounts import _build_account_type_map, _match_tax_bucket

# Tax-efficiency score for asset classes: higher = more tax-efficient
# (best placed in taxable; low-efficiency = prefer tax-deferred)
_ASSET_EFFICIENCY: dict[str, int] = {
    # High efficiency (good in taxable)
    "domestic_equity_index": 9,
    "international_equity":  8,
    "growth_equity":         7,
    "muni_bond":             9,
    # Medium
    "dividend_equity":       5,
    "balanced":              4,
    # Low efficiency (prefer tax-deferred/free)
    "reit":                  2,
    "bond_fund":             2,
    "tips":                  1,
    "high_yield_bond":       1,
    "money_market":          3,
}


def _classify_asset(ticker: str, description: str) -> str:
    """Heuristically classify a holding into an asset class."""
    t = (ticker or "").upper()
    d = (description or "").upper()
    combined = t + " " + d

    # Munis
    if any(x in combined for x in ("MUNI", "TAX-EXEMPT", "TAX EXEMPT")):
        return "muni_bond"
    # TIPS / inflation
    if any(x in combined for x in ("TIPS", "INFLATION", "INFL-PROT", "TREASURY INFLATION")):
        return "tips"
    # High-yield bonds
    if any(x in combined for x in ("HIGH YIELD", "JUNK", "HYG", "JNK", "HYLD")):
        return "high_yield_bond"
    # REITs
    if any(x in combined for x in ("REIT", "REAL ESTATE", "VNQ", "IYR", "SCHH")):
        return "reit"
    # Bond funds (broad)
    if any(x in combined for x in (
        "BOND", "FIXED INCOME", "INCOME FUND", "AGGREGATE", "TREASURY",
        "GOVT", "CORPORATE BOND", "AGG", "BND", "VBTLX", "TLT", "IEF", "SHY",
    )):
        return "bond_fund"
    # Money market / cash
    if any(x in combined for x in ("MONEY MARKET", "MMKT", "CASH", "TREASURY BILL", "T-BILL")):
        return "money_market"
    # International equity
    if any(x in combined for x in (
        "INTERNATIONAL", "INTL", "FOREIGN", "EMERGING", "EUROPE", "PACIFIC",
        "VXUS", "VEA", "VWO", "EFA", "EEM", "IXUS",
    )):
        return "international_equity"
    # Dividend-focused
    if any(x in combined for x in ("DIVIDEND", "INCOME EQUITY", "VALUE", "DVY", "VYM", "SCHD")):
        return "dividend_equity"
    # Index / passive domestic equity
    if any(x in combined for x in (
        "INDEX", "TOTAL MARKET", "S&P", "500", "VTSAX", "VTI", "SPY", "IVV", "SCHB", "FXAIX",
    )):
        return "domestic_equity_index"
    # Broad equity catch-all
    if any(x in combined for x in ("GROWTH", "EQUITY", "STOCK", "LARGE CAP", "SMALL CAP", "MID CAP")):
        return "growth_equity"

    return "domestic_equity_index"   # conservative default


async def get_asset_location_efficiency(http_session) -> dict:
    """
    Grade how well assets are positioned across account types for tax efficiency.

    The principle: tax-inefficient assets (bonds, REITs, high-dividend stocks)
    should be sheltered in tax-deferred or tax-free accounts; tax-efficient
    assets (index funds, growth stocks) can sit in taxable accounts.

    Returns a letter grade, position-by-position ratings, and specific
    improvement suggestions.
    """
    type_map = await _build_account_type_map(http_session)

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data  = resp.json()
    total = (data.get("Holdings") or 0) + (data.get("Cash") or 0)

    scored_positions = []
    suggestions = []
    total_weighted_score = 0.0
    total_weight = 0.0

    for acct in data.get("Accounts", []):
        acct_name  = acct.get("Name", "")
        tax_bucket = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            value       = h.get("Value") or 0.0
            if value <= 0:
                continue
            ticker      = h.get("Ticker") or ""
            description = h.get("Description") or ""
            asset_class = _classify_asset(ticker, description)
            efficiency  = _ASSET_EFFICIENCY.get(asset_class, 5)

            if tax_bucket == "Taxable":
                placement_score = efficiency
                well_placed     = efficiency >= 6
            elif tax_bucket in ("Tax-Deferred", "Tax-Free"):
                placement_score = 10 - efficiency
                well_placed     = efficiency <= 5
            else:
                placement_score = 5
                well_placed     = None

            weight = value / total if total > 0 else 0
            total_weighted_score += placement_score * weight
            total_weight += weight

            entry = {
                "ticker":          ticker,
                "description":     description[:40],
                "account":         acct_name,
                "tax_treatment":   tax_bucket,
                "asset_class":     asset_class,
                "efficiency_score": efficiency,
                "value":           round(value, 2),
                "well_placed":     well_placed,
            }
            scored_positions.append(entry)

            if well_placed is False and value >= 10_000:
                if tax_bucket == "Taxable" and efficiency < 6:
                    suggestions.append(
                        f"Consider moving '{ticker or description[:30]}' (${value:,.0f}, {asset_class}) "
                        f"from taxable '{acct_name}' to a tax-deferred or tax-free account."
                    )
                elif tax_bucket in ("Tax-Deferred", "Tax-Free") and efficiency >= 7:
                    suggestions.append(
                        f"Consider moving '{ticker or description[:30]}' (${value:,.0f}, {asset_class}) "
                        f"to a taxable account to free up tax-sheltered space for less-efficient assets."
                    )

    overall_score = round(total_weighted_score / total_weight, 1) if total_weight > 0 else 5.0
    if overall_score >= 8:
        grade = "A"
    elif overall_score >= 6.5:
        grade = "B"
    elif overall_score >= 5:
        grade = "C"
    elif overall_score >= 3.5:
        grade = "D"
    else:
        grade = "F"

    well_placed_count   = sum(1 for p in scored_positions if p["well_placed"] is True)
    poorly_placed_count = sum(1 for p in scored_positions if p["well_placed"] is False)

    return {
        "overall_grade":       grade,
        "overall_score":       f"{overall_score}/10",
        "well_placed_count":   well_placed_count,
        "poorly_placed_count": poorly_placed_count,
        "suggestions":         suggestions[:10],
        "positions":           sorted(scored_positions, key=lambda x: x["well_placed"] is False, reverse=True),
        "efficiency_guide": {
            "best_in_taxable":   ["index funds", "ETFs", "growth stocks", "municipal bonds"],
            "best_in_deferred":  ["bond funds", "REITs", "TIPS", "high-yield bonds", "high-dividend stocks"],
            "best_in_tax_free":  ["highest-growth assets (Roth)", "bond funds if no deferred space"],
        },
    }


async def get_rebalancing_targets(
    http_session,
    target_equity_pct: float = 60.0,
    target_bond_pct:   float = 30.0,
    target_cash_pct:   float = 10.0,
) -> dict:
    """
    Compute buy/sell amounts needed to reach a target asset allocation.

    Parameters
    ----------
    target_equity_pct : target percentage in equities (default 60)
    target_bond_pct   : target percentage in bonds/fixed income (default 30)
    target_cash_pct   : target percentage in cash/money market (default 10)
    """
    total_target = target_equity_pct + target_bond_pct + target_cash_pct
    if abs(total_target - 100) > 0.1:
        target_equity_pct = round(target_equity_pct / total_target * 100, 1)
        target_bond_pct   = round(target_bond_pct   / total_target * 100, 1)
        target_cash_pct   = round(100 - target_equity_pct - target_bond_pct, 1)

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data  = resp.json()
    portfolio_total = (data.get("Holdings") or 0) + (data.get("Cash") or 0)

    equity_value = 0.0
    bond_value   = 0.0
    cash_value   = data.get("Cash") or 0.0
    other_value  = 0.0

    position_details = []
    for acct in data.get("Accounts", []):
        for h in acct.get("Holdings", []):
            value       = h.get("Value") or 0.0
            ticker      = h.get("Ticker") or ""
            description = h.get("Description") or ""
            asset_class = _classify_asset(ticker, description)

            if asset_class in ("domestic_equity_index", "international_equity",
                               "growth_equity", "dividend_equity"):
                bucket = "equity"
                equity_value += value
            elif asset_class in ("bond_fund", "tips", "high_yield_bond", "muni_bond"):
                bucket = "bond"
                bond_value += value
            elif asset_class == "money_market":
                bucket = "cash"
                cash_value += value
            else:
                bucket = "equity"   # default: treat as equity
                equity_value += value

            position_details.append({
                "ticker":      ticker,
                "description": description[:40],
                "asset_class": asset_class,
                "bucket":      bucket,
                "value":       round(value, 2),
            })

    if portfolio_total <= 0:
        return {"error": "No portfolio data found."}

    current_equity_pct = round(equity_value / portfolio_total * 100, 1)
    current_bond_pct   = round(bond_value   / portfolio_total * 100, 1)
    current_cash_pct   = round(cash_value   / portfolio_total * 100, 1)

    target_equity_val = portfolio_total * target_equity_pct / 100
    target_bond_val   = portfolio_total * target_bond_pct   / 100
    target_cash_val   = portfolio_total * target_cash_pct   / 100

    equity_delta = round(target_equity_val - equity_value, 2)
    bond_delta   = round(target_bond_val   - bond_value,   2)
    cash_delta   = round(target_cash_val   - cash_value,   2)

    def _action(delta: float) -> str:
        if delta > 500:
            return f"BUY ${abs(delta):,.0f}"
        elif delta < -500:
            return f"SELL ${abs(delta):,.0f}"
        return "ON TARGET"

    return {
        "portfolio_total": round(portfolio_total, 2),
        "target_allocation": {
            "equity_pct": target_equity_pct,
            "bond_pct":   target_bond_pct,
            "cash_pct":   target_cash_pct,
        },
        "current_allocation": {
            "equity_pct": current_equity_pct,
            "equity_value": round(equity_value, 2),
            "bond_pct":   current_bond_pct,
            "bond_value": round(bond_value, 2),
            "cash_pct":   current_cash_pct,
            "cash_value": round(cash_value, 2),
        },
        "rebalancing_actions": {
            "equity": {"delta": equity_delta, "action": _action(equity_delta)},
            "bonds":  {"delta": bond_delta,   "action": _action(bond_delta)},
            "cash":   {"delta": cash_delta,   "action": _action(cash_delta)},
        },
        "drift_from_target": {
            "equity_drift_pct": round(current_equity_pct - target_equity_pct, 1),
            "bond_drift_pct":   round(current_bond_pct   - target_bond_pct,   1),
            "cash_drift_pct":   round(current_cash_pct   - target_cash_pct,   1),
        },
        "rebalance_needed": any(
            abs(d) >= 5 for d in [
                current_equity_pct - target_equity_pct,
                current_bond_pct   - target_bond_pct,
                current_cash_pct   - target_cash_pct,
            ]
        ),
        "position_breakdown": position_details,
        "note": (
            "Asset class assignment uses ticker/description heuristics. "
            "Verify classification for any position labeled unexpectedly. "
            "Consider executing sells first in tax-advantaged accounts to avoid taxable events."
        ),
    }


async def explore_emoney_cards(
    http_session,
    card_ids: list[int] | None = None,
) -> dict:
    """
    Probe unexplored Emoney CardSwitcher card endpoints to discover
    what data is available.  Useful for finding insurance, tax projection,
    estate, or other plan data not yet surfaced by the MCP.

    Parameters
    ----------
    card_ids : list of card IDs to probe (default: [5, 6, 7, 10, 12, 14, 15, 16])
    """
    http = await http_session.get_http()

    if card_ids is None:
        card_ids = [5, 6, 7, 10, 12, 14, 15, 16]

    results = {}
    for cid in card_ids:
        data = await _get_card(http, cid)
        if data is None:
            results[f"card_{cid}"] = {"status": "unavailable_or_error"}
        else:
            keys = list(data.keys()) if isinstance(data, dict) else []
            results[f"card_{cid}"] = {
                "status":    "available",
                "top_keys":  keys,
                "card_id":   cid,
                "data":      data,
            }

    available = [k for k, v in results.items() if v.get("status") == "available"]
    return {
        "probed_cards":     card_ids,
        "available_cards":  available,
        "unavailable_count": len(card_ids) - len(available),
        "results":          results,
        "note": (
            "Use this tool to discover new Emoney data sources. "
            "If a card returns useful financial data, it can be wrapped into a "
            "dedicated tool in a future update."
        ),
    }
