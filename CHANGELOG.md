# Changelog

All notable changes to emoney-mcp are documented here.

## [0.3.0] — 2026-06-08 (current)

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
- **`get_features`** — lists all available tools grouped by category with descriptions and example questions (this tool)

## [0.1.5] — 2025

### Added
- **`get_financial_summary`** — executive dashboard combining net worth, performance, income vs. spending, top 5 spending categories, and goal status in a single call
- **`search_transactions`** — search spending transactions by keyword, category, and/or amount range
- **`get_recurring_charges`** — detects recurring/subscription charges by analyzing 120-day transaction patterns; estimates monthly recurring spend
- **`get_net_worth_breakdown`** — breaks net worth down by person (Drew / Lacey / Joint), liquidity (liquid / semi-liquid / illiquid), and tax treatment (taxable / tax-deferred / tax-free)

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
