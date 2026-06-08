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

from ._helpers import _get_card, _CARD_URL

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
    import time
    ts = int(time.time() * 1000)
    http = await http_session.get_http()

    r9 = await http.get(f"{_CARD_URL}/9?_={ts}", timeout=20)
    if r9.status_code != 200 or "json" not in r9.headers.get("content-type", ""):
        return {"error": f"Card 9 returned {r9.status_code}. Session may have expired — call reset_session."}

    nw_data = r9.json().get("Data", {})
    net_worth    = nw_data.get("NetWorth")
    total_assets = nw_data.get("Assets")
    total_liab   = nw_data.get("Liabilities")

    r1 = await http.get(f"{_CARD_URL}/1?_={ts}", timeout=20)
    if r1.status_code != 200 or "json" not in r1.headers.get("content-type", ""):
        return {
            "net_worth": net_worth,
            "total_assets": total_assets,
            "total_liabilities": total_liab,
            "error": f"Card 1 (accounts) returned {r1.status_code}",
        }

    card1 = r1.json().get("Data", {})
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
