# Plan: emoney-mcp New Features

## Overview

7 new MCP tools + 3 infrastructure improvements. No new dependencies. All follow existing patterns documented in `CLAUDE.md`.

---

## New Tools

### 1. `get_monthly_review` → `scrapers/goals.py`

Parallel-fetches ~6 existing functions and compiles a structured monthly report. Pattern mirrors `get_financial_summary`.

```python
async def get_monthly_review(http_session) -> dict:
    # asyncio.gather: net worth (cards 9+11), performance (card 3),
    #   spending trends (SNB), budget vs actual (SNB), savings rate (SNB), goals (card 2)
    # Returns:
    {
      "period": "June 2026",
      "net_worth": {"current": ..., "change_mtd": ..., "change_ytd": ...},
      "investments": {"value": ..., "change_today": ..., "change_mtd": ...},
      "spending": {"total": ..., "vs_budget": ..., "top_categories": [...]},
      "savings_rate": ...,
      "goals_on_track": N,
      "goals_off_track": N,
      "action_items": [...]   # derived from anomalies + goal status
    }
```

---

### 2. `get_unusual_transactions` → `scrapers/spending.py`

Uses `_fetch_snb_data`. Groups transactions by normalized merchant, computes per-merchant historical average, flags transactions exceeding `threshold_pct` of that average OR category spend > 2x category monthly average.

**Parameters**: `days` (default 90 — lookback window), `threshold_pct` (default 150 — % above merchant average to flag)

```python
# Returns:
{
  "unusual_transactions": [
    {"date": ..., "merchant": ..., "amount": ..., "category": ...,
     "merchant_avg": ..., "pct_above_avg": ..., "reason": "..."}
  ],
  "summary": {"total_flagged": N, "total_flagged_amount": ...}
}
```

---

### 3. `get_merchant_spending` → `scrapers/spending.py`

Uses `_fetch_snb_data`. Groups by `_normalize_merchant()`, sums totals and counts, optionally filters to a specific merchant substring.

**Parameters**: `days` (default 365), `merchant` (optional filter string), `limit` (default 25, top N merchants)

```python
# Returns:
{
  "period_days": 365,
  "merchants": [
    {"merchant": "COSTCO", "total": ..., "count": ..., "avg_transaction": ..., "last_date": ...}
  ],
  "total_tracked": ...
}
```

---

### 4. `get_year_end_checklist` → `scrapers/tax.py`

Calls existing tax functions in parallel (contribution room, TLH, Roth conversion, cap gains exposure, bracket headroom, RMD) and synthesizes into a structured action checklist with status tags.

**Parameters**: none (always uses current year data)

```python
# Returns:
{
  "tax_year": 2025,
  "checklist": [
    {"item": "Max 401k contribution", "status": "action_needed",
     "detail": "$3,200 remaining room before Dec 31", "priority": "high"},
    {"item": "Tax-loss harvesting", "status": "opportunity",
     "detail": "3 positions with $12,400 unrealized loss in taxable accounts"},
    ...
  ],
  "estimated_tax_savings": ...
}
```

Status values: `"done"`, `"action_needed"`, `"opportunity"`, `"not_applicable"`

---

### 5. `run_scenario` → `scrapers/retirement.py`

Extends `get_net_worth_projection` to accept override parameters. Runs baseline + scenario side-by-side and returns a comparison. Uses same compound-growth math already in retirement.py.

**Parameters**:
- `monthly_savings_delta` — change in monthly savings vs. current (e.g., +500 or -200)
- `target_net_worth` — milestone to reach (default: retirement goal from card 2)
- `retirement_age` — override the goal's target retirement age
- `annual_return_pct` — override the assumed return rate

```python
# Returns:
{
  "scenario": {"monthly_savings_delta": ..., "annual_return": ..., "retirement_age": ...},
  "baseline": {"reach_target_age": ..., "reach_target_date": ..., "net_worth_at_65": ...},
  "scenario_result": {"reach_target_age": ..., "reach_target_date": ..., "net_worth_at_65": ...},
  "delta": {"years_earlier": ..., "additional_net_worth": ...},
  "milestones": [{"amount": ..., "baseline_date": ..., "scenario_date": ...}]
}
```

---

### 6. `get_cash_flow_forecast` → `scrapers/spending.py`

Uses recurring charges data (already computed) + recent income patterns to project month-by-month cash flow.

**Parameters**: `months` (1–6, default 3)

