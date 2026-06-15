"""
Portfolio-level analysis — asset location efficiency, rebalancing targets,
and a discovery tool for unexplored Emoney data cards.

Public functions
----------------
get_asset_location_efficiency(http_session)
    Grades how well each holding is positioned for tax efficiency using a
    simple heuristic: high-efficiency assets (index funds, growth equities)
    belong in taxable accounts; low-efficiency assets (bonds, REITs, TIPS)
    belong in tax-deferred or tax-free accounts.
    Returns an A–F letter grade, per-position ratings, and improvement suggestions.

get_rebalancing_targets(http_session, target_equity_pct, target_bond_pct,
                         target_cash_pct)
    Classifies every holding into equity / bond / cash and computes the
    dollar amount to buy or sell in each bucket to reach the target allocation.
    Default target is 60/30/10 (equity / bond / cash).

explore_emoney_cards(http_session, card_ids)
    Developer/debug tool.  Probes a list of CardSwitcher card IDs and returns
    the full payload of any that respond successfully.  Useful for discovering
    new Emoney data sources not yet wrapped into dedicated tools.

Internal helpers
----------------
_classify_asset(ticker, description)
    Heuristically maps a ticker symbol + description to one of the asset
    classes in ``_ASSET_EFFICIENCY`` using keyword matching.

_ASSET_EFFICIENCY
    Scores each asset class from 1 (most tax-inefficient — put in sheltered
    accounts) to 9 (most tax-efficient — fine in taxable).
"""

import time
from datetime import datetime

from ._helpers import _get_card, _INV_URL, _month_offset
from .accounts import _build_account_type_map, _match_tax_bucket

# ---------------------------------------------------------------------------
# Asset class tax-efficiency scores
# ---------------------------------------------------------------------------
# Scale: 1 = very tax-inefficient (high income / short-term distributions)
#        9 = very tax-efficient (low turnover, qualified dividends, no income)
#
# Assets scoring ≥ 6 are fine in taxable brokerage accounts.
# Assets scoring ≤ 5 ideally belong in a tax-deferred or Roth account.
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


# ---------------------------------------------------------------------------
# Shared investment data fetch helper
# ---------------------------------------------------------------------------

async def _get_investment_data(http_session) -> tuple[dict | None, dict | None]:
    """
    Fetch GetInvestmentData and return (data_dict, error_dict).

    On success: (data, None).  On failure: (None, {"error": "..."}).
    Callers should check the second element first.
    """
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={int(time.time()*1000)}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return None, {"error": f"GetInvestmentData returned {resp.status_code}. Session may have expired."}
    return resp.json(), None


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

    data, err = await _get_investment_data(http_session)
    if err:
        return err

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

    data, err = await _get_investment_data(http_session)
    if err:
        return err

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


# ---------------------------------------------------------------------------
# get_available_cards  (v0.8.0)
# ---------------------------------------------------------------------------

async def get_available_cards(
    http_session,
    card_ids: list[int] | None = None,
) -> dict:
    """
    Return a clean inventory of all responding Emoney CardSwitcher card IDs
    with their top-level data keys and a brief data-shape fingerprint.

    Wraps ``explore_emoney_cards`` with cleaner, more AI-readable output —
    useful for discovering new data sources without wading through raw payloads.

    Parameters
    ----------
    card_ids : list of card IDs to probe
               (default: all known + common unknown IDs [1–16])
    """
    if card_ids is None:
        card_ids = list(range(1, 17))   # probe cards 1–16 in one call

    raw = await explore_emoney_cards(http_session, card_ids=card_ids)

    inventory = []
    for cid in card_ids:
        entry = raw["results"].get(f"card_{cid}") or {}
        if entry.get("status") == "available":
            data = entry.get("data") or {}
            keys = list(data.keys()) if isinstance(data, dict) else []
            # Brief type hints for each key (str/int/float/list/dict/null)
            key_types = {}
            for k, v in data.items():
                if v is None:
                    key_types[k] = "null"
                elif isinstance(v, list):
                    key_types[k] = f"list[{len(v)}]"
                elif isinstance(v, dict):
                    key_types[k] = "dict"
                elif isinstance(v, bool):
                    key_types[k] = "bool"
                elif isinstance(v, float):
                    key_types[k] = "float"
                elif isinstance(v, int):
                    key_types[k] = "int"
                else:
                    key_types[k] = "str"
            inventory.append({
                "card_id":    cid,
                "status":     "available",
                "key_count":  len(keys),
                "keys":       key_types,
            })
        else:
            inventory.append({
                "card_id": cid,
                "status":  "unavailable",
            })

    available = [r for r in inventory if r["status"] == "available"]
    return {
        "probed_range":    f"cards {min(card_ids)}–{max(card_ids)}",
        "available_count": len(available),
        "total_probed":    len(card_ids),
        "inventory":       inventory,
        "known_cards": {
            1:  "Account groups with per-account detail",
            2:  "Financial plan goals",
            3:  "Investment portfolio value + today's change",
            4:  "Asset allocation model target",
            8:  "Net worth history array",
            9:  "Net worth totals (assets / liabilities)",
            11: "Net worth change MTD/YTD",
            13: "Cash flow summary + 5 recent transactions",
        },
        "note": (
            "Cards not listed in known_cards are undocumented. "
            "If an unknown card returns interesting data, use explore_emoney_cards "
            "to view its full payload and build a dedicated tool."
        ),
    }


