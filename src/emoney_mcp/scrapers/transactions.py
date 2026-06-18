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

   **ID wrapping (verified live 2026-06-18, #121):** ``ruleID`` and ``categoryID``
   are NOT bare strings — they are WCF DataContract complex types serialized as
   ``{"extensionData": {}, "value": "123"}``. The GET response wraps them, and
   Create/UpdateRule *require* the same ``{"value": ...}`` shape on input: a flat
   string yields ``HTTP 400 — could not be converted to DataBankTransactnRuleId``.
   Use ``_unwrap_id`` when reading and ``_wrap_id`` when writing.

   Migrated to SNB and verified live 2026-06-18 (#121):
     hide_transaction       → SNB ``ToggleTransactionVisibility`` {hideTransaction, transactionId}
     get_transaction_splits → SNB ``GetBankTransactionSplits?transactionID=<id>``
     delete_transaction_rule→ SNB has NO single delete; the UI bulk-replaces the
                              whole collection via ``POST /ema/CS/Spending/SetRules``
                              {rules:[{RuleID:{Value},CategoryID:{Value},...}]} (this
                              one legacy CS/Spending route is LIVE, not Nexus-dead),
                              CSRF token in the ``__RequestVerificationToken`` header.

2. **Legacy ``/ema/CS/Spending/*``** (ASP.NET anti-forgery POST via
   ``_csrf_post``) — the original reverse-engineered path, mostly served by the
   retired "Nexus" subsystem (``IsNexusAvailable:false`` for writes — permanent,
   not temporary). ``SetRules`` is the exception: it is the live persist path for
   rule changes (see ``_csrf_post_json``). Still on the dead legacy path / pending
   migration: ``apply_transaction_rule`` and ``update_transaction_splits`` — the
   SNB ``UpdateTransactionSplits`` (POST) endpoint exists but its body was not yet
   captured.
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


def _unwrap_id(v):
    """Unwrap an SNB ID field.

    ``GetBankTransactionRules`` serializes ``ruleID``/``categoryID`` as a WCF
    DataContract complex type ``{"extensionData": {}, "value": "123"}`` — not a
    bare string. Return the inner value (tolerating ``Value``/``value`` casing
    and an already-flat value).
    """
    if isinstance(v, dict):
        return v.get("value", v.get("Value"))
    return v


def _wrap_id(v):
    """Wrap a scalar ID as the SNB ``DataBankTransactnRuleId`` shape.

    The rule/split write endpoints reject a flat string for ``RuleID``/
    ``CategoryID`` (HTTP 400/500); they require ``{"Value": "123"}`` — the exact
    shape the web UI sends. Verified live 2026-06-18 (#121). .NET binds the keys
    case-insensitively, so PascalCase ``Value`` matches the captured request.
    """
    return {"Value": str(v) if v is not None else None}


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

    Posts to the SNB API (``ToggleTransactionVisibility`` —
    ``{hideTransaction, transactionId}``), the live web UI's path, verified live
    in both directions 2026-06-18 (#121). (The legacy
    ``/ema/CS/Spending/UpdateTransactionHiddenStatus`` Nexus endpoint is retired.)
    """
    result = await _snb_post(http_session, "ToggleTransactionVisibility", {
        "hideTransaction": bool(hidden),
        "transactionId": str(transaction_id),
    })
    if "error" in result:
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

    Reads the SNB API (``GetBankTransactionSplits?transactionID=<id>``), the live
    web UI's source (verified live 2026-06-18, #121). Each split is a WCF object
    with wrapped ``categoryID``/``transactionID`` (``{value}``); a non-split
    transaction returns a single element covering the full amount. (The legacy
    ``/ema/CS/Spending/GetAllBankTransactionSplits`` Nexus endpoint is retired.)
    """
    from .spending import _get_snb_credentials, _snb_headers
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return {"error": "Could not retrieve SNB credentials for GetBankTransactionSplits — "
                         "session may be stale (try sync_chrome_session)."}
    http = await http_session.get_http()
    resp = await http.get(
        f"{_SNB_API}/api/values/GetBankTransactionSplits",
        params={"transactionID": str(transaction_id)},
        headers=_snb_headers(jwt_token, api_key), timeout=20,
    )
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"GetBankTransactionSplits returned HTTP {resp.status_code}"}
    data = resp.json()
    items: list = (
        data if isinstance(data, list)
        else (data.get("splits") or data.get("Splits") or []) if isinstance(data, dict)
        else []
    )

    splits = []
    total = 0.0
    for item in items:
        cat_id = _unwrap_id(item.get("categoryID") or item.get("CategoryID"))
        amount = float(item.get("splitAmount") or item.get("SplitAmount") or 0)
        total += amount
        splits.append({
            "category_id": cat_id,
            "description": (item.get("userDescription") or item.get("cleanDescription")
                           or item.get("description") or item.get("UserDescription")),
            "original_description": item.get("description") or item.get("Description"),
            "amount": round(amount, 2),
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

    NOTE (#121): this is the LAST write still on the dead legacy
    ``/ema/CS/Spending/UpdateTransactionSplits`` (Nexus) path. The SNB endpoint
    ``UpdateTransactionSplits`` (POST) is confirmed to exist, but its request
    body shape has not yet been captured (writing splits in the live UI mutates a
    real transaction, so capture was deferred). Until migrated, this tool may
    fail with a Nexus maintenance error. ``get_transaction_splits`` is already on
    SNB; its GET shape (wrapped ``{value}`` ids) is the likely body template.
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
            "rule_id":              _unwrap_id(rule.get("ruleID")),
            "description_contains": rule.get("descriptionContains"),
            "category_id":          _unwrap_id(rule.get("categoryID")),
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
    live web UI's path, verified live 2026-06-18 (#121). The ``Rule`` object
    MUST omit ``RuleID`` on create — sending ``RuleID:{Value:null}`` causes an
    HTTP 500. ``CategoryID`` is wrapped ``{Value}``; ``TransactionID`` is optional.
    (The legacy ``/ema/CS/Spending/AddRule`` Nexus endpoint is retired.)
    """
    rule_obj: dict = {
        "CategoryID": _wrap_id(category_id),
        "DescriptionContains": description_contains,
        "UserDescription": user_description or description_contains,
        "MinAmount": min_amount,
        "MaxAmount": max_amount,
        "StartDay": None,
        "EndDay": None,
    }
    payload: dict = {"Rule": rule_obj}
    if transaction_id:
        payload["TransactionID"] = _wrap_id(transaction_id)

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
        "RuleID": _wrap_id(rule_id),
        "CategoryID": _wrap_id(category_id if category_id is not None else existing["category_id"]),
        "DescriptionContains": description_contains if description_contains is not None else (existing["description_contains"] or ""),
        "UserDescription": user_description if user_description is not None else (existing["user_description"] or ""),
        "MinAmount": min_amount if min_amount is not None else existing.get("min_amount"),
        "MaxAmount": max_amount if max_amount is not None else existing.get("max_amount"),
        "StartDay": existing.get("start_day"),
        "EndDay": existing.get("end_day"),
    }
    payload: dict = {"Rule": rule_obj}
    if transaction_id:
        payload["TransactionID"] = _wrap_id(transaction_id)

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


def _rule_to_snb(r: dict) -> dict:
    """Serialize a ``get_transaction_rules`` row to the SNB ``SetRules`` shape
    (PascalCase, wrapped ``{Value}`` ids) — the exact per-rule object the web UI
    sends. Captured live 2026-06-18 (#121)."""
    return {
        "RuleID":              _wrap_id(r["rule_id"]),
        "CategoryID":          _wrap_id(r["category_id"]),
        "DescriptionContains": r["description_contains"] or "",
        "UserDescription":     r["user_description"] or "",
        "MinAmount":           r.get("min_amount"),
        "MaxAmount":           r.get("max_amount"),
        "StartDay":            r.get("start_day"),
        "EndDay":              r.get("end_day"),
    }


async def _csrf_post_json(http_session, path: str, body: dict) -> dict:
    """POST a JSON body to a CS/Spending endpoint with the ASP.NET anti-forgery
    token in the ``__RequestVerificationToken`` header (the web UI's transport
    for JSON AJAX writes — verified live for ``SetRules``, #121)."""
    http = await http_session.get_http()
    token = await http_session.get_csrf_token()
    if not token:
        return {"error": f"Could not obtain CSRF token for {path} — "
                         "session may be stale (try sync_chrome_session)."}
    resp = await http.post(
        f"{_SPENDING}/{path}",
        json=body,
        headers={"X-Requested-With": "XMLHttpRequest", "__RequestVerificationToken": token},
        timeout=30,
    )
    if resp.status_code not in (200, 201):
        return {"error": f"{path} returned HTTP {resp.status_code}", "response_body": resp.text[:400]}
    ct = resp.headers.get("content-type", "")
    return resp.json() if "json" in ct else {"ok": True}


async def delete_transaction_rule(
    http_session,
    rule_id: str,
) -> dict:
    """
    Delete an auto-categorization rule.

    SNB has no single-rule delete endpoint — the web UI deletes by **replacing
    the whole rules collection** via ``POST /ema/CS/Spending/SetRules`` with
    ``{rules:[...]}`` (the full list minus the target). This reads the current
    rules, drops the one with ``rule_id``, and posts the remainder. Verified live
    2026-06-18 (#121); supersedes the dead legacy ``RemoveRule``/Nexus path.

    rule_id — the rule ID (from get_transaction_rules)

    Existing transactions categorized by the rule keep their categories;
    only future auto-categorization stops.
    """
    if rule_id is None or str(rule_id).strip() == "":
        return {"error": "rule_id is required."}

    rules_result = await get_transaction_rules(http_session)
    if "error" in rules_result:
        return rules_result
    existing = rules_result["rules"]
    if not any(str(r["rule_id"]) == str(rule_id) for r in existing):
        return {"error": f"Rule {rule_id} not found. Call get_transaction_rules to see available rules."}

    keep = [_rule_to_snb(r) for r in existing if str(r["rule_id"]) != str(rule_id)]
    result = await _csrf_post_json(http_session, "SetRules", {"rules": keep})
    if isinstance(result, dict) and "error" in result:
        return result
    return _maybe_raw({
        "success": True,
        "rule_id": rule_id,
        "deleted": True,
        "remaining_rule_count": len(keep),
    }, result)
