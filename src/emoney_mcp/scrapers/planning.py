"""
Financial planning gap analysis tools.

Public functions
----------------
get_insurance_gap_analysis(http_session, income_multiple, disability_pct)
    Computes insurance *need* from existing Emoney data (income, net worth,
    liquid assets, monthly expenses) using standard financial planning rules.
    Does not require unknown card IDs — purely analytical from SNB + balance sheet.

    Life insurance need:  income_multiple × gross annual income − liquid net worth
    Disability need:      disability_pct × gross monthly income
    Emergency fund:       3× and 6× monthly expenses vs. current liquid assets
"""

import asyncio
from datetime import datetime

from .accounts import get_accounts
from .spending import _fetch_snb_data, _INCOME_CATEGORIES, _EXCLUDE_CATEGORIES


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
    annual_income   = 0.0
    annual_spending = 0.0
    if snb_ok:
        for t in txns:
            if t["is_excluded"]:
                continue
            if t["is_income"]:
                annual_income   += t["amount"]
            else:
                annual_spending += t["amount"]

    annual_income   = round(annual_income, 2)
    annual_spending = round(annual_spending, 2)
    monthly_income  = round(annual_income / 12, 2)
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