async def get_portfolio_concentration(
    http_session,
    concentration_threshold_pct: float = 10.0,
) -> dict:
    """
    Identify overly concentrated positions and score overall diversification.

    Flags any single position that represents more than `concentration_threshold_pct`
    percent of the total portfolio.  Scores the portfolio A-F based on how many
    positions exceed 5%, 10%, and 20% thresholds.

    Parameters
    ----------
    concentration_threshold_pct : flag positions above this % (default 10%)
    """
    import asyncio as _asyncio

    # Fetch full holdings data and Card 6 (fast top-holdings with tickers) in parallel
    (data, err), card6 = await _asyncio.gather(
        _get_investment_data(http_session),
        _get_card(await http_session.get_http(), 6),
    )
    if err:
        return err

    total_value = 0.0
    all_positions: list[dict] = []

    for acct in data.get("Accounts", []):
        for h in acct.get("Holdings", []):
            val = h.get("Value") or 0.0
            total_value += val
            ticker = (h.get("Ticker") or "").upper().strip()
            desc   = h.get("Description") or h.get("Name") or ticker

            # Is it a fund or a single stock? Rough heuristic
            is_fund = (
                any(kw in desc.lower() for kw in ("fund", "etf", "index", "trust", "portfolio"))
                or (len(ticker) > 4)
                or ticker in ("SPY", "QQQ", "VTI", "VTSAX", "FSKAX", "BND", "AGG", "VXUS")
            )
            all_positions.append({
                "ticker":      ticker,
                "description": desc,
                "value":       round(val, 2),
                "is_fund":     is_fund,
                "account":     acct.get("AccountName") or acct.get("Name", ""),
            })

    if total_value <= 0:
        return {"error": "No investment positions found."}

    # Annotate with percentages
    for p in all_positions:
        p["pct_of_portfolio"] = round(p["value"] / total_value * 100, 2)

    all_positions.sort(key=lambda x: x["value"], reverse=True)
    top_10 = all_positions[:10]

    # Concentration analysis
    threshold = max(1.0, concentration_threshold_pct)
    concentrated = [p for p in all_positions if p["pct_of_portfolio"] >= threshold]
    over_20      = [p for p in all_positions if p["pct_of_portfolio"] >= 20]
    over_10      = [p for p in all_positions if p["pct_of_portfolio"] >= 10]
    over_5       = [p for p in all_positions if p["pct_of_portfolio"] >= 5]

    # Grade: A = no position > 5%, B = 1 position 5-10%, C = any >10%, D = any >20%, F = any >33%
    if any(p["pct_of_portfolio"] >= 33 for p in all_positions):
        grade = "F"
    elif over_20:
        grade = "D"
    elif over_10:
        grade = "C"
    elif over_5:
        grade = "B"
    else:
        grade = "A"

    single_stock_pct = round(
        sum(p["value"] for p in all_positions if not p["is_fund"]) / total_value * 100, 1
    )
    fund_pct = round(100 - single_stock_pct, 1)

    recommendations = []
    for p in over_20:
        if not p["is_fund"]:
            recommendations.append(
                f"Consider trimming {p['ticker']} ({p['pct_of_portfolio']:.1f}% of portfolio) — "
                "single-stock positions above 20% create significant concentration risk."
            )
    if single_stock_pct > 25:
        recommendations.append(
            f"Single stocks represent {single_stock_pct:.0f}% of the portfolio. "
            "Diversifying into broad index funds would reduce idiosyncratic risk."
        )
    if not recommendations:
        recommendations.append("Portfolio concentration looks healthy — no single position dominates.")

    return {
        "as_of":                   datetime.now().strftime("%Y-%m-%d"),
        "total_portfolio_value":   round(total_value, 2),
        "position_count":          len(all_positions),
        "diversification_grade":   grade,
        "concentrated_positions":  concentrated,
        "top_10_positions":        top_10,
        "asset_type_breakdown": {
            "single_stocks_pct": single_stock_pct,
            "funds_pct":         fund_pct,
        },
        "thresholds_exceeded": {
            "above_5pct":  len(over_5),
            "above_10pct": len(over_10),
            "above_20pct": len(over_20),
        },
        "recommendations":         recommendations,
        "card6_top_holdings":      [
            {
                "name":   h.get("Name", ""),
                "ticker": (h.get("Ticker") or "").upper().strip(),
                "value":  round(h.get("Value") or 0, 2),
            }
            for h in (card6.get("Investments") or [] if card6 else [])
        ],
        "note": (
            "Single stock vs. fund classification uses ticker length and description keywords. "
            "Review concentrated_positions to confirm they are single stocks and not misclassified funds."
        ),
    }


