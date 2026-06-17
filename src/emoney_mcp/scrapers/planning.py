"""
Financial planning gap analysis tools.

Public functions
----------------
get_insurance_gap_analysis(http_session, income_multiple, disability_pct)
    Computes insurance *need* from existing Emoney data (income, net worth,
    liquid assets, monthly expenses) using standard financial planning rules.

get_home_equity(http_session)
    Identifies property accounts and mortgage liabilities from Card 1 / Card 9
    and returns equity, LTV, and equity as % of net worth per property.

get_fire_number(http_session, swr, annual_return)
    Computes the Financial Independence number (annual_spending / swr), the
    gap from the current investable asset base, and the projected years-to-FI
    at the current savings rate.

get_gifting_and_estate_strategy(http_session, num_recipients, filing_status)
    Estate snapshot, annual gift exclusion availability, 529 superfunding
    opportunity, and a prioritised action list.
"""

import asyncio
import logging
import re
from datetime import datetime

from .accounts import get_accounts, get_retirement_accounts, _calc_investable_assets
from .spending import _fetch_snb_data, _sum_income_spending
from ._helpers import _get_card
from .tax import _CONTRIBUTION_LIMITS, _IRS_CAVEAT

_log = logging.getLogger("emoney_mcp.scrapers.planning")

# ---------------------------------------------------------------------------
# 2026 IRS constants used by get_gifting_and_estate_strategy
# ---------------------------------------------------------------------------
_ANNUAL_GIFT_EXCLUSION  = 19_000        # per recipient per year (2026)
_ESTATE_EXEMPTION_SINGLE = 13_610_000   # federal estate/gift tax exemption (2025)
_ESTATE_EXEMPTION_MFJ    = 27_220_000   # married — each spouse has their own exemption
_ESTATE_TOP_RATE         = 0.40         # federal estate tax top marginal rate
_529_SUPERFUND_YEARS     = 5            # years that can be front-loaded in one go


async def get_home_equity(http_session) -> dict:
    """
    Return home equity and loan-to-value ratio for all real-estate holdings.

    Scans the Emoney account list (Card 1) for property assets and their
    matching mortgage/HELOC liabilities.

    Returns
    -------
    dict with keys:
      properties       — list of {name, property_value, mortgage_balance,
                                   equity, ltv_pct, account_name}
      total_property_value
      total_mortgage_balance
      total_equity
      equity_pct_of_net_worth
      net_worth        — from Card 9 (for context)
    """
    import asyncio as _asyncio

    # Fetch accounts and Card 10 (cash + credit summary) in parallel
    accts, card10 = await _asyncio.gather(
        get_accounts(http_session),
        _get_card(await http_session.get_http(), 10),
    )
    if "error" in accts:
        return accts

    net_worth = accts.get("net_worth") or 0
    # Card 10 gives us a more granular cash + credit breakdown
    liquid_cash  = (card10.get("Cash")   or 0) if card10 else None
    credit_total = (card10.get("Credit") or 0) if card10 else None

    # Classify each account as property, mortgage/heloc, or neither
    _PROPERTY_KEYWORDS  = {"real estate", "property", "home", "house", "land", "realestate"}
    _MORTGAGE_KEYWORDS  = {"mortgage", "heloc", "home equity", "home loan"}
    _DEBT_TYPES         = {"Mortgage"}

    property_accounts = []   # positive balances
    mortgage_accounts = []   # negative balances

    for grp in accts.get("account_groups", []):
        for acct in grp.get("accounts", []):
            name_lower = (acct.get("name") or "").lower()
            type_lower = (acct.get("type") or "").lower()
            bal = acct.get("balance") or 0

            is_property = (
                any(kw in name_lower for kw in _PROPERTY_KEYWORDS)
                or "realestate" in type_lower
                or "RealEstate" in (acct.get("type") or "")
            )
            is_mortgage = (
                any(kw in name_lower for kw in _MORTGAGE_KEYWORDS)
                or acct.get("type") in _DEBT_TYPES
            )

            if is_property and bal > 0:
                property_accounts.append(acct)
            elif is_mortgage and bal < 0:
                mortgage_accounts.append(acct)

    total_property_value   = round(sum(a.get("balance", 0) or 0 for a in property_accounts), 2)
    total_mortgage_balance = round(abs(sum(a.get("balance", 0) or 0 for a in mortgage_accounts)), 2)
    total_equity           = round(total_property_value - total_mortgage_balance, 2)

    # Attribute each mortgage to a single property so a household with multiple
    # properties/mortgages doesn't get the combined mortgage total charged against
    # every property. Best-effort: match on shared name tokens; with exactly one
    # property, all mortgages belong to it. Mortgages that match no property are
    # left unallocated (and surfaced in the note) rather than double-counted.
    def _tokens(s: str) -> set:
        return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(t) > 2}

    prop_tokens   = [_tokens(p.get("name")) for p in property_accounts]
    prop_mortgage = [0.0 for _ in property_accounts]
    unmatched_mortgage = 0.0

    for mort in mortgage_accounts:
        bal       = abs(mort.get("balance") or 0)
        m_tokens  = _tokens(mort.get("name"))
        best_idx, best_overlap = None, 0
        for idx, pt in enumerate(prop_tokens):
            overlap = len(m_tokens & pt)
            if overlap > best_overlap:
                best_idx, best_overlap = idx, overlap
        if best_idx is None and len(property_accounts) == 1:
            best_idx = 0   # single property: no ambiguity
        if best_idx is None:
            unmatched_mortgage += bal
        else:
            prop_mortgage[best_idx] += bal

    properties = []
    for idx, prop in enumerate(property_accounts):
        prop_val = prop.get("balance") or 0
        matched_mortgage = round(prop_mortgage[idx], 2)
        equity  = round(prop_val - matched_mortgage, 2)
        ltv_pct = round(matched_mortgage / prop_val * 100, 1) if prop_val > 0 else None
        properties.append({
            "account_name":      prop.get("name"),
            "property_value":    round(prop_val, 2),
            "mortgage_balance":  matched_mortgage,
            "equity":            equity,
            "ltv_pct":           ltv_pct,
        })

    equity_pct_of_nw = round(total_equity / net_worth * 100, 1) if net_worth > 0 else None

    return {
        "as_of":                    datetime.now().strftime("%Y-%m-%d"),
        "properties":               properties,
        "total_property_value":     total_property_value,
        "total_mortgage_balance":   total_mortgage_balance,
        "total_equity":             total_equity,
        "equity_pct_of_net_worth":  equity_pct_of_nw,
        "net_worth":                round(net_worth, 2),
        "liquid_cash":              round(liquid_cash, 2) if liquid_cash is not None else None,
        "credit_card_balance":      round(credit_total, 2) if credit_total is not None else None,
        "unmatched_mortgage_balance": round(unmatched_mortgage, 2) if unmatched_mortgage else None,
        "note": (
            "Property values and mortgage balances are pulled from Emoney's account list. "
            "Per-property mortgage_balance is a best-effort match by account name; "
            "total_equity/total_mortgage_balance are exact aggregates. "
            + (
                f"${round(unmatched_mortgage, 2):,.2f} of mortgage debt could not be matched "
                "to a specific property and is excluded from per-property rows (but included "
                "in the totals). "
                if unmatched_mortgage else ""
            )
            + "Liquid cash and credit card total sourced from Card 10. "
            "Values reflect the last sync date shown in your Emoney dashboard."
        ),
    }


