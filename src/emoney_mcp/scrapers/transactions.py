"""
Transaction write operations and rules engine.

All write endpoints live on the main emaplan.com host under /ema/CS/Spending/
and require the ASP.NET anti-forgery token sent as __RequestVerificationToken
in the POST body (same pattern as the jQuery AJAX calls in the browser).

Read operations (GetRules, GetAllBankTransactionSplits) also use POST with
the token in the body to stay consistent.

Payload shapes (reverse-engineered from Capstone.Spending.Transactions.js):
  UpdateTransaction          {TransactionID:{Value}, UserDescription, CategoryID:{Value}}
  UpdateTransactionHiddenStatus  {transactionID:{Value}, isHidden}
  GetAllBankTransactionSplits    {transactionID:{Value}}
  UpdateTransactionSplits    {transactionSplits:[{TransactionSplitID,CategoryID:{Value},SplitAmount,UserDescription},...]}
  AddRule / UpdateRule       {rule:{...ruleObj}, transactionID}
  ApplyRule                  {ruleID, transactionID}
  GetTransactionsByRuleID    {ruleID, filter}
  GetRules                   {} (empty — just CSRF token)

Rule object shape:
  {RuleID:{Value,IsValid}, DescriptionContains, MinAmount, MaxAmount,
   StartDay, EndDay, UserDescription, CategoryID:{Value,IsValid}}
"""

import os

from ._helpers import BASE_URL

_SPENDING = f"{BASE_URL}/ema/CS/Spending"


def _maybe_raw(out: dict, raw) -> dict:
    """Attach the unprocessed API response only when EMONEY_DEV is set.

    Keeps normal tool output clean and small; exposes the raw Emoney payload
    for debugging when developing against the live API.
    """
    if os.environ.get("EMONEY_DEV"):
        out["raw"] = raw
    return out


