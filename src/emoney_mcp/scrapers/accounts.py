"""
Account and net-worth scraping.

Public functions
----------------
get_accounts(http_session)
    Fetches every account grouped by type (bank, investment, retirement, debt,
    property) plus top-level net worth / total assets / total liabilities.
    Uses CardSwitcher card 9 (net worth totals) + card 1 (account detail).

get_retirement_accounts(http_session)
    Filters get_accounts output to isolate tax-advantaged accounts (401k, IRA,
    Roth, HSA, 529, annuities) and returns totals by sub-category.

get_net_worth_breakdown(http_session)
    Three-dimensional breakdown of net worth:
      • By person  (account name keywords: Drew / Lacey / Joint)
      • By liquidity (Liquid / Semi-liquid / Illiquid)
      • By tax treatment (Taxable / Tax-Deferred / Tax-Free)

Internal helpers (also exported for use by other modules)
----------------------------------------------------------
_TAX_BUCKET           — dict mapping Emoney MajorType strings to tax buckets
_build_account_type_map(http_session) — {account_name_lower: tax_bucket}
_match_tax_bucket(name, type_map)     — fuzzy lookup in type_map
"""

from ._helpers import _get_card

# ---------------------------------------------------------------------------
# Tax-bucket classification map
# ---------------------------------------------------------------------------
# Maps Emoney's ``MajorType`` field (returned on each account) to one of three
# tax treatment buckets used throughout the tax and portfolio tools.
_TAX_BUCKET: dict[str, str] = {
    "InvestmentAsset":           "Taxable",
    "CashAsset":                 "Taxable",
    "RealEstateAsset":           "Taxable",
    "OptionPlan":                "Taxable",
    "OtherAsset":                "Taxable",
    "PreTaxSavingsAsset":        "Tax-Deferred",
    "AnnuityAsset":              "Tax-Deferred",
    "TaxFreeRothSavingsAsset":   "Tax-Free",
    "TaxFree529SavingsAsset":    "Tax-Free",
    "TaxFreeHealthSavingsAsset": "Tax-Free",
}


async def get_accounts(http_session) -> dict:
    """Fetch net worth and all accounts via the CardSwitcher API."""
    http = await http_session.get_http()

    nw_data = await _get_card(http, 9)
    if nw_data is None:
        return {"error": "Card 9 unavailable. Session may have expired — call reset_session."}

    net_worth    = nw_data.get("NetWorth")
    total_assets = nw_data.get("Assets")
    total_liab   = nw_data.get("Liabilities")

    card1 = await _get_card(http, 1)
    if card1 is None:
        return {
            "net_worth": net_worth,
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "error": "Card 1 (accounts) unavailable",
        }

    groups = []
    for grp in card1.get("AccountGroups", []):
        accounts = []
        for acct in grp.get("Accounts", []):
            accounts.append({
                "id":          acct.get("AccountID"),
                "name":        acct.get("AccountName"),
                "balance":     acct.get("Balance"),
                "institution": acct.get("Institution"),
                "type":        acct.get("MajorType"),
                "as_of":       (acct.get("AsOfDate") or "")[:10],
            })
        groups.append({
            "group":    grp.get("Title", "Unknown"),
            "total":    sum(a["balance"] for a in accounts if a["balance"] is not None),
            "accounts": accounts,
        })

    return {
        "net_worth":         net_worth,
        "total_assets":      total_assets,
        "total_liabilities": total_liab,
        "account_groups":    groups,
        "account_count":     sum(len(g["accounts"]) for g in groups),
    }


_ILLIQUID_ACCOUNT_KEYWORDS = frozenset({"real estate", "property", "home", "house", "land"})


def _calc_investable_assets(accts_data: dict) -> float:
    """
    Return net worth minus illiquid real-estate equity.

    Investable assets = net worth − (sum of positive-balance accounts whose
    name or MajorType indicates real estate).  Used by FI/FIRE calculations
    that need a deployable portfolio estimate.
    """
    net_worth = accts_data.get("net_worth") or 0
    illiquid  = 0.0
    for grp in accts_data.get("account_groups", []):
        for acct in grp.get("accounts", []):
            bal       = acct.get("balance") or 0
            name_lower = (acct.get("name") or "").lower()
            acct_type  = acct.get("type") or ""
            if bal > 0 and (
                any(kw in name_lower for kw in _ILLIQUID_ACCOUNT_KEYWORDS)
                or "RealEstate" in acct_type
            ):
                illiquid += bal
    return round(net_worth - illiquid, 2)