async def get_fire_number(
    http_session,
    swr: float = 0.04,
    annual_return: float = 0.07,
) -> dict:
    """
    Compute the Financial Independence (FI) number and time-to-FI estimate.

    The FI number is the portfolio size at which annual spending can be funded
    indefinitely at the given safe withdrawal rate.  Default: 25× annual spend
    (4% SWR).

    Parameters
    ----------
    swr           : safe withdrawal rate (default 0.04 = 4%)
    annual_return : expected portfolio return used to project growth (default 7%)

    Returns
    -------
    dict with keys:
      annual_spending, fi_number, lean_fi_number (3.5% SWR), fat_fi_number (3% SWR)
      current_investable_assets, gap_to_fi, pct_of_way_there,
      years_to_fi_at_current_savings, fi_date_estimate,
      monthly_savings_needed_by_age, note
    """
    swr = max(0.02, min(swr, 0.08))
    annual_return = max(0.03, min(annual_return, 0.12))

    accts, (txns, snb_ok) = await asyncio.gather(
        get_accounts(http_session),
        _fetch_snb_data(http_session, days=365),
    )
    if "error" in accts:
        return accts

    investable_assets = _calc_investable_assets(accts)
    accts.get("total_liabilities") or 0

    # 12-month spend from SNB
    annual_income, annual_spending = _sum_income_spending(txns) if snb_ok else (0.0, 0.0)
    monthly_savings = round((annual_income - annual_spending) / 12, 2) if annual_income > annual_spending else 0

    fi_number      = round(annual_spending / swr, 2) if annual_spending > 0 else 0
    lean_fi_number = round(annual_spending / 0.035, 2) if annual_spending > 0 else 0
    fat_fi_number  = round(annual_spending / 0.03, 2)  if annual_spending > 0 else 0
    gap_to_fi      = round(max(0, fi_number - investable_assets), 2)
    pct_of_way     = round(min(investable_assets / fi_number * 100, 100.0), 1) if fi_number > 0 else 0

    # Years to FI: future value of current assets + monthly contributions compounding to fi_number.
    # fi_status disambiguates the cases that all previously collapsed into years_to_fi=None:
    #   already_fi          — assets already meet the FI number
    #   no_current_savings  — not saving, so no projected progress toward FI
    #   on_track            — reaches FI within the 50-year horizon (years_to_fi populated)
    #   unreachable_in_50y  — at this pace, FI is not reached within 50 years
    years_to_fi = None
    fi_date_str = None
    if gap_to_fi <= 0:
        fi_status = "already_fi"
    elif monthly_savings <= 0 or annual_return <= 0:
        fi_status = "no_current_savings"
    else:
        fi_status = "unreachable_in_50y"
        r = annual_return / 12
        # FV = PV*(1+r)^n + PMT*((1+r)^n - 1)/r → solve for n numerically
        try:
            n = 0
            balance = investable_assets
            while balance < fi_number and n < 600:   # 600 months = 50-year cap
                balance = balance * (1 + r) + monthly_savings
                n += 1
            if balance >= fi_number:
                years_to_fi = round(n / 12, 1)
                fi_year = datetime.now().year + int(years_to_fi) + 1
                fi_date_str = str(fi_year)
                fi_status = "on_track"
        except Exception as e:
            _log.debug("FIRE projection calc failed: %s", type(e).__name__)

    # Monthly savings needed to hit FI at illustrative horizons (we don't know the
    # client's age here, so express targets as years from now rather than ages).
    monthly_needed: dict[str, float | None] = {}
    for label, yr in [("in_15_years", 15), ("in_20_years", 20), ("in_25_years", 25)]:
        if yr > 0 and gap_to_fi > 0:
            r  = annual_return / 12
            n  = yr * 12
            # PMT needed so that PV*(1+r)^n + PMT*((1+r)^n-1)/r = fi_number
            fv_pv = investable_assets * ((1 + r) ** n)
            fv_factor = ((1 + r) ** n - 1) / r
            needed_pmt = (fi_number - fv_pv) / fv_factor
            monthly_needed[label] = round(max(0, needed_pmt), 2)
        else:
            monthly_needed[label] = 0.0

    return {
        "as_of":                        datetime.now().strftime("%Y-%m-%d"),
        "annual_spending":              annual_spending,
        "annual_income":                annual_income,
        "current_monthly_savings":      monthly_savings,
        "current_investable_assets":    investable_assets,
        "fi_number":                    fi_number,
        "lean_fi_number_3pt5pct":       lean_fi_number,
        "fat_fi_number_3pct":           fat_fi_number,
        "gap_to_fi":                    gap_to_fi,
        "pct_of_way_there":             pct_of_way,
        "fi_status":                    fi_status,
        "years_to_fi_at_current_pace":  years_to_fi,
        "estimated_fi_year":            fi_date_str,
        "monthly_savings_needed":       monthly_needed,
        "assumptions": {
            "swr":           swr,
            "annual_return": annual_return,
        },
        "note": (
            "FI number = annual spending ÷ SWR. Investable assets exclude real-estate equity. "
            "Spending is the 12-month SNB history. Projections assume constant returns and savings."
        ),
    }


