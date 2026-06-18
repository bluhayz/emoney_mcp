"""
Transaction write operations and rules engine.

Two backends are in play:

1. **SNB API** (``api.emoneyadvisor.com/snb-api/api/values/*``) — the MODERN
   path the live web UI uses. JSON body, Bearer JWT + ``apikey`` auth (same
   credentials as the SNB *read* tools, via ``_get_snb_credentials``). Verified
   live (network capture of the official portal, 2026-06-18). Endpoints:
     UpdateTransaction          {transactionId, categoryId, userDescription, notes}
     GetBankTransactionRules    (GET) → [{ruleID, categoryID, descriptionContains,
                                          userDescription, minAmount, maxAmount,
                                          startDay, endDay, extensionData}, ...]
     CreateRule / UpdateRule    {Rule:{...ruleObj}, TransactionID}   (POST)
   (There is no standalone ApplyRule on SNB — application is folded into
   Create/UpdateRule via the TransactionID field.)

2. **Legacy ``/ema/CS/Spending/*``** (ASP.NET anti-forgery POST via
   ``_csrf_post``) — the original reverse-engineered path. This backend is now
   served by the "Nexus" subsystem which returns ``IsNexusAvailable:false`` /
   maintenance for writes, i.e. it is *retired*, not temporarily down — retrying
   never succeeds. Tools still on this path (apply rule, delete rule, hide,
   splits) are pending migration to the SNB API; their SNB endpoints have not
   yet been captured, so they remain here until they are.

Legacy payload shapes (kept for the not-yet-migrated tools):
  UpdateTransactionHiddenStatus  {transactionID:{Value}, isHidden}
  GetAllBankTransactionSplits    {transactionID:{Value}}
  UpdateTransactionSplits    {transactionSplits:[{TransactionSplitID,CategoryID:{Value},SplitAmount,UserDescription},...]}
"""

import logging
import os

from ._helpers import BASE_URL, _SNB_API

_SPENDING = f"{BASE_URL}/ema/CS/Spending"
_log = logging.getLogger("emoney_mcp.scrapers.transactions")


async def _snb_post(http_session, action: str, payload: dict) -> dict:
    """POST JSON to the SNB API (``/snb-api/api/values/<action>``).

    The modern write path — Bearer JWT + ``apikey`` (the same credentials the
    SNB read tools scrape). Returns ``{"ok": True, ...}`` on success, or an
    ``{"error": ...}`` dict (never raises, per the scraper convention).
    """
    # Imported lazily to avoid a circular import at module load (spending imports
    # nothing from here, but keep the dependency one-directional and explicit).
    from .spending import _get_snb_credentials, _snb_headers
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return {"error": f"Could not retrieve SNB credentials for {action} — "
                         "session may be stale (try sync_chrome_session)."}
    http = await http_session.get_http()
    headers = {**_snb_headers(jwt_token, api_key), "Content-Type": "application/json"}
    resp = await http.post(f"{_SNB_API}/api/values/{action}", json=payload,
                           headers=headers, timeout=20)
    if resp.status_code not in (200, 201, 204):
        try:
            body_snippet = resp.text[:400]
        except Exception:
            body_snippet = ""
        return {"error": f"{action} returned HTTP {resp.status_code}",
                "response_body": body_snippet}
    out: dict = {"ok": True}
    if resp.status_code != 204 and "json" in resp.headers.get("content-type", ""):
        try:
            out["data"] = resp.json()
        except Exception:
            pass
    return out


async def _snb_get(http_session, action: str) -> dict:
    """GET JSON from the SNB API (``/snb-api/api/values/<action>``)."""
    from .spending import _get_snb_credentials, _snb_headers
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return {"error": f"Could not retrieve SNB credentials for {action} — "
                         "session may be stale (try sync_chrome_session)."}
    http = await http_session.get_http()
    resp = await http.get(f"{_SNB_API}/api/values/{action}",
                          headers=_snb_headers(jwt_token, api_key), timeout=20)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"{action} returned HTTP {resp.status_code}"}
    return {"ok": True, "data": resp.json()}