async def get_retirement_accounts(http_session) -> dict:
    """
    Aggregate all tax-advantaged retirement and savings accounts.

    Filters account groups for retirement account types (401k, IRA, Roth,
    Annuity, HSA, 403b, SEP, SIMPLE) and returns a summary with totals.
    """
    result = await get_accounts(http_session)
    if "error" in result:
        return result

    _RETIREMENT_KEYWORDS = {
        "401", "ira", "roth", "annuit", "hsa", "403", "sep", "simple",
        "pension", "retirement", "deferred comp", "529", "education",
    }

    retirement_accounts = []
    taxable_accounts    = []
    debt_accounts       = []

    for group in result.get("account_groups", []):
        for acct in group.get("accounts", []):
            name_lower = (acct.get("name") or "").lower()
            type_lower = (acct.get("type") or "").lower()
            combined   = name_lower + " " + type_lower

            bal = acct.get("balance") or 0
            entry = {
                "name":        acct.get("name"),
                "institution": acct.get("institution"),
                "type":        acct.get("type"),
                "balance":     bal,
                "group":       group.get("group"),
                "as_of":       acct.get("as_of"),
            }

            if bal < 0:
                debt_accounts.append(entry)
            elif any(kw in combined for kw in _RETIREMENT_KEYWORDS):
                retirement_accounts.append(entry)
            else:
                taxable_accounts.append(entry)

    retirement_accounts.sort(key=lambda x: x["balance"], reverse=True)

    total_retirement = sum(a["balance"] for a in retirement_accounts)
    total_taxable    = sum(a["balance"] for a in taxable_accounts)

    def _bucket(accounts, keywords):
        return sum(a["balance"] for a in accounts
                   if any(kw in (a.get("name") or "").lower() + " " + (a.get("type") or "").lower()
                          for kw in keywords))

    return {
        "total_retirement_assets": round(total_retirement, 2),
        "total_taxable_assets":    round(total_taxable, 2),
        "retirement_breakdown": {
            "401k_403b":     round(_bucket(retirement_accounts, ["401", "403"]), 2),
            "ira_roth":      round(_bucket(retirement_accounts, ["ira", "roth"]), 2),
            "annuities":     round(_bucket(retirement_accounts, ["annuit"]), 2),
            "hsa":           round(_bucket(retirement_accounts, ["hsa"]), 2),
            "education_529": round(_bucket(retirement_accounts, ["529", "education"]), 2),
            "other":         round(_bucket(retirement_accounts, ["pension", "sep", "simple", "deferred"]), 2),
        },
        "retirement_accounts": retirement_accounts,
        "note": (
            "Retirement assets are identified by keyword matching on account name/type. "
            "Review the list to confirm correct categorization for your accounts."
        ),
    }


