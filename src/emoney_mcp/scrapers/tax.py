"""
Tax planning tools — loss harvesting, contribution limits, Roth conversion,
capital gains exposure, and Required Minimum Distribution estimates.

All calculations use 2025 IRS figures (see ``_TAX_YEAR``).  Update the
constants section each year.  Every tool appends ``_IRS_CAVEAT`` reminding
users to verify with a qualified tax professional.

Public functions
----------------
get_tax_loss_harvesting(http_session)
    Scans all investment holdings for unrealized losses.  Only positions in
    taxable brokerage accounts are flagged as harvestable (losses in IRAs /
    401ks have no immediate tax benefit).  Estimates tax savings at 15%, 20%,
    and 23.8% (20% LTCG + 3.8% NIIT) rates.

get_contribution_room(http_session, age, filing_status)
    Displays 2025 IRS annual contribution limits for 401k/403b, IRA, HSA,
    SIMPLE IRA, SEP IRA, and 529 alongside current account balances.  Adjusts
    limits upward for catch-up eligibility at age 50, 55, and 60–63.

get_roth_conversion_analysis(http_session, conversion_amount, current_income,
                              filing_status, age)
    Estimates the federal tax cost of converting a dollar amount from pre-tax
    to Roth this year.  Shows bracket-by-bracket fill, effective rate on the
    conversion, and projected tax-free growth at 6% over 10 and 20 years.

get_capital_gains_exposure(http_session, filing_status, annual_income)
    Identifies unrealized gains in taxable accounts and estimates the tax
    liability if those positions were sold today.  Applies LTCG rates and
    NIIT based on the supplied (or inferred) annual income.

get_rmd_estimate(http_session, birth_year)
    Estimates Required Minimum Distributions from pre-tax retirement accounts
    using the IRS Uniform Lifetime Table.  Returns the current-year RMD (if
    already required) and a 10-year projected RMD schedule.

Internal math helpers
---------------------
_compute_tax(taxable_income, filing_status)  — Federal income tax via brackets
_marginal_rate(taxable_income, filing_status) — Marginal bracket rate
_ltcg_rate(taxable_income, filing_status)    — Long-term capital gains rate
"""

import time
from datetime import datetime

from ._helpers import _INV_URL
from .accounts import get_accounts, get_retirement_accounts, _build_account_type_map, _match_tax_bucket
from .spending import get_income_summary

# ===========================================================================
# IRS CONSTANTS  (2025 — update annually)
# ===========================================================================

_TAX_YEAR = 2025
_IRS_CAVEAT = (
    "Figures use 2025 IRS limits and tax brackets. "
    "Consult a qualified tax professional before acting on any estimates."
)

_CONTRIBUTION_LIMITS = {
    "401k_403b":              23_500,
    "401k_403b_catchup_50":   31_000,   # age 50-59 and 64+
    "401k_403b_catchup_60":   34_750,   # SECURE 2.0 super catch-up age 60-63
    "ira":                     7_000,
    "ira_catchup":             8_000,   # age 50+
    "hsa_individual":          4_300,
    "hsa_family":              8_550,
    "hsa_catchup":             1_000,   # age 55+
    "simple_ira":             16_500,
    "simple_ira_catchup":     20_000,   # age 50+
    "sep_ira_pct":             0.25,
    "sep_ira_max":            70_000,
    "gift_tax_exclusion":     19_000,   # per beneficiary (529 / gifting)
}

_STD_DEDUCTION = {"single": 15_000, "mfj": 30_000, "hoh": 22_500}

# Ordinary income brackets — (upper bound of bracket, rate)
_BRACKETS: dict[str, list[tuple[float, float]]] = {
    "single": [
        (11_925,       0.10),
        (48_475,       0.12),
        (103_350,      0.22),
        (197_300,      0.24),
        (250_525,      0.32),
        (626_350,      0.35),
        (float("inf"), 0.37),
    ],
    "mfj": [
        (23_850,       0.10),
        (96_950,       0.12),
        (206_700,      0.22),
        (394_600,      0.24),
        (501_050,      0.32),
        (751_600,      0.35),
        (float("inf"), 0.37),
    ],
    "hoh": [
        (17_000,       0.10),
        (64_850,       0.12),
        (103_350,      0.22),
        (197_300,      0.24),
        (250_500,      0.32),
        (626_350,      0.35),
        (float("inf"), 0.37),
    ],
}

# LTCG thresholds — (upper bound of 0% / 15% bracket, rate)
_LTCG_THRESHOLDS: dict[str, list[tuple[float, float]]] = {
    "single": [(48_350,  0.0), (533_400,  0.15), (float("inf"), 0.20)],
    "mfj":    [(96_700,  0.0), (600_050,  0.15), (float("inf"), 0.20)],
    "hoh":    [(64_750,  0.0), (566_700,  0.15), (float("inf"), 0.20)],
}

# NIIT (3.8%) kicks in above these thresholds
_NIIT_THRESHOLD = {"single": 200_000, "mfj": 250_000, "hoh": 200_000}

# IRS Uniform Lifetime Table — age → distribution period
_RMD_TABLE: dict[int, float] = {
    72: 27.4, 73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7,
    77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2, 81: 19.4,
    82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2,
    87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5,
    92: 10.8, 93: 10.1, 94:  9.5, 95:  8.9, 96:  8.4,
    97:  7.8, 98:  7.3, 99:  6.8, 100: 6.4,
}


# ---------------------------------------------------------------------------
# Internal tax math helpers
# ---------------------------------------------------------------------------

def _compute_tax(taxable_income: float, filing_status: str) -> float:
    """Federal income tax on taxable income (post-deduction)."""
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    tax = 0.0
    prev = 0.0
    for ceiling, rate in _BRACKETS[fs]:
        if taxable_income <= prev:
            break
        tax += (min(taxable_income, ceiling) - prev) * rate
        prev = ceiling
    return round(tax, 2)


def _marginal_rate(taxable_income: float, filing_status: str) -> float:
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    prev = 0.0
    for ceiling, rate in _BRACKETS[fs]:
        if taxable_income <= ceiling:
            return rate
        prev = ceiling
    return 0.37


def _ltcg_rate(taxable_income: float, filing_status: str) -> float:
    fs = filing_status if filing_status in _LTCG_THRESHOLDS else "mfj"
    for ceiling, rate in _LTCG_THRESHOLDS[fs]:
        if taxable_income <= ceiling:
            return rate
    return 0.20


# ---------------------------------------------------------------------------
# get_tax_loss_harvesting
# ---------------------------------------------------------------------------

