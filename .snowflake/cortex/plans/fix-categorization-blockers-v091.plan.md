---
name: "fix categorization blockers v091"
created: "2026-06-13T23:28:34.730Z"
status: pending
---

# Plan: Fix categorization-write blockers (v0.9.1)

## Overview

Three targeted changes, none break existing response shapes:

1. **Bug 1** — `get_spending_transactions` drops `transaction_id` and `category_id` during serialization.
2. **Bug 2** — `get_transaction_rules` returns HTTP 500 because `_csrf_post` swallows the response body AND the empty POST body may trigger a server-side error; parser also can't handle a list-shaped response.
3. **Enhancement** — `get_categories` exposes the SNB category map already cached by `_fetch_snb_raw`.

---

## Step 1 — Bug 1: Add `transaction_id` and `category_id` to `get_spending_transactions`

**File**: `src/emoney_mcp/scrapers/spending.py`

In `get_spending_transactions`, the `for t in filtered` loop (lines 476–487) already extracts `cat_id` on line 478, but the dict literal that follows drops it and never reads `t.get("id")`.

**Change** — expand the dict literal to include both fields:

```python
transactions.append({
    "transaction_id": t.get("id") or t.get("transactionId") or t.get("Id"),
    "date":           (t.get("date") or "")[:10],
    "description":    desc,
    "category":       cat_name,
    "category_id":    int(cat_id) if cat_id else None,
    "amount":         t.get("value", 0),
    "is_pending":     t.get("isPending", False),
    "is_split":       t.get("isSplit", False),
})
```

The two new fields are prepended so they're visually first. Existing fields are unchanged.

---

## Step 2 — Bug 2a: Surface response body in `_csrf_post` errors

**File**: `src/emoney_mcp/scrapers/transactions.py`

Current error path in `_csrf_post`:

```python
if resp.status_code not in (200, 201):
    return {"error": f"{path} returned HTTP {resp.status_code}"}
```

Replace with:

```python
if resp.status_code not in (200, 201):
    try:
        body_snippet = resp.text[:400]
    except Exception:
        body_snippet = ""
    return {
        "error": f"{path} returned HTTP {resp.status_code}",
        "response_body": body_snippet,
    }
```

This is a **pure addition** — callers that already check `"error" in result` continue to work; they now also have `response_body` for diagnosis.

---

## Step 3 — Bug 2b/c: Fix `get_transaction_rules` payload and response parsing

**File**: `src/emoney_mcp/scrapers/transactions.py`

Two sub-issues:

**3a — Empty POST body triggers server 500.** ASP.NET MVC controllers often call `.HasValue` or dereference properties of a model parameter; if nothing is bound, a `NullReferenceException` fires → 500. Fix: send a `filter` param (empty string) so the model binder has something to bind:

```python
result = await _csrf_post(http_session, "GetRules", {"filter": ""})
```

**3b — Response may be a list, not a dict keyed by rule\_id.** The current parser does:

```python
for rule_id, rule in result.items():
```

This crashes if the API returns `[{...}, {...}]`. Fix: handle both shapes.

Updated `get_transaction_rules`:

```python
async def get_transaction_rules(http_session) -> dict:
    result = await _csrf_post(http_session, "GetRules", {"filter": ""})
    if isinstance(result, dict) and "error" in result:
        return result

    rules = []
    # API may return a list OR a dict keyed by rule_id
    if isinstance(result, list):
        items = result
    elif isinstance(result, dict):
        items = result.values()
    else:
        return {"error": f"Unexpected response type from GetRules: {type(result).__name__}"}

    for rule in items:
        if not isinstance(rule, dict):
            continue
        rules.append({
            "rule_id":             rule.get("RuleID", {}).get("Value") if isinstance(rule.get("RuleID"), dict) else rule.get("RuleID"),
            "description_contains": rule.get("DescriptionContains"),
            "category_id":         rule.get("CategoryID", {}).get("Value") if isinstance(rule.get("CategoryID"), dict) else rule.get("CategoryID"),
            "user_description":    rule.get("UserDescription"),
            "min_amount":          rule.get("MinAmount"),
            "max_amount":          rule.get("MaxAmount"),
            "start_day":           rule.get("StartDay"),
            "end_day":             rule.get("EndDay"),
        })
    return {"rules": rules, "count": len(rules)}
```

---

## Step 4 — Enhancement: `get_categories` in `spending.py`

**File**: `src/emoney_mcp/scrapers/spending.py`

Add after `clear_snb_cache()`:

```python
async def get_categories(http_session) -> dict:
    """Return all SNB spending categories with their numeric IDs."""
    ok, _, categories = await _fetch_snb_raw(http_session)
    if not ok:
        return {"error": "Could not retrieve SNB data. Try re-syncing Chrome session."}
    cats = sorted(
        [{"id": int(k), "name": v} for k, v in categories.items() if k and v],
        key=lambda x: x["name"],
    )
    return {"category_count": len(cats), "categories": cats}
```

No new HTTP call — `_fetch_snb_raw` is cached for 5 minutes.

---

## Step 5 — Wire into `scrapers/__init__.py` and `scraper.py`

**`scrapers/__init__.py`**: Add `get_categories` to the `from .spending import (...)` block and to `__all__`.

**`scraper.py`**: Add `get_categories` to the explicit import list under the v0.9.x section.

---

## Step 6 — Register `get_categories` as MCP tool in `server.py`

Three additions (same pattern as every other tool):

**`list_tools()`**:

```python
Tool(
    name="get_categories",
    description=(
        "Return all SNB spending category names and their numeric IDs. "
        "Use this to look up the category_id needed by update_transaction, "
        "add_transaction_rule, and update_transaction_rule."
    ),
    inputSchema={"type": "object", "properties": {}, "required": []},
),
```

**`_call_tool_inner()`**:

```python
elif name == "get_categories":
    result = await _get_categories()
```

**Private wrapper**:

```python
async def _get_categories() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_categories(sess)
```

---

## Step 7 — CHANGELOG.md

Add a `v0.9.1` section documenting the three fixes.

---

## Files touched

| File                       | Change                                                                                            |
| -------------------------- | ------------------------------------------------------------------------------------------------- |
| `scrapers/spending.py`     | Add `transaction_id`, `category_id` to `get_spending_transactions`; add `get_categories` function |
| `scrapers/transactions.py` | Improve `_csrf_post` error; fix `get_transaction_rules` payload + parser                          |
| `scrapers/__init__.py`     | Export `get_categories`                                                                           |
| `scraper.py`               | Import `get_categories`                                                                           |
| `server.py`                | Register `get_categories` tool (Tool + dispatch + wrapper)                                        |
| `CHANGELOG.md`             | v0.9.1 notes                                                                                      |