async def get_insurance_gap_analysis(
    http_session,
    income_multiple: float = 10.0,
    disability_pct:  float = 0.65,
) -> dict:
    """
    Estimate insurance coverage needs from existing financial data.

    Uses income from SNB transactions and assets from Emoney balance sheet.
    Does not read actual policy data (no card for that yet) — computes the
    *need* side so you can compare against your actual coverage manually.

    Parameters
    ----------
    income_multiple : life insurance need = this multiple × annual income (default 10)
    disability_pct  : recommended monthly disability benefit as a % of income (default 65%)
    """
    income_multiple = max(1.0, min(income_multiple, 30.0))
    disability_pct  = max(0.1, min(disability_pct, 1.0))

    # Fetch accounts and 12 months of SNB data in parallel
    accts, (txns, snb_ok) = await asyncio.gather(
        get_accounts(http_session),
        _fetch_snb_data(http_session, days=365),
    )

    if "error" in accts:
        return accts

    net_worth    = accts.get("net_worth") or 0
    total_assets = accts.get("total_assets") or 0
    total_liab   = accts.get("total_liabilities") or 0

    # Liquid assets: cash + bank accounts (first group matching "cash" or "bank")
    liquid_assets = 0.0
    for grp in accts.get("account_groups", []):
        grp_name = (grp.get("group") or "").lower()
        if "cash" in grp_name or "bank" in grp_name or "checking" in grp_name or "saving" in grp_name:
            liquid_assets += grp.get("total", 0) or 0

    # If no liquid group found, use a conservative 10% of total assets
    if liquid_assets <= 0:
        liquid_assets = total_assets * 0.10

    # Annual income and monthly expenses from SNB
    annual_income, annual_spending = _sum_income_spending(txns) if snb_ok else (0.0, 0.0)
    monthly_income   = round(annual_income / 12, 2)
    monthly_expenses = round(annual_spending / 12, 2)

    # --- Life insurance need ---
    life_need     = round(annual_income * income_multiple, 2)
    life_gap      = round(life_need - liquid_assets, 2)
    life_status   = "adequate" if life_gap <= 0 else "gap"

    # --- Disability insurance ---
    monthly_disability_benefit = round(monthly_income * disability_pct, 2)

    # --- Emergency fund ---
    emergency_3mo  = round(monthly_expenses * 3, 2)
    emergency_6mo  = round(monthly_expenses * 6, 2)
    if liquid_assets >= emergency_6mo:
        emergency_status = "above_target"
    elif liquid_assets >= emergency_3mo:
        emergency_status = "adequate"
    else:
        emergency_status = "below_minimum"
    emergency_gap = round(emergency_3mo - liquid_assets, 2) if emergency_status == "below_minimum" else 0

    # --- Debt-to-income context ---
    dti = round(total_liab / annual_income * 100, 1) if annual_income > 0 else None

    return {
        "as_of":               datetime.now().strftime("%Y-%m-%d"),
        "income_data": {
            "annual_income_estimate":   annual_income,
            "monthly_income":           monthly_income,
            "data_source":             "12-month SNB transaction history",
            "note": "Income = Paycheck/Salary, Income, ACH Transfer, Dividends, Interest." if snb_ok else "SNB data unavailable — figures may be $0.",
        },
        "life_insurance": {
            "methodology":             f"{income_multiple:.0f}× annual income minus liquid assets",
            "annual_income":            annual_income,
            "income_multiple":          income_multiple,
            "estimated_need":           life_need,
            "liquid_assets":            round(liquid_assets, 2),
            "estimated_gap":            life_gap,
            "status":                   life_status,
            "interpretation": (
                "A positive gap suggests you may be under-insured relative to the "
                f"{income_multiple:.0f}× income rule of thumb. "
                "Compare against your actual term/whole life policy death benefit."
            ),
        },
        "disability": {
            "monthly_income":               monthly_income,
            "recommended_monthly_benefit":  monthly_disability_benefit,
            "disability_pct":               round(disability_pct * 100, 0),
            "note": (
                "Standard recommendation: disability coverage replacing 60–70% of gross income. "
                "Compare against your employer and/or individual disability policy benefit."
            ),
        },
        "emergency_fund": {
            "monthly_expenses":          monthly_expenses,
            "recommended_minimum_3mo":   emergency_3mo,
            "recommended_target_6mo":    emergency_6mo,
            "liquid_assets":             round(liquid_assets, 2),
            "gap_to_minimum":            max(0, emergency_gap),
            "months_covered":            round(liquid_assets / monthly_expenses, 1) if monthly_expenses > 0 else None,
            "status":                    emergency_status,
        },
        "debt_context": {
            "total_liabilities":         round(total_liab, 2),
            "debt_to_annual_income_pct": dti,
            "note": "Debt-to-income above 36% is generally considered elevated.",
        },
        "net_worth_summary": {
            "net_worth":   round(net_worth, 2),
            "total_assets": round(total_assets, 2),
            "total_liabilities": round(total_liab, 2),
        },
        "caveat": (
            "This analysis estimates insurance *need* using standard planning rules. "
            "Actual coverage requirements depend on your dependents, existing policies, "
            "employer benefits, and estate planning goals. "
            "Consult a licensed insurance professional for personalized recommendations."
        ),
    }


