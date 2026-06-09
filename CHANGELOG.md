# Changelog

All notable changes to emoney-mcp are documented here.

## [0.7.0] — 2026-06-09 (current)

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
