# Fix 7 High-Severity Bugs — v1.0.39

## Overview

All 7 high-severity bugs independently verified. Fixes are grouped below by file touched. The most complex change is #133 (investment data cache), which adds a shared cached helper to `_helpers.py` and updates 7 call sites across 3 modules.

---

## Bug #133 — `GetInvestmentData` has no shared TTL cache

**Problem:** 7+ independent HTTP calls fire for the same endpoint across `investments.py`, `tax.py`, `portfolio.py` with a `?_={ts}` cache-buster that actively defeats HTTP caching.

**Fix — `scrapers/_helpers.py`:**

Add a module-level TTL cache and async helper alongside `_card_cache`:

```python
# Investment data cache (same TTL as cards)
_inv_cache: tuple[float, dict | None] | None = None

def clear_card_cache() -> None:
    global _inv_cache
    _card_cache.clear()
    _inv_cache = None            # also clear investment cache

async def _get_investment_data(http_session) -> tuple[dict | None, dict | None]:
    global _inv_cache
    now = time.time()
    if _inv_cache is not None:
        ts, data = _inv_cache
        ttl = _CARD_CACHE_TTL if data is not None else _CARD_ERROR_TTL
        if now - ts < ttl:
            return (data, None) if data is not None else (None, {"error": "GetInvestmentData unavailable (cached error)."})
    http = await http_session.get_http()
    resp = await http.get(f"{_INV_URL}/GetInvestmentData?_={int(now * 1000)}", timeout=30)
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        _inv_cache = (now, None)
        return None, {"error": f"GetInvestmentData returned {resp.status_code}. Session may have expired."}
    payload = resp.json()
    if not isinstance(payload, dict):
        _inv_cache = (now, None)
        return None, {"error": "GetInvestmentData returned an unexpected (non-object) body."}
    _inv_cache = (now, payload)
    return payload, None
```

**Fix — `scrapers/investments.py`** (4 functions):

Replace each direct `http.get(f"{_INV_URL}/GetInvestmentData?_=...")` block (~5 lines) in `get_holdings`, `get_asset_allocation`, `get_sector_geographic_allocation`, and `get_dividend_income_analysis` with:

```python
from ._helpers import _get_investment_data   # add to imports at top
# ...
data, err = await _get_investment_data(http_session)
if err:
    return err
```

**Fix — `scrapers/tax.py`** (2 functions):

Same pattern for `get_tax_loss_harvesting` and `get_capital_gains_exposure`.

**Fix — `scrapers/portfolio.py`**:

Remove the body of the existing `_get_investment_data` function and replace with delegation to `_helpers`:

```python
from ._helpers import _get_investment_data   # add to imports
# Remove the duplicate implementation entirely (the function in portfolio.py is now redundant)
```

Since `portfolio.py` already imports from `_helpers`, this is clean. The 4 portfolio functions that call `_get_investment_data(http_session)` continue to work unchanged.

**Fix — `tests/conftest.py`**:

Update the autouse fixture to also clear `_inv_cache` to prevent cross-test pollution:

```python
@pytest.fixture(autouse=True)
def clear_card_cache():
    from emoney_mcp.scrapers import _helpers
    _helpers._card_cache.clear()
    _helpers._inv_cache = None
    yield
    _helpers._card_cache.clear()
    _helpers._inv_cache = None
```

**Files:** `_helpers.py`, `investments.py`, `tax.py`, `portfolio.py`, `tests/conftest.py`

---

## Bug #134 — `userId` JWT claim injected into URL without UUID validation

**Fix — `scrapers/aggregation_api.py`**:

Add a UUID format check in `_get_agg_credentials` right after extracting `guid`:

```python
import re   # already imported

_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

guid = _jwt_user_guid(token)
if not guid:
    return None, None, None, {"error": "Could not read the aggregation user id from the token."}
if not _UUID_RE.match(guid):
    return None, None, None, {"error": f"Aggregation token userId is not a valid UUID: {guid!r}"}
```

**Files:** `aggregation_api.py`
**Tests:** None needed — existing tests mock the full `_get_agg_credentials` function.

---

## Bug #135 — Estate exemption uses 2025 values in 2026 context

**Fix — `scrapers/planning.py`**:

```python
_ESTATE_EXEMPTION_SINGLE = 13_990_000   # federal estate/gift tax exemption (2026)
_ESTATE_EXEMPTION_MFJ    = 27_980_000   # married (2026)
```

