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
from datetime import datetime

from .accounts import get_accounts, _calc_investable_assets
from .spending import _fetch_snb_data, _sum_income_spending
from ._helpers import _get_card

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

    # Simple matching: pair mortgages to properties by order / name proximity
    total_property_value   = round(sum(a.get("balance", 0) or 0 for a in property_accounts), 2)
    total_mortgage_balance = round(abs(sum(a.get("balance", 0) or 0 for a in mortgage_accounts)), 2)
    total_equity           = round(total_property_value - total_mortgage_balance, 2)

    properties = []
    for prop in property_accounts:
        prop_val = prop.get("balance") or 0
        # Best-effort: find the closest-named mortgage
        matched_mortgage = 0.0
        for mort in mortgage_accounts:
            matched_mortgage += abs(mort.get("balance") or 0)

        equity  = round(prop_val - matched_mortgage, 2)
        ltv_pct = round(matched_mortgage / prop_val * 100, 1) if prop_val > 0 else None
        properties.append({
            "account_name":      prop.get("name"),
            "property_value":    round(prop_val, 2),
            "mortgage_balance":  round(matched_mortgage, 2),
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
        "note": (
            "Property values and mortgage balances are pulled from Emoney's account list. "
            "Liquid cash and credit card total sourced from Card 10. "
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

    # Years to FI: future value of current assets + monthly contributions compounding to fi_number
    years_to_fi = None
    fi_date_str = None
    if gap_to_fi > 0 and monthly_savings > 0 and annual_return > 0:
        r = annual_return / 12
        # FV = PV*(1+r)^n + PMT*((1+r)^n - 1)/r → solve for n numerically
        try:
            # Newton approximation: use log formula for pure growth first, then iterate
            n = 0
            balance = investable_assets
            while balance < fi_number and n < 600:
                balance = balance * (1 + r) + monthly_savings
                n += 1
            if balance >= fi_number:
                years_to_fi = round(n / 12, 1)
                fi_year = datetime.now().year + int(years_to_fi) + 1
                fi_date_str = str(fi_year)
        except Exception:
            pass

    # Monthly savings needed to hit FI by target ages
    now_year = datetime.now().year
    monthly_needed: dict[str, float | None] = {}
    for target_age_offset in (55, 60, 65):
        target_age_offset - (now_year % 100)  # rough heuristic without knowing age
        # Instead express as years: 15, 20, 25 years from now as illustrative targets
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
