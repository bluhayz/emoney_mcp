# Plan: Fix v1.1 Bugs (Issues #156–#172)

## Overview
Fix all 17 open bug issues in a single branch. Most have clearly specified fixes in the issue descriptions and require only targeted changes to existing functions.

---

## Branch strategy
`fix/v1-1-bugs` off `main`. Version bump → **1.1.0** (v1.1 milestone complete).

---

## Group A — One-liner guards (#157, #164, #168)

### #157 — savings_rate null when expenses=0 (`spending.py:639`)
```python
# Before
if income and expenses and income > 0:
# After
if income and income > 0 and expenses is not None:
```

### #164 — `get_year_end_checklist` isinstance order (`tax.py` × 5 sites)
All 5 patterns: `"error" not in bh and not isinstance(bh, Exception)` → `not isinstance(bh, Exception) and "error" not in bh`.
Also normalize: exceptions right after gather → `{}` so downstream code never sees them.

### #168 — SNB cache not invalidated after writes (`transactions.py`)
Add `from .spending import clear_snb_cache; clear_snb_cache()` after every successful mutating call in:
- `hide_transaction`
- `update_transaction_splits`
- `add_transaction_rule`
- `update_transaction_rule`
- `delete_transaction_rule`

---

## Group B — Data/display bugs (#156, #158, #163, #171)

### #156 — `get_income_summary` drops oldest month (`spending.py:~910`)
Current: `month_labels` derived from `days // 30` (misses the partial oldest month).
Fix: derive from `first_transaction_date` to `now`, or use `months_back + 1` when days straddles a boundary. Safest: collect all months seen in the transaction data and use that set as labels, keeping them sorted. Sum all transactions regardless, not just those in `month_set`.

### #158 — `get_budget_vs_actual` partial-month comparison (`spending.py:~1169`)
Option 2 (additive): add `month_progress_pct` (`day_of_month / days_in_month`) and `pace_projected_total` (`actual / month_progress_pct`) to the per-category and summary output. Add a note that `this_month_actual` is partial.

### #163 — Debt payoff double-counts freed minimums (`accounts.py:~397`)
Remove `freed` accumulator entirely from `_simulate_payoff`. The budget allocated to the focus debt is simply `remaining_budget` (which already includes the minimums of paid-off debts since those minimums were never deducted):
```python
focus_pay = min(remaining_budget, balances[0])  # drop `+ freed`
```
And remove all `freed +=` lines.

### #171 — `get_dividend_income_analysis` window mismatch (`investments.py:~445`)
Clamp `days = min(max(days, 1), 365)` before calling `get_transactions`. Report `trailing_window_days: effective_days` (clamped value). Annualize `projected_forward_income = total_income * 365 / effective_days` and recompute `portfolio_yield_pct` on the annualized figure.

---

## Group C — Tax/retirement math (#162, #169, #170)