async def get_gifting_and_estate_strategy(
    http_session,
    num_recipients: int = 2,
    filing_status: str = "mfj",
) -> dict:
    """
    Estate snapshot and gifting strategy recommendations.

    Uses net worth from Card 9 + 2026 IRS estate and gift tax constants to
    show current estate tax exposure, available annual exclusion gifting
    capacity, and 529 superfunding opportunity.

    Parameters
    ----------
    num_recipients : number of people you plan to gift to per year (default 2)
    filing_status  : 'mfj' (married filing jointly) or 'single'
    """
    accts = await get_accounts(http_session)
    if "error" in accts:
        return accts

    filing_status   = filing_status.lower().strip()
    is_mfj          = filing_status == "mfj"
    gross_estate    = round(accts.get("total_assets") or 0, 2)
    total_liab      = round(accts.get("total_liabilities") or 0, 2)
    net_estate      = round(gross_estate - total_liab, 2)
    round(accts.get("net_worth") or 0, 2)

    federal_exemption = _ESTATE_EXEMPTION_MFJ if is_mfj else _ESTATE_EXEMPTION_SINGLE
    taxable_estate    = round(max(0, net_estate - federal_exemption), 2)
    est_estate_tax    = round(taxable_estate * _ESTATE_TOP_RATE, 2)

    # Annual gift exclusion
    # MFJ couples can gift-split: $18k × 2 donors per recipient
    annual_per_recipient_mfj = _ANNUAL_GIFT_EXCLUSION * (2 if is_mfj else 1)
    total_annual_exclusion   = annual_per_recipient_mfj * num_recipients

    # 529 superfunding (5-year front-load)
    superfund_per_beneficiary = annual_per_recipient_mfj * _529_SUPERFUND_YEARS

    # Strategies list
    strategies = []
    if taxable_estate > 0:
        strategies.append({
            "strategy":        "Annual exclusion gifting",
            "description":     f"Gift ${total_annual_exclusion:,.0f}/year to {num_recipients} recipient(s) — reduces taxable estate with no gift tax.",
            "annual_impact":   -total_annual_exclusion,
            "priority":        "high",
        })
        strategies.append({
            "strategy":        "529 superfunding",
            "description":     f"Front-load 5 years of gift exclusions into a 529 (${superfund_per_beneficiary:,.0f} per beneficiary). Counts as using future annual exclusions — no gift tax if donor survives 5 years.",
            "one_time_impact": -superfund_per_beneficiary,
            "priority":        "high",
        })
        strategies.append({
            "strategy":        "Charitable giving",
            "description":     "Qualified charitable distributions from IRAs or donor-advised fund contributions reduce both income tax and taxable estate.",
            "priority":        "medium",
        })
        if taxable_estate > 1_000_000:
            strategies.append({
                "strategy":   "Irrevocable trust (ILIT / SLATs)",
                "description": "Larger estates benefit from irrevocable life insurance trusts or spousal lifetime access trusts to move assets outside the estate. Consult an estate planning attorney.",
                "priority":   "high",
            })
    else:
        strategies.append({
            "strategy":    "Estate currently below exemption",
            "description": f"Net estate of ${net_estate:,.0f} is below the {filing_status.upper()} federal exemption of ${federal_exemption:,.0f}. Focus on wealth building before formal estate planning. Annual gifting is still beneficial for education funding.",
            "priority":    "low",
        })

    return {
        "as_of":         datetime.now().strftime("%Y-%m-%d"),
        "filing_status": filing_status.upper(),
        "estate_snapshot": {
            "gross_estate":         gross_estate,
            "total_liabilities":    total_liab,
            "net_estate":           net_estate,
            "federal_exemption":    federal_exemption,
            "taxable_estate":       taxable_estate,
            "estimated_estate_tax": est_estate_tax,
            "estate_tax_exposed":   taxable_estate > 0,
        },
        "annual_gifting": {
            "exclusion_per_donor_per_recipient": _ANNUAL_GIFT_EXCLUSION,
            "gift_splitting_available":          is_mfj,
            "effective_exclusion_per_recipient": annual_per_recipient_mfj,
            "num_recipients":                    num_recipients,
            "total_annual_exclusion_capacity":   total_annual_exclusion,
        },
        "529_superfunding": {
            "max_per_beneficiary":    superfund_per_beneficiary,
            "years_front_loaded":     _529_SUPERFUND_YEARS,
            "note":                   "Donor must survive 5 years; unused exclusions return to estate if donor dies within 5 years.",
        },
        "strategies":   strategies,
        "caveat": (
            "Estate and gift tax rules are complex and change with legislation. "
            "This analysis uses 2026 IRS constants and does not account for state estate taxes, "
            "step-up in basis considerations, or existing trust structures. "
            "Consult an estate planning attorney for a formal plan."
        ),
    }