async def get_net_worth_breakdown(http_session) -> dict:
    """
    Break down net worth by three lenses:
      1. By person   — Drew, Lacey, Joint, or Other
      2. By liquidity — Liquid (cash/checking/savings), Semi-liquid (brokerage),
                        Illiquid (real estate, annuities, options/RSU)
      3. By tax treatment — Taxable, Tax-Deferred (traditional 401k/IRA/annuity),
                            Tax-Free (Roth, HSA, 529)

    Source: get_accounts (CardSwitcher cards 9 + 1)
    """
    accounts_result = await get_accounts(http_session)
    if "error" in accounts_result:
        return accounts_result

    net_worth    = accounts_result["net_worth"]
    total_assets = accounts_result["total_assets"]

    # Flatten all accounts
    all_accts = []
    for group in accounts_result.get("account_groups", []):
        for acct in group["accounts"]:
            all_accts.append({**acct, "group": group["group"]})

    # ── 1. By person ──────────────────────────────────────────────────────
    def _person(name: str) -> str:
        n = (name or "").lower()
        has_drew  = "drew" in n
        has_lacey = "lacey" in n
        has_joint = "joint" in n or "parker" in n  # joint includes kid accounts
        if has_drew and not has_lacey:
            return "Drew"
        if has_lacey and not has_drew:
            return "Lacey"
        if has_joint or (has_drew and has_lacey):
            return "Joint / Family"
        return "Other"

    by_person: dict[str, float] = {}
    for a in all_accts:
        bal = a.get("balance") or 0
        if bal <= 0:
            continue
        p = _person(a["name"])
        by_person[p] = round(by_person.get(p, 0) + bal, 2)

    # ── 2. By liquidity ───────────────────────────────────────────────────
    _LIQUID_TYPES = {"CashAsset"}
    _SEMI_LIQUID_TYPES = {"InvestmentAsset", "TaxFreeRothSavingsAsset",
                           "TaxFree529SavingsAsset", "TaxFreeHealthSavingsAsset",
                           "PreTaxSavingsAsset"}
    _ILLIQUID_TYPES = {"AnnuityAsset", "RealEstateAsset", "OptionPlan",
                        "OtherAsset", "Mortgage"}

    by_liquidity: dict[str, float] = {"Liquid": 0.0, "Semi-liquid": 0.0, "Illiquid": 0.0}
    for a in all_accts:
        bal  = a.get("balance") or 0
        atyp = a.get("type") or ""
        if bal <= 0:
            continue
        if atyp in _LIQUID_TYPES:
            by_liquidity["Liquid"] = round(by_liquidity["Liquid"] + bal, 2)
        elif atyp in _SEMI_LIQUID_TYPES:
            by_liquidity["Semi-liquid"] = round(by_liquidity["Semi-liquid"] + bal, 2)
        else:
            by_liquidity["Illiquid"] = round(by_liquidity["Illiquid"] + bal, 2)

    # ── 3. By tax treatment ───────────────────────────────────────────────
    _TAX_FREE_TYPES = {"TaxFreeRothSavingsAsset", "TaxFree529SavingsAsset",
                        "TaxFreeHealthSavingsAsset"}
    _TAX_DEFERRED_TYPES = {"PreTaxSavingsAsset", "AnnuityAsset"}
    _TAXABLE_TYPES = {"InvestmentAsset", "CashAsset", "RealEstateAsset",
                       "OptionPlan", "OtherAsset"}

    by_tax: dict[str, float] = {"Taxable": 0.0, "Tax-Deferred": 0.0, "Tax-Free": 0.0}
    for a in all_accts:
        bal  = a.get("balance") or 0
        atyp = a.get("type") or ""
        if bal <= 0:
            continue
        if atyp in _TAX_FREE_TYPES:
            by_tax["Tax-Free"] = round(by_tax["Tax-Free"] + bal, 2)
        elif atyp in _TAX_DEFERRED_TYPES:
            by_tax["Tax-Deferred"] = round(by_tax["Tax-Deferred"] + bal, 2)
        else:
            by_tax["Taxable"] = round(by_tax["Taxable"] + bal, 2)

    def _pct(val):
        return round(val / total_assets * 100, 1) if total_assets else 0

    return {
        "net_worth":    net_worth,
        "total_assets": total_assets,
        "by_person": [
            {"person": k, "value": v, "percent": _pct(v)}
            for k, v in sorted(by_person.items(), key=lambda x: x[1], reverse=True)
        ],
        "by_liquidity": [
            {"bucket": k, "value": v, "percent": _pct(v)}
            for k, v in by_liquidity.items()
        ],
        "by_tax_treatment": [
            {"bucket": k, "value": v, "percent": _pct(v)}
            for k, v in sorted(by_tax.items(), key=lambda x: x[1], reverse=True)
        ],
        "note": (
            "Person attribution based on account name keywords (Drew/Lacey/Joint). "
            "Liabilities (mortgage, credit cards) excluded from asset breakdowns."
        ),
    }


