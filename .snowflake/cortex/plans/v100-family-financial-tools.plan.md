# Plan: v1.0.0 — Family Financial Planning Tools (12 new tools)

## Overview

Add 12 new tools across 5 scraper modules. Tool count goes from 64 → 76. Version bump to 1.0.0 (major milestone release).

---

## Tool placement

| Tool | Module | Data sources |
|------|--------|-------------|
| `get_home_equity` | `planning.py` | Cards 9 + 1 (via `get_accounts`) |
| `get_fire_number` | `planning.py` | Cards 9 + 1, SNB 365-day spend |
| `get_gifting_and_estate_strategy` | `planning.py` | Cards 9 + 1 + 2025 IRS constants |
| `get_debt_overview` | `accounts.py` | Cards 9 + 1 (via `get_accounts`) |
| `get_50_30_20_analysis` | `spending.py` | SNB API |
| `get_spending_by_account` | `spending.py` | SNB raw transactions (`_fetch_snb_raw`) |
| `get_upcoming_bills` | `spending.py` | SNB raw + `get_recurring_charges` logic |
| `get_portfolio_concentration` | `portfolio.py` | `GetInvestmentData` |
| `get_net_worth_velocity` | `portfolio.py` | Card 8 history |
| `get_tax_drag_analysis` | `portfolio.py` | `GetInvestmentData` + `_build_account_type_map` |
| `get_financial_independence_roadmap` | `retirement.py` | Cards 9 + 1, SNB, `get_savings_rate` |
| `get_annual_tax_advantaged_summary` | `tax.py` | `get_retirement_accounts`, investment transactions, 2025 IRS limits |

---

## Step 1 — `planning.py`: `get_home_equity` + `get_fire_number`

### `get_home_equity`

Scans `get_accounts` output for property accounts (group name / account type contains "real estate", "property", "home", "house") and mortgage/HELOC liabilities (balance < 0, name contains "mortgage", "heloc", "home loan").

```python
async def get_home_equity(http_session) -> dict:
    """
    Returns: {
      properties: [{name, value, mortgage_balance, equity, ltv_pct}],
      total_property_value, total_mortgage_balance, total_equity,
      equity_pct_of_net_worth, note
    }
    """
```

### `get_fire_number`

Uses 12-month SNB spending as the annual spending baseline. FI number = 25× annual spending (4% SWR). Computes gap from current investable net worth and years-to-FI at current savings rate.

```python
async def get_fire_number(
    http_session,
    swr: float = 0.04,         # safe withdrawal rate (default 4%)
    annual_return: float = 0.07,
) -> dict:
    """
    Returns: {
      annual_spending, fi_number, current_investable_assets,
      gap_to_fi, pct_of_way_there,
      years_to_fi_at_current_savings, fi_date_estimate,
      monthly_savings_needed_by_age: {55, 60, 65},
      lean_fi_number (3% SWR), fat_fi_number (3.5% spending bump),
      note
    }
    """
```

---

## Step 2 — `planning.py`: `get_gifting_and_estate_strategy` | `accounts.py`: `get_debt_overview`

### `get_gifting_and_estate_strategy`

Uses net worth from Card 9 and 2025 IRS constants (annual gift exclusion = $18,000/person, estate exemption = $13,610,000/person). Optionally reads spending/income to estimate gift capacity.

```python
async def get_gifting_and_estate_strategy(
    http_session,
    num_recipients: int = 2,    # people you plan to gift to per year
    filing_status: str = "mfj",
) -> dict:
    """
    Returns: {
      estate_snapshot: {gross_estate, federal_exemption, taxable_estate, estimated_estate_tax},
      annual_gifting: {exclusion_per_person, total_annual_exclusion, recipients},
      529_superfunding: {max_5yr_front_load, per_beneficiary},
      strategies: [list of action items with estimated impact],
      note
    }
    """
```

### `get_debt_overview` (in `accounts.py`)

Filters all accounts with negative balance from `get_accounts`. Groups by debt type (mortgage, HELOC, auto, student loan, credit card) using keyword matching. Computes estimated monthly interest, total annual interest cost, and payoff date per account using assumed APRs.

```python
async def get_debt_overview(
    http_session,
    assumed_mortgage_apr: float = 0.065,
    assumed_cc_apr: float = 0.22,
    assumed_auto_apr: float = 0.07,
    assumed_student_apr: float = 0.055,
) -> dict:
    """
    Returns: {
      debts: [{name, balance, type, assumed_apr, est_monthly_interest, est_payoff_months}],
      summary: {total_debt, total_monthly_interest, total_annual_interest,
                debt_to_assets_pct, debt_free_date_estimate},
      by_type: {mortgage, credit_card, auto, student, other},
      note
    }
    """
```

---

## Step 3 — `spending.py`: Three new tools

### `get_50_30_20_analysis`