# ---------------------------------------------------------------------------
# Mortgage analysis suite (#99)
# ---------------------------------------------------------------------------

def _monthly_payment(principal: float, annual_rate: float, months: int) -> float:
    """Standard fully-amortizing monthly payment."""
    if months <= 0:
        return 0.0
    r = annual_rate / 12
    if r == 0:
        return principal / months
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def _amortize(principal: float, annual_rate: float, months: int, extra_monthly: float = 0.0):
    """Run an amortization; return (months_to_payoff, total_interest)."""
    r = annual_rate / 12
    pay = _monthly_payment(principal, annual_rate, months) + max(0.0, extra_monthly)
    bal = principal
    total_interest = 0.0
    m = 0
    while bal > 0.01 and m < 1200:     # 100-year safety cap; cent-level epsilon
        interest = bal * r
        principal_paid = pay - interest
        if principal_paid <= 0:        # payment can't cover interest
            return None, None
        bal -= principal_paid
        total_interest += interest
        m += 1
    return m, round(total_interest, 2)


async def get_mortgage_amortization_schedule(
    http_session,
    balance: float,
    annual_rate: float,
    years_remaining: float,
    extra_monthly: float = 0.0,
) -> dict:
    """
    Amortization schedule for a mortgage: per-year interest vs. principal, total
    interest, and payoff date — with an optional extra monthly principal payment.

    Parameters
    ----------
    balance         : current mortgage balance
    annual_rate     : annual interest rate (e.g. 0.065 for 6.5%)
    years_remaining : years left on the loan
    extra_monthly   : extra principal paid each month (default 0)
    """
    months = int(round(years_remaining * 12))
    if balance <= 0 or months <= 0:
        return {"error": "balance and years_remaining must be positive."}
    exact_payment = _monthly_payment(balance, annual_rate, months)
    base_payment = round(exact_payment, 2)
    r = annual_rate / 12
    pay = exact_payment + max(0.0, extra_monthly)   # unrounded internally to avoid residual drift

    schedule = []
    bal = balance
    total_interest = 0.0
    year_int = year_prin = 0.0
    m = 0
    while bal > 0.01 and m < 1200:
        interest = bal * r
        principal_paid = min(pay - interest, bal)
        if principal_paid <= 0:
            return {"error": "Monthly payment does not cover interest; increase the term or payment."}
        bal -= principal_paid
        total_interest += interest
        year_int += interest
        year_prin += principal_paid
        m += 1
        if m % 12 == 0 or bal <= 0:
            schedule.append({
                "year":             (m + 11) // 12,
                "interest_paid":    round(year_int, 2),
                "principal_paid":   round(year_prin, 2),
                "ending_balance":   round(max(0.0, bal), 2),
            })
            year_int = year_prin = 0.0

    payoff_year = datetime.now().year + (m + 11) // 12
    base_months, base_interest = _amortize(balance, annual_rate, months)
    interest_saved = round((base_interest or 0) - total_interest, 2) if extra_monthly > 0 else 0.0
    return {
        "as_of":               datetime.now().strftime("%Y-%m-%d"),
        "balance":             round(balance, 2),
        "annual_rate_pct":     round(annual_rate * 100, 3),
        "monthly_payment":     base_payment,
        "extra_monthly":       round(extra_monthly, 2),
        "months_to_payoff":    m,
        "payoff_year_est":     payoff_year,
        "total_interest":      round(total_interest, 2),
        "interest_saved_vs_no_extra": interest_saved,
        "yearly_schedule":     schedule,
        "note": "Excludes taxes, insurance, and PMI. Rate assumed fixed.",
    }


