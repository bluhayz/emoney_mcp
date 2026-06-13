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
  ApplyRule                  {rule:{...ruleObj}, transactionID}
  GetTransactionsByRuleID    {ruleID, filter}

Rule object shape:
  {RuleID:{Value,IsValid}, DescriptionContains, MinAmount, MaxAmount,
   StartDay, EndDay, UserDescription, CategoryID:{Value,IsValid}}
"""

from ._helpers import BASE_URL

_SPENDING = f"{BASE_URL}/ema/CS/Spending"


async def _csrf_post(http_session, path: str, data: dict) -> dict | list:
    """POST to a Spending endpoint with CSRF token in body."""
    http = await http_session.get_http()
    token = await http_session.get_csrf_token()
    payload = {**data, "__RequestVerificationToken": token}
    resp = await http.post(
        f"{_SPENDING}/{path}",
        data=payload,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        return {"error": f"{path} returned HTTP {resp.status_code}"}
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
    # Normalize the response
    if isinstance(result, dict):
        splits = result.get("Splits") or result.get("splits") or []
        total = result.get("Total") or result.get("total")
        return {
            "transaction_id": transaction_id,
            "splits": splits,
            "total": total,
            "raw": result,
        }
    return {"transaction_id": transaction_id, "raw": result}


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
    import json
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
    result = await _csrf_post(http_session, "GetRules", {})
    if isinstance(result, dict) and "error" in result:
        return result
    if not isinstance(result, dict):
        return {"error": f"Unexpected response type: {type(result).__name__}"}

    rules = []
    for rule_id, rule in result.items():
        rules.append({
            "rule_id": rule.get("RuleID", {}).get("Value"),
            "description_contains": rule.get("DescriptionContains"),
            "category_id": rule.get("CategoryID", {}).get("Value"),
            "user_description": rule.get("UserDescription"),
            "min_amount": rule.get("MinAmount"),
            "max_amount": rule.get("MaxAmount"),
            "start_day": rule.get("StartDay"),
            "end_day": rule.get("EndDay"),
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

    # Flatten rule into rule[Field] form that jQuery would produce
    flat: dict = {}
    for k, v in rule.items():
        flat[f"rule[{k.replace('[', '.').replace(']', '')}]"] = v
    if transaction_id:
        flat["transactionID"] = transaction_id

    # Correct jQuery-style nested form: rule[RuleID][Value] etc.
    flat2: dict = {}
    for k, v in rule.items():
        # k is like "RuleID[Value]" → want "rule[RuleID][Value]"
        flat2[f"rule[{k}]"] = v
    if transaction_id:
        flat2["transactionID"] = transaction_id

    result = await _csrf_post(http_session, "AddRule", flat2)
    if isinstance(result, dict) and "error" in result:
        return result
    return {
        "success": True,
        "description_contains": description_contains,
        "category_id": category_id,
        "raw": result,
    }


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

    rule_id        — the rule ID (from get_transaction_rules)
    transaction_id — optional: apply to a specific transaction only
    """
    rules_result = await get_transaction_rules(http_session)
    if "error" in rules_result:
        return rules_result
    existing = next((r for r in rules_result["rules"] if str(r["rule_id"]) == str(rule_id)), None)
    if not existing:
        return {"error": f"Rule {rule_id} not found. Call get_transaction_rules to see available rules."}

    rule_obj = {
        "RuleID[Value]": str(rule_id),
        "RuleID[IsValid]": "true",
        "DescriptionContains": existing["description_contains"] or "",
        "CategoryID[Value]": str(existing["category_id"] or ""),
        "CategoryID[IsValid]": "true",
        "UserDescription": existing["user_description"] or "",
    }
    flat: dict = {f"rule[{k}]": v for k, v in rule_obj.items()}
    if transaction_id:
        flat["transactionID"] = transaction_id

    result = await _csrf_post(http_session, "ApplyRule", flat)
    if isinstance(result, dict) and "error" in result:
        return result
    return {
        "success": True,
        "rule_id": rule_id,
        "description": f"Rule '{existing['description_contains']}' applied to matching transactions.",
        "raw": result,
    }