async def get_debt_payoff_plan(
    http_session,
    extra_monthly_payment: float = 0.0,
    assumed_credit_card_apr: float = 0.22,
    assumed_loan_apr: float = 0.07,
) -> dict:
    """
    Model debt payoff using the avalanche (highest rate first) and snowball
    (smallest balance first) strategies.

    Emoney does not expose interest rates on debt accounts, so APRs are
    assumed by account type:
      • Accounts with 'credit', 'card', or 'visa/mc/amex/discover' in the
        name → ``assumed_credit_card_apr``
      • All other debt accounts (mortgages, loans) → ``assumed_loan_apr``

    Mortgages (keywords: 'mortgage', 'home loan') are included in the
    summary but excluded from the payoff plan because accelerating a
    low-rate mortgage is rarely optimal vs. investing.

    Parameters
    ----------
    extra_monthly_payment    : additional payment above minimums each month
    assumed_credit_card_apr  : default APR for credit cards (default 22%)
    assumed_loan_apr         : default APR for other loans (default 7%)
    """
    import math

    result = await get_accounts(http_session)
    if "error" in result:
        return result

    # Identify debt accounts (negative balance)
    _MORTGAGE_KEYWORDS = {"mortgage", "home loan", "heloc"}
    _CC_KEYWORDS       = {"credit", "card", "visa", "mastercard", "mc", "amex",
                          "discover", "citi", "chase", "capital one"}

    all_debts    = []
    mortgages    = []

    for grp in result.get("account_groups", []):
        for a in grp.get("accounts", []):
            bal = a.get("balance") or 0
            if bal >= 0:
                continue  # not a debt
            name_lower = (a.get("name") or "").lower()

            if any(kw in name_lower for kw in _MORTGAGE_KEYWORDS):
                mortgages.append({
                    "name":    a.get("name"),
                    "balance": round(abs(bal), 2),
                    "type":    "mortgage",
                })
                continue

            is_cc = any(kw in name_lower for kw in _CC_KEYWORDS)
            apr   = assumed_credit_card_apr if is_cc else assumed_loan_apr
            debt_type = "credit_card" if is_cc else "loan"

            # Standard minimum payment: 2% of balance for CC, $50 floor for loans
            min_payment = round(max(abs(bal) * 0.02, 25), 2) if is_cc else round(max(abs(bal) * 0.01, 50), 2)

            all_debts.append({
                "name":           a.get("name"),
                "balance":        round(abs(bal), 2),
                "type":           debt_type,
                "assumed_apr":    apr,
                "min_payment":    min_payment,
            })

    if not all_debts:
        return {
            "message": "No non-mortgage debt accounts found in Emoney.",
            "mortgages": mortgages,
            "total_mortgage_balance": round(sum(m["balance"] for m in mortgages), 2),
        }

    total_debt       = round(sum(d["balance"] for d in all_debts), 2)
    total_min        = round(sum(d["min_payment"] for d in all_debts), 2)
    monthly_budget   = round(total_min + extra_monthly_payment, 2)

    def _simulate_payoff(debts_ordered: list[dict]) -> dict:
        """
        Simulate debt payoff for a given ordering of debts.
        Returns months_to_payoff and total_interest_paid.
        """
        balances  = [d["balance"] for d in debts_ordered]
        rates     = [d["assumed_apr"] / 12 for d in debts_ordered]
        minimums  = [d["min_payment"] for d in debts_ordered]

        total_interest = 0.0
        months         = 0
        budget         = monthly_budget

        while any(b > 0 for b in balances) and months < 600:
            months    += 1
            freed      = 0.0

            # Apply interest to each active balance
            for i in range(len(balances)):
                if balances[i] <= 0:
                    continue
                interest        = balances[i] * rates[i]
                total_interest += interest
                balances[i]     = round(balances[i] + interest, 2)

            # Pay minimums on all but the target debt
            remaining_budget = budget
            for i in range(1, len(balances)):   # 0 = focus debt
                if balances[i] <= 0:
                    freed += minimums[i]
                    continue
                pay           = min(minimums[i], balances[i])
                balances[i]   = round(balances[i] - pay, 2)
                remaining_budget -= pay
                if balances[i] <= 0:
                    freed += minimums[i]

            # All available budget goes to focus debt
            focus_pay    = min(remaining_budget + freed, balances[0])
            balances[0]  = round(balances[0] - focus_pay, 2)

            # When focus debt is paid, shift remaining budget to next
            if balances[0] <= 0:
                balances.pop(0)
                rates.pop(0)
                minimums.pop(0)
                if not balances:
                    break

        return {
            "months_to_payoff":     months,
            "years_to_payoff":      round(months / 12, 1),
            "total_interest_paid":  round(total_interest, 2),
        }

    # Avalanche: highest APR first
    avalanche_order = sorted(all_debts, key=lambda d: d["assumed_apr"], reverse=True)
    avalanche       = _simulate_payoff(avalanche_order)

    # Snowball: smallest balance first
    snowball_order  = sorted(all_debts, key=lambda d: d["balance"])
    snowball        = _simulate_payoff(snowball_order)

    interest_saved  = round(snowball["total_interest_paid"] - avalanche["total_interest_paid"], 2)

    return {
        "total_debt":               total_debt,
        "debt_accounts":            all_debts,
        "mortgages_excluded":       mortgages,
        "total_minimum_payment":    total_min,
        "extra_monthly_payment":    extra_monthly_payment,
        "total_monthly_payment":    monthly_budget,
        "avalanche_strategy": {
            "description":          "Pay highest APR debt first (minimises total interest)",
            "payoff_order":         [d["name"] for d in avalanche_order],
            **avalanche,
        },
        "snowball_strategy": {
            "description":          "Pay smallest balance first (fastest early wins)",
            "payoff_order":         [d["name"] for d in snowball_order],
            **snowball,
        },
        "avalanche_saves_vs_snowball": max(0, interest_saved),
        "recommendation": (
            "Avalanche saves more interest; snowball provides faster motivational wins. "
            "Avalanche is mathematically optimal when rates differ significantly."
        ),
        "note": (
            f"APRs are assumed ({int(assumed_credit_card_apr*100)}% for credit cards, "
            f"{int(assumed_loan_apr*100)}% for loans). "
            "Update assumptions based on your actual statements for accurate projections. "
            "Minimum payments estimated as 2% of balance for credit cards."
        ),
    }