async def get_mortgage_refinance_analysis(
    http_session,
    balance: float,
    current_rate: float,
    current_years_remaining: float,
    new_rate: float,
    new_term_years: float,
    closing_costs: float = 0.0,
) -> dict:
    """
    Compare the current mortgage to a refinance: monthly payment change, break-even
    month on closing costs, and lifetime interest difference.
    """
    cur_months = int(round(current_years_remaining * 12))
    new_months = int(round(new_term_years * 12))
    if balance <= 0 or cur_months <= 0 or new_months <= 0:
        return {"error": "balance and both terms must be positive."}

    cur_pay = round(_monthly_payment(balance, current_rate, cur_months), 2)
    new_pay = round(_monthly_payment(balance, new_rate, new_months), 2)
    monthly_savings = round(cur_pay - new_pay, 2)
    _, cur_interest = _amortize(balance, current_rate, cur_months)
    _, new_interest = _amortize(balance, new_rate, new_months)

    break_even = None
    if monthly_savings > 0 and closing_costs > 0:
        break_even = round(closing_costs / monthly_savings, 1)

    return {
        "as_of":                datetime.now().strftime("%Y-%m-%d"),
        "balance":              round(balance, 2),
        "current": {"rate_pct": round(current_rate * 100, 3), "years": current_years_remaining,
                    "monthly_payment": cur_pay, "total_interest": cur_interest},
        "refinanced": {"rate_pct": round(new_rate * 100, 3), "years": new_term_years,
                       "monthly_payment": new_pay, "total_interest": new_interest},
        "monthly_savings":      monthly_savings,
        "closing_costs":        round(closing_costs, 2),
        "break_even_months":    break_even,
        "lifetime_interest_change": round((new_interest or 0) - (cur_interest or 0), 2),
        "note": (
            "A negative lifetime_interest_change means the refi saves interest overall; "
            "a longer new term can lower the monthly payment while raising total interest. "
            "Break-even is closing costs ÷ monthly savings."
        ),
    }


async def get_mortgage_payoff_vs_invest(
    http_session,
    balance: float,
    annual_rate: float,
    years_remaining: float,
    extra_monthly: float,
    investment_return: float = 0.07,
    tax_rate_on_gains: float = 0.15,
) -> dict:
    """
    Compare putting an extra monthly amount toward the mortgage versus investing
    it — over the loan's remaining life, after tax on investment gains.
    """
    months = int(round(years_remaining * 12))
    if balance <= 0 or months <= 0 or extra_monthly <= 0:
        return {"error": "balance, years_remaining, and extra_monthly must be positive."}

    base_months, base_interest = _amortize(balance, annual_rate, months)
    fast_months, fast_interest = _amortize(balance, annual_rate, months, extra_monthly)
    interest_saved = round((base_interest or 0) - (fast_interest or 0), 2)

    # Invest the same extra each month until the original payoff date.
    r = investment_return / 12
    contributions = extra_monthly * months
    fv = 0.0
    for _ in range(months):
        fv = fv * (1 + r) + extra_monthly
    gains = max(0.0, fv - contributions)
    after_tax_value = round(fv - gains * tax_rate_on_gains, 2)
    invest_net_gain = round(after_tax_value - contributions, 2)

    winner = "invest" if invest_net_gain > interest_saved else "pay_off_mortgage"
    return {
        "as_of":                 datetime.now().strftime("%Y-%m-%d"),
        "extra_monthly":         round(extra_monthly, 2),
        "pay_off_mortgage": {
            "interest_saved":    interest_saved,
            "months_earlier":    (base_months or 0) - (fast_months or 0),
        },
        "invest_instead": {
            "assumed_return_pct": round(investment_return * 100, 1),
            "future_value":       round(fv, 2),
            "after_tax_value":    after_tax_value,
            "net_gain_vs_contributions": invest_net_gain,
        },
        "advantage":             winner,
        "advantage_amount":      round(abs(invest_net_gain - interest_saved), 2),
        "note": (
            "Paying down a mortgage is a guaranteed, risk-free return equal to the rate; "
            "investing carries market risk for a higher expected (not guaranteed) return. "
            "Ignores the mortgage-interest deduction and state tax. A guaranteed rate near "
            "the expected after-tax market return favors paying down for the certainty."
        ),
    }