# Allowlist of fields accepted per split when building the UpdateTransactionSplits
# form body — so caller-supplied dict keys can't smuggle arbitrary form fields
# into the Emoney write request.
_ALLOWED_SPLIT_KEYS = {"TransactionSplitID", "CategoryID", "SplitAmount", "UserDescription"}
_ALLOWED_SPLIT_SUBKEYS = {"Value", "IsValid"}


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

    # The SNB UpdateTransaction replaces the whole object, so the live web UI
    # always sends the full {transactionId, categoryId, userDescription, notes}.
    # Look up the transaction's CURRENT values from the SNB read cache and merge
    # the requested change over them — otherwise a category-only update would
    # null out the user description (and vice-versa).
    from .spending import _fetch_snb_raw
    cur_cat = cur_desc = cur_notes = None
    ok, txns, _ = await _fetch_snb_raw(http_session)
    if ok:
        match = next((t for t in txns if str(t.get("id")) == str(transaction_id)), None)
        if match:
            cur_cat = match.get("categoryId")
            cur_desc = match.get("userDescription")
            cur_notes = match.get("notes")

    payload = {
        "transactionId": str(transaction_id),
        "categoryId": str(category_id) if category_id is not None
                      else (str(cur_cat) if cur_cat is not None else None),
        "userDescription": description if description is not None else cur_desc,
        "notes": cur_notes,
    }

    result = await _snb_post(http_session, "UpdateTransaction", payload)
    if "error" in result:
        return result
    return {
        "success": True,
        "transaction_id": str(transaction_id),
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
            if key not in _ALLOWED_SPLIT_KEYS:
                _log.debug("Ignoring unexpected transaction-split field: %r", key)
                continue
            if isinstance(val, dict):
                for subkey, subval in val.items():
                    if subkey not in _ALLOWED_SPLIT_SUBKEYS:
                        _log.debug("Ignoring unexpected split subfield: %s[%r]", key, subkey)
                        continue
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

    Reads the SNB API (``GetBankTransactionRules``) — the live web UI's source.
    (The legacy ``/ema/CS/Spending/GetRules`` Nexus endpoint is dead and reports
    zero rules even when rules exist.)
    """
    result = await _snb_get(http_session, "GetBankTransactionRules")
    if "error" in result:
        return result

    data = result.get("data")
    # SNB returns a bare list; tolerate a wrapped {Rules:[...]} just in case.
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("Rules") or data.get("rules") or list(data.values())
    else:
        items = []

    rules = []
    for rule in items:
        if not isinstance(rule, dict):
            continue
        rules.append({
            "rule_id":              rule.get("ruleID"),
            "description_contains": rule.get("descriptionContains"),
            "category_id":          rule.get("categoryID"),
            "user_description":     rule.get("userDescription"),
            "min_amount":           rule.get("minAmount"),
            "max_amount":           rule.get("maxAmount"),
            "start_day":            rule.get("startDay"),
            "end_day":              rule.get("endDay"),
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

    Posts to the SNB API (``CreateRule`` — ``{Rule:{...}, TransactionID}``), the
    live web UI's path. The ``Rule`` object mirrors the flat camelCase shape
    ``GetBankTransactionRules`` returns. (The legacy ``/ema/CS/Spending/AddRule``
    Nexus endpoint is retired and returns ``IsNexusAvailable:false``.)
    """
    rule_obj: dict = {
        "ruleID": None,
        "categoryID": str(category_id),
        "descriptionContains": description_contains,
        "userDescription": user_description or description_contains,
        "minAmount": min_amount,
        "maxAmount": max_amount,
        "startDay": None,
        "endDay": None,
    }
    payload: dict = {"Rule": rule_obj}
    if transaction_id:
        payload["TransactionID"] = str(transaction_id)

    result = await _snb_post(http_session, "CreateRule", payload)
    if "error" in result:
        return result
    return _maybe_raw({
        "success": True,
        "description_contains": description_contains,
        "category_id": category_id,
    }, result.get("data"))


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

    Posts to the SNB API (``UpdateRule`` — ``{Rule:{...}, TransactionID}``). The
    full ``Rule`` object is sent (the modern endpoint replaces the whole rule),
    so unspecified fields are carried over from the current rule.
    """
    # Fetch current rules to find this one
    rules_result = await get_transaction_rules(http_session)
    if "error" in rules_result:
        return rules_result
    existing = next((r for r in rules_result["rules"] if str(r["rule_id"]) == str(rule_id)), None)
    if not existing:
        return {"error": f"Rule {rule_id} not found. Call get_transaction_rules to see available rules."}

    # Merge requested changes over the existing rule and send the whole object.
    rule_obj = {
        "ruleID": str(rule_id),
        "categoryID": str(category_id) if category_id is not None else str(existing["category_id"] or ""),
        "descriptionContains": description_contains if description_contains is not None else (existing["description_contains"] or ""),
        "userDescription": user_description if user_description is not None else (existing["user_description"] or ""),
        "minAmount": min_amount if min_amount is not None else existing.get("min_amount"),
        "maxAmount": max_amount if max_amount is not None else existing.get("max_amount"),
        "startDay": existing.get("start_day"),
        "endDay": existing.get("end_day"),
    }
    payload: dict = {"Rule": rule_obj}
    if transaction_id:
        payload["TransactionID"] = str(transaction_id)

    result = await _snb_post(http_session, "UpdateRule", payload)
    if "error" in result:
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


async def delete_transaction_rule(
    http_session,
    rule_id: str,
) -> dict:
    """
    Delete an auto-categorization rule.

    JS signature: RemoveRule(ruleID) — POST /ema/CS/Spending/RemoveRule.

    rule_id — the rule ID (from get_transaction_rules)

    Existing transactions categorized by the rule keep their categories;
    only future auto-categorization stops.
    """
    if rule_id is None or str(rule_id).strip() == "":
        return {"error": "rule_id is required."}

    result = await _csrf_post(http_session, "RemoveRule", {"ruleID": str(rule_id)})
    if isinstance(result, dict) and "error" in result:
        return result
    return _maybe_raw({
        "success": True,
        "rule_id": rule_id,
        "deleted": True,
    }, result)
