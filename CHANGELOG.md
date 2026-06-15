# Changelog

All notable changes to emoney-mcp are documented here.

## [1.0.7] — 2026-06-15 (current)

### Fixed

- **`scrapers/_helpers.py`** — `_get_card` no longer crashes with `AttributeError` when a card returns a JSON `null` or non-object body. Previously `resp.json().get("Data")` raised on such responses; `get_available_cards` (which probes undocumented card IDs 1–16) hit this on the live account. Now guards the payload type before `.get`. **Found by the new live smoke harness (#20).**

### Added

- **`scripts/smoke.py`** (#20) — opt-in live smoke test that runs all 46 read-only tools against a real session and cross-checks for shape/ordering bugs (e.g. `velocity.current_net_worth == get_accounts.net_worth`, chronological history). Not part of CI; run via `uv run python scripts/smoke.py`.
- **`browser.py`** (#25) — macOS Chrome cookie extraction (Keychain `Chrome Safe Storage` key + PBKDF2-HMAC-SHA1 → AES-128-CBC). `sync_chrome_session` now works natively on macOS instead of always falling back to the nodriver window.
- **ruff + mypy** (#21) — `ruff` (pyflakes/F + E9) is now a hard CI gate; `mypy` runs as an advisory step. Cleaned 98 lint findings (unused imports/vars, empty f-strings). Re-export shims (`scraper.py`, `scrapers/__init__.py`) are excluded from F401/F403.

### Changed

- **`scrapers/transactions.py`, `scrapers/reports.py`** (#24) — `add_transaction_rule`, `apply_transaction_rule`, and `get_report_url` no longer leak raw Emoney payloads by default. The `raw` field is included only when `EMONEY_DEV` is set (via a `_maybe_raw` helper); the unrecognized-response paths now return a clean error.

### CI

- **Version-bump guard** (#22) — a new CI job fails any push/PR that changes `src/emoney_mcp/` without bumping the `pyproject.toml` version, so a code change can't silently skip the PyPI release.

## [1.0.6] — 2026-06-15

### Security

- **`scrapers/_helpers.py`** — `_get_card` now coerces `card_id` to `int` before building the request URL and returns `None` if coercion fails. Previously a crafted value (e.g. via the user/model-supplied `card_ids` list passed to `explore_emoney_cards`/`get_available_cards`) was interpolated directly into `…/GetCard/{card_id}?…`, allowing path or query injection against the authenticated emaplan.com session (e.g. `"8/../SignOut"`). The guard rejects such input without issuing any HTTP request; valid integer and numeric-string IDs are unaffected.
- **`browser.py`** — `save_cookies` now hardens permissions on every write: the session directory is set to `0o700` and the cookie file to `0o600` via `os.fchmod`. The mode passed to `os.open` only applies when a file is first created, so a pre-existing session file with looser permissions was never tightened — leaving credential-equivalent session cookies potentially readable by other local users. Guarded for platforms without `os.fchmod` (Windows).

### Tests

- Added `TestGetCardIdCoercion` (4 tests) covering path/query injection rejection, numeric-string acceptance, and `None` handling.
- Added `TestSaveCookiesPermissions` (2 tests) verifying new-file and pre-existing-file permission tightening (skipped where POSIX perms don't apply).

## [1.0.5] — 2026-06-15

### Fixed

- **`scrapers/portfolio.py`** — `get_net_worth_velocity` read the Card 8 `History` array with the wrong ordering assumption ("newest first" + take first N + label index 0 as the current month). Card 8 is actually a dict whose `History` array is **oldest first / newest last** (same shape `get_net_worth_history` already relied on). The bug **reversed the series**, turning real net-worth growth into a fabricated decline — e.g. reporting current net worth as $6.02M instead of the true $8.47M, an avg "loss" of $611k/month, and projecting **negative net worth** in 12 months when net worth had actually grown ~40% over the period. Now extracts `History` from the dict, keeps the most recent N via `[-months:]`, labels the newest point as the current month, and sources current net worth from the authoritative `NetWorth` field (falling back to the newest history point).
- **`tests/test_portfolio_extended.py`** — the `_NW_HISTORY_GROWING` fixture was a bare **newest-first** list, which did not match the real Card 8 payload and is why the bug went undetected. Replaced with a realistic Card 8 dict (`NetWorth` + oldest-first `History`). Updated assertions: current net worth must equal the newest point, and `monthly_history` must run chronologically oldest→newest. Added `test_history_is_chronological_oldest_first`.

## [1.0.4] — 2026-06-15

### Fixed

- **`scrapers/transactions.py`** — corrected three transaction/rules bugs found during live testing: `get_transaction_splits` now normalizes the bare-list API response into a clean `{split_count, is_split, splits[], total_amount}` shape (was leaking raw Emoney internals); `get_transaction_rules` sends an empty payload (matching the JS `data:{}`) instead of `filter=""` and treats Emoney's HTTP 500 "no rules" response as an empty result; `apply_transaction_rule` sends the correct `{ruleID, transactionID}` payload instead of a full rule object. Removed dead payload-construction code in `add_transaction_rule`.
- **`tests/test_transaction_writes.py`** — updated to match corrected behavior (clean splits output, empty GetRules payload, direct ApplyRule payload).

## [1.0.3] — 2026-06-14

### Fixed — Category classification correctness

**Phase 1: Corrected name-based classification sets to use real Emoney category names**

All hardcoded category name sets in `spending.py` had a mix of phantom names (strings that don't exist in the actual Emoney category list) and missing real categories, causing silent misclassification.

- `_INCOME_CATEGORIES` — Added `Net Salary`, `Bonus`, `Investment Income`, `Other Income`, `Tax Refund`. Removed `ACH Transfer` and `Dividend & Cap Gains` (phantom names). Fixed `Dividend` (was wrong). Now covers all 9 real income category types.
- `_EXCLUDE_CATEGORIES` — Added `Excluded` (Emoney's `-1` hidden category). Removed `Internal Transfer` (phantom). Now 3 correct entries.
- `_NON_MERCHANT_CATEGORIES` — Replaced phantom names (`ACH Transfer`, `Internal Transfer`, `Investment`, `Dividend & Cap Gains`) with real equivalents. Added all tax payment categories and savings/investment contribution categories.
- `_NEEDS_CATEGORIES` (50/30/20) — Completely rebuilt with real Emoney names. Was using `"Healthcare"`, `"Utilities"`, `"Mortgage"`, `"Gas/Fuel"`, `"Auto Maintenance"` etc. (none of which exist). Now maps to actual categories: `"Health & Fitness"`, `"Bills & Utilities"`, `"Mortgage & Rent"`, `"Gas & Fuel"`, `"Auto Service"`, etc. Added 30+ correctly-named categories.
- `_SAVINGS_CATEGORIES` (50/30/20) — Replaced `"Investment"`, `"Retirement"`, `"401k"`, `"IRA"`, `"529"` (phantom) with real categories: `"Investment Savings"`, `"Retirement Savings"`, `"College Savings"`, `"Savings"`. Removed income categories from savings bucket (they belong in the income denominator, not the bucket).

**Phase 2: ID-based income and exclude classification in `_fetch_snb_data`**

- Added `_INCOME_CATEGORY_IDS` and `_EXCLUDE_CATEGORY_IDS` frozensets using numeric IDs (immune to category renames).
- `_fetch_snb_data` now classifies `is_income` and `is_excluded` using integer `categoryId` from the raw SNB payload, not string category name lookup. This prevents `Bonus`, `Net Salary`, and other income categories from being counted as spending.
- Added `category_id` field to normalized transaction dicts returned by `_fetch_snb_data`.
- `get_spending_by_account` and `get_upcoming_bills` also switched to ID-based income/exclude filtering.

**Practical impact of the bugs fixed:**
- `Bonus` income was being counted as spending in savings rate, FIRE number, and insurance gap calculations
- `Net Salary` was being counted as spending  
- `get_50_30_20_analysis` needs/savings buckets were almost entirely wrong (phantom names never matched real transactions)
- `Excluded` transactions (-1) were not being excluded from analytics

---

## [1.0.2] — 2026-06-14

### Added — Live endpoint discoveries (2 new tools + 3 enhancements)

Discovered via live session probing with `explore_emoney_site` + direct API calls.

#### New tools

- **`get_client_profile`** — Returns household profile from `Profile/GetProfileData`: names, dates of birth, computed ages, dependents (Parker), and property entries. The `primary.age` and `primary.birth_year` fields auto-populate retirement and tax tools that previously required manual `age`/`birth_year` parameters.

- **`get_aggregation_status`** — Returns account connection health from CardSwitcher Card 20. Shows which institution connections are `Disconnected` (preventing data refresh), with institution name and status description. Useful for diagnosing stale balances.

#### Enhancements

- **`get_spending_by_account`** now resolves account names using the SNB `GetAccounts` endpoint (`api.emoneyadvisor.com/snb-api/api/values/GetAccounts`). Previously the `accountId` field on raw transactions was an opaque integer ID — now it maps to the actual account name ("Drew Visa", "Lacey MC", etc.). Falls back gracefully if `GetAccounts` fails.

- **`get_portfolio_concentration`** now fetches CardSwitcher Card 6 in parallel with `GetInvestmentData`. Card 6 returns the top holdings with live ticker symbols (VTSAX, SNOW, SPAXX, etc.) in a lightweight format, surfaced as `card6_top_holdings` in the response. This supplements the full holdings breakdown from `GetInvestmentData`.

- **`get_home_equity`** now fetches CardSwitcher Card 10 in parallel with `get_accounts`. Card 10 returns `Cash` and `Credit` totals from a different data path than Card 9, providing more granular liquid cash and credit card totals in the response (`liquid_cash`, `credit_card_balance` fields).

#### Internal

- `_fetch_snb_account_map()` helper added to `spending.py` — TTL-cached SNB account map (account_id → name). Called in parallel with `_fetch_snb_raw` by `get_spending_by_account`.
- `clear_snb_cache()` now also clears `_snb_account_cache`.

### Tests

- `test_live_endpoint_discoveries.py` — 25 new tests covering all new tools and enhancements
- Updated `test_spending_extended.py` to mock `_fetch_snb_account_map` in `TestGetSpendingByAccount`
- Total: **361 tests**

---

## [1.0.1] — 2026-06-14

### Fixed
- **`run_scenario` milestone list** was missing the $10M milestone — `_MILESTONES` was silently redefined inside `_project()` with `[500k, 1M, 2M, 5M]` instead of the canonical `[500k, 1M, 2M, 5M, 10M]` used by `get_net_worth_projection`. Results for high-net-worth projections now match between the two tools.

### Refactored
- **`get_accounts` now uses `_get_card()` cache** — previously bypassed the TTL cache with raw HTTP calls, causing duplicate card 9 and card 1 requests on multi-tool turns. All account tools now share the 5-minute card cache.
- **`_month_offset` moved to `_helpers.py`** — eliminated a byte-for-byte duplicate (`_month_offset_local`) that existed in `portfolio.py`. Both `spending.py` and `portfolio.py` now import from the single canonical definition.
- **`_get_investment_data()` helper in `portfolio.py`** — the 4-line `GetInvestmentData` HTTP fetch + error check was duplicated across `get_asset_location_efficiency`, `get_rebalancing_targets`, `get_portfolio_concentration`, and `get_tax_drag_analysis`. All four now delegate to the new helper.
- **`_calc_investable_assets()` helper in `accounts.py`** — the investable-assets calculation (net worth minus real-estate equity) was duplicated in `get_fire_number` and `get_financial_independence_roadmap`. Both now use the shared helper.
- **`_sum_income_spending()` helper in `spending.py`** — the SNB transaction income/spending accumulation loop was duplicated across `get_fire_number`, `get_insurance_gap_analysis`, and `get_financial_independence_roadmap`. All three now use the shared helper.
- **Removed dead code** — `if False` unreachable branch in `portfolio.get_portfolio_concentration`.
- **Replaced `__import__("datetime")` antipattern** with proper module-level `from datetime import datetime` imports in `portfolio.py` and `retirement.py`.
- **Removed redundant in-function imports** — 14 instances of `import asyncio`, `import math`, `from ._helpers import _INV_URL`, etc. that duplicated module-level imports across `portfolio.py` and `retirement.py`.
- **Test isolation** — added `autouse` fixture in `conftest.py` to clear the card cache between tests, preventing cross-test pollution from the now-shared `_get_card()` cache.

---

## [1.0.0] — 2026-06-14

### Added — 12 new family financial planning tools (76 tools total)

#### Home & Real Estate (`scrapers/planning.py`)
- **`get_home_equity`** — Returns property values, mortgage balances, equity, and LTV per property, plus equity as % of net worth. Sources data from Card 1 account groups.

#### Financial Independence (`scrapers/planning.py`, `scrapers/retirement.py`)
- **`get_fire_number`** — Computes the FI number (annual_spending ÷ SWR), gap from current investable assets, percent of the way there, years-to-FI at current savings rate, and monthly savings needed to reach FI in 15/20/25 years. Uses 12-month SNB spend as the baseline.
- **`get_financial_independence_roadmap`** — Progress against Fidelity's salary-multiple milestones (1× by 30, 3× by 40, 6× by 50, 10× by 65) plus Coast FI calculation. Optional `current_age` and `retirement_age` parameters.

#### Debt & Estate Planning (`scrapers/accounts.py`, `scrapers/planning.py`)
- **`get_debt_overview`** — Consolidated debt table with type classification (mortgage, credit card, auto, student), estimated APR, monthly interest, annual interest cost, and payoff date at minimum payments.
- **`get_gifting_and_estate_strategy`** — Estate tax exposure, annual gift exclusion capacity, 529 superfunding opportunity, and prioritized action list. Uses 2025 IRS constants (exemption $13.61M single / $27.22M MFJ, $18k annual exclusion).

#### Spending Analysis (`scrapers/spending.py`)
- **`get_50_30_20_analysis`** — Classifies all spending categories into Needs/Wants/Savings buckets and compares against the 50/30/20 guideline with status and recommendations.
- **`get_spending_by_account`** — Groups spending by linked bank/credit card account, showing totals and top categories per account. Useful for families with multiple cards.
- **`get_upcoming_bills`** — Projects recurring charges expected in the next N days from 120-day charge history. Flags overdue charges (expected but not yet seen in the feed).

#### Portfolio Analysis (`scrapers/portfolio.py`)
- **`get_portfolio_concentration`** — Flags positions exceeding a concentration threshold (default 10%), scores diversification A-F, and returns single-stock vs. fund breakdown.
- **`get_net_worth_velocity`** — Rate of net worth growth from Card 8 history: month-over-month change, year-over-year comparison, trend (accelerating/stable/decelerating), and 12-month projection.
- **`get_tax_drag_analysis`** — Estimates annual dollar cost of holding tax-inefficient assets (bonds, REITs) in taxable accounts. Returns per-position drag and priority swap list.

#### Tax Planning (`scrapers/tax.py`)
- **`get_annual_tax_advantaged_summary`** — Shows 2025 IRS limits for 401k, IRA, HSA, and 529 alongside current balances, catch-up eligibility by age, and key contribution deadlines.

### Tests
- 72 new tests across 4 new test files: `test_planning_extended.py`, `test_spending_extended.py`, `test_portfolio_extended.py`, `test_retirement_tax_extended.py`
- Total test count: 336

---

## [0.9.2] — 2026-06-13

### Changed
- Version bump to trigger PyPI release of v0.9.1 fixes (transaction_id/category_id in spend transactions, GetRules 500 fix, get_categories tool, 66 new tests).

---

## [0.9.1] — 2026-06-13

### Fixed

- **`get_spending_transactions`** now includes `transaction_id` and `category_id` on every transaction record. Both fields were present in the SNB payload but were being dropped during serialization. `update_transaction` and `add_transaction_rule` require these values, so the previous output was effectively unusable for writes.
- **`get_transaction_rules`** was returning HTTP 500 consistently. Two fixes applied:
  - The `GetRules` POST now sends `filter: ""` in the body; an empty POST body appears to trigger a server-side NullReference in the ASP.NET controller.
  - Response parsing handles both list-shaped (`[{...}]`) and dict-shaped (`{id: {...}}`) responses, and defensively unwraps both `{Value, IsValid}` wrapper objects and plain scalar values for `RuleID`/`CategoryID` fields.
- **`_csrf_post`** error messages now include the first 400 bytes of the response body (`response_body` key). Previously the status code alone made remote debugging impossible.

### Added

- **`get_categories`** — returns the full SNB category name→ID map (`[{id, name}]`, sorted by name). Backed by the `GetCategories` cache already populated by `_fetch_snb_raw`, so the call is free when any other SNB tool has run in the same conversation turn. Eliminates the need to reverse-engineer category IDs from rules.

### Changed

- Updated `CHANGELOG.md` with full v0.9.0 detail (pulled from GitHub 2026-06-13).

---

## [0.9.0] — 2026-06-13

### Added — 10 new tools (61 tools total)

#### Transaction writes (`scrapers/transactions.py`)

- **`update_transaction`** — Edit a bank/CC transaction: rename it (`description`) and/or reassign its spending category (`category_id`). POSTs to `CS/Spending/UpdateTransaction` with jQuery bracket-notation payload and ASP.NET anti-forgery token.
- **`hide_transaction`** — Exclude a transaction from Emoney's cash flow view. Required: `transaction_id`.
- **`get_transaction_splits`** — Return existing sub-splits for a transaction. Required: `transaction_id`.
- **`update_transaction_splits`** — Write a new split configuration: list of `{amount, category_id, description}` dicts that must sum to the original transaction amount. Required: `transaction_id`, `splits`.

#### Rules engine (`scrapers/transactions.py`)

- **`get_transaction_rules`** — Fetch the full auto-categorization rule set Emoney applies to new transaction imports. Returns rule ID, merchant/keyword match criteria, and assigned category for each rule.
- **`add_transaction_rule`** — Create a new rule. Pass a `rule` dict with fields like `merchantName`, `keyword`, `categoryId`, `amountMin`, `amountMax`. Emoney applies the rule to future imports.
- **`update_transaction_rule`** — Overwrite specific fields on an existing rule. Required: `rule_id`, `rule` dict with updated fields.
- **`apply_transaction_rule`** — Re-run an existing rule against the full transaction history. Required: `rule_id`.

#### Reports (`scrapers/reports.py`)

- **`get_reports`** — Parse the Emoney Reports page HTML to extract the full report catalog grouped by family (e.g. `LiquidityReport`, `AssetTaxTypeReport`, `EstateTransferReport`). Returns report IDs, labels, and families.
- **`get_report_url`** — POST to `CS/Reports/GetReportUrl` to generate a browser-ready URL for a specific report. Required: `report_id` (from `get_reports`).

### Infrastructure

- All write endpoints use `_csrf_post()` helper that appends `__RequestVerificationToken` (reused from `http_session.get_csrf_token()`) and sets `X-Requested-With: XMLHttpRequest`.
- Nested Emoney form fields use jQuery bracket notation replicated in Python dict keys (e.g. `"TransactionID[Value]"`).
- Rule payloads use flat `rule[field]` notation (`{f"rule[{k}]": v for k, v in rule.items()}`).

---

## [0.7.3] — 2026-06-12

### Fixed

- **`server.py`** — `search_transactions` and `get_spending_transactions` both crashed with `TypeError` on every call because their inner handler functions were missing the `max_results` and `max_transactions` kwargs that `call_tool` was passing; both signatures and downstream scraper calls are now aligned
- **`server.py`** — added a top-level `try/except` in `call_tool` (dispatches via new `_call_tool_inner`) so any unhandled exception returns a structured `{"error": "...", "tool": "..."}` JSON response instead of crashing the MCP session with a protocol error
- **`browser.py`** — session cookie file (`~/.emoney_mcp/session.json`) is now written with `os.open` mode `0o600` (owner-read-only) instead of the process umask default which made it world-readable
- **`browser.py`** — replaced unsafe `tempfile.mktemp()` (TOCTOU race) with `NamedTemporaryFile(delete=False)` in the Chrome cookie extraction path
- **`scrapers/tax.py`** — `get_rmd_estimate` was incorrectly including Roth IRA balances in the RMD base (Roth IRAs have no RMD requirement); now computes traditional-IRA-only balance by filtering individual account records to exclude accounts with "roth" in the name or type
- **`scrapers/spending.py`** — five functions (`get_spending_trends`, `get_savings_rate`, `get_budget_vs_actual`, `get_income_summary`, `get_cash_flow_projection`) were generating month labels with `timedelta(days=i*28)` / `timedelta(days=i*32)` approximations that accumulate a full calendar month of drift over 12 months; replaced with a `_month_offset` helper that uses correct calendar arithmetic
- **`scrapers/_helpers.py`** — failed Emoney card fetches were cached for the full 5-minute TTL, blocking all dependent tools for 5 minutes on a transient server error; error responses are now cached for 30 seconds

## [0.7.2] — 2026-06-09

### Changed
- **`README_PYPI.md`** (new file) — added a separate end-user-focused PyPI description covering installation, Claude Desktop config, first-use login flow, example questions, and a simplified tool reference. Developer content (architecture, internal endpoints, local dev install, testing) remains in `README.md` for GitHub only.
- **`pyproject.toml`** — `readme` now points to `README_PYPI.md` so PyPI displays the end-user description; `README.md` is unchanged and continues to serve as the full GitHub project reference.

## [0.7.1] — 2026-06-09

### Fixed
- **`pyproject.toml`** — added `readme = "README.md"` so PyPI publishes the full project description (tool list, examples, architecture). Previously the PyPI page showed no long description.

## [0.7.0] — 2026-06-09

### Added — 4 new advanced planning tools (42 tools total)

- **`run_monte_carlo_retirement`** — Monte Carlo retirement simulation engine: runs 1,000–10,000 stochastic paths drawing annual returns from a normal distribution parameterized by `mean_return`/`std_dev` and independent inflation draws each year. Returns probability of success, median/10th/25th/75th/90th percentile ending balances, worst/median depletion year, the safe withdrawal rate that achieves 90% success, and a year-by-year percentile table. Accepts `social_security_annual` to offset withdrawals, `withdrawal_rate` as a portfolio percentage override, and configurable simulation count and horizon.

- **`get_dynamic_withdrawal_guardrails`** — Implements Guyton-Klinger guardrail rules to dynamically adjust retirement withdrawals. Compares the current withdrawal rate against upper and lower guardrails defined relative to the initial rate: if the rate drops more than `raise_guard_pct`% below initial the withdrawal is raised 10% (up to a ceiling); if it rises more than `cut_guard_pct`% above initial it is cut 10% (down to a floor). Returns action (RAISE / HOLD / CUT), adjusted annual and monthly withdrawal, and the dollar change from current.

- **`get_social_security_optimizer`** — Computes optimal Social Security claiming age by comparing lifetime benefits at age 62, Full Retirement Age (FRA), and 70. Calculates the FRA by birth year (2026 schedule), applies exact SSA early-reduction and delayed-credit factors, and shows monthly benefit, annual benefit, lifetime value at a configurable life expectancy, and breakeven crossover ages (62 vs. 67, 67 vs. 70, 62 vs. 70). Includes spousal benefit analysis when spouse parameters are provided. Uses a $2,000/mo placeholder if no SSA estimate is supplied and clearly flags the placeholder.

- **`get_quarterly_estimated_taxes`** — Calculates Q1–Q4 federal estimated tax payment amounts and IRS due dates for the current year. Computes two methods — current-year annualized (from inferred or provided income) and IRS safe harbor (100% of prior-year tax; 110% if income > $150k) — and recommends whichever is lower. Accounts for expected W-2 withholding. Returns effective rate, marginal rate, and the full payment schedule for both methods.

## [0.6.0] — 2026-06-09

### Fixed / Refactored
- **`spending.py`** — moved `import asyncio` and `_SNB_API` to module top-level; removed duplicate `from ._helpers import _get_card` calls that were buried inside two functions (`get_budget_vs_actual`, `get_cash_flow_projection`); `_SNB_API` now imported from `_helpers` instead of being redefined locally
- **`accounts.py`** — moved `import time` out of the `get_accounts` function body to module top-level
- **`investments.py`** — removed redundant `from ._helpers import _INV_URL` inside `get_transactions` (already imported at module top)
- **`_helpers.py`** — removed dead `_SPEND_URL` constant (was defined but never imported by any module); `_SNB_API` is now the single source of truth for the SNB API base URL
- **`server.py`** — `_get_features()` now reads the installed package version via `importlib.metadata` instead of hardcoding `"0.5.0"`

## [0.5.0] — 2026

### Added — 8 new planning tools
- **`get_quick_status`** — 5-number snapshot (net worth, portfolio change, savings rate, top spending category, goal status); designed for minimal token usage
- **`get_tax_bracket_headroom`** — shows remaining room in the current ordinary income and LTCG bracket before the next threshold; infers income automatically if not supplied
- **`get_budget_vs_actual`** — compares this month's actual spending against a rolling N-month average per category; flags categories tracking above their average
- **`get_year_over_year`** — compares this year's spending and income to the same calendar year-to-date period last year
- **`get_cash_flow_projection`** — projects future monthly cash flow using actual average income/spending from the last 90 days, layered with known recurring charges
- **`get_college_savings_gap`** — estimates the funding gap between current 529 balances and projected college costs for each education goal
- **`get_debt_payoff_plan`** — models debt payoff using avalanche (highest rate first) and snowball (smallest balance first) strategies with month-by-month simulation
- **`get_net_worth_projection`** — projects net worth forward at configurable return rates

### Refactored
- `scraper.py` split into a `scrapers/` package with domain modules: `accounts`, `investments`, `spending`, `goals`, `tax`, `retirement`, `portfolio`, `_helpers`
- TTL caching added for CardSwitcher cards (5-minute window) and SNB API responses — parallel tool calls within one turn share a single HTTP request per endpoint
- `scraper.py` kept as a backward-compatible re-export shim so `server.py` requires no changes

## [0.4.0] — 2026

### Refactored
- Merged `feature/working-mcp-server` branch into `main`
- Added module-level docstrings and inline comments to all scraper modules
- Codebase restructured in preparation for the v0.5.0 domain split

## [0.3.0] — 2026-06-08

### Added
- **`get_features`** — lists all available tools grouped by category with descriptions, example questions, and parameter summaries; no session or authentication required
- **`CHANGELOG.md`** — full version history from v0.1.0 to present

## [0.2.0] — 2025

### Added
- **`get_tax_loss_harvesting`** — identifies taxable positions with unrealized losses ranked by size, with estimated tax savings at 15%, 20%, and 23.8% LTCG+NIIT rates
- **`get_contribution_room`** — shows 2025 IRS limits for 401k, IRA, HSA, SIMPLE IRA, SEP IRA, and 529 accounts alongside current balances; adjusts for catch-up contributions
- **`get_roth_conversion_analysis`** — estimates federal tax cost and long-term benefit of converting a specified amount from pre-tax to Roth; bracket-by-bracket breakdown
- **`get_capital_gains_exposure`** — identifies taxable positions with large unrealized gains and estimates tax liability if sold today
- **`get_rmd_estimate`** — estimates Required Minimum Distributions using the IRS Uniform Lifetime Table with a 10-year projected RMD schedule
- **`get_retirement_runway`** — models how many years the portfolio sustains withdrawals under conservative (4%), base (6%), and optimistic (8%) return scenarios
- **`get_withdrawal_rate_analysis`** — projects portfolio to retirement year and shows income at 3–5% withdrawal rates
- **`get_asset_location_efficiency`** — grades how well assets are positioned across account types for tax efficiency (A–F) with specific swap suggestions
- **`get_rebalancing_targets`** — computes exact buy/sell amounts to reach a target equity/bond/cash allocation
- **`get_financial_health_score`** — composite 0–100 score across six dimensions: savings rate, goal funding, debt-to-asset ratio, emergency fund, diversification, net worth trend

## [0.1.5] — 2025

### Added
- **`get_financial_summary`** — executive dashboard combining net worth, performance, income vs. spending, top 5 spending categories, and goal status in a single call
- **`search_transactions`** — search spending transactions by keyword, category, and/or amount range
- **`get_recurring_charges`** — detects recurring/subscription charges by analyzing 120-day transaction patterns; estimates monthly recurring spend
- **`get_net_worth_breakdown`** — breaks net worth down by person, liquidity (liquid / semi-liquid / illiquid), and tax treatment (taxable / tax-deferred / tax-free)

## [0.1.4] — 2025

### Added
- **`get_spending_trends`** — month-over-month spending by category showing which categories are trending up, down, or stable
- **`get_income_summary`** — income sources and monthly income trend; identifies paychecks, dividends, and interest income grouped by source
- **`get_savings_rate`** — month-by-month savings rate (income minus spending / income)

## [0.1.3] — 2025

### Added
- **`get_version`** — returns installed version, cookie file path, and session status for debugging
- **`get_spending_transactions`** — bank and credit card transactions with category labels for everyday spending (distinct from investment transactions)

### Fixed
- Cookie file path now stored in `~/.emoney_mcp/session.json` for compatibility with uvx/PyPI installs

## [0.1.2] — 2025

### Added
- PyPI publish workflow; package installable via `uvx emoney-mcp@latest`
- uvx support and updated installation instructions

## [0.1.1] — 2025

### Added
- 73 unit tests and GitHub Actions CI workflow

### Fixed
- `get_performance` and `get_spending` scraper bugs
- `get_goals` added to fix missing endpoint

## [0.1.0] — 2025 (initial release)

### Added
- **`get_accounts`** — all financial accounts grouped by type with balances and net worth summary
- **`get_net_worth`** — current net worth (assets minus liabilities)
- **`get_net_worth_history`** — monthly net worth trend (up to 60 months)
- **`get_retirement_accounts`** — aggregates all tax-advantaged retirement accounts
- **`get_holdings`** — all investment positions with ticker, units, price, value, cost basis, and unrealized gain/loss
- **`get_asset_allocation`** — portfolio allocation by asset class with top 10 holdings
- **`get_performance`** — portfolio value change across MTD, YTD, 1-year, and longer periods
- **`get_transactions`** — investment transactions (buys, sells, dividends) for a date range
- **`get_capital_gains`** — realized capital gains summary for a given tax year
- **`get_goals`** — financial goals and funding status from the Emoney plan
- **`get_spending`** — spending by category for recent months
- **`sync_chrome_session`** — pull active Emoney session from running Chrome without re-login
- **`reset_session`** — clear saved session and force fresh login
- **`explore_emoney_cards`** — probes unexplored Emoney CardSwitcher endpoints to discover additional data
