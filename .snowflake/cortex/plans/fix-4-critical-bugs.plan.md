# Fix 4 Critical Bugs — v1.0.38

## Overview

Four critical bugs confirmed by two independent code reviewers. All fixes are surgical with no API surface changes except Bug 4 (deprecation notice added to `apply_transaction_rule`).

---

## Bug 1 — `get_capital_gains` wrong date range (`investments.py:399-401`)

**Problem:** `get_capital_gains` computes `days` from the requested year's date span, then passes it to `get_transactions(http_session, days=days)`. `get_transactions` anchors its window to **today minus days**, not to the requested year's start. For 2024 called in mid-2026, this returns June 2025–June 2026 data instead of Jan–Dec 2024.

**Fix:** Add two optional parameters to `get_transactions`:
```python
async def get_transactions(
    http_session,
    days: int = 30,
    account_id: str | None = None,
    start_date: str | None = None,   # MM/DD/YYYY — overrides days-based computation
    end_date: str | None = None,     # MM/DD/YYYY — overrides days-based computation
) -> dict:
    days = min(max(days, 1), 365)
    if start_date and end_date:
        start_str, end_str = start_date, end_date
    else:
        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(days=days)
        start_str = start_dt.strftime("%m/%d/%Y")
        end_str   = end_dt.strftime("%m/%d/%Y")
```

Update `get_capital_gains` to pass the already-correct dates directly and remove the `days` intermediate:
```python
txns_result = await get_transactions(
    http_session,
    start_date=start_str,
    end_date=end_str,
)
```

**Files:** `src/emoney_mcp/scrapers/investments.py`
**Tests:** Existing `TestGetCapitalGains` tests mock the endpoint response, not the date logic — they continue to pass. A new test should be added verifying that a prior-year call uses the correct date strings, not today-anchored ones.

---

## Bug 2 — 6 Dead expressions (no-op lines)

**Problem:** Six lines compute values with no assignment. Confirmed by two reviewers that none corrupt output (downstream code uses `total_assets`/`total_liabilities` instead), but they are genuine dead code defects.

**Fix:** Delete all 6 lines:

| File | Line | Delete |
|------|------|--------|
| `scrapers/retirement.py` | ~75 | `accts.get("net_worth") or 0` |
| `scrapers/retirement.py` | ~166 | `accts.get("net_worth") or 0` |
| `scrapers/goals.py` | ~204 | `accts.get("net_worth") or 0` |
| `scrapers/planning.py` | ~231 | `accts.get("total_liabilities") or 0` |
| `scrapers/planning.py` | ~470 | `round(accts.get("net_worth") or 0, 2)` |
| `scrapers/tax.py` | ~1463 | `datetime.now().month` |

**Files:** 4 files above
**Tests:** No test changes needed — no test checks for these intermediate computations.

---

## Bug 3 — Dead `pass/pass` stub in `get_roth_conversion_analysis` (`tax.py:697-701`)

**Problem:** The conversion-favorable analysis block is a stub with both branches as empty `pass` statements. The `conversion_favored: bool` field is set but no recommendation or reasoning is provided.

**Fix:** Replace the stub with a concrete recommendation string, add it to the return dict:

```python
# Replace the pass/pass block:
if marginal > 0 and effective_rate_on_conversion > 0:
    if effective_rate_on_conversion < marginal:
        recommendation = (
            f"Tax-favored: you are paying {round(effective_rate_on_conversion * 100, 1)}% "
            f"on this conversion vs. your {int(marginal * 100)}% marginal rate. "
            "Consider converting up to the top of your current bracket."
        )
    else:
        recommendation = (
            f"Not tax-favored at this amount: effective rate on conversion "
            f"({round(effective_rate_on_conversion * 100, 1)}%) meets or exceeds "
            f"your {int(marginal * 100)}% marginal rate. "
            "Consider a smaller conversion or waiting for a lower-income year."
        )
else:
    recommendation = (
        "Conversion analysis unavailable — insufficient tax data."
    )
```

Add `"recommendation": recommendation` to the return dict (alongside the existing `conversion_favored` and `breakeven_note` fields).

**Files:** `src/emoney_mcp/scrapers/tax.py`
**Tests:** Dispatch test mocks the return value — no changes needed. Add a test to `test_tax_math.py` verifying `"recommendation"` is present and non-empty.

---

## Bug 4 — `apply_transaction_rule` posts to dead Nexus endpoint

**Problem:** `transactions.py:630` calls `_csrf_post(http_session, "ApplyRule", data)` which resolves to `POST /ema/CS/Spending/ApplyRule` — a retired Nexus endpoint returning HTTP 500 on every call. Tool description in the MCP registry claims it works.

**Fix:**
1. In `transactions.py`: Replace the body with an immediate deprecation error — no HTTP call:
```python
async def apply_transaction_rule(http_session, rule_id: str, transaction_id: str | None = None) -> dict:
    """
    DEPRECATED: The ApplyRule Nexus endpoint is retired and always returns HTTP 500.
    Use add_transaction_rule(transaction_id=...) to apply a rule to a specific transaction.
    """
    return {
        "error": (
            "apply_transaction_rule is non-functional: the ApplyRule endpoint "
            "was retired with the Nexus backend. "
            "To apply a rule to a specific transaction, use add_transaction_rule "
            "or update_transaction_rule with the transaction_id parameter."
        ),
        "deprecated": True,
    }
```

2. In `server.py`: Update the tool description to clearly say it is non-functional.

3. In `test_transaction_writes.py`: Update `TestApplyTransactionRule`:
   - `test_apply_rule_success` → expect `"error" in result` and `result.get("deprecated") is True`
   - `test_apply_sends_rule_id_in_payload` → remove (tests implementation detail of dead path)
   - `test_apply_with_transaction_id` → remove (same)
   - `test_csrf_error_propagates` → already passes (already checks `"error" in result`)

**Files:** `src/emoney_mcp/scrapers/transactions.py`, `src/emoney_mcp/server.py`, `tests/test_transaction_writes.py`

---

## Version bump

`pyproject.toml`: `version = "1.0.37"` → `version = "1.0.38"`

---

## PR and CI

- Branch: `fix/critical-bugs-code-review`
- Commit message: `fix: resolve 4 critical bugs from code review (closes #129-#132)`
- PR closes issues #129, #130, #131, #132
- After CI passes, merge with `gh pr merge --merge`
