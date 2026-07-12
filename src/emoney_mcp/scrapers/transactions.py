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
     hide_transaction          → SNB ``ToggleTransactionVisibility`` {hideTransaction, transactionId}
     get_transaction_splits    → SNB ``GetBankTransactionSplits?transactionID=<id>``
     update_transaction_splits → SNB ``updateTransactionSplits`` — POST a bare ARRAY
                                 of split objects (first = parent w/ transactionID set;
                                 rest = children w/ parentTransactionID set + identity).
     delete_transaction_rule   → SNB has NO single delete; the UI bulk-replaces the
                                 whole collection via ``POST /ema/CS/Spending/SetRules``
                                 {rules:[{RuleID:{Value},CategoryID:{Value},...}]} (this
                                 one legacy CS/Spending route is LIVE, not Nexus-dead),
                                 CSRF token in the ``__RequestVerificationToken`` header.

2. **Legacy ``/ema/CS/Spending/*``** (ASP.NET anti-forgery POST via
   ``_csrf_post``) — the original reverse-engineered path, mostly served by the
   retired "Nexus" subsystem (``IsNexusAvailable:false`` for writes — permanent,
   not temporary). ``SetRules`` is the exception: it is the live persist path for
   rule changes (see ``_csrf_post_json``). The only tool still on the dead legacy
   path is ``apply_transaction_rule`` — there is no standalone SNB ApplyRule
   (application folds into Create/UpdateRule via the ``TransactionID`` field), so
   the standalone tool is effectively deprecated.
"""

import os

from ._helpers import BASE_URL, _SNB_API

_SPENDING = f"{BASE_URL}/ema/CS/Spending"


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
    cur_cat = cur_desc = cur_notes = cur_clean = None
    ok, txns, _ = await _fetch_snb_raw(http_session)
    if ok:
        match = next((t for t in txns if str(t.get("id")) == str(transaction_id)), None)
        if match:
            cur_cat = match.get("categoryId")
            cur_desc = match.get("userDescription")
            cur_notes = match.get("notes")
            cur_clean = match.get("cleanDescription") or match.get("description")

    # CRITICAL (#126): the SNB UpdateTransaction endpoint SILENTLY no-ops — it
    # returns HTTP 200 but persists nothing — when userDescription is null. That
    # is the stored state for every transaction the user never manually renamed,
    # so category-only updates on those failed invisibly (the false-positive this
    # tool shipped). The live web UI NEVER sends null: it always populates
    # userDescription with the transaction's display text (cleanDescription).
    # Mirror that exactly. Verified by capturing the official client's request
    # and replaying it on our own session, 2026-06-19.
    effective_desc = description if description is not None else (cur_desc or cur_clean)

    payload = {
        "transactionId": str(transaction_id),
        "categoryId": str(category_id) if category_id is not None
                      else (str(cur_cat) if cur_cat is not None else None),
        "userDescription": effective_desc,
        "notes": cur_notes,
    }

    result = await _snb_post(http_session, "UpdateTransaction", payload)
    if "error" in result:
        return result

    requested = {k: v for k, v in
                 [("category_id", category_id), ("description", description)]
                 if v is not None}

    # Post-write verification (#126). The SNB UpdateTransaction endpoint returns
    # HTTP 200 even when the change does not commit to the store the read tools
    # query — so a bare "ok" is NOT proof the write persisted. Reporting success
    # on the 200 alone produces a false positive that silently misleads the
    # caller (a financial-data correctness bug). Re-read the transaction and only
    # claim success if the requested change is actually reflected.
    from .spending import clear_snb_cache
    clear_snb_cache()  # bust the 5-min SNB cache so we read post-write state
    ok2, txns2, _ = await _fetch_snb_raw(http_session)
    if not ok2:
        # Couldn't read back — don't claim a verified success, but don't invent a
        # failure either (the write may well have landed). Flag it honestly.
        return {
            "success": True,
            "verified": False,
            "transaction_id": str(transaction_id),
            "updated": requested,
            "warning": "Write was accepted (HTTP 200) but could not be confirmed "
                       "— re-reading the transaction failed. Verify manually.",
        }

    match2 = next((t for t in txns2 if str(t.get("id")) == str(transaction_id)), None)
    mismatches = {}
    if match2 is None:
        mismatches["transaction"] = {"expected": "present", "actual": "not found on read-back"}
    else:
        if category_id is not None and str(match2.get("categoryId")) != str(category_id):
            mismatches["category_id"] = {
                "expected": str(category_id),
                "actual": (None if match2.get("categoryId") is None
                           else str(match2.get("categoryId"))),
            }
        if description is not None and (match2.get("userDescription") or "") != description:
            mismatches["description"] = {
                "expected": description,
                "actual": match2.get("userDescription"),
            }

    if mismatches:
        return {
            "error": "UpdateTransaction returned HTTP 200 but the change did not "
                     "persist (confirmed by reading the transaction back). The SNB "
                     "write was a no-op — the transaction is unchanged.",
            "transaction_id": str(transaction_id),
            "attempted": requested,
            "actual": mismatches,
        }

    return {
        "success": True,
        "verified": True,
        "transaction_id": str(transaction_id),
        "updated": requested,
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
    from .spending import clear_snb_cache
    clear_snb_cache()
    return {
        "success": True,
        "transaction_id": transaction_id,
        "hidden": hidden,
    }


async def _fetch_splits_raw(http_session, transaction_id: str):
    """GET the raw SNB split objects for a transaction.

    Returns ``(items, None)`` on success or ``(None, error_dict)``. A non-split
    transaction returns a single element covering the full amount.
    """
    from .spending import _get_snb_credentials, _snb_headers
    jwt_token, api_key = await _get_snb_credentials(http_session)
    if not jwt_token:
        return None, {"error": "Could not retrieve SNB credentials for GetBankTransactionSplits — "
                               "session may be stale (try sync_chrome_session)."}
    http = await http_session.get_http()
    resp = await http.get(
        f"{_SNB_API}/api/values/GetBankTransactionSplits",
        params={"transactionID": str(transaction_id)},
        headers=_snb_headers(jwt_token, api_key), timeout=20,
    )
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return None, {"error": f"GetBankTransactionSplits returned HTTP {resp.status_code}"}
    data = resp.json()
    items = (
        data if isinstance(data, list)
        else (data.get("splits") or data.get("Splits") or []) if isinstance(data, dict)
        else []
    )
    return items, None


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
    items, err = await _fetch_splits_raw(http_session, transaction_id)
    if err:
        return err

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


def _split_id(v):
    """Wrap a split id as ``{"value": ...}`` (lowercase).

    Intentionally different casing from ``_wrap_id`` (``{"Value": ...}`` PascalCase):
    the ``updateTransactionSplits`` endpoint sends lowercase as captured from the
    live web UI (#121). .NET binds case-insensitively so both forms work server-side,
    but we mirror the exact shape the web UI sends to each endpoint.
    """
    return {"value": str(v)} if v is not None else None


async def update_transaction_splits(
    http_session,
    transaction_id: str,
    splits: list[dict],
) -> dict:
    """
    Replace ALL splits on a transaction with the provided list.

    transaction_id — the SNB transaction ID to split
    splits         — list of ``{"category_id": "65", "amount": -25.00,
                     "description": "optional"}``. Amounts must sum to the
                     transaction total (negative for expenses). Pass a single
                     split to un-split (merge back to one).

    Posts the bare split array to the SNB API (``updateTransactionSplits``), the
    live web UI's path, verified live 2026-06-18 (#121). The first split becomes
    the parent (``transactionID`` set, ``parentTransactionID`` null); additional
    splits are children (``transactionID`` null, ``parentTransactionID`` set).
    Transaction metadata (descriptions, dates) is carried over from the existing
    record. (The legacy ``/ema/CS/Spending/UpdateTransactionSplits`` Nexus
    endpoint is retired.)
    """
    if not splits:
        return {"error": "Provide at least one split."}
    for s in splits:
        if s.get("category_id") is None:
            return {"error": "Each split needs a 'category_id'."}
        if s.get("amount") is None:
            return {"error": "Each split needs an 'amount'."}

    # Carry the transaction's metadata (description/dates) from the existing record.
    existing, err = await _fetch_splits_raw(http_session, transaction_id)
    if err:
        return err
    if not existing:
        return {"error": f"Transaction {transaction_id} not found (no splits returned)."}
    parent = existing[0]
    clean_desc = parent.get("cleanDescription")
    orig_desc  = parent.get("description")
    post_date  = parent.get("postDate")
    txn_date   = parent.get("transactionDate")

    body = []
    for i, s in enumerate(splits):
        obj = {
            "extensionData":    {},
            "action":           0,
            "categoryID":       _split_id(s["category_id"]),
            "cleanDescription": clean_desc,
            "description":      s.get("description") or orig_desc,
            "postDate":         post_date,
            "splitAmount":      f"{float(s['amount']):.2f}",
            "transactionDate":  txn_date,
            "userDescription":  s.get("user_description"),
        }
        if i == 0:
            obj["transactionID"] = _split_id(transaction_id)
            obj["parentTransactionID"] = None
        else:
            obj["transactionID"] = None
            obj["parentTransactionID"] = _split_id(transaction_id)
            obj["identity"] = i
        body.append(obj)

    result = await _snb_post(http_session, "updateTransactionSplits", body)
    if "error" in result:
        return result
    from .spending import clear_snb_cache
    clear_snb_cache()
    total = round(sum(float(s["amount"]) for s in splits), 2)
    return _maybe_raw({
        "success": True,
        "transaction_id": transaction_id,
        "splits_written": len(splits),
        "total_amount": total,
        "is_split": len(splits) > 1,
    }, result.get("data"))


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
    from .spending import clear_snb_cache
    clear_snb_cache()
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
    from .spending import clear_snb_cache
    clear_snb_cache()
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
    DEPRECATED: The ApplyRule Nexus endpoint is retired and returns HTTP 500 on
    every call. Use add_transaction_rule or update_transaction_rule with the
    transaction_id parameter to apply a rule to a specific transaction.
    """
    return {
        "error": (
            "apply_transaction_rule is non-functional: the ApplyRule endpoint "
            "was retired with the Nexus backend. "
            "To apply a rule to a specific transaction, pass transaction_id to "
            "add_transaction_rule or update_transaction_rule instead."
        ),
        "deprecated": True,
    }


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
    from .spending import clear_snb_cache
    clear_snb_cache()
    return _maybe_raw({
        "success": True,
        "rule_id": rule_id,
        "deleted": True,
        "remaining_rule_count": len(keep),
    }, result)