Classifies all SNB spending categories into three buckets using a hardcoded map:
- **Needs** (50%): Groceries, Utilities, Insurance, Healthcare, Housing/Rent, Auto (gas/maintenance/insurance), Minimum debt payments
- **Wants** (30%): Dining, Entertainment, Shopping, Travel, Subscriptions, Personal care, Hobbies
- **Savings/Debt** (20%): Paycheck/Salary (negative = contribution), Investment, Transfer to savings

```python
async def get_50_30_20_analysis(http_session, months: int = 3) -> dict:
    """
    Returns: {
      period_months, total_income, total_spending,
      needs:   {actual, actual_pct, target_pct: 50, status, top_categories},
      wants:   {actual, actual_pct, target_pct: 30, status, top_categories},
      savings: {actual, actual_pct, target_pct: 20, status, top_categories},
      monthly_averages: {needs, wants, savings, income},
      recommendations: [list of actionable items],
      note
    }
    """
```

### `get_spending_by_account`

Groups SNB raw transactions by their `accountId` field. Emoney names accounts in the transaction payload. Returns spending totals per linked account so the family can see which card/account is being used for what.

```python
async def get_spending_by_account(http_session, days: int = 30) -> dict:
    """
    Returns: {
      period_days, accounts: [{
        account_id, account_name, total_spent, transaction_count,
        top_categories: [{category, total}],
        transactions_returned
      }],
      note
    }
    """
```

### `get_upcoming_bills`

Uses `_fetch_snb_raw` to find all recurring-pattern merchants (same logic as `get_recurring_charges`) and projects their next occurrence date forward from the last charge date.

```python
async def get_upcoming_bills(http_session, days_ahead: int = 30) -> dict:
    """
    Returns: {
      as_of, days_ahead,
      upcoming: [{merchant, expected_date, expected_amount, cadence, days_until,
                  overdue (bool — charge expected but not yet seen this cycle)}],
      total_expected_amount,
      note
    }
    """
```

---

## Step 4 — `portfolio.py`: Three new tools

### `get_portfolio_concentration`

Computes each position as % of total portfolio. Flags positions exceeding 5%, 10%, and 20% thresholds. Adds sector concentration (bond vs equity vs cash vs other) and single-stock vs fund risk score.

```python
async def get_portfolio_concentration(
    http_session,
    concentration_threshold_pct: float = 10.0,
) -> dict:
    """
    Returns: {
      total_portfolio_value,
      concentrated_positions: [{ticker, description, value, pct_of_portfolio, risk_level}],
      diversification_grade: A-F,
      top_10_positions: [{ticker, value, pct}],
      asset_type_breakdown: {single_stocks_pct, funds_pct, other_pct},
      recommendations: [],
      note
    }
    """
```

### `get_net_worth_velocity`

Pulls Card 8 (up to 60 months of history). Computes monthly change, 3-month rolling average, 12-month growth rate, year-over-year comparison, and projects 12-months-ahead at current velocity.

```python
async def get_net_worth_velocity(http_session, months: int = 12) -> dict:
    """
    Returns: {
      current_net_worth, months_analyzed,
      avg_monthly_gain, avg_annual_gain_rate_pct,
      this_year_gain, last_year_gain, yoy_acceleration_pct,
      projected_net_worth_12mo, projected_net_worth_date,
      monthly_history: [{month, net_worth, change, change_pct}],
      trend: "accelerating" | "decelerating" | "stable",
      note
    }
    """
```

### `get_tax_drag_analysis`

Extends `get_asset_location_efficiency` by computing dollar-cost estimates. For each misplaced holding: estimate annual income/distribution yield by asset class, compute tax drag = yield × balance × (marginal_rate − qualified_dividend_rate). Returns total annual drag and prioritized swap list.

```python
async def get_tax_drag_analysis(
    http_session,
    marginal_rate: float = 0.32,
    ltcg_rate: float = 0.15,
) -> dict:
    """
    Returns: {
      total_annual_tax_drag_estimate,
      misplaced_positions: [{ticker, account, account_type, asset_class,
                             balance, est_yield, annual_drag_estimate, recommended_account}],
      well_placed_positions: count,
      total_drag_as_pct_of_portfolio,
      priority_swaps: top 5 by drag,
      note
    }
    """
```

---

## Step 5 — `retirement.py`: `get_financial_independence_roadmap`

Applies Fidelity's salary-multiple benchmarks (1× by 30, 3× by 40, 6× by 50, 8× by 60, 10× by retirement) to the family's current position. Also computes Coast FI (the portfolio value needed today such that growth alone reaches FI without further contributions).

```python
async def get_financial_independence_roadmap(
    http_session,
    current_age: int | None = None,
    retirement_age: int = 65,
) -> dict:
    """
    Returns: {
      current_investable_assets, annual_income,
      fidelity_benchmarks: [{age, multiplier, target, current_gap, on_track}],
      current_milestone: {label, multiplier, achieved_at_age},
      next_milestone: {label, target, gap, years_at_current_pace},
      coast_fi: {target_today, current_assets, gap, years_until_coast},
      fi_number (25× spending), years_to_fi,
      note
    }
    """
```

---

## Step 6 — `tax.py`: `get_annual_tax_advantaged_summary`

