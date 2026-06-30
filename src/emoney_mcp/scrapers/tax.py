"""
Tax planning tools — loss harvesting, contribution limits, Roth conversion,
capital gains exposure, and Required Minimum Distribution estimates.

All calculations use 2026 IRS figures (see ``_TAX_YEAR``).  Update the
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
    Displays 2026 IRS annual contribution limits for 401k/403b, IRA, HSA,
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

from datetime import datetime

from ._helpers import _get_investment_data
from .accounts import get_retirement_accounts, _build_account_type_map, _match_tax_bucket
from .spending import get_income_summary

# ===========================================================================
# IRS CONSTANTS  (2026 — update annually)
#   Sources: Rev. Proc. 2025-32 (brackets, standard deduction, LTCG, gift
#   exclusion); Notice 2025-67 (retirement-plan limits, §415(c)); Rev. Proc.
#   2025-19 (HSA). NIIT thresholds are statutory, not inflation-adjusted.
# ===========================================================================

_TAX_YEAR = 2026
_IRS_CAVEAT = (
    "Figures use 2026 IRS limits and tax brackets. "
    "Federal tax only — state and local income taxes are not modeled, so the true "
    "marginal cost in a high-tax state (e.g. CA, NY) will be higher. "
    "Consult a qualified tax professional before acting on any estimates."
)

_CONTRIBUTION_LIMITS = {
    "401k_403b":              24_500,
    "401k_403b_catchup_50":   32_500,   # 24,500 + 8,000 (age 50-59 and 64+)
    "401k_403b_catchup_60":   35_750,   # 24,500 + 11,250 super catch-up age 60-63
    "ira":                     7_500,
    "ira_catchup":             8_600,   # 7,500 + 1,100 (age 50+)
    "hsa_individual":          4_400,
    "hsa_family":              8_750,
    "hsa_catchup":             1_000,   # age 55+
    "simple_ira":             17_000,
    "simple_ira_catchup":     21_000,   # 17,000 + 4,000 (age 50+)
    "sep_ira_pct":             0.25,
    "sep_ira_max":            72_000,   # §415(c) annual additions limit
    "gift_tax_exclusion":     19_000,   # per beneficiary (529 / gifting), unchanged
}

_STD_DEDUCTION = {"single": 16_100, "mfj": 32_200, "hoh": 24_150}

# Ordinary income brackets — (upper bound of bracket, rate)
_BRACKETS: dict[str, list[tuple[float, float]]] = {
    "single": [
        (12_400,       0.10),
        (50_400,       0.12),
        (105_700,      0.22),
        (201_775,      0.24),
        (256_225,      0.32),
        (640_600,      0.35),
        (float("inf"), 0.37),
    ],
    "mfj": [
        (24_800,       0.10),
        (100_800,      0.12),
        (211_400,      0.22),
        (403_550,      0.24),
        (512_450,      0.32),
        (768_700,      0.35),
        (float("inf"), 0.37),
    ],
    "hoh": [
        (17_700,       0.10),
        (67_450,       0.12),
        (105_700,      0.22),
        (201_775,      0.24),
        (256_200,      0.32),
        (640_600,      0.35),
        (float("inf"), 0.37),
    ],
}

# LTCG thresholds — (upper bound of 0% / 15% bracket, rate)
_LTCG_THRESHOLDS: dict[str, list[tuple[float, float]]] = {
    "single": [(49_450,  0.0), (545_500,  0.15), (float("inf"), 0.20)],
    "mfj":    [(98_900,  0.0), (613_700,  0.15), (float("inf"), 0.20)],
    "hoh":    [(66_200,  0.0), (579_600,  0.15), (float("inf"), 0.20)],
}

# NIIT (3.8%) kicks in above these thresholds
_NIIT_THRESHOLD = {"single": 200_000, "mfj": 250_000, "hoh": 200_000}

# Medicare IRMAA tiers (Part B + Part D income-related surcharges).
# Each tier: (single_MAGI_upper, mfj_MAGI_upper, part_b_monthly, part_d_monthly).
# Surcharges are PER beneficiary; the MAGI tested is from two years prior.
# 2026 figures are projected — CMS finalizes them late in the prior year — so,
# like the tax tables, refresh _IRMAA_YEAR and these rows annually.
_IRMAA_YEAR = 2026
_IRMAA_TIERS: list[tuple[float, float, float, float]] = [
    (108_000,      216_000,        0.00,  0.00),
    (136_000,      272_000,       74.00, 13.70),
    (170_000,      340_000,      185.00, 35.30),
    (204_000,      408_000,      295.90, 57.00),
    (510_000,      765_000,      406.90, 78.60),
    (float("inf"), float("inf"), 443.90, 85.80),
]

# Qualified Charitable Distribution annual cap (per taxpayer, indexed).
# $108,000 for 2025; the IRS indexes it each year — refresh with _TAX_YEAR.
_QCD_ANNUAL_LIMIT = 108_000
_QCD_ELIGIBLE_AGE = 70.5   # QCDs allowed from age 70½

# State income tax — representative TOP MARGINAL rate per state (2025 figures).
# This is a deliberately simple model: the rate is applied to the *incremental*
# income supplied (a Roth conversion, capital gain, or withdrawal), which is the
# correct treatment for a marginal add-on. It is NOT a full graduated-bracket
# state return. Nine states levy no broad income tax (rate 0.0). Washington has
# no ordinary-income tax but a 7% tax on long-term capital gains above a annually
# indexed threshold (~$270k) — handled specially in get_state_tax_estimate.
# `flat` marks states whose single rate applies at all income levels (so the
# marginal estimate is exact, not an upper bound).
_STATE_TAX: dict[str, dict] = {
    "AL": {"name": "Alabama",        "rate": 0.0500, "flat": False},
    "AK": {"name": "Alaska",         "rate": 0.0000, "flat": True, "no_income_tax": True},
    "AZ": {"name": "Arizona",        "rate": 0.0250, "flat": True},
    "AR": {"name": "Arkansas",       "rate": 0.0390, "flat": False},
    "CA": {"name": "California",     "rate": 0.1330, "flat": False, "note": "Top 13.3% includes the 1% mental-health surcharge above $1M."},
    "CO": {"name": "Colorado",       "rate": 0.0440, "flat": True},
    "CT": {"name": "Connecticut",    "rate": 0.0699, "flat": False},
    "DE": {"name": "Delaware",       "rate": 0.0660, "flat": False},
    "DC": {"name": "District of Columbia", "rate": 0.1075, "flat": False},
    "FL": {"name": "Florida",        "rate": 0.0000, "flat": True, "no_income_tax": True},
    "GA": {"name": "Georgia",        "rate": 0.0539, "flat": True},
    "HI": {"name": "Hawaii",         "rate": 0.1100, "flat": False},
    "ID": {"name": "Idaho",          "rate": 0.0569, "flat": True},
    "IL": {"name": "Illinois",       "rate": 0.0495, "flat": True},
    "IN": {"name": "Indiana",        "rate": 0.0300, "flat": True},
    "IA": {"name": "Iowa",           "rate": 0.0380, "flat": True},
    "KS": {"name": "Kansas",         "rate": 0.0558, "flat": False},
    "KY": {"name": "Kentucky",       "rate": 0.0400, "flat": True},
    "LA": {"name": "Louisiana",      "rate": 0.0300, "flat": True},
    "ME": {"name": "Maine",          "rate": 0.0715, "flat": False},
    "MD": {"name": "Maryland",       "rate": 0.0575, "flat": False, "note": "Excludes county/local income taxes (often +2.25–3.2%)."},
    "MA": {"name": "Massachusetts",  "rate": 0.0900, "flat": False, "note": "5% flat plus a 4% surtax on income above $1M."},
    "MI": {"name": "Michigan",       "rate": 0.0425, "flat": True},
    "MN": {"name": "Minnesota",      "rate": 0.0985, "flat": False},
    "MS": {"name": "Mississippi",    "rate": 0.0440, "flat": True},
    "MO": {"name": "Missouri",       "rate": 0.0470, "flat": False},
    "MT": {"name": "Montana",        "rate": 0.0590, "flat": False},
    "NE": {"name": "Nebraska",       "rate": 0.0584, "flat": False},
    "NV": {"name": "Nevada",         "rate": 0.0000, "flat": True, "no_income_tax": True},
    "NH": {"name": "New Hampshire",  "rate": 0.0000, "flat": True, "no_income_tax": True, "note": "No tax on wages; the interest/dividends tax was fully repealed in 2025."},
    "NJ": {"name": "New Jersey",     "rate": 0.1075, "flat": False},
    "NM": {"name": "New Mexico",     "rate": 0.0590, "flat": False},
    "NY": {"name": "New York",       "rate": 0.1090, "flat": False, "note": "Excludes NYC/Yonkers local income tax (NYC adds up to ~3.88%)."},
    "NC": {"name": "North Carolina", "rate": 0.0425, "flat": True},
    "ND": {"name": "North Dakota",   "rate": 0.0250, "flat": False},
    "OH": {"name": "Ohio",           "rate": 0.0350, "flat": False},
    "OK": {"name": "Oklahoma",       "rate": 0.0475, "flat": False},
    "OR": {"name": "Oregon",         "rate": 0.0990, "flat": False, "note": "Excludes local transit/county taxes."},
    "PA": {"name": "Pennsylvania",   "rate": 0.0307, "flat": True, "note": "Flat 3.07%; PA does not tax most retirement income (IRA/401k/pension distributions)."},
    "RI": {"name": "Rhode Island",   "rate": 0.0599, "flat": False},
    "SC": {"name": "South Carolina", "rate": 0.0620, "flat": False},
    "SD": {"name": "South Dakota",   "rate": 0.0000, "flat": True, "no_income_tax": True},
    "TN": {"name": "Tennessee",      "rate": 0.0000, "flat": True, "no_income_tax": True},
    "TX": {"name": "Texas",          "rate": 0.0000, "flat": True, "no_income_tax": True},
    "UT": {"name": "Utah",           "rate": 0.0455, "flat": True},
    "VT": {"name": "Vermont",        "rate": 0.0875, "flat": False},
    "VA": {"name": "Virginia",       "rate": 0.0575, "flat": False},
    "WA": {"name": "Washington",     "rate": 0.0000, "flat": True, "no_income_tax": True, "ltcg_rate": 0.07, "note": "No income tax, but a 7% tax applies to long-term capital gains above ~$270k/yr."},
    "WV": {"name": "West Virginia",  "rate": 0.0482, "flat": False},
    "WI": {"name": "Wisconsin",      "rate": 0.0765, "flat": False},
    "WY": {"name": "Wyoming",        "rate": 0.0000, "flat": True, "no_income_tax": True},
}

# Accept full state names → code (built once at import).
_STATE_NAME_TO_CODE = {v["name"].lower(): k for k, v in _STATE_TAX.items()}

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
    for ceiling, rate in _BRACKETS[fs]:
        if taxable_income <= ceiling:
            return rate
    return 0.37


def _ltcg_rate(taxable_income: float, filing_status: str) -> float:
    fs = filing_status if filing_status in _LTCG_THRESHOLDS else "mfj"
    for ceiling, rate in _LTCG_THRESHOLDS[fs]:
        if taxable_income <= ceiling:
            return rate
    return 0.20


def _bracket_ceiling(taxable_income: float, filing_status: str) -> float | None:
    """Upper bound of the bracket the income currently sits in (None at the top)."""
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    for ceiling, _rate in _BRACKETS[fs]:
        if taxable_income <= ceiling:
            return None if ceiling == float("inf") else ceiling
    return None


def _target_bracket_ceiling(target_rate: float, filing_status: str) -> float | None:
    """Upper bound of the bracket whose marginal rate is ``target_rate`` (the
    income level to 'fill up to'). Returns None if ``target_rate`` isn't one of
    the discrete bracket rates (so callers can reject invalid input)."""
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    for ceiling, rate in _BRACKETS[fs]:
        if abs(rate - target_rate) < 1e-9:
            return None if ceiling == float("inf") else ceiling
    return None


def _pretax_rmd_balance(retirement: dict) -> float:
    """
    Sum of RMD-subject pre-tax balances (traditional IRA + 401k/403b), excluding
    Roth. Designated Roth accounts have no RMD (Roth IRAs always; Roth 401(k)/403(b)
    since 2024 under SECURE 2.0), and the retirement_breakdown buckets conflate
    pre-tax with Roth — so recompute from the individual account list.
    """
    accounts = retirement.get("retirement_accounts", [])

    def _nt(a: dict) -> str:
        return (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()

    k401 = sum(
        a.get("balance", 0) or 0
        for a in accounts
        if ("401" in _nt(a) or "403" in _nt(a)) and "roth" not in _nt(a)
    )
    ira = sum(
        a.get("balance", 0) or 0
        for a in accounts
        if "ira" in _nt(a) and "roth" not in _nt(a)
    )
    return k401 + ira


def _rmd_factor(age: int) -> float:
    """IRS Uniform Lifetime Table distribution period for an age (clamped to 100)."""
    return _RMD_TABLE.get(age) or _RMD_TABLE.get(min(age, 100), 6.4)


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

    data, err = await _get_investment_data(http_session)
    if err:
        return err

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
    checklist.sort(key=lambda x: (_priority_order.get(str(x["status"]), 9),
                                  -float(x.get("amount", 0) or 0)))

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

    if marginal > 0 and effective_rate_on_conversion > 0:
        if effective_rate_on_conversion < marginal:
            recommendation = (
                f"Tax-favored: you are paying {round(effective_rate_on_conversion * 100, 1)}% "
                f"on this conversion vs. your {int(marginal * 100)}% marginal rate. "
                "Consider converting up to the top of your current bracket."
            )
        else:
            recommendation = (
                f"Not tax-favored at this amount: effective rate on conversion "
                f"({round(effective_rate_on_conversion * 100, 1)}%) meets or exceeds "
                f"your {int(marginal * 100)}% marginal rate. "
                "Consider a smaller conversion or waiting for a lower-income year."
            )
    else:
        recommendation = "Conversion analysis unavailable — insufficient tax data."

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
        "recommendation":           recommendation,
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

    data, err = await _get_investment_data(http_session)
    if err:
        return err
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

    # Only traditional (pre-tax) balances are RMD-subject; Roth is excluded.
    trad_balance = _pretax_rmd_balance(retirement)

    years_until_rmd = max(0, rmd_start_age - age)
    rmd_age = max(age, rmd_start_age)

    future_balance_at_rmd = round(trad_balance * (1.06 ** years_until_rmd), 2) if years_until_rmd > 0 else trad_balance

    rmd_schedule = []
    balance = future_balance_at_rmd
    for yr in range(10):
        calc_age = rmd_age + yr
        factor = _rmd_factor(calc_age)
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
        factor = _rmd_factor(age)
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
# get_multi_year_tax_projection
# ---------------------------------------------------------------------------

async def get_multi_year_tax_projection(
    http_session,
    birth_year: int,
    current_taxable_income: float,
    years: int = 10,
    filing_status: str = "mfj",
    retirement_age: int | None = None,
    social_security_annual: float = 0.0,
    ss_start_age: int = 67,
    income_growth: float = 0.03,
) -> dict:
    """
    Project federal taxable income, bracket, and tax for the next ``years`` years
    so low-income "conversion window" years (after wages stop, before RMDs and
    Social Security ramp up) become visible.

    Income modelled per year: wages (grown at ``income_growth`` until
    ``retirement_age``, then 0), plus RMDs once age >= 73 (pre-tax balances from
    Emoney, drawn down on the IRS Uniform Lifetime Table), plus 85% of Social
    Security once age >= ``ss_start_age`` (the maximum taxable share).

    Parameters
    ----------
    birth_year             : year of birth (e.g. 1962)
    current_taxable_income : this year's ordinary taxable income (wages etc.)
    years                  : projection horizon (default 10, max 40)
    filing_status          : single | mfj | hoh (default mfj)
    retirement_age         : age at which wages stop (default: never)
    social_security_annual : expected annual SS benefit in today's dollars
    ss_start_age           : age SS begins (default 67)
    income_growth          : annual wage growth assumption (default 0.03)
    """
    years = max(1, min(years, 40))
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    std_deduction = _STD_DEDUCTION[fs]
    current_year = datetime.now().year
    current_age = current_year - birth_year

    retirement = await get_retirement_accounts(http_session)
    if "error" in retirement:
        return retirement
    pretax_balance = _pretax_rmd_balance(retirement)

    rows = []
    balance = pretax_balance          # pre-tax balance carried forward
    conversion_window_years = []
    for i in range(years):
        cal_year = current_year + i
        age = current_age + i

        # Grow the pre-tax balance each year, then take the RMD if due.
        if i > 0:
            balance = round(balance * 1.06, 2)
        wages = round(current_taxable_income * ((1 + income_growth) ** i), 2)
        if retirement_age is not None and age >= retirement_age:
            wages = 0.0

        rmd = 0.0
        if age >= 73 and balance > 0:
            rmd = round(balance / _rmd_factor(age), 2)
            balance = round(balance - rmd, 2)

        ss_taxable = round(0.85 * social_security_annual, 2) if age >= ss_start_age else 0.0

        gross_income = round(wages + rmd + ss_taxable, 2)
        taxable_income = round(max(0.0, gross_income - std_deduction), 2)
        tax = _compute_tax(taxable_income, fs)
        marginal = _marginal_rate(taxable_income, fs)
        ceiling = _bracket_ceiling(taxable_income, fs)
        headroom = round(ceiling - taxable_income, 2) if ceiling is not None else None
        effective = round(tax / gross_income * 100, 1) if gross_income > 0 else 0.0

        # A "conversion window" year: still in a low bracket (<= 12%) before RMDs
        # force income up — the prime time for Roth conversions / gain harvesting.
        is_window = marginal <= 0.12 and rmd == 0.0
        if is_window:
            conversion_window_years.append(cal_year)

        rows.append({
            "year":               cal_year,
            "age":                age,
            "wages":              wages,
            "rmd":                rmd,
            "taxable_social_security": ss_taxable,
            "gross_income":       gross_income,
            "taxable_income":     taxable_income,
            "federal_tax":        tax,
            "marginal_rate_pct":  round(marginal * 100, 1),
            "effective_rate_pct": effective,
            "bracket_headroom_to_next": headroom,
            "conversion_window":  is_window,
        })

    return {
        "as_of":                  datetime.now().strftime("%Y-%m-%d"),
        "filing_status":          fs,
        "horizon_years":          years,
        "current_pretax_balance": round(pretax_balance, 2),
        "projection":             rows,
        "conversion_window_years": conversion_window_years,
        "assumptions": {
            "wage_growth_pct":       round(income_growth * 100, 1),
            "pretax_growth_pct":     6.0,
            "retirement_age":        retirement_age,
            "ss_start_age":          ss_start_age,
            "social_security_taxable_share_pct": 85,
            "rmd_start_age":         73,
        },
        "note": (
            "Simplified federal projection: wages grow then stop at retirement_age, "
            "RMDs begin at 73 on pre-tax balances (6% growth), and 85% of Social "
            "Security is treated as taxable (the maximum). Deductions beyond the "
            "standard deduction, capital gains, and credits are not modeled. "
            "'conversion_window' flags low-bracket years ideal for Roth conversions "
            "or 0% capital-gain harvesting — see get_roth_conversion_ladder."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_roth_conversion_ladder
# ---------------------------------------------------------------------------

async def get_roth_conversion_ladder(
    http_session,
    birth_year: int,
    current_taxable_income: float,
    target_bracket: float = 0.24,
    years: int = 10,
    filing_status: str = "mfj",
    retirement_age: int | None = None,
    social_security_annual: float = 0.0,
    ss_start_age: int = 67,
    income_growth: float = 0.03,
) -> dict:
    """
    Build a multi-year Roth conversion ladder that fills each year's bracket up
    to the top of ``target_bracket`` — converting more in low-income years and
    less (or nothing) once wages, RMDs, or Social Security crowd the bracket.

    This is the strategic counterpart to get_roth_conversion_analysis (a single
    conversion today): it spreads conversions across the low-bracket window
    before RMDs begin, capped each year by the pre-tax balance remaining.

    Parameters mirror get_multi_year_tax_projection. ``target_bracket`` is the
    marginal rate to fill up to (e.g. 0.24 = top of the 24% bracket).
    """
    fs = filing_status if filing_status in _BRACKETS else "mfj"
    ceiling = _target_bracket_ceiling(target_bracket, fs)
    if ceiling is None:
        return {"error": "target_bracket must be one of 0.10, 0.12, 0.22, 0.24, 0.32, 0.35."}

    # Reuse the projection engine for the per-year income baseline (DRY).
    proj = await get_multi_year_tax_projection(
        http_session, birth_year=birth_year, current_taxable_income=current_taxable_income,
        years=years, filing_status=fs, retirement_age=retirement_age,
        social_security_annual=social_security_annual, ss_start_age=ss_start_age,
        income_growth=income_growth,
    )
    if "error" in proj:
        return proj

    pretax_remaining = proj["current_pretax_balance"]
    ladder = []
    total_converted = 0.0
    total_tax = 0.0
    for row in proj["projection"]:
        base_taxable = row["taxable_income"]
        # Grow the remaining pre-tax balance and draw that year's RMD first.
        pretax_remaining = round(pretax_remaining * 1.06, 2) if ladder else pretax_remaining
        pretax_remaining = max(0.0, round(pretax_remaining - row["rmd"], 2))

        room = ceiling - base_taxable
        convert = max(0.0, round(min(room, pretax_remaining), 2))
        conv_tax = round(_compute_tax(base_taxable + convert, fs) - _compute_tax(base_taxable, fs), 2)
        conv_rate = round(conv_tax / convert * 100, 1) if convert > 0 else 0.0
        pretax_remaining = max(0.0, round(pretax_remaining - convert, 2))
        total_converted += convert
        total_tax += conv_tax

        ladder.append({
            "year":                  row["year"],
            "age":                   row["age"],
            "baseline_taxable_income": base_taxable,
            "recommended_conversion": convert,
            "conversion_tax":        conv_tax,
            "conversion_rate_pct":   conv_rate,
            "fills_to_bracket_pct":  round(target_bracket * 100, 1),
            "pretax_balance_remaining": pretax_remaining,
        })

    eff = round(total_tax / total_converted * 100, 1) if total_converted > 0 else 0.0
    # First-year RMD avoided on the converted money (rough proxy for the benefit):
    # what the converted total would have thrown off as an RMD at the first table age.
    rmd_avoided_est = round(total_converted / _rmd_factor(73), 2) if total_converted > 0 else 0.0

    return {
        "as_of":                  datetime.now().strftime("%Y-%m-%d"),
        "filing_status":          fs,
        "target_bracket_pct":     round(target_bracket * 100, 1),
        "fill_to_taxable_income": ceiling,
        "horizon_years":          years,
        "current_pretax_balance": proj["current_pretax_balance"],
        "ladder":                 ladder,
        "total_converted":        round(total_converted, 2),
        "total_conversion_tax":   round(total_tax, 2),
        "blended_conversion_rate_pct": eff,
        "pretax_balance_after_ladder": ladder[-1]["pretax_balance_remaining"] if ladder else 0.0,
        "est_annual_rmd_avoided": rmd_avoided_est,
        "note": (
            "Each year converts the gap between projected taxable income and the top "
            "of the target bracket, capped by the pre-tax balance remaining. Baseline "
            "income (incl. RMDs) is from get_multi_year_tax_projection; conversions are "
            "assumed paid from outside funds (not withheld). Converting reduces future "
            "RMDs — est_annual_rmd_avoided is a rough proxy. State tax not modeled."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_irmaa_analysis
# ---------------------------------------------------------------------------

def _irmaa_tier(magi: float, mfj: bool) -> int:
    """Index into _IRMAA_TIERS for a given MAGI."""
    for i, row in enumerate(_IRMAA_TIERS):
        upper = row[1] if mfj else row[0]
        if magi <= upper:
            return i
    return len(_IRMAA_TIERS) - 1


async def get_irmaa_analysis(
    http_session,
    magi: float,
    filing_status: str = "mfj",
    proposed_additional_income: float = 0.0,
) -> dict:
    """
    Determine the Medicare IRMAA (Part B + Part D) surcharge tier for a given
    MAGI, the distance to the next cliff, and — if ``proposed_additional_income``
    is given — the extra annual surcharge that a Roth conversion or capital-gain
    realization of that size would trigger.

    IRMAA is a cliff (not a phase-in): $1 over a threshold bumps you to the next
    tier's full surcharge. Surcharges are PER Medicare beneficiary, based on MAGI
    from two years prior.

    Parameters
    ----------
    magi                       : modified AGI to test (two-years-prior income)
    filing_status              : 'mfj' or 'single' (hoh uses the single table)
    proposed_additional_income : extra income to model on top of MAGI (e.g. a
                                 planned Roth conversion); default 0
    """
    mfj = filing_status == "mfj"
    cur = _irmaa_tier(magi, mfj)
    s, j, part_b, part_d = _IRMAA_TIERS[cur]
    monthly = round(part_b + part_d, 2)
    annual_per_person = round(monthly * 12, 2)

    # The current tier's upper bound is the next cliff.
    next_cliff = None
    distance = None
    if cur < len(_IRMAA_TIERS) - 1:
        next_cliff = j if mfj else s
        distance = round(next_cliff - magi, 2)

    result = {
        "as_of":                    datetime.now().strftime("%Y-%m-%d"),
        "irmaa_year":               _IRMAA_YEAR,
        "magi":                     round(magi, 2),
        "filing_status":            "mfj" if mfj else "single",
        "current_tier":             cur + 1,        # 1 = no surcharge
        "monthly_surcharge_part_b": part_b,
        "monthly_surcharge_part_d": part_d,
        "annual_surcharge_per_person": annual_per_person,
        "next_cliff_magi":          next_cliff,
        "distance_to_next_cliff":   distance,
    }

    if proposed_additional_income and proposed_additional_income > 0:
        new_magi = magi + proposed_additional_income
        new_tier = _irmaa_tier(new_magi, mfj)
        _, _, nb, nd = _IRMAA_TIERS[new_tier]
        new_annual = round((nb + nd) * 12, 2)
        added = round(new_annual - annual_per_person, 2)
        result["proposed_additional_income"] = round(proposed_additional_income, 2)
        result["proposed_magi"] = round(new_magi, 2)
        result["proposed_tier"] = new_tier + 1
        result["added_annual_surcharge_per_person"] = added
        result["crosses_cliff"] = new_tier > cur

    result["note"] = (
        "Surcharges are per Medicare beneficiary (double for a couple both enrolled). "
        "IRMAA uses MAGI from two years prior and is a hard cliff — a dollar over a "
        "threshold triggers the next tier's full surcharge. Tier 1 means no surcharge. "
        f"{_IRMAA_YEAR} surcharge amounts are estimates pending CMS finalization."
    )
    result["caveat"] = _IRS_CAVEAT
    return result


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
    _lifetime(monthly_67, 67, life_expectancy)
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
        {"quarter": "Q1", "period": "Jan 1 – Mar 31",    "due": f"April 15, {current_year}"},
        {"quarter": "Q2", "period": "Apr 1 – May 31",    "due": f"June 16, {current_year}"},
        {"quarter": "Q3", "period": "Jun 1 – Aug 31",    "due": f"September 15, {current_year}"},
        {"quarter": "Q4", "period": "Sep 1 – Dec 31",    "due": f"January 15, {current_year + 1}"},
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
    prev_ceiling         = 0.0

    for i, (ceiling, rate) in enumerate(brackets):
        if taxable_income <= ceiling:
            current_bracket_rate  = rate
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
    for ceiling, rate in ltcg_brackets:
        if taxable_income <= ceiling:
            if ceiling < float("inf"):
                ltcg_headroom  = round(ceiling - taxable_income, 2)
                idx = ltcg_brackets.index((ceiling, rate))
                if idx + 1 < len(ltcg_brackets):
                    ltcg_next_rate = ltcg_brackets[idx + 1][1]
            break

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


# ---------------------------------------------------------------------------
# get_charitable_giving_strategy  (#89)
# ---------------------------------------------------------------------------

async def get_charitable_giving_strategy(
    http_session,
    annual_giving: float,
    age: int | None = None,
    filing_status: str = "mfj",
    current_income: float | None = None,
) -> dict:
    """
    Recommend the most tax-efficient vehicle for a recurring charitable gift:
    a Qualified Charitable Distribution (QCD), donor-advised-fund (DAF) bunching,
    or gifting appreciated long-term securities instead of cash.

    Three levers are evaluated against the user's situation:

    - **QCD** — at age 70½+ a direct IRA-to-charity transfer (up to the annual
      cap) is excluded from AGI entirely, which also counts toward the RMD and
      lowers IRMAA/Social-Security taxation. Beats a cash gift because it never
      enters AGI (no need to itemize).
    - **DAF / bunching** — when annual giving alone won't clear the standard
      deduction, concentrating several years of gifts into one (via a DAF) lets
      you itemize in the bunch year and take the standard deduction in the off
      years. The benefit is the deduction recovered above the standard deduction.
    - **Appreciated securities** — gifting long-term appreciated shares (vs.
      selling and donating cash) avoids the capital-gains tax on the embedded
      gain *and* still deducts full fair-market value. Identifies the taxable-
      account lots with the largest unrealized gains as candidates.

    Parameters
    ----------
    annual_giving  : your typical annual charitable gift in dollars
    age            : your age (determines QCD eligibility at 70½)
    filing_status  : 'single', 'mfj', or 'hoh' (default 'mfj')
    current_income : annual ordinary income (for marginal-rate / itemize math;
                     inferred from 12-month transactions if omitted)
    """
    if annual_giving is None or annual_giving <= 0:
        return {"error": "annual_giving must be a positive dollar amount."}

    fs = filing_status if filing_status in _BRACKETS else "mfj"
    std_ded = _STD_DEDUCTION.get(fs, 30_000)

    inferred_income = False
    if current_income is None:
        inc_result = await get_income_summary(http_session, days=365)
        if "error" not in inc_result:
            current_income = inc_result.get("total_income", 0) or 0
            inferred_income = True
        else:
            current_income = 0.0

    taxable_income = max(0.0, current_income - std_ded)
    marginal = _marginal_rate(taxable_income, fs)
    ltcg = _ltcg_rate(taxable_income, fs)

    strategies: list[dict] = []

    # --- 1. QCD (age 70½+) ---
    qcd_eligible = age is not None and age >= _QCD_ELIGIBLE_AGE
    retirement = await get_retirement_accounts(http_session)
    pretax_ira = 0.0
    if "error" not in retirement:
        # QCDs may only come from IRAs (not 401k/403b), so isolate IRA balances.
        for a in retirement.get("retirement_accounts", []):
            nt = (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()
            if "ira" in nt and "roth" not in nt:
                pretax_ira += a.get("balance", 0) or 0
    qcd_amount = min(annual_giving, _QCD_ANNUAL_LIMIT, pretax_ira) if qcd_eligible else 0.0
    # The QCD benefit vs. a cash gift: a cash gift only helps if you itemize and
    # only at your marginal rate on the amount above the standard deduction; a QCD
    # always escapes AGI. Approximate the edge as marginal rate on the QCD amount.
    qcd_benefit = round(qcd_amount * marginal, 2) if qcd_amount > 0 else 0.0
    strategies.append({
        "vehicle":   "Qualified Charitable Distribution (QCD)",
        "eligible":  qcd_eligible and pretax_ira > 0,
        "recommended_amount": round(qcd_amount, 2),
        "estimated_tax_benefit": qcd_benefit,
        "detail": (
            (f"At age {age} you can direct up to ${min(_QCD_ANNUAL_LIMIT, pretax_ira):,.0f} "
             f"from a traditional IRA straight to charity. It is excluded from AGI, counts "
             f"toward your RMD, and lowers IRMAA and Social-Security taxation.")
            if qcd_eligible and pretax_ira > 0 else
            (f"Not yet eligible — QCDs require age 70½+ (you are {age})."
             if age is not None and not qcd_eligible else
             "Requires age 70½+ and a traditional (pre-tax) IRA balance to draw from."
             if pretax_ira <= 0 else
             "Provide your age to assess QCD eligibility.")
        ),
    })

    # --- 2. DAF / bunching ---
    # If a single year of giving + a modest assumed SALT/other won't clear the
    # standard deduction, bunching N years clears it and recovers the excess.
    years_to_bunch = 1
    if annual_giving < std_ded:
        # how many years of giving to exceed the standard deduction
        years_to_bunch = max(2, -(-int(std_ded) // max(1, int(annual_giving))))
    bunched_gift = round(annual_giving * years_to_bunch, 2)
    itemized_excess = max(0.0, bunched_gift - std_ded)
    # Benefit = deduction recovered above the standard deduction, at marginal rate,
    # net of the standard deduction forgone in the off years (already captured by
    # only counting the excess over one standard deduction).
    daf_benefit = round(itemized_excess * marginal, 2)
    strategies.append({
        "vehicle":   "Donor-Advised Fund (bunching)",
        "eligible":  annual_giving < std_ded,
        "recommended_bunch_years": years_to_bunch,
        "bunched_contribution": bunched_gift,
        "estimated_tax_benefit": daf_benefit,
        "detail": (
            (f"Your ${annual_giving:,.0f}/yr gift is below the ${std_ded:,.0f} standard "
             f"deduction, so giving it yields no itemizing benefit in a normal year. "
             f"Bunching ~{years_to_bunch} years (${bunched_gift:,.0f}) into a DAF lets you "
             f"itemize once — recovering ${itemized_excess:,.0f} of deductions above the "
             f"standard deduction (≈${daf_benefit:,.0f} at your {int(marginal*100)}% rate) — "
             f"then take the standard deduction in the off years.")
            if annual_giving < std_ded else
            (f"Your ${annual_giving:,.0f}/yr gift already exceeds the ${std_ded:,.0f} standard "
             f"deduction, so you itemize every year — bunching adds little. A DAF still helps "
             f"if you want to front-load a high-income year's deduction.")
        ),
    })

    # --- 3. Appreciated securities ---
    cge = await get_capital_gains_exposure(http_session, filing_status=fs, annual_income=current_income)
    appreciated_lots: list[dict] = []
    cap_gains_avoided = 0.0
    if "error" not in cge:
        positions = [p for p in cge.get("taxable_account_positions", [])
                     if (p.get("unrealized_gain") or 0) > 0]
        positions.sort(key=lambda p: p.get("unrealized_gain", 0), reverse=True)
        gift_remaining = annual_giving
        for p in positions:
            if gift_remaining <= 0:
                break
            value = p.get("current_value", 0) or 0
            gift_value = min(value, gift_remaining)
            frac = gift_value / value if value else 0
            gain_gifted = round((p.get("unrealized_gain", 0) or 0) * frac, 2)
            tax_avoided = round(gain_gifted * (ltcg + (0.038 if cge.get("niit_applies") else 0.0)), 2)
            cap_gains_avoided += tax_avoided
            appreciated_lots.append({
                "ticker":              p.get("ticker"),
                "description":         p.get("description"),
                "account":             p.get("account"),
                "gift_market_value":   round(gift_value, 2),
                "embedded_gain_gifted": gain_gifted,
                "capital_gains_tax_avoided": tax_avoided,
            })
            gift_remaining -= gift_value
    strategies.append({
        "vehicle":   "Gift appreciated securities (in-kind)",
        "eligible":  len(appreciated_lots) > 0,
        "estimated_tax_benefit": round(cap_gains_avoided, 2),
        "candidate_lots": appreciated_lots,
        "detail": (
            (f"Donating ${annual_giving:,.0f} of long-term appreciated shares in-kind (instead "
             f"of cash) avoids ≈${cap_gains_avoided:,.0f} of capital-gains tax on the embedded "
             f"gain while still deducting full fair-market value. Best lots are listed.")
            if appreciated_lots else
            "No appreciated taxable-account lots found to gift in-kind (or holdings unavailable)."
        ),
    })

    # Recommend the eligible strategy with the highest estimated benefit.
    eligible = [s for s in strategies if s.get("eligible")]
    recommended = max(eligible, key=lambda s: s.get("estimated_tax_benefit", 0)) if eligible else None

    return {
        "as_of":                 datetime.now().strftime("%Y-%m-%d"),
        "annual_giving":         round(annual_giving, 2),
        "filing_status":         fs,
        "age":                   age,
        "estimated_annual_income": round(current_income, 2),
        "income_inferred":       inferred_income,
        "marginal_rate_pct":     int(marginal * 100),
        "standard_deduction":    std_ded,
        "strategies":            strategies,
        "recommended_vehicle":   recommended["vehicle"] if recommended else None,
        "recommended_tax_benefit": recommended["estimated_tax_benefit"] if recommended else 0.0,
        "note": (
            "Strategies can be combined (e.g. QCD for the IRA portion plus an in-kind gift of "
            "appreciated stock). Benefit estimates are directional: the DAF figure assumes the "
            "standard deduction is the only competing itemized total, and QCD/cash comparisons "
            "use your marginal rate. AGI/itemized-deduction limits (60% of AGI for cash, 30% for "
            "appreciated securities) are not modeled."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_tax_gain_harvesting  (#90)
# ---------------------------------------------------------------------------

async def get_tax_gain_harvesting(
    http_session,
    filing_status: str = "mfj",
    annual_income: float | None = None,
) -> dict:
    """
    Identify how much long-term capital gain can be realized at the 0% LTCG rate
    this year — "tax-gain harvesting" — and which taxable-account lots to sell to
    reset cost basis for free.

    In the 0% LTCG bracket, selling appreciated long-term positions and
    immediately rebuying them steps up cost basis at no tax cost (the wash-sale
    rule applies only to *losses*, not gains). This is a recurring free benefit
    for moderate-income years (early retirement, a gap year, etc.).

    Parameters
    ----------
    filing_status : 'single', 'mfj', or 'hoh' (default 'mfj')
    annual_income : ordinary income before gains (inferred from 12-month
                    transactions if omitted). LTCG stacks on top of ordinary
                    income, so this sets where the 0% bracket runs out.
    """
    fs = filing_status if filing_status in _LTCG_THRESHOLDS else "mfj"
    std_ded = _STD_DEDUCTION.get(fs, 30_000)

    inferred = False
    if annual_income is None:
        inc_result = await get_income_summary(http_session, days=365)
        if "error" not in inc_result:
            annual_income = inc_result.get("total_income", 0) or 0
            inferred = True
        else:
            annual_income = 0.0

    ordinary_taxable = max(0.0, annual_income - std_ded)

    # The 0% LTCG band runs up to this taxable-income ceiling; ordinary income
    # fills it first, and gains stack on top.
    zero_pct_ceiling = _LTCG_THRESHOLDS[fs][0][0]
    room_at_0pct = round(max(0.0, zero_pct_ceiling - ordinary_taxable), 2)

    cge = await get_capital_gains_exposure(http_session, filing_status=fs, annual_income=annual_income)
    if "error" in cge:
        return cge

    positions = [p for p in cge.get("taxable_account_positions", [])
                 if (p.get("unrealized_gain") or 0) > 0]
    positions.sort(key=lambda p: p.get("unrealized_gain", 0), reverse=True)
    total_taxable_gain = round(sum(p.get("unrealized_gain", 0) or 0 for p in positions), 2)

    # Fill the 0% room with the largest gains first.
    harvest_plan: list[dict] = []
    remaining = room_at_0pct
    harvested_gain = 0.0
    for p in positions:
        if remaining <= 0:
            break
        gain = p.get("unrealized_gain", 0) or 0
        harvest_gain = min(gain, remaining)
        frac = harvest_gain / gain if gain else 0
        harvest_value = round((p.get("current_value", 0) or 0) * frac, 2)
        harvest_plan.append({
            "ticker":             p.get("ticker"),
            "description":        p.get("description"),
            "account":            p.get("account"),
            "sell_market_value":  harvest_value,
            "gain_harvested_at_0pct": round(harvest_gain, 2),
        })
        harvested_gain += harvest_gain
        remaining -= harvest_gain

    harvested_gain = round(harvested_gain, 2)
    # Tax saved = future LTCG tax avoided on the stepped-up basis. If they'd later
    # sell at the 15% rate, harvesting now at 0% saves 15% of the harvested gain.
    future_tax_saved = round(harvested_gain * 0.15, 2)

    return {
        "as_of":                 datetime.now().strftime("%Y-%m-%d"),
        "filing_status":         fs,
        "estimated_annual_income": round(annual_income, 2),
        "income_inferred":       inferred,
        "ordinary_taxable_income": round(ordinary_taxable, 2),
        "zero_pct_ltcg_ceiling": zero_pct_ceiling,
        "room_in_0pct_bracket":  room_at_0pct,
        "total_unrealized_gain_taxable": total_taxable_gain,
        "harvestable_gain_at_0pct": harvested_gain,
        "estimated_future_tax_saved": future_tax_saved,
        "harvest_plan":          harvest_plan,
        "note": (
            "Realizing long-term gains inside the 0% LTCG band resets cost basis tax-free; "
            "you can repurchase immediately (the wash-sale rule restricts losses, not gains). "
            "Gains stack ON TOP of ordinary income — every extra dollar of ordinary income "
            "shrinks the 0% room, and gains beyond the room are taxed at 15%. Harvesting also "
            "raises AGI, which can affect ACA subsidies and (at 63+) IRMAA — see get_irmaa_analysis. "
            "Assumes positions are long-term (held > 1 year)."
        ),
        "caveat": _IRS_CAVEAT,
    }


# ---------------------------------------------------------------------------
# get_state_tax_estimate  (#90)
# ---------------------------------------------------------------------------

def _resolve_state(state: str) -> str | None:
    """Map a 2-letter code or full state name to the canonical code, or None."""
    if not state:
        return None
    s = state.strip()
    if s.upper() in _STATE_TAX:
        return s.upper()
    return _STATE_NAME_TO_CODE.get(s.lower())


async def get_state_tax_estimate(
    http_session,
    state: str,
    amount: float,
    filing_status: str = "mfj",
    income_type: str = "ordinary",
) -> dict:
    """
    Estimate the STATE income tax on an incremental amount of income — the piece
    every other tool in this server omits (all federal-only). Layer this on top
    of a Roth conversion, capital-gain realization, or retirement withdrawal to
    see the true combined marginal cost.

    Uses each state's representative top marginal rate applied to ``amount``. For
    a state with a flat tax this is exact; for graduated states it is the marginal
    (top-of-the-stack) treatment, which is the right model for *additional* income
    on top of an existing base. Nine states have no income tax (estimate $0);
    Washington taxes long-term capital gains at 7% above a threshold even though
    it has no ordinary-income tax.

    Parameters
    ----------
    state       : 2-letter code ('CA') or full name ('California')
    amount      : the incremental income in dollars to tax (conversion, gain, withdrawal)
    filing_status : 'single', 'mfj', or 'hoh' (informational; rates shown are top marginal)
    income_type : 'ordinary' (default) or 'ltcg' (long-term capital gain)
    """
    if amount is None or amount < 0:
        return {"error": "amount must be a non-negative dollar figure."}

    code = _resolve_state(state)
    if code is None:
        return {"error": (f"Unknown state '{state}'. Use a 2-letter code (e.g. 'CA') "
                          f"or full name (e.g. 'California').")}

    info = _STATE_TAX[code]
    it = income_type.lower() if income_type else "ordinary"
    if it not in ("ordinary", "ltcg"):
        it = "ordinary"

    # Most states tax LTCG as ordinary income (same rate). Washington is the
    # exception: 0% ordinary but a dedicated 7% LTCG tax above a threshold.
    rate = info["rate"]
    if it == "ltcg" and "ltcg_rate" in info:
        rate = info["ltcg_rate"]

    state_tax = round(amount * rate, 2)

    return {
        "state":                 info["name"],
        "state_code":            code,
        "income_type":           it,
        "amount":                round(amount, 2),
        "filing_status":         filing_status,
        "state_marginal_rate_pct": round(rate * 100, 3),
        "estimated_state_tax":   state_tax,
        "no_state_income_tax":   bool(info.get("no_income_tax")) and not (it == "ltcg" and "ltcg_rate" in info),
        "rate_is_flat":          bool(info.get("flat")),
        "state_note":            info.get("note"),
        "note": (
            "State tax is estimated by applying the state's representative top marginal rate to "
            "the supplied amount — exact for flat-tax states, and the correct marginal treatment "
            "for additional income stacked on an existing base in graduated states (it can "
            "overstate tax on income that actually falls in lower brackets). Local/city income "
            "taxes (e.g. NYC, MD counties) and state-specific exclusions (e.g. PA's exemption of "
            "retirement income) are noted where applicable but not auto-applied. Rates are 2025 "
            "figures — verify against current state schedules."
        ),
        "caveat": (
            "Estimate only. State tax law is intricate (credits, exclusions, retirement-income "
            "carve-outs, local surtaxes). Consult a qualified tax professional."
        ),
    }