# ---------------------------------------------------------------------------
# get_healthcare_cost_projection  (#102)
# ---------------------------------------------------------------------------

# National-average annual healthcare cost estimates, PER PERSON, in today's
# dollars. These are deliberately conservative planning placeholders, not quotes.
#   pre-65 ACA: unsubsidized benchmark premium (~$600/mo) + out-of-pocket.
#   post-65 Medicare: Part B (~$2,220) + Part D (~$600) + Medigap (~$2,000) +
#     dental/vision/hearing + OOP (~$2,200).
_ACA_ANNUAL_PER_PERSON      = 9_600     # ~$700/mo premium + ~$1,200 OOP
_MEDICARE_ANNUAL_PER_PERSON = 7_000     # Part B + D + Medigap + OOP, base
_MEDICARE_START_AGE         = 65


async def get_healthcare_cost_projection(
    http_session,
    current_age: int,
    retirement_age: int = 65,
    coverage: str = "individual",
    life_expectancy: int = 90,
    health_inflation: float = 0.05,
) -> dict:
    """
    Project lifetime retirement healthcare costs as a plan line item, split into
    the pre-65 (ACA marketplace) and post-65 (Medicare + premiums + out-of-pocket)
    phases, inflated at a medical-specific rate.

    Healthcare is the expense retirees most underestimate. This models it from
    retirement (or now, if already retired) to ``life_expectancy``: ACA-style
    unsubsidized premiums + OOP before Medicare eligibility at 65, then Medicare
    Part B/D + Medigap + OOP after — both inflated each year and scaled for one
    person or a couple.

    Parameters
    ----------
    current_age      : your age today
    retirement_age   : age you leave employer coverage (default 65)
    coverage         : 'individual' (one person) or 'couple' (two)
    life_expectancy  : age through which to project (default 90)
    health_inflation : annual medical inflation assumption (default 0.05 = 5%)
    """
    if current_age is None or current_age <= 0:
        return {"error": "current_age must be a positive integer."}
    people = 2 if str(coverage).lower() in ("couple", "joint", "family", "mfj") else 1
    start_age = max(current_age, min(retirement_age, life_expectancy))
    if life_expectancy <= start_age:
        return {"error": "life_expectancy must be greater than the retirement/start age."}

    schedule: list[dict] = []
    total_pre65 = 0.0
    total_post65 = 0.0
    for age in range(start_age, life_expectancy + 1):
        years_from_now = age - current_age
        inflator = (1 + health_inflation) ** max(0, years_from_now)
        if age < _MEDICARE_START_AGE:
            base = _ACA_ANNUAL_PER_PERSON
            phase = "pre-65 (ACA marketplace)"
        else:
            base = _MEDICARE_ANNUAL_PER_PERSON
            phase = "post-65 (Medicare)"
        annual = round(base * people * inflator, 2)
        if age < _MEDICARE_START_AGE:
            total_pre65 += annual
        else:
            total_post65 += annual
        schedule.append({
            "age":          age,
            "year":         datetime.now().year + years_from_now,
            "phase":        phase,
            "annual_cost":  annual,
        })

    total = round(total_pre65 + total_post65, 2)
    return {
        "as_of":              datetime.now().strftime("%Y-%m-%d"),
        "current_age":        current_age,
        "retirement_age":     retirement_age,
        "coverage":           "couple" if people == 2 else "individual",
        "people":             people,
        "life_expectancy":    life_expectancy,
        "health_inflation_pct": round(health_inflation * 100, 1),
        "phase_totals": {
            "pre_65_aca_total":      round(total_pre65, 2),
            "post_65_medicare_total": round(total_post65, 2),
        },
        "total_projected_healthcare_cost": total,
        "todays_dollars_annual_per_person": {
            "pre_65_aca":      _ACA_ANNUAL_PER_PERSON,
            "post_65_medicare": _MEDICARE_ANNUAL_PER_PERSON,
        },
        "annual_schedule":    schedule,
        "note": (
            "Costs use conservative national-average per-person placeholders "
            f"(${_ACA_ANNUAL_PER_PERSON:,.0f}/yr pre-65 ACA, ${_MEDICARE_ANNUAL_PER_PERSON:,.0f}/yr "
            "Medicare incl. Part B/D, Medigap, and out-of-pocket) inflated at the medical rate. "
            "Long-term care is NOT included — model it separately. High-income retirees also owe "
            "IRMAA surcharges on Medicare premiums (see get_irmaa_analysis). Replace the placeholders "
            "with quotes for your region and plan for a precise figure."
        ),
        "caveat": (
            "Planning estimate only; actual costs vary widely by health, region, and plan choice."
        ),
    }


# ---------------------------------------------------------------------------
# get_hsa_optimization  (#102)
# ---------------------------------------------------------------------------