async def get_net_worth_velocity(http_session, months: int = 12) -> dict:
    """
    Compute the rate of net worth growth from historical Card 8 data.

    Returns month-over-month changes, rolling average growth rate, year-over-year
    comparison, and a 12-month-forward projection at current velocity.

    Parameters
    ----------
    months : months of history to analyse (default 12, max 60)
    """
    months = min(max(months, 3), 60)

    http   = await http_session.get_http()
    card8  = await _get_card(http, 8)
    if card8 is None:
        return {"error": "Card 8 (net worth history) unavailable. Session may have expired."}

    # Card 8 is a dict with a History array of net-worth values, oldest first
    # (newest last); NetWorth holds the current value. Mirror get_net_worth_history
    # so ordering stays consistent across tools.
    raw_history = (card8.get("History") if isinstance(card8, dict) else card8) or []
    if not raw_history:
        return {"error": "No net worth history data found in Card 8."}

    raw_history = raw_history[-months:]  # keep the most recent N months

    # Label each point; the newest element (last) is the current month (months_ago = 0).
    now = datetime.now()
    labelled = []
    total = len(raw_history)
    for i, val in enumerate(raw_history):
        months_ago = total - 1 - i
        dt = _month_offset(now, months_ago)
        labelled.append({"month": dt.strftime("%Y-%m"), "net_worth": val})
    # raw_history is already oldest-first — no reverse needed.

    # Compute month-over-month changes
    history_out = []
    for i, entry in enumerate(labelled):
        prev_val = labelled[i - 1]["net_worth"] if i > 0 else None
        change      = round(entry["net_worth"] - prev_val, 2) if prev_val is not None else None
        change_pct  = round(change / abs(prev_val) * 100, 2) if (prev_val and change is not None) else None
        history_out.append({
            "month":       entry["month"],
            "net_worth":   round(entry["net_worth"], 2),
            "change":      change,
            "change_pct":  change_pct,
        })

    # Prefer the card's authoritative NetWorth field; fall back to the newest point.
    current_nw = round(
        (card8.get("NetWorth") if isinstance(card8, dict) else None)
        or labelled[-1]["net_worth"], 2
    )

    # Rolling averages
    monthly_changes = [h["change"] for h in history_out if h["change"] is not None]
    avg_monthly_gain = round(sum(monthly_changes) / len(monthly_changes), 2) if monthly_changes else 0
    avg_annual_rate  = round(avg_monthly_gain * 12 / abs(current_nw) * 100, 2) if current_nw != 0 else None

    # YoY: compare last 12 months vs prior 12 months
    this_year_gain = last_year_gain = None
    yoy_accel      = None
    if len(monthly_changes) >= 12:
        this_year_gain = round(sum(monthly_changes[-12:]), 2)
    if len(monthly_changes) >= 24:
        last_year_gain = round(sum(monthly_changes[-24:-12]), 2)
    if this_year_gain is not None and last_year_gain is not None and last_year_gain != 0:
        yoy_accel = round((this_year_gain - last_year_gain) / abs(last_year_gain) * 100, 1)

    # Trend
    if len(monthly_changes) >= 6:
        first_half  = sum(monthly_changes[:len(monthly_changes) // 2])
        second_half = sum(monthly_changes[len(monthly_changes) // 2:])
        trend = "accelerating" if second_half > first_half * 1.05 else (
                "decelerating" if second_half < first_half * 0.95 else "stable")
    else:
        trend = "insufficient_data"

    # 12-month projection
    projected_12mo     = round(current_nw + avg_monthly_gain * 12, 2)
    proj_date          = _month_offset(now, -12).strftime("%Y-%m")

    return {
        "as_of":                       now.strftime("%Y-%m-%d"),
        "months_analyzed":             len(labelled),
        "current_net_worth":           current_nw,
        "avg_monthly_gain":            avg_monthly_gain,
        "avg_annual_gain_rate_pct":    avg_annual_rate,
        "this_year_gain":              this_year_gain,
        "last_year_gain":              last_year_gain,
        "yoy_acceleration_pct":        yoy_accel,
        "trend":                       trend,
        "projected_net_worth_12mo":    projected_12mo,
        "projected_12mo_date":         proj_date,
        "monthly_history":             history_out,
        "note": (
            "Net worth history sourced from CardSwitcher Card 8 (up to 60 months). "
            "Projection assumes constant monthly growth equal to the historical average."
        ),
    }



async def get_tax_drag_analysis(
    http_session,
    marginal_rate: float = 0.32,
    ltcg_rate: float = 0.15,
) -> dict:
    """
    Quantify the annual dollar cost of holding tax-inefficient assets in taxable accounts.

    Builds on the asset location logic in get_asset_location_efficiency to compute
    estimated annual tax drag — the extra tax owed because bonds, REITs, and high-
    distribution funds are in taxable brokerage accounts instead of tax-deferred
    or Roth accounts.

    Parameters
    ----------
    marginal_rate : federal marginal ordinary income tax rate (default 32%)
    ltcg_rate     : long-term capital gains tax rate (default 15%)
    """
    marginal_rate = max(0.10, min(marginal_rate, 0.50))
    ltcg_rate     = max(0.0,  min(ltcg_rate, 0.238))

    type_map  = await _build_account_type_map(http_session)
    data, err = await _get_investment_data(http_session)
    if err:
        return err

    # Estimated distribution yields by asset class (conservative estimates)
    _YIELD_BY_CLASS: dict[str, float] = {
        "bond_fund":             0.045,  # 4.5% yield → ordinary income
        "reit":                  0.040,  # 4% distribution → ordinary income
        "tips":                  0.035,
        "money_market":          0.050,
        "dividend_equity":       0.025,  # qualified dividends (lower rate)
        "balanced":              0.025,
        "domestic_equity_index": 0.015,  # mostly qualified dividends
        "international_equity":  0.020,
        "growth_equity":         0.005,
        "muni_bond":             0.035,  # tax-exempt — no drag
        "cash":                  0.050,
        "other":                 0.015,
    }
    # Is distribution ordinary income (True) or qualified dividend (False)?
    _ORDINARY_INCOME_CLASS = {
        "bond_fund", "reit", "tips", "money_market", "cash",
    }

    misplaced  = []
    well_placed = 0
    total_drag  = 0.0
    total_value = 0.0

    for acct in data.get("Accounts", []):
        acct_name   = acct.get("AccountName") or acct.get("Name", "")
        tax_bucket  = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            val    = h.get("Value") or 0.0
            ticker = (h.get("Ticker") or "").upper().strip()
            desc   = h.get("Description") or h.get("Name") or ticker
            total_value += val

            asset_class = _classify_asset(ticker, desc)
            efficiency  = _ASSET_EFFICIENCY.get(asset_class, 5)
            yield_rate  = _YIELD_BY_CLASS.get(asset_class, 0.015)
            is_ordinary = asset_class in _ORDINARY_INCOME_CLASS

            # Drag only applies when tax-inefficient asset is in taxable account
            if tax_bucket == "Taxable" and efficiency <= 4 and val > 0:
                tax_rate        = marginal_rate if is_ordinary else ltcg_rate
                sheltered_rate  = 0.0  # what it would be in tax-deferred
                annual_income   = val * yield_rate
                annual_drag     = round(annual_income * (tax_rate - sheltered_rate), 2)
                total_drag      = round(total_drag + annual_drag, 2)

                misplaced.append({
                    "ticker":              ticker,
                    "description":         desc,
                    "account":             acct_name,
                    "account_type":        tax_bucket,
                    "asset_class":         asset_class,
                    "value":               round(val, 2),
                    "est_yield_pct":       round(yield_rate * 100, 1),
                    "annual_drag_est":     annual_drag,
                    "income_type":         "ordinary" if is_ordinary else "qualified_dividend",
                    "recommended_account": "Tax-Deferred or Tax-Free",
                })
            else:
                well_placed += 1

    misplaced.sort(key=lambda x: x["annual_drag_est"], reverse=True)
    drag_as_pct = round(total_drag / total_value * 100, 3) if total_value > 0 else 0.0

    return {
        "as_of":                       datetime.now().strftime("%Y-%m-%d"),
        "total_annual_tax_drag_est":   total_drag,
        "total_drag_as_pct_portfolio": drag_as_pct,
        "misplaced_position_count":    len(misplaced),
        "well_placed_position_count":  well_placed,
        "misplaced_positions":         misplaced,
        "priority_swaps":              misplaced[:5],
        "assumptions": {
            "marginal_rate":    marginal_rate,
            "ltcg_rate":        ltcg_rate,
        },
        "note": (
            "Tax drag is estimated using typical asset class yields and your marginal rates. "
            "Actual drag depends on your specific fund distributions, holding period, and tax situation. "
            "Swapping bond funds / REITs from taxable to tax-deferred accounts is generally the highest-impact action."
        ),
    }