### #162 — SS spousal benefit uses monthly_70 instead of PIA (`tax.py:~1399`)
```python
spousal_benefit = round(fra_monthly * 0.50, 2)   # was: monthly_70 * 0.50
```
Also:
- Derive spouse FRA from `spouse_birth_year` using the existing FRA lookup logic (same as the primary worker's `fra` calculation)
- Delete the dead statement `_lifetime(monthly_67, 67, life_expectancy)` at line ~1331

### #169 — Retirement keyword substring matching (`accounts.py:~139`)
Replace `_RETIREMENT_KEYWORDS` dict + `any(kw in combined ...)` with `re.search`:
```python
import re
_RET_PATTERNS = [
    (re.compile(r"\b(401|403|529|hsa|ira|roth|pension|annuit|retirement|deferred comp|sep ira|simple ira|education\b)", re.I), ...),
]
```
Use `\b` word boundaries for alpha keywords; numeric tokens (`401`, `403`, `529`) match as word boundaries naturally.
For `_bucket()`: convert to **first-match-wins** with priority list: `401/403` → `roth`+`ira` combo → `roth` alone → `ira` alone → `hsa` → `529/education` → `annuit` → `pension/sep/simple/deferred/other`.

### #170 — Q2 estimated-tax due date hardcoded (`tax.py:~1497`)
Add a `_next_business_day(d: date) -> date` helper (rolls Sat→Mon, Sun→Mon). Use it for all 4 statutory dates:
```python
_next_bd = _next_business_day
due_dates = [
    {"quarter": "Q1", "due": _next_bd(date(current_year,  4, 15)).strftime("%B %d, %Y")},
    {"quarter": "Q2", "due": _next_bd(date(current_year,  6, 15)).strftime("%B %d, %Y")},
    {"quarter": "Q3", "due": _next_bd(date(current_year,  9, 15)).strftime("%B %d, %Y")},
    {"quarter": "Q4", "due": _next_bd(date(current_year+1,1, 15)).strftime("%B %d, %Y")},
]
```
Add a note: "Federal holiday shifts are not modeled."

---

## Group D — Investable assets + portfolio consistency (#160, #161)

### #160 — `_calc_investable_assets` subtracts gross property value (`accounts.py:~105`)
Fix: compute net real-estate equity = (sum of positive RE balances) − (sum of negative RE/mortgage/HELOC balances in same real-estate classification).
```python
re_assets  = sum(bal for bal > 0 and is_real_estate)
re_liabs   = sum(abs(bal) for bal < 0 and is_real_estate_debt)   # mortgages/HELOCs
re_equity  = max(0.0, re_assets - re_liabs)
investable = max(0.0, net_worth - re_equity)
```
The mortgage balance is already factored into `net_worth` (as a negative), so we should NOT subtract `re_equity` from `net_worth` — instead: `investable = total_investable_assets - re_equity` where `total_investable_assets = total_assets - RE_assets`. Or equivalently: `investable = max(0.0, net_worth + re_liabs - re_assets + re_equity)`.

Actually the cleanest: `investable = net_worth - re_equity`. Where `re_equity = max(0, re_assets - matched_re_liabs)`. Since `net_worth = assets - liabs` already, and we want to exclude the illiquid equity from the investable pool.

Check: cash $500k + house $1.0M + mortgage −$600k → net_worth $900k. re_equity = $1.0M − $600k = $400k. investable = $900k − $400k = $500k. ✓

### #161 — Retirement tools use full net worth as portfolio (`retirement.py`)
Change these 4 functions to use `_calc_investable_assets(accts_data)` instead of `total_assets - total_liabilities`:
- `get_retirement_runway` (line ~76)
- `run_monte_carlo_retirement` (has its own inline calculation)
- `get_dynamic_withdrawal_guardrails`
- `get_sequence_of_returns_stress_test`

Add `note` field to each: "Portfolio = net worth minus real-estate equity (home equity excluded as non-withdrawable)."

---

## Group E — Session/browser hardening (#165, #166, #167)

### #165 — `reset_session` stale singleton (`server.py`, `browser.py`)
- Remove `_http_session` from the explicit `from .browser import (...)` in server.py
- Add `import emoney_mcp.browser as _bmod` at module level
- Replace the 3 usage sites of `_http_session` (health check + 2 in `_sync_chrome_session`) with `_bmod._http_session`
- In `_reset()`, before rebinding: attempt to close the old underlying `AsyncSession` (add `close_underlying()` to `EmoneyHttpSession` that calls `self._session.close()` if `self._session` exists)

### #166 — Health check blocks tool calls with Akamai false-negative (`server.py:~2564`)
Change `is_logged_in()` (retries=0 default) to `is_logged_in(retries=1)` in the health-check call. This accepts one retry per 5-minute window in exchange for eliminating the false-negative Akamai flake.

### #167 — nodriver browser not closed on success (`browser.py:~595`)
```python
# Wrap browser lifetime in try/finally
try:
    ...poll loop...
    if cookies:
        _http_session.save_cookies(cookies)
        self._done_event.set()
    return
finally:
    try:
        browser.stop()
    except Exception:
        pass
```
In `_read_profile_cookies`: filter `{name: value for name, value, host in rows if value}`.

---

## Group F — Portability + test infra (#159, #172)

### #159 — Permission test fails on Windows (`tests/test_browser_helpers.py`)
```python
@pytest.mark.skipif(sys.platform == 'win32', reason='NTFS does not enforce POSIX permission bits')
```
Add to both `test_new_file_is_owner_only` and `test_preexisting_loose_file_is_tightened`.

### #172 — Hardcoded names Drew/Lacey/Parker (`accounts.py:~225`, `server.py` tool desc)
In `get_net_worth_breakdown`:
1. Call `await get_client_profile(http_session)` at the top
2. Build `_name_to_role` map from profile: `{first_name.lower(): "primary"/"spouse"/"joint"}` for primary, spouse, dependents
3. Replace `_person()` hardcoded checks with a lookup in `_name_to_role`; default to "Other"
4. Update server.py tool description for `get_net_worth_breakdown` to say "by household member" instead of naming family members

---

## Testing
Each group gets at least one new test (or an existing test updated). Key new tests:
- #157: `income=1000, expenses=0` → `savings_rate_pct == 100.0`
- #158: `month_progress_pct` present in output
- #163: payoff simulation with a small debt that pays off mid-sim; verify months and interest
- #160: mortgaged property scenario; investable ≥ 0 and correct
- #164: one sub-tool raises; checklist still returns partial data
- #170: `date(2026, 6, 15)` is Monday → output is "June 15, 2026"

## Version
Bump `pyproject.toml` version `1.0.41` → **`1.1.0`** (v1.1 milestone complete).

## Close issues
After merge: `gh issue close 156 157 158 159 160 161 162 163 164 165 166 167 168 169 170 171 172`.
