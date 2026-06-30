# Fix Remaining Open Issues — v1.0.41

## Overview

7 remaining open issues from the code review. Grouped by effort:

---

## Trivial (1–3 lines each)

### #145 — Cache `datetime.now().year` before loops

**Files:** `retirement.py` (~line 1151), `planning.py` (~lines 819, 920, 1228), `tax.py` (any remaining)

In each function that has `datetime.now().year + i` or `datetime.now().year + years_from_now` inside a loop body, hoist to a variable before the loop:

```python
# Before loop:
current_year = datetime.now().year
# Inside loop: use current_year + i instead of datetime.now().year + i
```

The specific locations are `get_retirement_income_plan` (retirement.py:1151), `get_healthcare_cost_projection` (planning.py:920), `get_hsa_optimization` (planning.py:1228), and any tax.py loops.

---

### #146 — Health score 90-day spending divided by approximate 3 months

**File:** `goals.py` — `get_financial_health_score`

Replace the approximate `/3` with a count of distinct calendar months in the transaction window:

```python
months_in_window = len({t.get("date","")[:7] for t in txns if not t["is_income"] and not t["is_excluded"] and t.get("date")}) or 3
monthly_spending = sum(...) / months_in_window
```

---

### #147 — Fix incorrect AES-CBC comment in `browser.py`

**File:** `browser.py` (~line 225)

Change:
```python
# Chrome 80+ prepends a 32-byte SHA256(domain) to the plaintext.
```
To:
```python
# Chrome AES-CBC: strip the leading prefix bytes (IV/padding) before the plaintext.
```

---

### #149 — Clarify `_wrap_id` vs `_split_id` comment divergence

**File:** `transactions.py` (~line 381)

The two functions intentionally use different casing (`{"Value": ...}` vs `{"value": ...}`) because they mirror what the live web UI sends to different endpoints. Add a cross-reference comment to `_split_id` so the inconsistency is explained rather than confusing:

```python
def _split_id(v):
    """Wrap a split id as ``{"value": ...}`` (lowercase).

    Intentionally different from ``_wrap_id`` (``{"Value": ...}`` PascalCase):
    the ``updateTransactionSplits`` endpoint sends lowercase as captured live (#121).
    .NET binds case-insensitively so both work, but we mirror the exact UI shape.
    """
    return {"value": str(v)} if v is not None else None
```

---

## Moderate

### #140 — `run_scenario` retirement age fallback assumes age 40

**File:** `retirement.py` (~line 693 + 790)

Add an optional `current_age` parameter to `run_scenario`. When provided, use it for the retirement comparison year. When absent, fall back to the existing 20-year default (removing the incorrect age-40 assumption):

```python
async def run_scenario(
    http_session,
    monthly_savings_delta: float = 0,
    target_net_worth: float | None = None,
    retirement_age: int | None = None,
    annual_return_pct: float = 7,
    current_age: int | None = None,   # new — used with retirement_age
) -> dict:
```

Change line 790:
```python
elif retirement_age:
    if current_age is not None:
        compare_year = current_year + max(0, retirement_age - current_age)
    else:
        compare_year = current_year + 20  # default when age unknown
```

Also update the tool description in `server.py` to expose `current_age`.

**Files:** `retirement.py`, `server.py`

---

### #141 — Roth conversion ladder double-applies 6% growth

**File:** `tax.py` (~lines 1095–1105)

The bug: `pretax_remaining` in the ladder grows at 6% independently while `row["rmd"]` was computed from the projection's balance which also grew at 6% — but without the ladder's conversions deducted. The fix: recompute the RMD each year from the ladder's own running balance using `_rmd_factor`, and adjust the taxable income accordingly:

```python
for row in proj["projection"]:
    if ladder:  # year 2+: apply growth to the ladder's own balance
        pretax_remaining = round(pretax_remaining * 1.06, 2)

    # Recompute RMD from the ladder's actual balance (not the projection's,
    # which doesn't account for prior conversions — bug #141)
    age = row["age"]
    if age >= 73 and pretax_remaining > 0:
        actual_rmd = round(pretax_remaining / _rmd_factor(age), 2)
    else:
        actual_rmd = 0.0
    pretax_remaining = max(0.0, round(pretax_remaining - actual_rmd, 2))

    # Adjust taxable income for the corrected RMD
    base_taxable = round(row["taxable_income"] - row["rmd"] + actual_rmd, 2)
    room = ceiling - base_taxable
    convert = max(0.0, round(min(room, pretax_remaining), 2))
    ...
```

---

### #144 — Card cache has no concurrent request deduplication

**File:** `scrapers/_helpers.py`

In asyncio's single-threaded event loop, two coroutines that both see a cache miss before either makes the HTTP request will both fire separate requests. Fix: use an `asyncio.Future` per card_id to deduplicate in-flight requests.

```python
_card_futures: dict[int, asyncio.Future] = {}

async def _get_card(http, card_id: int) -> dict | None:
    # ... int coercion, TTL check ...

    # Deduplicate in-flight requests: if another coroutine is already fetching
    # this card, await its result instead of firing a duplicate request.
    if card_id in _card_futures:
        try:
            return await _card_futures[card_id]
        except Exception:
            return None

    fut = asyncio.get_running_loop().create_future()
    _card_futures[card_id] = fut
    try:
        resp = await http.get(...)
        ...
        data = payload.get("Data") if isinstance(payload, dict) else None
        _card_cache[card_id] = (now, data)
        fut.set_result(data)
        return data
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        _card_futures.pop(card_id, None)
```

Also update `clear_card_cache()` to cancel and clear any pending futures, and update `tests/conftest.py` to also clear `_card_futures` between tests.

---

## Version bump

`pyproject.toml`: `1.0.40` → `1.0.41`

---

## PR issue closure

Use one `closes` per issue in the PR body:
```
Closes #140, closes #141, closes #144, closes #145,
closes #146, closes #147, closes #149
```
