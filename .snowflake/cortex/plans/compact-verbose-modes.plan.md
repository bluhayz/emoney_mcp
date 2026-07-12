# Plan: compact/verbose response modes (#182)

## Overview
Add a global `EMONEY_COMPACT=1` env var that truncates large per-row arrays in the 4 biggest tools. **Default: unset = verbose (current behavior unchanged)**. No tool-schema changes, no per-call params.

---

## Design

### What "compact" means per tool

| Tool | Full (default) | Compact (`EMONEY_COMPACT=1`) |
|------|---------------|------------------------------|
| `get_lifetime_cash_flow_projection` | all N plan years in `years[]` | 5–10 key years: first, every-5th, peak, first-negative-CF, depletion (if any), last; rest dropped; adds `years_total` + `years_shown` |
| `run_monte_carlo_retirement` | all years in `year_by_year_percentiles[]` (30–60 rows) | `year_by_year_percentiles` key omitted; all scalar summary fields kept |
| `get_budget_vs_actual` | all categories in `categories[]` (can be 100+) | `categories` truncated to top-10 by `abs(variance)`; adds `categories_total` + `categories_shown` |
| `get_official_plan_projection` | all years in `asset_spread[]` | every-5th-year downsampled; adds `asset_spread_total` + `asset_spread_shown` |

All compact responses include an `"output_mode": "compact"` field (absent in verbose) so callers can detect truncation.

### Helper

Add `_is_compact() -> bool` to `scrapers/_helpers.py`:
```python
import os
def _is_compact() -> bool:
    return os.environ.get("EMONEY_COMPACT", "").strip().lower() in ("1", "true", "yes")
```

Export it from `scrapers/__init__.py` and expose it in `scraper.py`.

---

## File-by-file changes

### `scrapers/_helpers.py`
Add `_is_compact()` function at the bottom.

### `scrapers/plan_api.py` — `get_lifetime_cash_flow_projection`
After building `rows`, before the return:
```python
if _is_compact() and len(rows) > 10:
    key_years = {rows[0]["year"], rows[-1]["year"], peak["year"]}
    if first_negative:  key_years.add(first_negative)
    if depletion_year:  key_years.add(depletion_year)
    # also every 5th year
    for r in rows:
        if r["year"] % 5 == 0:
            key_years.add(r["year"])
    rows_out = [r for r in rows if r["year"] in key_years]
    compact_fields = {"output_mode": "compact", "years_total": len(rows), "years_shown": len(rows_out)}
else:
    rows_out = rows
    compact_fields = {}
```
Return `rows_out` as `years`, merge `compact_fields` into result.

### `scrapers/retirement.py` — `run_monte_carlo_retirement`
```python
if _is_compact():
    result = {...all scalars...}   # omit year_by_year_percentiles key
    result["output_mode"] = "compact"
    result["year_by_year_percentiles_note"] = "Omitted in compact mode. Set EMONEY_COMPACT= to see per-year data."
else:
    result["year_by_year_percentiles"] = year_summary
```

### `scrapers/spending.py` — `get_budget_vs_actual`
```python
if _is_compact() and len(category_comparison) > 10:
    cats_out = category_comparison[:10]   # already sorted by abs(variance) desc
    compact_fields = {"output_mode": "compact", "categories_total": len(category_comparison), "categories_shown": 10}
else:
    cats_out = category_comparison
    compact_fields = {}
```

### `scrapers/plan_api.py` — `get_official_plan_projection`
```python
if _is_compact() and len(spread_years) > 10:
    spread_out = spread_years[::5] or spread_years   # every 5th year
    compact_fields = {"output_mode": "compact", "asset_spread_total": len(spread_years), "asset_spread_shown": len(spread_out)}
else:
    spread_out = spread_years
    compact_fields = {}
```

### `server.py` — update 4 tool descriptions
Add a sentence to each affected tool: `"Set EMONEY_COMPACT=1 in your environment for a truncated summary output."` (and document `EMONEY_COMPACT` in the `get_features` tool response and CLAUDE.md).

### `CLAUDE.md` — env var table
Add `EMONEY_COMPACT` row.

---

## Tests
- `test_compact_modes.py` (new file):
  - `EMONEY_COMPACT=1` → `get_budget_vs_actual` returns ≤10 categories with `categories_total`
  - `EMONEY_COMPACT=1` → `run_monte_carlo_retirement` has no `year_by_year_percentiles` key
  - `EMONEY_COMPACT=1` → `get_lifetime_cash_flow_projection` has `years_total` + `years_shown` < `years_total`
  - `EMONEY_COMPACT` unset → no `output_mode` field (backward compat)

## Version
Bump `1.2.0` → `1.2.1` (additive env-var feature, backward-compat).