Pulls YTD contribution estimates from investment transactions filtered to contribution-type transactions (deposits to retirement accounts). Cross-references against 2025 limits from existing `_CONTRIBUTION_LIMITS` constant. Falls back to showing current balance + remaining room if transactions don't clearly show contributions.

```python
async def get_annual_tax_advantaged_summary(
    http_session,
    age: int | None = None,
) -> dict:
    """
    Returns: {
      tax_year, as_of,
      accounts: [{
        account_type, account_name, ytd_contributions_estimate,
        annual_limit, catch_up_eligible, remaining_room,
        current_balance, pct_of_limit_used
      }],
      totals: {ytd_all_accounts, remaining_room_all, annual_limits_total},
      deadline: "April 15 for IRA, December 31 for 401k/HSA",
      note
    }
    """
```

---

## Step 7 — Wire all 12 tools

For each new tool, follow the 6-location pattern from CLAUDE.md:

1. **`scrapers/<module>.py`** — implement the function (done in Steps 1-6)
2. **`scrapers/__init__.py`** — add to `from .<module> import (...)` block and `__all__`
3. **`scraper.py`** — add to explicit import list (v1.0.0 section)
4. **`server.py` → `list_tools()`** — add `Tool(name=..., description=..., inputSchema=...)`
5. **`server.py` → `_call_tool_inner()`** — add `elif name == "..."` dispatch
6. **`server.py`** — add `async def _<tool_name>(...) -> dict:` private wrapper

---

## Step 8 — Tests

Create two new test files:

### `tests/test_planning_extended.py`
- `TestGetHomeEquity` — property/mortgage extraction, LTV calculation, no-property edge case, error propagation
- `TestGetFireNumber` — FI number math, gap calculation, years-to-FI, monthly savings targets
- `TestGetGiftingAndEstateStrategy` — estate tax threshold logic, annual exclusion math, 529 superfunding
- `TestGetDebtOverview` — debt categorization, interest estimates, debt-free date

### `tests/test_spending_extended.py`
- `TestGet503020Analysis` — bucket classification, % calculation, over/under status
- `TestGetSpendingByAccount` — grouping by account_id, top categories per account
- `TestGetUpcomingBills` — next-occurrence projection, overdue detection

### `tests/test_portfolio_extended.py`
- `TestGetPortfolioConcentration` — flagging thresholds, diversification grade, top-10 list
- `TestGetNetWorthVelocity` — monthly change calc, YoY comparison, projection
- `TestGetTaxDragAnalysis` — drag estimate for misplaced bond funds, well-placed assets show $0 drag

### Additions to `tests/test_tax_math.py`
- `TestGetAnnualTaxAdvantagedSummary` — contribution limits, remaining room, catch-up eligibility

### Additions to `tests/test_v0_8_features.py` (or new retirement test file)
- `TestGetFinancialIndependenceRoadmap` — Fidelity benchmarks, Coast FI math

---

## Step 9 — Docs

**`CHANGELOG.md`**: Add `[1.0.0]` section documenting all 12 new tools.

**`README.md`**: 
- Update the intro question list with examples of new tools ("Are we on track for retirement by Fidelity's benchmarks?", "When can we FI?", "What's our home equity?")
- Update tool count (76 tools)
- Add new tools to the tools table

**`README_PYPI.md`**: Mirror the key changes.

---

## Step 10 — Version bump + release

- `pyproject.toml`: `0.9.2` → `1.0.0`
- Commit with message `feat: v1.0.0 — 12 new family financial planning tools (76 tools total)`
- Push to `origin/main` (triggers PyPI publish workflow)

---

## File change summary

| File | Change |
|------|--------|
| `scrapers/planning.py` | +3 functions: `get_home_equity`, `get_fire_number`, `get_gifting_and_estate_strategy` |
| `scrapers/accounts.py` | +1 function: `get_debt_overview` |
| `scrapers/spending.py` | +3 functions: `get_50_30_20_analysis`, `get_spending_by_account`, `get_upcoming_bills` |
| `scrapers/portfolio.py` | +3 functions: `get_portfolio_concentration`, `get_net_worth_velocity`, `get_tax_drag_analysis` |
| `scrapers/retirement.py` | +1 function: `get_financial_independence_roadmap` |
| `scrapers/tax.py` | +1 function: `get_annual_tax_advantaged_summary` |
| `scrapers/__init__.py` | +12 imports and `__all__` entries |
| `scraper.py` | +12 imports |
| `server.py` | +12 Tool definitions + dispatch branches + private wrappers |
| `tests/test_planning_extended.py` | new file |
| `tests/test_spending_extended.py` | new file |
| `tests/test_portfolio_extended.py` | new file |
| `tests/test_tax_math.py` | additions |
| `tests/test_v0_8_features.py` or new file | additions |
| `CHANGELOG.md` | v1.0.0 section |
| `README.md` | updated examples + tool count |
| `README_PYPI.md` | updated |
| `pyproject.toml` | `1.0.0` |