async def _build_account_type_map(http_session) -> dict[str, str]:
    """Return {account_name_lower: tax_bucket} from card 1."""
    accts = await get_accounts(http_session)
    mapping: dict[str, str] = {}
    for grp in accts.get("account_groups", []):
        for a in grp.get("accounts", []):
            name  = (a.get("name") or "").strip()
            atype = a.get("type") or ""
            bucket = _TAX_BUCKET.get(atype, "Unknown")
            mapping[name.lower()] = bucket
    return mapping


def _match_tax_bucket(account_name: str, type_map: dict[str, str]) -> str:
    """Fuzzy-match an account name from holdings to its tax bucket."""
    key = account_name.lower()
    if key in type_map:
        return type_map[key]
    # substring match
    for mapped_name, bucket in type_map.items():
        if mapped_name in key or key in mapped_name:
            return bucket
    return "Unknown"


async def get_debt_overview(
    http_session,
    assumed_mortgage_apr: float = 0.065,
    assumed_cc_apr: float = 0.22,
    assumed_auto_apr: float = 0.07,
    assumed_student_apr: float = 0.055,
) -> dict:
    """
    Consolidated view of all debts with estimated interest costs and payoff dates.

    Classifies every negative-balance account by debt type using keyword matching,
    then computes estimated monthly interest, annual interest cost, and approximate
    months to payoff assuming minimum payments equal to 1% of balance (or 1.5% for
    credit cards).

    Parameters
    ----------
    assumed_mortgage_apr  : assumed APR for mortgage/HELOC accounts (default 6.5%)
    assumed_cc_apr        : assumed APR for credit card accounts (default 22%)
    assumed_auto_apr      : assumed APR for auto loan accounts (default 7%)
    assumed_student_apr   : assumed APR for student loan accounts (default 5.5%)
    """
    from datetime import datetime, timedelta

    result = await get_accounts(http_session)
    if "error" in result:
        return result

    _DEBT_CLASSIFICATION = [
        ("mortgage",     {"mortgage", "heloc", "home equity", "home loan"},     assumed_mortgage_apr),
        ("credit_card",  {"credit", "visa", "mastercard", "amex", "discover",
                          "card", "citi", "chase", "capital one"},              assumed_cc_apr),
        ("auto",         {"auto", "car", "vehicle", "truck"},                   assumed_auto_apr),
        ("student",      {"student", "sallie", "navient", "great lakes",
                          "nelnet", "fedloan"},                                  assumed_student_apr),
    ]

    debts = []
    now = datetime.now()

    for grp in result.get("account_groups", []):
        for acct in grp.get("accounts", []):
            bal = acct.get("balance") or 0
            if bal >= 0:
                continue

            balance = abs(bal)
            name_lower = (acct.get("name") or "").lower()

            # Classify
            debt_type = "other"
            apr = assumed_mortgage_apr  # conservative default for unknown
            for dtype, keywords, rate in _DEBT_CLASSIFICATION:
                if any(kw in name_lower for kw in keywords):
                    debt_type = dtype
                    apr = rate
                    break

            monthly_rate     = apr / 12
            monthly_interest = round(balance * monthly_rate, 2)
            annual_interest  = round(monthly_interest * 12, 2)

            # Minimum payment estimate: credit cards 2% of balance, others 1%
            min_payment_pct  = 0.02 if debt_type == "credit_card" else 0.01
            min_payment      = max(round(balance * min_payment_pct, 2), 25.0)

            # Months to payoff (assuming fixed minimum payment — approximate)
            payoff_months = None
            payoff_date   = None
            if min_payment > monthly_interest and balance > 0:
                # n = -log(1 - r*PV/PMT) / log(1+r)
                try:
                    import math
                    n = -math.log(1 - monthly_rate * balance / min_payment) / math.log(1 + monthly_rate)
                    payoff_months = round(n)
                    payoff_date   = (now + timedelta(days=payoff_months * 30.44)).strftime("%Y-%m")
                except Exception:
                    pass

            debts.append({
                "name":                  acct.get("name"),
                "type":                  debt_type,
                "balance":               round(balance, 2),
                "assumed_apr_pct":       round(apr * 100, 2),
                "est_monthly_interest":  monthly_interest,
                "est_annual_interest":   annual_interest,
                "est_min_payment":       min_payment,
                "est_payoff_months":     payoff_months,
                "est_payoff_date":       payoff_date,
            })

    debts.sort(key=lambda d: d["balance"], reverse=True)

    total_balance         = round(sum(d["balance"] for d in debts), 2)
    total_monthly_interest = round(sum(d["est_monthly_interest"] for d in debts), 2)
    total_annual_interest  = round(total_monthly_interest * 12, 2)
    total_assets           = result.get("total_assets") or 0
    debt_to_assets_pct     = round(total_balance / total_assets * 100, 1) if total_assets > 0 else None

    by_type: dict[str, dict] = {}
    for d in debts:
        t = d["type"]
        if t not in by_type:
            by_type[t] = {"count": 0, "total_balance": 0.0, "total_annual_interest": 0.0}
        by_type[t]["count"]                += 1
        by_type[t]["total_balance"]        = round(by_type[t]["total_balance"] + d["balance"], 2)
        by_type[t]["total_annual_interest"] = round(by_type[t]["total_annual_interest"] + d["est_annual_interest"], 2)

    return {
        "as_of":  datetime.now().strftime("%Y-%m-%d"),
        "debts":  debts,
        "summary": {
            "total_debt":               total_balance,
            "total_monthly_interest":   total_monthly_interest,
            "total_annual_interest":    total_annual_interest,
            "debt_to_assets_pct":       debt_to_assets_pct,
            "debt_count":               len(debts),
        },
        "by_type":  by_type,
        "note": (
            "APRs are estimated by account type — Emoney does not expose actual interest rates. "
            "Minimum payments are estimated at 1-2% of balance. "
            "For accurate payoff planning, use get_debt_payoff_plan with actual rates."
        ),
    }