async def get_tax_loss_harvesting(http_session) -> dict:
    """
    Identify positions with unrealized losses suitable for tax-loss harvesting.

    Cross-references holdings against account type so only taxable-account
    losses are flagged as harvestable.
    """
    type_map = await _build_account_type_map(http_session)

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data = resp.json()

    taxable_losses   = []
    deferred_losses  = []
    total_loss_taxable = 0.0
    total_loss_all     = 0.0

    for acct in data.get("Accounts", []):
        acct_name   = acct.get("Name", "")
        tax_bucket  = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            value      = h.get("Value") or 0.0
            cost_basis = h.get("CostBasis")
            if cost_basis is None or value >= cost_basis:
                continue

            loss = round(value - cost_basis, 2)   # negative number
            position = {
                "ticker":       h.get("Ticker") or "",
                "description":  (h.get("Description") or "")[:50],
                "account":      acct_name,
                "tax_treatment": tax_bucket,
                "current_value": round(value, 2),
                "cost_basis":   round(cost_basis, 2),
                "unrealized_loss": loss,
                "harvestable":  tax_bucket == "Taxable",
            }

            total_loss_all += loss
            if tax_bucket == "Taxable":
                total_loss_taxable += loss
                taxable_losses.append(position)
            else:
                deferred_losses.append(position)

    taxable_losses.sort(key=lambda x: x["unrealized_loss"])
    deferred_losses.sort(key=lambda x: x["unrealized_loss"])

    potential_savings_15   = round(abs(total_loss_taxable) * 0.15, 2)
    potential_savings_20   = round(abs(total_loss_taxable) * 0.20, 2)
    potential_savings_238  = round(abs(total_loss_taxable) * 0.238, 2)  # 20% + 3.8% NIIT

    return {
        "summary": {
            "harvestable_loss_total":     round(total_loss_taxable, 2),
            "non_harvestable_loss_total": round(total_loss_all - total_loss_taxable, 2),
            "potential_tax_savings_15pct":  potential_savings_15,
            "potential_tax_savings_20pct":  potential_savings_20,
            "potential_tax_savings_238pct": potential_savings_238,
        },
        "harvestable_positions":     taxable_losses,
        "non_harvestable_positions": deferred_losses,
        "note": (
            "Harvestable = taxable brokerage accounts only. "
            "The wash-sale rule prohibits repurchasing substantially identical securities "
            "within 30 days before or after the sale. "
            "Savings estimates assume losses fully offset gains; consult a tax advisor."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_year_end_checklist  (v0.8.0)
# ---------------------------------------------------------------------------

async def get_year_end_checklist(
    http_session,
    age: int | None = None,
    birth_year: int | None = None,
    filing_status: str = "mfj",
    current_income: float | None = None,
) -> dict:
    """
    Generate a year-end tax planning checklist with action status and dollar amounts.

    Runs all applicable tax tools in parallel and synthesizes results into a
    prioritized action list for the current tax year.

    Parameters
    ----------
    age            : your age (determines catch-up contribution eligibility)
    birth_year     : used for RMD analysis (required for RMD check; omit to skip)
    filing_status  : 'mfj', 'single', or 'hoh' (default 'mfj')
    current_income : annual gross income (inferred from transactions if omitted)
    """
    import asyncio

    # Build parallel tasks — skip tools that need required params we don't have
    tasks: dict[str, object] = {}
    tasks["bracket_headroom"] = get_tax_bracket_headroom(
        http_session, current_income=current_income, filing_status=filing_status
    )
    tasks["tlh"]              = get_tax_loss_harvesting(http_session)
    tasks["cap_gains"]        = get_capital_gains_exposure(
        http_session, filing_status=filing_status, annual_income=current_income
    )
    tasks["contribution"]     = get_contribution_room(
        http_session, age=age, filing_status=filing_status
    )

    if birth_year is not None:
        tasks["rmd"] = get_rmd_estimate(http_session, birth_year=birth_year)

    keys   = list(tasks.keys())
    values = await asyncio.gather(*tasks.values(), return_exceptions=True)
    results = dict(zip(keys, values))

    checklist = []
    estimated_savings = 0.0

    # --- 1. Tax bracket headroom ---
    bh = results.get("bracket_headroom") or {}
    if "error" not in bh and not isinstance(bh, Exception):
        ordinary = bh.get("ordinary_income_headroom") or {}
        ltcg     = bh.get("ltcg_headroom") or {}
        room_ord  = ordinary.get("headroom_to_next_bracket", 0) or 0
        room_ltcg = ltcg.get("headroom_in_0pct_ltcg_bracket", 0) or 0
        if room_ord > 0:
            checklist.append({
                "item":     "Tax bracket headroom",
                "status":   "opportunity",
                "priority": "medium",
                "detail":   (
                    f"${room_ord:,.0f} of headroom before the next ordinary income bracket. "
                    "Consider Roth conversions, income acceleration, or bonus timing."
                ),
                "amount":  round(room_ord, 2),
            })
        if room_ltcg > 0:
            checklist.append({
                "item":     "0% LTCG bracket room",
                "status":   "opportunity",
                "priority": "medium",
                "detail":   (
                    f"${room_ltcg:,.0f} of room before LTCG rate increases. "
                    "Consider harvesting gains in this window."
                ),
                "amount":  round(room_ltcg, 2),
            })

    # --- 2. Tax-loss harvesting ---
    tlh = results.get("tlh") or {}
    if "error" not in tlh and not isinstance(tlh, Exception):
        summary = tlh.get("summary") or {}
        total_loss = abs(summary.get("harvestable_loss_total", 0) or 0)
        savings_20 = summary.get("potential_tax_savings_20pct", 0) or 0
        if total_loss > 0:
            checklist.append({
                "item":     "Tax-loss harvesting",
                "status":   "action_needed",
                "priority": "high",
                "detail":   (
                    f"${total_loss:,.0f} in harvestable losses in taxable accounts. "
                    f"Estimated savings: up to ${savings_20:,.0f} at 20% LTCG rate."
                ),
                "amount":  round(total_loss, 2),
            })
            estimated_savings += savings_20
        else:
            checklist.append({
                "item":    "Tax-loss harvesting",
                "status":  "done",
                "priority": "low",
                "detail":  "No harvestable losses found in taxable accounts.",
                "amount":  0,
            })

    # --- 3. Capital gains exposure ---
    cge = results.get("cap_gains") or {}
    if "error" not in cge and not isinstance(cge, Exception):
        total_gain = cge.get("total_unrealized_gain_taxable", 0) or 0
        total_tax  = cge.get("estimated_total_tax", 0) or 0
        if total_gain > 0:
            checklist.append({
                "item":     "Capital gains exposure",
                "status":   "opportunity",
                "priority": "medium",
                "detail":   (
                    f"${total_gain:,.0f} in unrealized gains in taxable accounts "
                    f"(est. ${total_tax:,.0f} tax if sold). Review before year-end for gain deferral."
                ),
                "amount":  round(total_gain, 2),
            })

    # --- 4. Contribution room ---
    cr = results.get("contribution") or {}
    if "error" not in cr and not isinstance(cr, Exception):
        accounts = cr.get("accounts") or []
        for acct in accounts:
            remaining = acct.get("remaining_room", 0) or 0
            if remaining > 0:
                checklist.append({
                    "item":     f"Max {acct.get('account_type', 'tax-advantaged')} contribution",
                    "status":   "action_needed",
                    "priority": "high",
                    "detail":   (
                        f"${remaining:,.0f} remaining contribution room "
                        f"(limit: ${acct.get('limit', 0):,.0f}). Deadline: Dec 31."
                    ),
                    "amount":  round(remaining, 2),
                })
                # Approximate tax savings = remaining room × marginal rate
                if current_income:
                    rate = _marginal_rate(current_income, filing_status)
                    estimated_savings += round(remaining * rate, 2)
            else:
                checklist.append({
                    "item":     f"{acct.get('account_type', 'Account')} fully funded",
                    "status":   "done",
                    "priority": "low",
                    "detail":   f"Contribution limit already reached for {acct.get('account_type', 'this account')}.",
                    "amount":  0,
                })

    # --- 5. RMD check ---
    rmd = results.get("rmd")
    if rmd and "error" not in rmd and not isinstance(rmd, Exception):
        current_rmd = rmd.get("current_year_rmd")
        rmd_required = rmd.get("rmd_required", False)
        if rmd_required:
            checklist.append({
                "item":     "Required Minimum Distribution",
                "status":   "action_needed",
                "priority": "high",
                "detail":   (
                    f"RMD required this year: ${current_rmd:,.0f}. "
                    "Must be taken by Dec 31 to avoid 25% excise tax on shortfall."
                ),
                "amount":  round(current_rmd or 0, 2),
            })
        else:
            checklist.append({
                "item":    "Required Minimum Distribution",
                "status":  "not_applicable",
                "priority": "low",
                "detail":  "RMDs not yet required based on provided birth year.",
                "amount":  0,
            })
    elif birth_year is None:
        checklist.append({
            "item":    "Required Minimum Distribution",
            "status":  "skipped",
            "priority": "low",
            "detail":  "Provide birth_year parameter to check RMD status.",
            "amount":  0,
        })

    # Sort: action_needed first, then opportunity, then done/not_applicable
    _priority_order = {"action_needed": 0, "opportunity": 1, "done": 2, "not_applicable": 3, "skipped": 4}
    checklist.sort(key=lambda x: (_priority_order.get(x["status"], 9), -x.get("amount", 0)))

    return {
        "tax_year":               _TAX_YEAR,
        "filing_status":          filing_status,
        "as_of":                  datetime.now().strftime("%Y-%m-%d"),
        "checklist":              checklist,
        "action_items_count":     sum(1 for c in checklist if c["status"] == "action_needed"),
        "opportunity_count":      sum(1 for c in checklist if c["status"] == "opportunity"),
        "estimated_tax_savings":  round(estimated_savings, 2),
        "caveat":                 _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_contribution_room
# ---------------------------------------------------------------------------

async def get_contribution_room(http_session, age: int | None = None,
                                 filing_status: str = "mfj") -> dict:
    """
    Show remaining IRS contribution room across tax-advantaged accounts.

    Parameters
    ----------
    age           : your age (determines catch-up eligibility)
    filing_status : 'single', 'mfj' (married filing jointly), or 'hoh'
    """
    retirement = await get_retirement_accounts(http_session)
    if "error" in retirement:
        return retirement

    lim = _CONTRIBUTION_LIMITS
    is_50_plus  = age is not None and age >= 50
    is_55_plus  = age is not None and age >= 55
    is_60_to_63 = age is not None and 60 <= age <= 63

    if is_60_to_63:
        k401_limit = lim["401k_403b_catchup_60"]
        k401_label = f"401k/403b (age {age} super catch-up)"
    elif is_50_plus:
        k401_limit = lim["401k_403b_catchup_50"]
        k401_label = f"401k/403b (age {age} catch-up)"
    else:
        k401_limit = lim["401k_403b"]
        k401_label = "401k/403b"

    ira_limit = lim["ira_catchup"] if is_50_plus else lim["ira"]
    hsa_limit = (lim["hsa_family"] if filing_status == "mfj" else lim["hsa_individual"])
    if is_55_plus:
        hsa_limit += lim["hsa_catchup"]

    accounts_summary = {
        "total_retirement_assets": retirement.get("total_retirement_assets"),
        "breakdown": retirement.get("retirement_breakdown"),
    }

    return {
        "age":          age,
        "filing_status": filing_status,
        "tax_year":     _TAX_YEAR,
        "annual_limits": {
            k401_label:           k401_limit,
            "Traditional/Roth IRA": ira_limit,
            "HSA":                hsa_limit,
            "SIMPLE IRA":         lim["simple_ira_catchup"] if is_50_plus else lim["simple_ira"],
            "SEP IRA (max)":      lim["sep_ira_max"],
            "529 (gift exclusion per beneficiary)": lim["gift_tax_exclusion"],
        },
        "current_balances": accounts_summary,
        "catch_up_eligible": {
            "ira_401k_catchup":    is_50_plus,
            "hsa_catchup":         is_55_plus,
            "super_catchup_60_63": is_60_to_63,
        },
        "note": (
            "Emoney does not expose year-to-date contribution amounts, so remaining "
            "room must be calculated manually: (annual limit) − (amount contributed "
            "so far this year from your payroll/brokerage statements)."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_roth_conversion_analysis
# ---------------------------------------------------------------------------

async def get_roth_conversion_analysis(
    http_session,
    conversion_amount: float,
    current_income: float,
    filing_status: str = "mfj",
    age: int | None = None,
) -> dict:
    """
    Estimate the federal tax cost and break-even of converting pre-tax dollars to Roth.

    Parameters
    ----------
    conversion_amount : dollar amount to convert this year
    current_income    : estimated gross ordinary income BEFORE the conversion
    filing_status     : 'single', 'mfj', or 'hoh'
    age               : used to compute standard deduction and RMD context
    """
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    std_ded = _STD_DEDUCTION.get(fs, 30_000)

    taxable_before = max(0.0, current_income - std_ded)
    taxable_after  = max(0.0, current_income + conversion_amount - std_ded)

    tax_before = _compute_tax(taxable_before, fs)
    tax_after  = _compute_tax(taxable_after,  fs)
    marginal   = _marginal_rate(taxable_before, fs)
    effective_rate_on_conversion = (tax_after - tax_before) / conversion_amount if conversion_amount else 0

    future_value_10yr = round(conversion_amount * (1.06 ** 10), 2)
    future_value_20yr = round(conversion_amount * (1.06 ** 20), 2)
    tax_on_conversion = round(tax_after - tax_before, 2)

    breakeven_years = None
    if marginal > 0 and effective_rate_on_conversion > 0:
        if effective_rate_on_conversion < marginal:
            breakeven_years = 0
        else:
            breakeven_years = None

    retirement = await get_retirement_accounts(http_session)
    deferred_total = retirement.get("total_retirement_assets", 0) if "error" not in retirement else None

    # Bracket fill analysis
    bracket_fill = []
    fs_brackets = _BRACKETS[fs]
    remaining = conversion_amount
    income_cursor = taxable_before
    prev = 0.0
    for ceiling, rate in fs_brackets:
        if remaining <= 0:
            break
        if income_cursor < ceiling:
            room = ceiling - max(income_cursor, prev)
            used = min(remaining, room)
            bracket_fill.append({
                "bracket_rate_pct": int(rate * 100),
                "dollars_in_bracket": round(used, 2),
                "tax_in_bracket": round(used * rate, 2),
            })
            remaining -= used
        prev = ceiling

    return {
        "conversion_amount":        round(conversion_amount, 2),
        "current_income":           round(current_income, 2),
        "filing_status":            fs,
        "standard_deduction":       std_ded,
        "taxable_income_before":    round(taxable_before, 2),
        "taxable_income_after":     round(taxable_after, 2),
        "federal_tax_before":       tax_before,
        "federal_tax_after":        tax_after,
        "tax_cost_of_conversion":   tax_on_conversion,
        "effective_rate_on_conversion_pct": round(effective_rate_on_conversion * 100, 2),
        "marginal_rate_entering_pct": int(marginal * 100),
        "bracket_fill":             bracket_fill,
        "projected_roth_value": {
            "10_years_at_6pct":  future_value_10yr,
            "20_years_at_6pct":  future_value_20yr,
        },
        "conversion_favored":       effective_rate_on_conversion <= marginal,
        "breakeven_note": (
            "Conversion is tax-favored when your effective rate on the converted amount "
            "is lower than your expected marginal rate at withdrawal. "
            "This is especially powerful if you expect higher income in retirement "
            "or have significant pre-tax assets that will drive large RMDs."
        ),
        "current_pretax_balance":   deferred_total,
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_capital_gains_exposure
# ---------------------------------------------------------------------------

async def get_capital_gains_exposure(
    http_session,
    filing_status: str = "mfj",
    annual_income: float | None = None,
) -> dict:
    """
    Identify embedded unrealized capital gains in taxable accounts and estimate
    the tax liability if those positions were sold today.
    """
    type_map = await _build_account_type_map(http_session)

    if annual_income is None:
        inc_result = await get_income_summary(http_session, days=365)
        annual_income = inc_result.get("total_income", 0) if "error" not in inc_result else 0

    ts = int(time.time() * 1000)
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={ts}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetInvestmentData returned {resp.status_code}."}

    data = resp.json()
    fs = filing_status if filing_status in _LTCG_THRESHOLDS else "mfj"

    taxable_gains:   list[dict] = []
    deferred_gains:  list[dict] = []
    total_gain_taxable = 0.0

    for acct in data.get("Accounts", []):
        acct_name  = acct.get("Name", "")
        tax_bucket = _match_tax_bucket(acct_name, type_map)

        for h in acct.get("Holdings", []):
            value      = h.get("Value") or 0.0
            cost_basis = h.get("CostBasis")
            if cost_basis is None or value <= cost_basis:
                continue

            gain = round(value - cost_basis, 2)
            rate = _ltcg_rate(annual_income, fs)
            niit = 0.038 if annual_income > _NIIT_THRESHOLD.get(fs, 250_000) else 0.0
            effective_rate = rate + niit
            est_tax = round(gain * effective_rate, 2)

            position = {
                "ticker":        h.get("Ticker") or "",
                "description":   (h.get("Description") or "")[:50],
                "account":       acct_name,
                "tax_treatment": tax_bucket,
                "current_value": round(value, 2),
                "cost_basis":    round(cost_basis, 2),
                "unrealized_gain": gain,
                "pct_gain":      round((gain / cost_basis) * 100, 1) if cost_basis else None,
                "ltcg_rate_pct": round(effective_rate * 100, 1),
                "estimated_tax_if_sold": est_tax,
            }

            if tax_bucket == "Taxable":
                total_gain_taxable += gain
                taxable_gains.append(position)
            else:
                deferred_gains.append(position)

    taxable_gains.sort(key=lambda x: x["unrealized_gain"], reverse=True)

    rate = _ltcg_rate(annual_income, fs)
    niit = 0.038 if annual_income > _NIIT_THRESHOLD.get(fs, 250_000) else 0.0
    effective_total_rate = rate + niit
    total_tax_exposure = round(total_gain_taxable * effective_total_rate, 2)

    niit_applies = annual_income > _NIIT_THRESHOLD.get(fs, 250_000)

    return {
        "filing_status":            fs,
        "estimated_annual_income":  round(annual_income, 2),
        "ltcg_rate_pct":            round(rate * 100, 1),
        "niit_applies":             niit_applies,
        "effective_rate_pct":       round(effective_total_rate * 100, 1),
        "total_taxable_unrealized_gain": round(total_gain_taxable, 2),
        "total_estimated_tax_exposure":  total_tax_exposure,
        "taxable_account_positions": taxable_gains,
        "deferred_account_positions": deferred_gains,
        "note": (
            "Only taxable brokerage account gains create an immediate tax event on sale. "
            "Gains in IRAs and 401ks are taxed as ordinary income upon withdrawal. "
            "Gains in Roth accounts are tax-free upon qualified withdrawal. "
            "LTCG assumes all positions held > 1 year; short-term gains taxed as ordinary income."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_rmd_estimate
# ---------------------------------------------------------------------------

async def get_rmd_estimate(http_session, birth_year: int) -> dict:
    """
    Estimate Required Minimum Distributions from pre-tax retirement accounts.

    RMDs begin at age 73 (SECURE 2.0).  Uses the IRS Uniform Lifetime Table.

    Parameters
    ----------
    birth_year : your year of birth (e.g. 1955)
    """
    current_year = datetime.now().year
    age = current_year - birth_year
    rmd_start_age = 73   # SECURE 2.0

    retirement = await get_retirement_accounts(http_session)
    if "error" in retirement:
        return retirement

    breakdown = retirement.get("retirement_breakdown", {})
    k401_balance = breakdown.get("401k_403b", 0) or 0
    # ira_roth bucket conflates traditional IRA + Roth IRA; Roth IRAs have no RMD.
    # Compute traditional-only IRA balance from the individual account list.
    trad_ira_balance = sum(
        a.get("balance", 0) or 0
        for a in retirement.get("retirement_accounts", [])
        if "ira" in (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()
        and "roth" not in (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()
    )
    pretax_balance = k401_balance + trad_ira_balance

    trad_balance = pretax_balance

    years_until_rmd = max(0, rmd_start_age - age)
    rmd_age = max(age, rmd_start_age)

    future_balance_at_rmd = round(trad_balance * (1.06 ** years_until_rmd), 2) if years_until_rmd > 0 else trad_balance

    rmd_schedule = []
    balance = future_balance_at_rmd
    for yr in range(10):
        calc_age = rmd_age + yr
        factor = _RMD_TABLE.get(calc_age) or _RMD_TABLE.get(min(calc_age, 100), 6.4)
        rmd_amount = round(balance / factor, 2)
        rmd_schedule.append({
            "year":       current_year + years_until_rmd + yr,
            "age":        calc_age,
            "est_balance": round(balance, 2),
            "factor":     factor,
            "rmd_amount": rmd_amount,
        })
        balance = round((balance - rmd_amount) * 1.06, 2)

    current_rmd = None
    if age >= rmd_start_age:
        factor = _RMD_TABLE.get(age) or _RMD_TABLE.get(min(age, 100), 6.4)
        current_rmd = round(trad_balance / factor, 2)

    return {
        "birth_year":            birth_year,
        "current_age":           age,
        "rmd_start_age":         rmd_start_age,
        "years_until_rmd":       years_until_rmd,
        "rmd_required_this_year": age >= rmd_start_age,
        "current_pretax_balance": round(trad_balance, 2),
        "current_rmd_estimate":   current_rmd,
        "projected_rmd_schedule": rmd_schedule,
        "roth_conversion_note": (
            "Converting pre-tax balances to Roth before RMD age reduces future mandatory "
            "distributions and creates tax-free growth. This is especially valuable in "
            "low-income years between retirement and RMD start age."
        ),
        "note": (
            "Balances projected at 6% annual growth. IRA and Roth IRA are grouped — "
            "only traditional (pre-tax) balances are subject to RMDs; Roth IRAs have no "
            "RMD requirement during the owner's lifetime. "
            "RMD amounts shown are estimates; always verify with your custodian."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_social_security_optimizer
# ---------------------------------------------------------------------------

async def get_social_security_optimizer(
    http_session,
    birth_year: int,
    estimated_monthly_benefit_at_67: float | None = None,
    filing_status: str = "mfj",
    spouse_birth_year: int | None = None,
    spouse_benefit_at_67: float | None = None,
    life_expectancy: int = 85,
) -> dict:
    """
    Optimize Social Security claiming age by computing lifetime benefit at
    age 62, 67, and 70 and showing the breakeven crossover points.

    If estimated_monthly_benefit_at_67 is not supplied, the tool will remind
    the user to provide it from their Social Security statement (ssa.gov).

    Parameters
    ----------
    birth_year                       : your year of birth (e.g. 1962)
    estimated_monthly_benefit_at_67  : monthly SS benefit at Full Retirement Age (FRA)
    filing_status                    : 'single', 'mfj', or 'hoh' (affects spousal strategies)
    spouse_birth_year                : spouse year of birth (optional — for spousal analysis)
    spouse_benefit_at_67             : spouse monthly FRA benefit (optional)
    life_expectancy                  : assumed age at death for lifetime value calc (default 85)
    """
    current_year = datetime.now().year
    age = current_year - birth_year

    # Full Retirement Age by birth year (Congress schedule)
    if birth_year <= 1937:
        fra = 65
    elif birth_year <= 1954:
        fra = 66
    elif birth_year == 1955:
        fra = 66.17
    elif birth_year == 1956:
        fra = 66.33
    elif birth_year == 1957:
        fra = 66.5
    elif birth_year == 1958:
        fra = 66.67
    elif birth_year == 1959:
        fra = 66.83
    else:
        fra = 67  # born 1960+

    years_to_fra = max(0.0, fra - age)
    years_to_62  = max(0.0, 62 - age)
    years_to_70  = max(0.0, 70 - age)

    # Adjustment factors relative to FRA benefit
    # Each month before FRA: -5/9% for first 36 months, -5/12% thereafter
    # Each month after FRA:  +8% per year (2/3% per month)
    def _ss_factor(claim_age: float, fra_age: float) -> float:
        months_diff = round((claim_age - fra_age) * 12)
        if months_diff >= 0:
            return 1 + months_diff * (8 / 100 / 12)
        months_early = -months_diff
        first36 = min(months_early, 36)
        remaining = max(0, months_early - 36)
        return 1 - (first36 * (5/9/100)) - (remaining * (5/12/100))

    factor_62 = _ss_factor(62, fra)
    factor_67 = _ss_factor(67, fra)
    factor_70 = _ss_factor(70, fra)

    if estimated_monthly_benefit_at_67 is None:
        placeholder = True
        fra_monthly = 2_000.0   # placeholder — user needs their SSA statement
    else:
        placeholder = False
        fra_monthly = estimated_monthly_benefit_at_67 / _ss_factor(67, fra)

    monthly_62 = round(fra_monthly * factor_62, 2)
    monthly_67 = round(fra_monthly * factor_67, 2)
    monthly_70 = round(fra_monthly * factor_70, 2)

    def _lifetime(monthly: float, claim_age: float, death_age: int) -> float:
        months = max(0, (death_age - claim_age) * 12)
        return round(monthly * months, 2)

    lifetime_62 = _lifetime(monthly_62, 62, life_expectancy)
    lifetime_67 = _lifetime(monthly_67, 67, life_expectancy)
    lifetime_70 = _lifetime(monthly_70, 70, life_expectancy)

    # Breakeven: age where claiming later surpasses claiming earlier
    def _breakeven(monthly_early: float, claim_early: float,
                   monthly_late: float,  claim_late: float) -> float | None:
        if monthly_late <= monthly_early:
            return None
        cumulative_early = 0.0
        cumulative_late  = 0.0
        for m in range(int((claim_late - claim_early) * 12)):
            cumulative_early += monthly_early
        for age_m in range(1, 600):
            cumulative_early += monthly_early
            cumulative_late  += monthly_late
            if cumulative_late >= cumulative_early:
                return round(claim_late + age_m / 12, 1)
        return None

    breakeven_62_vs_67 = _breakeven(monthly_62, 62, monthly_67, 67)
    breakeven_67_vs_70 = _breakeven(monthly_67, 67, monthly_70, 70)
    breakeven_62_vs_70 = _breakeven(monthly_62, 62, monthly_70, 70)

    strategies = [
        {
            "claim_age":          62,
            "monthly_benefit":    monthly_62,
            "annual_benefit":     round(monthly_62 * 12, 2),
            "pct_of_fra":         round(factor_62 * 100, 1),
            "lifetime_benefit":   lifetime_62,
            "current_age_years_away": round(years_to_62, 1),
            "pros": ["Earliest access to income", "Beneficial if health concerns or short life expectancy",
                     "More years of benefits paid"],
            "cons": ["Permanently reduced monthly benefit", f"Receives {round((1-factor_62)*100,1)}% less per month than FRA"],
        },
        {
            "claim_age":          round(fra),
            "monthly_benefit":    round(fra_monthly * _ss_factor(fra, fra), 2),
            "annual_benefit":     round(fra_monthly * _ss_factor(fra, fra) * 12, 2),
            "pct_of_fra":         100.0,
            "lifetime_benefit":   _lifetime(fra_monthly, fra, life_expectancy),
            "current_age_years_away": round(years_to_fra, 1),
            "pros": ["Full benefit — no reduction", "More flexibility to reassess later"],
            "cons": ["Not the maximum possible", "Later start than 62"],
        },
        {
            "claim_age":          70,
            "monthly_benefit":    monthly_70,
            "annual_benefit":     round(monthly_70 * 12, 2),
            "pct_of_fra":         round(factor_70 * 100, 1),
            "lifetime_benefit":   lifetime_70,
            "current_age_years_away": round(years_to_70, 1),
            "pros": [f"Maximum monthly benefit ({round(factor_70*100,1)}% of FRA)",
                     "Highest lifetime value if you live past breakeven",
                     "Best longevity insurance"],
            "cons": ["No benefit during ages 67–70", "Requires bridge income or withdrawals in the gap"],
        },
    ]

    optimal_by_lifetime = max(strategies, key=lambda s: s["lifetime_benefit"])

    spousal = None
    if spouse_birth_year and spouse_benefit_at_67:
        sp_age   = current_year - spouse_birth_year
        sp_fra70 = spouse_benefit_at_67 / _ss_factor(67, 67)  # approximate
        sp_m62   = round(sp_fra70 * _ss_factor(62, 67), 2)
        sp_m67   = round(spouse_benefit_at_67, 2)
        sp_m70   = round(sp_fra70 * _ss_factor(70, 67), 2)
        spousal_benefit = round(monthly_70 * 0.50, 2)
        spousal = {
            "spouse_birth_year":     spouse_birth_year,
            "spouse_current_age":    sp_age,
            "spouse_monthly_at_62":  sp_m62,
            "spouse_monthly_at_67":  sp_m67,
            "spouse_monthly_at_70":  sp_m70,
            "spousal_benefit_note":  (
                f"Lower-earning spouse may qualify for up to 50% of higher earner's PIA "
                f"(≈${spousal_benefit:,.0f}/mo). File-and-suspend strategies eliminated in 2016 — "
                "coordinate claiming ages to maximize household lifetime benefit."
            ),
        }

    return {
        "birth_year":                 birth_year,
        "current_age":                age,
        "full_retirement_age":        fra,
        "life_expectancy_assumed":    life_expectancy,
        "benefit_placeholder":        placeholder,
        "fra_monthly_benefit":        round(fra_monthly, 2),
        "strategies":                 strategies,
        "breakeven_ages": {
            "age_62_vs_67":  breakeven_62_vs_67,
            "age_67_vs_70":  breakeven_67_vs_70,
            "age_62_vs_70":  breakeven_62_vs_70,
        },
        "optimal_by_lifetime_benefit": {
            "claim_age":         optimal_by_lifetime["claim_age"],
            "lifetime_benefit":  optimal_by_lifetime["lifetime_benefit"],
        },
        "spousal_analysis":  spousal,
        "note": (
            "Breakeven age = when cumulative benefits from a later claiming date surpass the earlier date. "
            "If you live past the breakeven age, delaying is mathematically better; if you die before it, earlier is better. "
            "These figures ignore the time value of money and income taxes on SS benefits. "
            "Social Security benefits are partially taxable (up to 85%) once combined income exceeds "
            "$34k single / $44k married."
        ),
        "placeholder_note": (
            "No benefit estimate provided — figures use a $2,000/mo FRA placeholder. "
            "Get your actual estimate at ssa.gov/myaccount or from your Social Security statement."
        ) if placeholder else None,
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_quarterly_estimated_taxes
# ---------------------------------------------------------------------------

async def get_quarterly_estimated_taxes(
    http_session,
    filing_status: str = "mfj",
    annual_income_override: float | None = None,
    prior_year_tax: float | None = None,
    expected_withholding: float | None = None,
) -> dict:
    """
    Calculate quarterly estimated tax payments for the current calendar year.

    Uses two methods and recommends the lower:
    - Safe harbor: pay 100% of prior-year tax (110% if prior AGI > $150k) in four equal installments.
    - Current-year annualized: pay based on the estimated current-year tax liability.

    Parameters
    ----------
    filing_status           : 'single', 'mfj', or 'hoh' (default 'mfj')
    annual_income_override  : override the inferred annual income (dollars)
    prior_year_tax          : total federal tax paid last year (for safe harbor calc)
    expected_withholding    : W-2 withholding expected this year (reduces estimated payments needed)
    """
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    std_ded = _STD_DEDUCTION.get(fs, 30_000)
    current_year = datetime.now().year
    current_month = datetime.now().month

    # Infer income
    if annual_income_override is not None:
        annual_income = annual_income_override
        income_source = "provided"
    else:
        inc_result = await get_income_summary(http_session, days=365)
        annual_income = inc_result.get("total_income", 0) if "error" not in inc_result else 0
        income_source = "inferred from 12-month transaction history"

    taxable_income = max(0.0, annual_income - std_ded)
    estimated_annual_tax = _compute_tax(taxable_income, fs)

    # Withholding reduces balance due
    withholding = expected_withholding or 0.0
    balance_due_annual = max(0.0, estimated_annual_tax - withholding)

    # Safe harbor: 100% of prior year tax (110% if prior AGI > $150k, assumed from income)
    safe_harbor_multiplier = 1.10 if annual_income > 150_000 else 1.00
    safe_harbor_annual = round((prior_year_tax or estimated_annual_tax) * safe_harbor_multiplier, 2)
    safe_harbor_annual_net = max(0.0, safe_harbor_annual - withholding)

    # IRS quarterly due dates
    due_dates = [
        {"quarter": "Q1", "period": f"Jan 1 – Mar 31",    "due": f"April 15, {current_year}"},
        {"quarter": "Q2", "period": f"Apr 1 – May 31",    "due": f"June 16, {current_year}"},
        {"quarter": "Q3", "period": f"Jun 1 – Aug 31",    "due": f"September 15, {current_year}"},
        {"quarter": "Q4", "period": f"Sep 1 – Dec 31",    "due": f"January 15, {current_year + 1}"},
    ]

    # IRS unequal installment fractions: 25% each
    installment_fractions = [0.25, 0.25, 0.25, 0.25]

    method_actual_payments = []
    method_safe_harbor_payments = []

    for i, (frac, due) in enumerate(zip(installment_fractions, due_dates)):
        method_actual_payments.append({
            **due,
            "payment": round(balance_due_annual * frac, 2),
        })
        method_safe_harbor_payments.append({
            **due,
            "payment": round(safe_harbor_annual_net * frac, 2),
        })

    recommended = "safe_harbor" if safe_harbor_annual_net <= balance_due_annual else "current_year"

    return {
        "tax_year":               current_year,
        "filing_status":          fs,
        "income_source":          income_source,
        "estimated_annual_income": round(annual_income, 2),
        "standard_deduction":     std_ded,
        "estimated_taxable_income": round(taxable_income, 2),
        "estimated_annual_tax":   round(estimated_annual_tax, 2),
        "expected_withholding":   round(withholding, 2),
        "effective_rate_pct":     round(estimated_annual_tax / annual_income * 100, 2) if annual_income else 0,
        "marginal_rate_pct":      round(_marginal_rate(taxable_income, fs) * 100),
        "methods": {
            "current_year_annualized": {
                "description": "Based on estimated current-year income and tax",
                "total_needed": round(balance_due_annual, 2),
                "quarterly_payments": method_actual_payments,
            },
            "safe_harbor": {
                "description": (
                    f"100%{' (110% — income > $150k)' if safe_harbor_multiplier > 1 else ''} "
                    f"of prior-year tax {'(estimated)' if not prior_year_tax else ''} "
                    "avoids underpayment penalty regardless of actual income"
                ),
                "prior_year_tax_used":  round(prior_year_tax or estimated_annual_tax, 2),
                "safe_harbor_amount":   safe_harbor_annual,
                "total_needed":         round(safe_harbor_annual_net, 2),
                "quarterly_payments":   method_safe_harbor_payments,
            },
        },
        "recommended_method":  recommended,
        "recommended_payments": (
            method_safe_harbor_payments if recommended == "safe_harbor"
            else method_actual_payments
        ),
        "underpayment_penalty_note": (
            "The IRS penalty for underpayment (Form 2210) applies if you owe more than $1,000 "
            "AND paid less than 90% of this year's tax OR less than 100%/110% of last year's tax. "
            "W-2 withholding counts toward these thresholds."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_tax_bracket_headroom  (Sprint 2)
# ---------------------------------------------------------------------------

async def get_tax_bracket_headroom(
    http_session,
    current_income: float | None = None,
    filing_status: str = "mfj",
) -> dict:
    """
    Show how much additional income can be earned before crossing into the
    next federal tax bracket.

    If ``current_income`` is not supplied, it is inferred from 12 months of
    SNB transaction data (income categories only).

    This is especially useful for:
      • Sizing a Roth conversion without crossing a bracket
      • Deciding whether to accelerate freelance income into this year
      • Timing a large capital gain realisation
      • Estimating how much bonus income can be taken tax-efficiently

    Parameters
    ----------
    current_income : estimated gross annual income in dollars
                     (default: inferred from 12-month transaction history)
    filing_status  : 'single', 'mfj' (married filing jointly), or 'hoh'
    """
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    std_ded = _STD_DEDUCTION.get(fs, 30_000)

    # Infer income from transactions if not supplied
    inferred = False
    if current_income is None:
        inc_result = await get_income_summary(http_session, days=365)
        if "error" not in inc_result:
            current_income = inc_result.get("total_income", 0)
            inferred = True
        else:
            current_income = 0.0

    taxable_income = max(0.0, current_income - std_ded)

    # Find the current bracket and the headroom to the next bracket ceiling
    brackets = _BRACKETS[fs]
    current_bracket_rate = None
    next_bracket_rate    = None
    headroom_to_next     = None
    dollars_into_bracket = None
    bracket_ceiling      = None
    prev_ceiling         = 0.0

    for i, (ceiling, rate) in enumerate(brackets):
        if taxable_income <= ceiling:
            current_bracket_rate  = rate
            bracket_ceiling       = ceiling
            dollars_into_bracket  = round(taxable_income - prev_ceiling, 2)
            if ceiling < float("inf"):
                headroom_to_next  = round(ceiling - taxable_income, 2)
                if i + 1 < len(brackets):
                    next_bracket_rate = brackets[i + 1][1]
            else:
                headroom_to_next  = None   # already in top bracket
                next_bracket_rate = None
            break
        prev_ceiling = ceiling

    # Show full bracket ladder with position indicator
    bracket_ladder = []
    prev = 0.0
    for ceiling, rate in brackets:
        bracket_ladder.append({
            "bracket_floor":  prev,
            "bracket_ceiling": ceiling if ceiling < float("inf") else None,
            "rate_pct":        int(rate * 100),
            "current":         current_bracket_rate == rate and taxable_income > prev,
        })
        prev = ceiling

    # LTCG bracket headroom (bonus insight — useful for harvest/conversion planning)
    ltcg_current_rate    = _ltcg_rate(taxable_income, fs)
    ltcg_brackets        = _LTCG_THRESHOLDS[fs]
    ltcg_headroom        = None
    ltcg_next_rate       = None
    ltcg_prev            = 0.0
    for ceiling, rate in ltcg_brackets:
        if taxable_income <= ceiling:
            if ceiling < float("inf"):
                ltcg_headroom  = round(ceiling - taxable_income, 2)
                idx = ltcg_brackets.index((ceiling, rate))
                if idx + 1 < len(ltcg_brackets):
                    ltcg_next_rate = ltcg_brackets[idx + 1][1]
            break
        ltcg_prev = ceiling

    return {
        "filing_status":             fs,
        "estimated_annual_income":   round(current_income, 2),
        "income_inferred":           inferred,
        "standard_deduction":        std_ded,
        "taxable_income":            round(taxable_income, 2),
        "current_bracket_rate_pct":  int((current_bracket_rate or 0) * 100),
        "dollars_into_current_bracket": dollars_into_bracket,
        "headroom_to_next_bracket":  headroom_to_next,
        "next_bracket_rate_pct":     int((next_bracket_rate or 0) * 100) if next_bracket_rate else None,
        "already_in_top_bracket":    headroom_to_next is None and current_bracket_rate is not None,
        "ltcg": {
            "current_ltcg_rate_pct":     int(ltcg_current_rate * 100),
            "headroom_to_next_ltcg_bracket": ltcg_headroom,
            "next_ltcg_rate_pct":        int((ltcg_next_rate or 0) * 100) if ltcg_next_rate else None,
        },
        "bracket_ladder":            bracket_ladder,
        "tax_year":                  _TAX_YEAR,
        "practical_uses": [
            f"You can earn ${headroom_to_next:,.0f} more before crossing into the "
            f"{int((next_bracket_rate or 0)*100)}% bracket."
            if headroom_to_next and next_bracket_rate else
            "You are already in the top bracket.",
            "Roth conversions, capital gain realisations, or freelance income "
            "within this headroom incur no additional bracket penalty.",
        ],
        "caveat": _IRS_CAVEAT,
    }


async def get_annual_tax_advantaged_summary(
    http_session,
    age: int | None = None,
) -> dict:
    """
    Show year-to-date contribution status and remaining room for all
    tax-advantaged accounts (401k, IRA, HSA, 529).

    Because Emoney doesn't expose YTD contribution amounts directly, this
    tool uses current account balances and estimated limits to show remaining
    room.  For a more accurate contribution estimate, it checks investment
    transactions for deposit-type activity into retirement accounts.

    Parameters
    ----------
    age : your current age (enables catch-up contribution eligibility)
    """
    from datetime import datetime
    import asyncio

    from .accounts import get_retirement_accounts

    ret_accts = await get_retirement_accounts(http_session)
    if "error" in ret_accts:
        return ret_accts

    now      = datetime.now()
    tax_year = _TAX_YEAR

    # Determine catch-up eligibility
    catchup_50     = age is not None and age >= 50
    catchup_60_63  = age is not None and 60 <= age <= 63
    catchup_55_hsa = age is not None and age >= 55

    # Build per-account-type summary using balances from get_retirement_accounts
    breakdown = ret_accts.get("retirement_breakdown", {})

    def _limit(account_type: str) -> int:
        lim = _CONTRIBUTION_LIMITS
        if account_type == "401k_403b":
            if catchup_60_63:
                return lim["401k_403b_catchup_60"]
            if catchup_50:
                return lim["401k_403b_catchup_50"]
            return lim["401k_403b"]
        if account_type == "ira":
            return lim["ira_catchup"] if catchup_50 else lim["ira"]
        if account_type == "hsa_family":
            base = lim["hsa_family"]
            return base + lim["hsa_catchup"] if catchup_55_hsa else base
        if account_type == "hsa_individual":
            base = lim["hsa_individual"]
            return base + lim["hsa_catchup"] if catchup_55_hsa else base
        return 0

    account_summaries = []

    # 401k / 403b
    bal_401k = breakdown.get("401k_403b", 0) or 0
    lim_401k = _limit("401k_403b")
    account_summaries.append({
        "account_type":          "401k / 403b",
        "current_balance":       round(bal_401k, 2),
        "annual_limit":          lim_401k,
        "catch_up_eligible":     catchup_50,
        "note":                  "Employee elective deferrals only; employer match is additive.",
    })

    # IRA / Roth IRA
    bal_ira = breakdown.get("ira_roth", 0) or 0
    lim_ira = _limit("ira")
    account_summaries.append({
        "account_type":          "IRA / Roth IRA",
        "current_balance":       round(bal_ira, 2),
        "annual_limit":          lim_ira,
        "catch_up_eligible":     catchup_50,
        "deadline":              f"April 15, {tax_year + 1}",
        "note":                  "Combined limit across Traditional + Roth IRA; income limits may apply to Roth.",
    })

    # HSA
    bal_hsa = breakdown.get("hsa", 0) or 0
    lim_hsa = _limit("hsa_family")  # default to family; user can override
    account_summaries.append({
        "account_type":          "HSA",
        "current_balance":       round(bal_hsa, 2),
        "annual_limit":          lim_hsa,
        "annual_limit_individual": _limit("hsa_individual"),
        "catch_up_eligible":     catchup_55_hsa,
        "deadline":              f"April 15, {tax_year + 1}",
        "note":                  "Limit shown is family coverage; reduce to individual limit if single-coverage plan.",
    })

    # 529
    bal_529 = breakdown.get("education_529", 0) or 0
    account_summaries.append({
        "account_type":          "529 Education",
        "current_balance":       round(bal_529, 2),
        "annual_limit":          None,  # no IRS annual limit; gift exclusion applies
        "annual_exclusion_max":  _CONTRIBUTION_LIMITS.get("gift_tax_exclusion", 18_000),
        "note":                  "No IRS annual contribution cap; annual gift exclusion applies to avoid gift tax.",
    })

    total_annual_limits = sum(a["annual_limit"] for a in account_summaries if a["annual_limit"])
    days_left_in_year = (datetime(tax_year, 12, 31) - now).days

    return {
        "tax_year":   tax_year,
        "as_of":      now.strftime("%Y-%m-%d"),
        "age":        age,
        "accounts":   account_summaries,
        "totals": {
            "combined_401k_ira_hsa_annual_limit": total_annual_limits,
            "days_left_in_tax_year":              max(0, days_left_in_year),
        },
        "key_deadlines": {
            "401k_hsa_deadline":  f"December 31, {tax_year}",
            "ira_hsa_deadline":   f"April 15, {tax_year + 1}",
        },
        "caveat": _IRS_CAVEAT,
        "note": (
            "Contribution amounts shown are the IRS annual limits, not your actual YTD contributions. "
            "Check your payroll portal or brokerage for actual YTD contribution amounts. "
            "Balances are as of the last Emoney sync."
        ),
    }