async def _csrf_post(http_session, path: str, data: dict) -> dict | list:
    """POST to a Spending endpoint with CSRF token in body."""
    http = await http_session.get_http()
    token = await http_session.get_csrf_token()
    if not token:
        return {"error": f"Could not obtain CSRF token for {path} — "
                         "Emoney page layout may have changed or the session expired."}
    payload = {**data, "__RequestVerificationToken": token}
    resp = await http.post(
        f"{_SPENDING}/{path}",
        data=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        try:
            body_snippet = resp.text[:400]
        except Exception:
            body_snippet = ""
        return {"error": f"{path} returned HTTP {resp.status_code}", "response_body": body_snippet}
    ct = resp.headers.get("content-type", "")
    if "json" not in ct:
        return {"error": f"{path} returned non-JSON response — session may be stale"}
    return resp.json()


# ---------------------------------------------------------------------------
# Transaction updates
# ---------------------------------------------------------------------------

async def update_transaction(
    http_session,
    transaction_id: str,
    category_id: str | None = None,
    description: str | None = None,
) -> dict:
    """
    Update a transaction's category and/or user-visible description.

    transaction_id — the SNB transaction ID (string, e.g. "13221007043")
    category_id    — numeric category ID as a string (get IDs from get_spending_transactions)
    description    — user-facing description override (empty string clears it)

    At least one of category_id or description must be provided.
    """
    if category_id is None and description is None:
        return {"error": "Provide at least one of category_id or description."}

    data: dict = {"TransactionID[Value]": transaction_id}
    if description is not None:
        data["UserDescription"] = description
    if category_id is not None:
        data["CategoryID[Value]"] = str(category_id)

    result = await _csrf_post(http_session, "UpdateTransaction", data)
    if isinstance(result, dict) and "error" in result:
        return result
    return {
        "success": True,
        "transaction_id": transaction_id,
        "updated": {k: v for k, v in
                    [("category_id", category_id), ("description", description)]
                    if v is not None},
    }


async def hide_transaction(
    http_session,
    transaction_id: str,
    hidden: bool = True,
) -> dict:
    """
    Hide or un-hide a transaction from spending views.

    transaction_id — the SNB transaction ID
    hidden         — True to hide, False to un-hide
    """
    result = await _csrf_post(http_session, "UpdateTransactionHiddenStatus", {
        "transactionID[Value]": transaction_id,
        "isHidden": "true" if hidden else "false",
    })
    if isinstance(result, dict) and "error" in result:
        return result
    return {
        "success": True,
        "transaction_id": transaction_id,
        "hidden": hidden,
    }


async def get_transaction_splits(http_session, transaction_id: str) -> dict:
    """
    Return all splits for a transaction. Returns the existing splits plus
    the total amount and whether the transaction is fully allocated.

    transaction_id — the SNB transaction ID
    """
    result = await _csrf_post(http_session, "GetAllBankTransactionSplits", {
        "transactionID[Value]": transaction_id,
    })
    if isinstance(result, dict) and "error" in result:
        return result

    # API returns a list of split objects directly; occasionally wrapped in a dict
    items: list = (
        result if isinstance(result, list)
        else result.get("Splits") or result.get("splits") or []
        if isinstance(result, dict) else []
    )

    splits = []
    total = 0.0
    for item in items:
        cat_raw = item.get("CategoryID")
        cat_id = cat_raw.get("Value") if isinstance(cat_raw, dict) else cat_raw
        amount = float(item.get("SplitAmount") or 0)
        total += amount
        splits.append({
            "category_id": cat_id,
            "description": item.get("UserDescription") or item.get("CleanDescription") or item.get("Description"),
            "original_description": item.get("Description"),
            "amount": amount,
        })

    return {
        "transaction_id": transaction_id,
        "split_count": len(splits),
        "is_split": len(splits) > 1,
        "splits": splits,
        "total_amount": round(total, 2),
    }


async def update_transaction_splits(
    http_session,
    transaction_splits: list[dict],
) -> dict:
    """
    Replace all splits on a transaction.

    transaction_splits — list of split dicts, each with:
      {
        "TransactionSplitID": "...",   (omit or null for new splits)
        "CategoryID": {"Value": "65"},
        "SplitAmount": 25.00,
        "UserDescription": "optional label"
      }
    Amounts must sum to the transaction total. Negative amounts for expenses.
    """
    # Emoney expects a flat form-encoded representation of the array.
    # jQuery serializes [{...},{...}] as transactionSplits[0][Field]=..., etc.
    flat: dict = {}
    for i, split in enumerate(transaction_splits):
        for key, val in split.items():
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    flat[f"transactionSplits[{i}][{key}][{subkey}]"] = str(subval) if subval is not None else ""
            else:
                flat[f"transactionSplits[{i}][{key}]"] = str(val) if val is not None else ""

    result = await _csrf_post(http_session, "UpdateTransactionSplits", flat)
    if isinstance(result, dict) and "error" in result:
        return result
    return {
        "success": True,
        "splits_updated": len(transaction_splits),
    }


# ---------------------------------------------------------------------------
# Rules engine
# ---------------------------------------------------------------------------

async def get_transaction_rules(http_session) -> dict:
    """
    Return all saved auto-categorization rules as a list.

    Each rule has: rule_id, description_contains, category_id, user_description,
    min_amount, max_amount, start_day, end_day.
    """
    # JS sends data:{} — empty payload (just the CSRF token from _csrf_post)
    result = await _csrf_post(http_session, "GetRules", {})
    if isinstance(result, dict) and "error" in result:
        body = result.get("response_body", "") or ""
        body_lower = body.lower()
        # A Nexus maintenance-window 500 (IsNexusAvailable:false / "unavailable due
        # to maintenance") must surface as a real error — not be mistaken for
        # "no rules configured" by the empty-body heuristic below.
        if "isnexusavailable" in body_lower or "maintenance" in body_lower:
            return result
        # Emoney genuinely returns HTTP 500 with an empty/generic body when no
        # rules exist — treat that (and only that) as an empty rule set.
        if "500" in result.get("error", "") and (
            "unexpected error" in body_lower or not body
        ):
            return {"rules": [], "count": 0, "note": "No categorization rules configured."}
        return result

    # API may return a list [{...}, ...] OR a dict {rule_id: {...}, ...}
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = list(result.values())
    else:
        return {"error": f"Unexpected response type from GetRules: {type(result).__name__}"}

    rules = []
    for rule in items:
        if not isinstance(rule, dict):
            continue
        # RuleID and CategoryID may be wrapped {Value, IsValid} or plain scalars
        rule_id_raw = rule.get("RuleID")
        cat_id_raw  = rule.get("CategoryID")
        rules.append({
            "rule_id":              rule_id_raw.get("Value") if isinstance(rule_id_raw, dict) else rule_id_raw,
            "description_contains": rule.get("DescriptionContains"),
            "category_id":          cat_id_raw.get("Value")  if isinstance(cat_id_raw,  dict) else cat_id_raw,
            "user_description":     rule.get("UserDescription"),
            "min_amount":           rule.get("MinAmount"),
            "max_amount":           rule.get("MaxAmount"),
            "start_day":            rule.get("StartDay"),
            "end_day":              rule.get("EndDay"),
        })
    return {"rules": rules, "count": len(rules)}


async def add_transaction_rule(
    http_session,
    description_contains: str,
    category_id: str,
    user_description: str | None = None,
    transaction_id: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
) -> dict:
    """
    Create a new auto-categorization rule.

    description_contains — substring that must appear in the transaction description
    category_id          — numeric category ID to assign (string)
    user_description     — optional display name for the rule (defaults to description_contains)
    transaction_id       — optional SNB transaction ID that triggered this rule
    min_amount / max_amount — optional amount range filter
    """
    rule: dict = {
        "RuleID[Value]": "",
        "RuleID[IsValid]": "false",
        "DescriptionContains": description_contains,
        "CategoryID[Value]": str(category_id),
        "CategoryID[IsValid]": "true",
        "UserDescription": user_description or description_contains,
    }
    if min_amount is not None:
        rule["MinAmount"] = str(min_amount)
    if max_amount is not None:
        rule["MaxAmount"] = str(max_amount)

    # jQuery encodes {rule: {RuleID: {Value: ""}}} as rule[RuleID][Value]=...
    flat: dict = {f"rule[{k}]": v for k, v in rule.items()}
    if transaction_id:
        flat["transactionID"] = transaction_id

    result = await _csrf_post(http_session, "AddRule", flat)
    if isinstance(result, dict) and "error" in result:
        return result
    return _maybe_raw({
        "success": True,
        "description_contains": description_contains,
        "category_id": category_id,
    }, result)


async def update_transaction_rule(
    http_session,
    rule_id: str,
    description_contains: str | None = None,
    category_id: str | None = None,
    user_description: str | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    transaction_id: str | None = None,
) -> dict:
    """
    Update an existing rule. Fetches the current rule first, then applies changes.

    rule_id — the rule ID (from get_transaction_rules)
    All other parameters are optional — only provided fields are changed.
    """
    # Fetch current rules to find this one
    rules_result = await get_transaction_rules(http_session)
    if "error" in rules_result:
        return rules_result
    existing = next((r for r in rules_result["rules"] if str(r["rule_id"]) == str(rule_id)), None)
    if not existing:
        return {"error": f"Rule {rule_id} not found. Call get_transaction_rules to see available rules."}

    # Merge changes
    merged = {
        "RuleID[Value]": str(rule_id),
        "RuleID[IsValid]": "true",
        "DescriptionContains": description_contains if description_contains is not None else (existing["description_contains"] or ""),
        "CategoryID[Value]": str(category_id) if category_id is not None else str(existing["category_id"] or ""),
        "CategoryID[IsValid]": "true",
        "UserDescription": user_description if user_description is not None else (existing["user_description"] or ""),
    }
    if min_amount is not None:
        merged["MinAmount"] = str(min_amount)
    elif existing["min_amount"] is not None:
        merged["MinAmount"] = str(existing["min_amount"])
    if max_amount is not None:
        merged["MaxAmount"] = str(max_amount)
    elif existing["max_amount"] is not None:
        merged["MaxAmount"] = str(existing["max_amount"])

    flat: dict = {f"rule[{k}]": v for k, v in merged.items()}
    if transaction_id:
        flat["transactionID"] = transaction_id

    result = await _csrf_post(http_session, "UpdateRule", flat)
    if isinstance(result, dict) and "error" in result:
        return result
    return {
        "success": True,
        "rule_id": rule_id,
        "updated": {k: v for k, v in [
            ("description_contains", description_contains),
            ("category_id", category_id),
            ("user_description", user_description),
        ] if v is not None},
    }


async def apply_transaction_rule(
    http_session,
    rule_id: str,
    transaction_id: str | None = None,
) -> dict:
    """
    Apply an existing rule to all matching transactions.

    JS signature: ApplyRule({ruleID, transactionID})

    rule_id        — the rule ID (from get_transaction_rules)
    transaction_id — optional: scope to a specific transaction
    """
    data: dict = {"ruleID": str(rule_id)}
    if transaction_id:
        data["transactionID"] = transaction_id

    result = await _csrf_post(http_session, "ApplyRule", data)
    if isinstance(result, dict) and "error" in result:
        return result
    return _maybe_raw({
        "success": True,
        "rule_id": rule_id,
    }, result)