# ---------------------------------------------------------------------------
# Profile / identity data
# ---------------------------------------------------------------------------

async def get_client_profile(http_session) -> dict:
    """
    Return household profile: names, dates of birth, family members, and
    property facts from Emoney's Profile controller.

    Source: GET /ema/CS/Profile/GetProfileData — returns JSON with Clients
    (primary + spouse), People (dependents), and Property entries.

    Use the returned ``date_of_birth`` and ``age`` fields to auto-populate
    the ``age``, ``birth_year``, and ``current_age`` parameters required by
    tax and retirement planning tools.
    """
    from datetime import datetime

    from ._helpers import BASE_URL
    http = await http_session.get_http()
    resp = await http.get(
        f"{BASE_URL}/ema/CS/Profile/GetProfileData",
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        timeout=20,
    )
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"Profile/GetProfileData returned {resp.status_code}. Session may have expired."}

    data = resp.json()
    today = datetime.now()

    def _parse_person(raw: dict, is_spouse: bool = False) -> dict:
        dob_str = raw.get("DateOfBirth") or ""
        dob = None
        age  = None
        birth_year = None
        if dob_str:
            try:
                dob_dt = datetime.strptime(dob_str.split(" ")[0], "%m/%d/%Y")
                dob    = dob_dt.strftime("%Y-%m-%d")
                age    = today.year - dob_dt.year - (
                    (today.month, today.day) < (dob_dt.month, dob_dt.day)
                )
                birth_year = dob_dt.year
            except ValueError:
                pass
        return {
            "name":       raw.get("Name", ""),
            "is_spouse":  is_spouse,
            "date_of_birth": dob,
            "age":        age,
            "birth_year": birth_year,
            "email":      raw.get("EmailAddress") or None,
        }

    clients = []
    for c in data.get("Clients", []):
        clients.append(_parse_person(c, is_spouse=bool(c.get("IsSpouse"))))

    dependents = []
    for p in (data.get("People") or {}).get("People", []):
        dep = _parse_person(p)
        dep["is_spouse"] = False
        dep["is_dependent"] = True
        dependents.append(dep)

    properties = []
    for prop in (data.get("Property") or {}).get("Properties", []):
        properties.append({
            "name":    prop.get("Name", ""),
            "fact_id": prop.get("FactID"),
        })

    # Convenience: primary and spouse objects
    primary = next((c for c in clients if not c["is_spouse"]), None)
    spouse  = next((c for c in clients if c["is_spouse"]), None)

    return {
        "as_of":      today.strftime("%Y-%m-%d"),
        "primary":    primary,
        "spouse":     spouse,
        "dependents": dependents,
        "properties": properties,
        "household_size": len(clients) + len(dependents),
        "note": (
            "Use primary.age and primary.birth_year as inputs to tax/retirement tools. "
            "Profile data reflects the last advisor-confirmed sync, not real-time."
        ),
    }