async def get_hsa_optimization(
    http_session,
    current_age: int | None = None,
    current_hsa_balance: float | None = None,
    annual_contribution: float | None = None,
    coverage: str = "family",
    marginal_rate: float = 0.24,
    growth_rate: float = 0.06,
    target_age: int = 65,
) -> dict:
    """
    Frame the HSA as the most tax-advantaged retirement account available (triple
    tax benefit), compare investing the balance vs. spending it on current medical
    bills, and project its balance trajectory to ``target_age``.

    An HSA is triple-tax-advantaged: contributions are deductible (and avoid FICA
    if made via payroll), growth is tax-free, and qualified medical withdrawals
    are tax-free. Paying current medical costs out of pocket and letting the HSA
    invest turns it into a stealth IRA that also covers tax-free medical expenses
    in retirement (including Medicare premiums).

    Parameters
    ----------
    current_age         : your age (enables the 55+ catch-up; default: skip catch-up)
    current_hsa_balance : current HSA balance (pulled from Emoney if omitted)
    annual_contribution : planned annual contribution (defaults to the IRS limit)
    coverage            : 'family' or 'individual' HDHP coverage (sets the limit)
    marginal_rate       : your marginal tax rate for the deduction value (default 0.24)
    growth_rate         : assumed annual investment return (default 0.06)
    target_age          : age to project the balance to (default 65)
    """
    lim = _CONTRIBUTION_LIMITS
    is_family = str(coverage).lower() in ("family", "couple", "joint")
    base_limit = lim["hsa_family"] if is_family else lim["hsa_individual"]
    catchup_eligible = current_age is not None and current_age >= 55
    contribution_limit = base_limit + (lim["hsa_catchup"] if catchup_eligible else 0)

    if current_hsa_balance is None:
        retirement = await get_retirement_accounts(http_session)
        if "error" in retirement:
            return retirement
        current_hsa_balance = (retirement.get("retirement_breakdown", {}) or {}).get("hsa", 0) or 0
        balance_source = "Emoney retirement accounts"
    else:
        balance_source = "provided"

    contribution = annual_contribution if annual_contribution is not None else contribution_limit
    contribution = min(contribution, contribution_limit)

    # Balance trajectory: contribute each year and grow until target_age.
    years = max(0, target_age - current_age) if current_age is not None else 0
    trajectory: list[dict] = []
    balance = float(current_hsa_balance)
    for i in range(years + 1):
        if i > 0:
            balance = round(balance * (1 + growth_rate) + contribution, 2)
        if current_age is not None:
            trajectory.append({
                "age":     current_age + i,
                "year":    datetime.now().year + i,
                "balance": round(balance, 2),
            })
    projected_balance = round(balance, 2)
    total_contributed = round(float(current_hsa_balance) + contribution * years, 2)
    growth_component = round(projected_balance - total_contributed, 2)

    # Annual tax savings from a deductible contribution (income-tax shield; via
    # payroll it also dodges 7.65% FICA — shown separately).
    income_tax_saved = round(contribution * marginal_rate, 2)
    fica_saved_if_payroll = round(contribution * 0.0765, 2)

    return {
        "as_of":                datetime.now().strftime("%Y-%m-%d"),
        "current_age":          current_age,
        "coverage":             "family" if is_family else "individual",
        "current_hsa_balance":  round(float(current_hsa_balance), 2),
        "balance_source":       balance_source,
        "contribution_limit":   contribution_limit,
        "catch_up_eligible_55plus": catchup_eligible,
        "planned_annual_contribution": round(contribution, 2),
        "triple_tax_advantage": [
            "1. Contributions are tax-deductible (pre-tax via payroll, also avoiding 7.65% FICA).",
            "2. Investment growth is entirely tax-free.",
            "3. Withdrawals for qualified medical expenses are tax-free — at any age.",
        ],
        "annual_tax_savings": {
            "income_tax_shield":     income_tax_saved,
            "fica_savings_if_payroll": fica_saved_if_payroll,
            "combined_if_payroll":   round(income_tax_saved + fica_saved_if_payroll, 2),
        },
        "invest_vs_spend": {
            "recommendation": "invest",
            "detail": (
                "Pay current medical bills out of pocket (keep the receipts) and let the HSA stay "
                "invested. The balance compounds tax-free and can be reimbursed for those past "
                "expenses any time — or used tax-free for medical costs and Medicare premiums in "
                "retirement. After age 65, non-medical withdrawals are taxed as ordinary income "
                "with no penalty (like a traditional IRA), so the HSA is never wasted."
            ),
        },
        "projection": {
            "target_age":            target_age,
            "years_projected":       years,
            "assumed_return_pct":    round(growth_rate * 100, 1),
            "projected_balance":     projected_balance,
            "total_contributed":     total_contributed,
            "tax_free_growth":       growth_component,
            "trajectory":            trajectory,
        },
        "note": (
            "Requires enrollment in a qualifying high-deductible health plan (HDHP) to contribute. "
            "HSA contributions must stop once you enroll in Medicare (typically age 65). The 55+ "
            "catch-up is $1,000. Limits shown are 2026 IRS figures. State treatment varies — a few "
            "states (e.g. CA, NJ) tax HSA contributions and growth."
        ),
        "caveat": _IRS_CAVEAT,
    }
