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
    pretax_balance = (breakdown.get("401k_403b", 0) or 0) + (breakdown.get("ira_roth", 0) or 0)

    trad_balance = pretax_balance  # conservative: assume all is pre-tax for RMD

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