**Files:** `planning.py`
**Tests:** Existing tests check behavior at $500K (below exemption) and $30M (above exemption) — both still hold at the updated values. No changes needed.

---

## Bug #136 — Monte Carlo SWR search reuses main simulation RNG state

**Fix — `scrapers/retirement.py`**:

Before the SWR search loop (line ~480), create an independent RNG instance:

```python
# Find the safe withdrawal rate using an independent RNG to avoid
# sensitivity to the simulations count (bug #136)
swr_rng = random.Random(42)
safe_swr = None
for candidate_rate_bp in range(500, 100, -25):
    ...
    for _ in range(200):
        ...
        for _yr in range(years):
            ret = swr_rng.gauss(mean_return, std_dev)    # was: rng.gauss
            inf = max(0.0, swr_rng.gauss(inflation_mean, inflation_std))  # was: rng.gauss
```

Replace every `rng.gauss(...)` call inside the SWR search block with `swr_rng.gauss(...)`. The main simulation loop above (using `rng`) is unchanged.

**Files:** `retirement.py`
**Tests:** No test checks for SWR sensitivity to `simulations` count — existing tests continue to pass. The SWR value itself may change slightly (different RNG path) which is correct behavior.

---

## Bug #137 — SQLite connection leak in `_read_profile_cookies`

**Fix — `browser.py`**:

Wrap the SQLite operations in `try/finally` to mirror the existing `_read_macos_cookie_rows` pattern:

```python
conn = sqlite3.connect(str(cookie_db))
try:
    rows = conn.execute(
        "SELECT name, value, host_key FROM cookies WHERE host_key LIKE '%emaplan%'"
    ).fetchall()
finally:
    conn.close()
return {name: value for name, value, host in rows}
```

**Files:** `browser.py`
**Tests:** `test_browser_nodriver_patch.py` is excluded from local runs (missing `nodriver` module). No test changes needed.

---

## Bug #138 — Reports first regex loop silently discards flat-array reports

**Fix — `scrapers/reports.py`**:

Change the first loop to add found report objects to `families` under an "Uncategorized" group instead of only recording IDs:

```python
uncategorized = []
for blob in re.finditer(r'\[(\{"ReportID"[^]]{20,5000})\]', html):
    try:
        reports = json.loads("[" + blob.group(1) + "]")
    except Exception:
        continue
    for r in reports:
        rid = r.get("ReportID")
        if rid and rid not in seen_ids:
            seen_ids.add(rid)
            uncategorized.append({
                "report_id":   rid,
                "name":        r.get("Name", rid),
                "short_name":  r.get("ShortName", ""),
                "description": r.get("Description", ""),
            })

if uncategorized:
    families.append({"family": "Uncategorized", "reports": uncategorized})
```

The second loop (family-grouped) and third loop (standalone IDs) are unchanged. Reports found in the flat-array pass that are ALSO found in a family group will be deduplicated via `seen_ids` (so they won't appear twice in "Uncategorized" AND their real family — the second loop's `seen_ids.add(rid)` prevents duplication).

**Files:** `reports.py`
**Tests:** Existing tests mock HTTP responses — no changes needed, but a new test verifying flat-array reports are included would be valuable. Adding one to `test_scraper_reports.py`.

---

## Bug #139 — `get_accounts` returns mixed data + error dict simultaneously

**Fix — `scrapers/accounts.py`**:

Remove the partial data fields from the Card 1 failure return — it should be a clean error dict:

```python
card1 = await _get_card(http, 1)
if card1 is None:
    return {
        "error": (
            "Card 1 (accounts) unavailable. "
            "Net-worth totals are still accessible via get_net_worth."
        )
    }
```

This eliminates the confusing mixed state. Callers already handle `"error" in result` correctly — their behavior is unchanged. The net worth data (Card 9) remains available via `get_net_worth` which calls Card 9 directly.

**Files:** `accounts.py`
**Tests:** Tests that mock Card 1 as unavailable check `"error" in result` — still true. No changes needed.

---

## Version bump

`pyproject.toml`: `1.0.38` → `1.0.39`

---

## PR and issue closure

- Branch: `fix/high-severity-bugs-code-review`
- PR body must use individual `closes` keywords per issue for auto-close to work:
  ```
  Closes #133, closes #134, closes #135, closes #136,
  closes #137, closes #138, closes #139
  ```
- After CI passes, merge with `gh pr merge --merge`