```python
# Returns:
{
  "forecast": [
    {
      "month": "July 2026",
      "projected_income": ...,
      "projected_expenses": {"recurring": ..., "discretionary_estimate": ..., "total": ...},
      "projected_net": ...,
      "cumulative_net": ...
    }
  ],
  "assumptions": {"income_basis": "3-month average", "discretionary_basis": "6-month average"}
}
```

---

### 7. `get_insurance_gap_analysis` → `scrapers/planning.py` (new file)

Computes *insurance need* from existing data (income, net worth, goals/dependents) using standard financial planning rules. Does not require unknown card IDs — purely analytical.

- **Life insurance need**: 10–12× gross annual income, minus liquid net worth
- **Disability need**: 60–70% of gross monthly income
- **Emergency fund adequacy**: 3–6 months of expenses (cross-checks against liquid assets)

**Parameters**: `income_multiple` (default 10), `disability_pct` (default 0.65)

```python
# Returns:
{
  "annual_income_estimate": ...,
  "life_insurance": {
    "estimated_need": ...,
    "liquid_assets": ...,
    "gap": ...,           # positive = under-insured
    "methodology": "10x annual income minus liquid net worth"
  },
  "disability": {
    "monthly_income": ...,
    "recommended_monthly_benefit": ...,
    "note": "Compare against your actual disability policy benefit"
  },
  "emergency_fund": {
    "monthly_expenses": ...,
    "recommended_minimum": ...,   # 3 months
    "recommended_target": ...,    # 6 months
    "liquid_assets": ...,
    "status": "adequate" | "below_minimum" | "above_target"
  }
}
```

---

## Infrastructure Changes

### 8. Selective cache invalidation

**Files**: `scrapers/_helpers.py`, `scrapers/spending.py`, `scrapers/__init__.py`, `server.py`

Add `clear_cache(module: str = "all")` to `__init__.py`:
- `"cards"` → calls `clear_card_cache()` from `_helpers.py`
- `"spending"` → calls `clear_snb_cache()` from `spending.py`
- `"all"` → both (same as existing `clear_caches()`)

Register as an MCP tool in `server.py`:
```
Tool(name="clear_cache", inputSchema: {module: enum["cards","spending","all"], default "all"})
```

---

### 9. Session health check

**File**: `server.py` (`_get_session_or_err`)

Add a module-level `_last_health_ts: float = 0`. In `_get_session_or_err()`, if more than 5 minutes since last check, call `session.is_logged_in()` in the background. If it returns False, inject `"session_warning": "Session may be stale — run sync_chrome_session if you see errors"` into the next tool result. Does not block the tool call.

---

### 10. Enhance `explore_emoney_cards` → `get_available_cards`

**File**: `scrapers/portfolio.py`, `server.py`

Rename/alias `explore_emoney_cards` to also be accessible as `get_available_cards`. Improve the output to include a summary table of responding cards with their top-level key names (data shape fingerprint), sorted by card ID. Keep `explore_emoney_cards` as-is for backward compatibility.

---

## File Touch Map

| File | Changes |
|------|---------|
| `scrapers/goals.py` | Add `get_monthly_review` |
| `scrapers/spending.py` | Add `get_unusual_transactions`, `get_merchant_spending`, `get_cash_flow_forecast` |
| `scrapers/tax.py` | Add `get_year_end_checklist` |
| `scrapers/retirement.py` | Add `run_scenario` |
| `scrapers/planning.py` | **New file** — `get_insurance_gap_analysis` |
| `scrapers/portfolio.py` | Enhance `explore_emoney_cards` / add `get_available_cards` |
| `scrapers/__init__.py` | Export all new functions + `clear_cache` |
| `scraper.py` | Add all new names to explicit import list |
| `server.py` | 8 new `Tool(...)` entries in `list_tools()`, 8 new `elif` branches + private wrappers |
| `tests/` | New test files for each tool |
| `CLAUDE.md` | Update module map + tool count |
| `pyproject.toml` | Bump version (0.8.0) |

---

## Implementation Order

1. Infrastructure first (cache invalidation #8, session health #9) — low risk, immediately useful
2. Spending tools (#2, #3, #6) — same module, same SNB data pattern
3. Synthesizer tools (#1, #4) — depend on existing tools being stable
4. Retirement/scenario tools (#5) — self-contained math
5. Planning tools (#7, #10) — new module + portfolio enhancement
6. server.py registration — all at once after scraper work is done
7. Tests + CLAUDE.md + version bump