# ---------------------------------------------------------------------------
# Account aggregation / connection health
# ---------------------------------------------------------------------------

async def get_aggregation_status(http_session) -> dict:
    """
    Return the health and freshness status of all linked account aggregations.

    Source: CardSwitcher Card 20 — returns BrokenConnections and Accounts with
    their last-sync timestamps. Useful for diagnosing stale balances or data gaps.

    A broken connection means that account's data may be out of date until
    re-authenticated in the Emoney portal.
    """
    from datetime import datetime

    http = await http_session.get_http()
    card20 = await _get_card(http, 20)
    if card20 is None:
        return {"error": "Card 20 (aggregation status) unavailable. Session may have expired."}

    broken_connections = []
    for conn in card20.get("BrokenConnections") or []:
        broken_connections.append({
            "institution":   conn.get("Name") or conn.get("InstitutionName", "Unknown"),
            "status":        conn.get("ConnectionStatusName", ""),
            "level":         conn.get("ConnectionStatusLevel", ""),
            "description":   conn.get("ConnectionStatusDescription", ""),
            "connection_id": (conn.get("ConnectionID") or {}).get("Value"),
        })

    accounts_status = []
    for acct in card20.get("Accounts") or []:
        accounts_status.append({
            "name":           acct.get("Name") or acct.get("AccountName", ""),
            "institution":    acct.get("InstitutionName", ""),
            "last_updated":   (acct.get("AsOfDate") or "")[:10] or None,
            "connection_ok":  acct.get("IsConnected", True),
        })

    healthy   = len(broken_connections) == 0
    today_str = datetime.now().strftime("%Y-%m-%d")

    return {
        "as_of":             today_str,
        "overall_status":    "healthy" if healthy else "attention_needed",
        "broken_count":      len(broken_connections),
        "broken_connections": broken_connections,
        "accounts_monitored": len(accounts_status),
        "accounts":          accounts_status,
        "note": (
            "Broken connections indicate accounts that cannot refresh automatically. "
            "Re-authenticate the affected institution in your Emoney portal to restore data flow."
        ),
    }
