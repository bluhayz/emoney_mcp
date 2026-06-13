# emoney_mcp

MCP server for [Emoney Advisor](https://wealth.emaplan.com) — exposes your complete financial picture as tools Claude Desktop can call.

> **Ask Claude:** *"How are my finances looking?"* · *"Am I over budget this month?"* · *"When will I hit $2M?"* · *"What's my year-end tax checklist?"* · *"If I save $500/month more, when can I retire?"* · *"Are there any unusual charges this month?"* · *"What are my odds of not running out of money in retirement?"* · *"Should I claim Social Security at 62 or 70?"*

---

## How it works

1. On first use, a Chrome window opens (via [nodriver](https://github.com/ultrafunkamsterdam/nodriver)) — log in normally including SMS MFA.
2. The server saves your session cookies to `~/.emoney_mcp/session.json`.
3. All subsequent data fetches use [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprint) to call Emoney's internal JSON APIs — no browser needed until the session expires.

Emoney has no public API, so this uses browser automation for login and reverse-engineered internal endpoints for data.

---

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — fast Python package manager
- Google Chrome installed at the default path
- Claude Desktop

> **Why uv?** `uvx` creates a temporary isolated environment and runs the server in a single command — no `pip install`, no virtual environment to manage, no local clone required.

---

## Installation — one line

**Install uv** (if you don't have it):

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

That's it. `uvx` will download and run `emoney-mcp` automatically the first time Claude Desktop starts it.

---

## Claude Desktop configuration

Add to `%APPDATA%\Claude\claude_desktop_config.json` (Windows) or `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "emoney": {
      "command": "uvx",
      "args": ["emoney-mcp@latest"],
      "env": {
        "EMONEY_SUBDOMAIN": "wealth"
      }
    }
  }
}
```

Restart Claude Desktop after saving. The first startup takes ~30 seconds while `uvx` downloads dependencies; subsequent starts are instant (cached).

---

## Alternative: local development install

If you want to edit the code:

```bash
git clone https://github.com/bluhayz/emoney_mcp.git
cd emoney_mcp
uv pip install -e .
```

Then point Claude Desktop at your local clone:

```json
{
  "mcpServers": {
    "emoney": {
      "command": "uvx",
      "args": ["--from", "/path/to/emoney_mcp", "emoney-mcp"],
      "env": {
        "EMONEY_SUBDOMAIN": "wealth",
        "EMONEY_DEV": "1"
      }
    }
  }
}
```

> **`EMONEY_DEV=1`** enables hot-reload: edit any file in `src/emoney_mcp/scrapers/` and changes take effect on the next tool call without restarting Claude Desktop.

---

## Available tools (61 total)

### 🏠 Overview & Dashboard

| Tool | Description |
|------|-------------|
| `get_quick_status` | **5-number snapshot** — net worth, portfolio today's change, this month's savings rate, top spending category, and goal on-track status. Designed for quick checks with minimal token usage. |
| `get_financial_summary` | **Full executive dashboard** — net worth, portfolio performance, this month's income vs. spending, top 5 categories, and goal status. Best for broad *"How are my finances?"* questions. |
| `get_financial_health_score` | **0–100 composite score** with A–F letter grade across six dimensions: savings rate, goal funding, debt-to-asset ratio, emergency fund coverage, diversification, and net worth trend. |
| `get_monthly_review` | **Structured monthly report** — net worth change, investment performance, this month's income vs. spending, top categories, savings rate, goal status, and a prioritized action item list. One call, no parameters. |

### 💰 Balance Sheet

| Tool | Description |
|------|-------------|
| `get_accounts` | All financial accounts grouped by type (investments, bank, retirement, debt, property) with balances and full net worth summary |
| `get_net_worth` | Current net worth — total assets minus total liabilities |
| `get_net_worth_history` | Monthly net worth trend. Parameter: `months` (default 12, max 60) |
| `get_net_worth_breakdown` | Net worth broken down three ways: by **person** (per household member/joint), by **liquidity** (Liquid/Semi-liquid/Illiquid), and by **tax treatment** (Taxable/Tax-Deferred/Tax-Free) |
| `get_retirement_accounts` | Aggregates all tax-advantaged accounts — 401k, IRA, Roth IRA, annuities, HSA, 529 — with subtotals by category |

### 📈 Investments

| Tool | Description |
|------|-------------|
| `get_holdings` | All investment positions across every account — ticker, units, price, value, cost basis, unrealized gain/loss |
| `get_asset_allocation` | Portfolio asset allocation by asset class (Equities, Fixed Income, Cash, etc.) plus top 10 holdings by weight |
| `get_performance` | Portfolio value change today + MTD net worth change + computed returns from historical data |
| `get_transactions` | Investment transactions (buys, sells, dividends). Parameters: `days` (default 30, max 365), `account_id` (optional GUID) |
| `get_capital_gains` | Realized gains summary — sell proceeds, dividends, interest by tax year. Parameter: `year` (default current) |

### 🎯 Goals

| Tool | Description |
|------|-------------|
| `get_goals` | Financial goals and funding status from Emoney's plan — retirement, education, and spending goals with percent funded |

### 💸 Tax Planning

| Tool | Description |
|------|-------------|
| `get_tax_bracket_headroom` | **How much more income before the next bracket?** Shows remaining room in the current ordinary income bracket and LTCG bracket. Infers income automatically if not supplied. Optional: `current_income`, `filing_status` |
| `get_tax_loss_harvesting` | Identifies positions with unrealized losses in **taxable accounts** suitable for harvesting. Returns losses sorted by magnitude with estimated tax savings at 15%, 20%, and 23.8% (LTCG + NIIT) rates. |
| `get_contribution_room` | Shows 2025 IRS annual limits for all tax-advantaged accounts (401k, IRA, HSA, SIMPLE IRA, SEP IRA, 529). Adjusts for catch-up contributions by age including the SECURE 2.0 super catch-up (ages 60–63). Parameters: `age`, `filing_status` |
| `get_roth_conversion_analysis` | Estimates the federal tax cost and long-term benefit of converting pre-tax dollars to Roth. Shows bracket-by-bracket impact, effective rate on conversion, and projected tax-free growth. Required: `conversion_amount`, `current_income`. Optional: `filing_status`, `age` |
| `get_capital_gains_exposure` | Identifies embedded unrealized gains in taxable accounts and estimates the tax bill if positions were sold today. Applies LTCG rates and NIIT based on income. Optional: `filing_status`, `annual_income` |
| `get_rmd_estimate` | Estimates Required Minimum Distributions from pre-tax retirement accounts using the IRS Uniform Lifetime Table (RMDs begin at age 73, SECURE 2.0). Returns current-year RMD and a 10-year projected schedule. Required: `birth_year` |
| `get_social_security_optimizer` | **Optimize your SS claiming age.** Compares monthly benefit, annual benefit, and lifetime value at 62, FRA, and 70. Shows breakeven crossover ages. Includes spousal analysis. Required: `birth_year`. Optional: `estimated_monthly_benefit_at_67` (from ssa.gov), `life_expectancy` (default 85), `spouse_birth_year`, `spouse_benefit_at_67` |
| `get_quarterly_estimated_taxes` | Calculates Q1–Q4 federal estimated tax payments and due dates. Uses current-year annualized and safe-harbor methods, recommends the lower one. Optional: `filing_status`, `annual_income_override`, `prior_year_tax`, `expected_withholding` |
| `get_year_end_checklist` | **Year-end action list.** Synthesizes all tax tools into a prioritized checklist with status (action\_needed / opportunity / done) and dollar amounts. Optional: `age`, `birth_year` (for RMD check), `filing_status`, `current_income` |

### 🏖️ Retirement & Long-range Planning

| Tool | Description |
|------|-------------|
| `get_retirement_runway` | Models how many years the current portfolio can sustain withdrawals under conservative (4%), base (6%), and optimistic (8%) return scenarios. Also shows sustainable withdrawal amounts at 3.5%–4.5% SWR. Optional: `annual_spending`, `return_rate` |
| `get_withdrawal_rate_analysis` | Projects portfolio to your Emoney retirement goal date, then shows annual and monthly income at 3%–5% withdrawal rates with estimated years funded. Uses retirement start/end year from Emoney goals. |
| `get_net_worth_projection` | **"When will I hit $X?"** Projects net worth forward using compound growth + actual monthly savings. Shows $500k/$1M/$2M/$5M/$10M milestone years and a 30-year snapshot table. Optional: `target_net_worth`, `annual_return` (default 7%), `annual_savings_override` |
| `get_debt_payoff_plan` | Models **avalanche** (highest APR first) vs. **snowball** (smallest balance first) strategies. Returns months-to-payoff and total interest for each. Optional: `extra_monthly_payment`, `assumed_credit_card_apr` (default 22%), `assumed_loan_apr` (default 7%) |
| `get_college_savings_gap` | Estimates the gap between current 529 savings and projected college costs from Emoney's education goals. Shows required monthly contribution to close the gap by the goal start year. Optional: `annual_return` (default 6%), `annual_college_inflation` (default 5%) |
| `run_monte_carlo_retirement` | **Monte Carlo simulation** — runs 1,000–10,000 stochastic paths with random annual returns and inflation draws to compute the probability your portfolio survives your retirement horizon. Returns success rate, median/10th/90th percentile ending balances, worst-case depletion year, and the safe withdrawal rate at 90% success. Optional: `simulations` (default 1000), `years` (default 30), `mean_return` (default 7%), `std_dev` (default 15%), `social_security_annual`, `withdrawal_rate` |
| `get_dynamic_withdrawal_guardrails` | **Guyton-Klinger guardrails** — determines whether to raise, hold, or cut the current withdrawal based on portfolio performance vs. the starting value. Returns RAISE / HOLD / CUT with adjusted annual and monthly amounts. Optional: `initial_withdrawal_rate` (default 5%), `initial_portfolio_value`, `current_annual_withdrawal` |
| `run_scenario` | **What-if modeling.** Runs a scenario projection alongside the baseline and returns a comparison. e.g. *"If I save $500/month more, when do I retire?"* or *"What if I assume 8% returns?"* Optional: `monthly_savings_delta`, `target_net_worth`, `retirement_age`, `annual_return_pct` |

### 🛡️ Planning

| Tool | Description |
|------|-------------|
| `get_insurance_gap_analysis` | **Insurance coverage need.** Estimates life insurance need (income multiple minus liquid assets), recommended disability benefit, and emergency fund adequacy from your actual income and balance sheet. Optional: `income_multiple` (default 10×), `disability_pct` (default 65%) |

### ⚖️ Portfolio Analysis

| Tool | Description |
|------|-------------|
| `get_asset_location_efficiency` | Grades how well assets are positioned for tax efficiency across account types. Tax-inefficient assets (bonds, REITs, TIPS) should be in tax-deferred/free accounts; tax-efficient assets (index funds) can be in taxable. Returns A–F letter grade, per-position ratings, and specific swap suggestions. |
| `get_rebalancing_targets` | Computes exact dollar amounts to buy/sell to reach a target allocation. Classifies holdings into equity, bond, and cash buckets and shows drift from target. Parameters: `target_equity_pct` (default 60), `target_bond_pct` (default 30), `target_cash_pct` (default 10) |

### 💳 Cash Flow & Spending

| Tool | Description |
|------|-------------|
| `get_spending` | Cash flow summary — income, expenses, net cash flow, savings rate, and 5 most recent transactions. Parameter: `months` (default 1) |
| `get_spending_transactions` | Bank and credit card transactions with **category labels** (Groceries, Dining, Travel, etc.) and **top merchants** with location dedup. Parameters: `days` (default 30, max 365), `max_transactions` (default 100; pass 0 for all) |
| `get_spending_trends` | Month-over-month category comparison — which categories are trending up/down, plus monthly income vs. spending per month. Parameter: `months` (default 3, max 12) |
| `get_budget_vs_actual` | **"Am I over budget?"** Compares this month's actual spending to the rolling N-month category average. Flags categories >15% above benchmark. Also compares against any total budget set in Emoney. Parameter: `months_avg` (default 3) |
| `get_year_over_year` | **"Am I spending more than last year?"** Compares this year's YTD spending and income to the same period last year with a full per-category breakdown. Requires ~2 years of SNB history. |
| `get_cash_flow_projection` | Projects monthly cash flow 1–24 months forward using actual income/spending averages from the last 90 days. Includes a running balance estimate. Parameter: `months_ahead` (default 6, max 24) |
| `get_cash_flow_forecast` | **Recurring vs. discretionary cash flow.** Breaks projected spending into detected fixed recurring charges and estimated discretionary, giving a more structured forecast. Parameter: `months` (1–6, default 3) |
| `get_income_summary` | Income sources and monthly income trend — paychecks, direct deposits, dividends, interest grouped by source. Parameter: `days` (default 90, max 365) |
| `get_savings_rate` | Month-by-month savings rate (income minus spending ÷ income). Parameter: `months` (default 6, max 12) |
| `search_transactions` | Search transactions by keyword, category, and/or amount range across up to 365 days. Parameters: `query`, `category`, `days`, `min_amount`, `max_amount`, `max_results` (default 100; pass 0 for all) |
| `get_recurring_charges` | Detects subscriptions and recurring bills by analyzing 120 days of transaction patterns. Returns weekly/monthly/quarterly charges and total estimated monthly recurring spend. |
| `get_unusual_transactions` | **Anomaly detection.** Flags transactions that are unusually large vs. the merchant's or category's historical average. Parameters: `days` (default 90), `threshold_pct` (default 150%) |
| `get_merchant_spending` | **Spending by merchant.** Aggregated totals grouped by normalized merchant name with transaction count and date range. Parameters: `days` (default 365), `merchant` (substring filter), `limit` (default 25 merchants) |

### ✏️ Transaction Management

| Tool | Description |
|------|-------------|
| `update_transaction` | **Edit a spending transaction** — rename it (UserDescription), reassign its category (CategoryID), or both. Required: `transaction_id`. Optional: `description`, `category_id` |
| `hide_transaction` | **Hide a transaction** from the spending view in Emoney (marks it as excluded from cash flow). Required: `transaction_id` |
| `get_transaction_splits` | **Get splits for a transaction** — returns existing sub-transactions if the transaction has been split. Required: `transaction_id` |
| `update_transaction_splits` | **Split a transaction across multiple categories** — pass a list of `{"amount": ..., "category_id": ..., "description": ...}` dicts. Required: `transaction_id`, `splits` |

### 📋 Transaction Rules

| Tool | Description |
|------|-------------|
| `get_transaction_rules` | **List all auto-categorization rules** — returns the full rule set Emoney applies to new transactions (merchant match, keyword match, amount range, category assignment) |
| `add_transaction_rule` | **Create a new categorization rule** — define how Emoney should auto-categorize future transactions. Required: `rule` dict with rule fields (merchant, keyword, category_id, etc.) |
| `update_transaction_rule` | **Modify an existing rule** — overwrite specific rule fields while keeping others. Required: `rule_id`, `rule` dict with updated fields |
| `apply_transaction_rule` | **Apply a rule immediately** to existing transactions — re-runs the rule against the transaction history without waiting for new imports. Required: `rule_id` |

### 📊 Reports

| Tool | Description |
|------|-------------|
| `get_reports` | **List all available Emoney reports** — returns the full report catalog grouped by family (Liquidity, Asset Tax Type, Estate Transfer, etc.) with report IDs |
| `get_report_url` | **Get a URL to open a specific report** — POSTs to Emoney to generate a report URL you can open in a browser. Required: `report_id` (from `get_reports`) |

### 🔧 Debug & Session Management

| Tool | Description |
|------|-------------|
| `sync_chrome_session` | Pull active Emoney session from a running Chrome browser (no re-login if already logged in) |
| `reset_session` | Clear saved session and force a fresh login on next call |
| `get_version` | Returns installed version, cookie file path, and session status — useful for debugging |
| `get_features` | Lists all available tools grouped by category with descriptions and example questions |
| `explore_emoney_cards` | Probes unexplored Emoney CardSwitcher endpoints (cards 5, 6, 7, 10, 12, 14–16) to discover additional data. Optional: `card_ids` list |
| `get_available_cards` | **Clean card inventory.** Returns a structured inventory of all responding card IDs (1–16 by default) with key names and data-type fingerprints. Optional: `card_ids` list |
| `clear_cache` | **Selective cache invalidation.** Purge card or SNB transaction cache without a full session reset. Parameter: `module` (`'cards'`, `'spending'`, or `'all'`; default `'all'`) |

---

## Example questions to ask Claude

### Quick checks
```
How am I doing today?
Give me my monthly review.
Give me a 5-number snapshot of my finances.
```

### Overview & Health
```
How are my finances looking?
Give me a complete financial summary.
What's my financial health score?
What should I focus on improving financially?
```

### Net Worth & Wealth
```
What's my current net worth?
How has my net worth changed over the last 6 months?
When will I hit $2 million?
How is our wealth split between my spouse and me?
How much of my assets are liquid vs. illiquid?
How much do I have in tax-free vs. tax-deferred accounts?
```

### Budgeting & Spending Comparison
```
Am I over budget this month?
Are there any unusual charges this month?
Which spending categories are tracking above normal?
Am I spending more this year than last year?
How has my grocery spending changed year-over-year?
Compare this month's dining to my average.
```

### Cash Flow & Projections
```
Will I have enough cash to cover a big purchase in 3 months?
What does my monthly cash flow look like next quarter?
How much of my spending is fixed vs. discretionary?
Project my finances for the next 6 months.
```

### Tax Planning
```
What are my year-end tax action items?
How much can I convert to Roth without crossing the next bracket?
How much freelance income can I take on this year at my current rate?
Where can I harvest tax losses this year?
What would it cost to convert $150,000 to Roth this year?
What's my capital gains tax exposure if I sell my concentrated positions?
How much can I still contribute to my IRA and HSA this year?
When do I have to start taking RMDs, and how much will they be?
```

### Retirement & Scenario Planning
```
Can I afford to retire now?
If I save $1,000/month more, how much sooner can I retire?
What if I assume 8% returns — when do I hit $3M?
How long will my money last at different withdrawal rates?
What does a 4% withdrawal rate give me each month?
Am I on track for retirement?
When will I be debt-free?
Which debt payoff strategy saves the most interest?
Are we on track for Parker's college savings?
How much do we need to save monthly for the 529?
What are the odds my portfolio lasts 30 years?
Run a Monte Carlo simulation on my retirement plan.
Should I adjust my withdrawals this year given how the market has performed?
Should I claim Social Security at 62 or wait until 70?
What is the Social Security breakeven age for me?
How much do I owe in estimated taxes each quarter?
What are my Q3 estimated federal tax payments?
```

### Insurance & Protection
```
Am I adequately insured?
How much life insurance do I need?
Do I have enough saved for an emergency?
```

### Investments
```
What are my biggest investment holdings?
How is my portfolio performing this month?
Are my assets in the right accounts for tax efficiency?
Which positions are in the wrong account types?
How do I rebalance to a 60/40 allocation?
How concentrated am I in any single stock?
What are my realized capital gains this year?
```

### Transaction Management & Reports
```
Rename the Starbucks transaction from yesterday to "Coffee with client".
Recategorize transaction 12345 as Dining Out.
Hide the duplicate Amazon charge from last week.
Split the $200 Target purchase between Groceries and Household Supplies.
Show me all my Emoney auto-categorization rules.
What reports are available in Emoney?
Get me a link to the Liquidity Report.
```

### Spending & Cash Flow Detail
```
What did I spend last month vs. what came in?
What are my top spending categories over the last 60 days?
How much did I spend on groceries last month?
Is my dining spending going up or down?
What are my top merchants by total spending this year?
How much did I spend at Amazon last year?
What subscriptions am I paying for?
What are my recurring monthly bills?
How much have I spent at Costco this year?
Show me all Amazon charges over $50.
```

---

## Merchant normalization

All spending tools normalize raw bank descriptions before grouping, so visits to the same merchant at different locations are counted together:

| Raw description | Normalized |
|----------------|------------|
| `APLPAY FOOD LION VA` | `FOOD LION` |
| `COSTCO WHSE PHOENIX US` | `COSTCO WHSE` |
| `COSTCO WHSE TUCSON AZ` | `COSTCO WHSE` ← grouped |
| `UNITED AIRLINES HOUSTON TX` | `UNITED AIRLINES` |
| `TST AUSTIN GRILL VA` | `AUSTIN GRILL` |
| `SQ *BLUE BOTTLE COFFEE` | `BLUE BOTTLE COFFEE` |

**Stripped:** payment-network prefixes (`APLPAY`, `SQ *`, `TST`, `PP *`), trailing state abbreviations, city names, country suffixes, ZIP codes, store numbers.

**Protected words** (`MARKET`, `TIMES`, `GRILL`, `STORE`, etc.) are never stripped — preventing false positives like `WHOLE FOODS MARKET` → `WHOLE FOODS`.

---

## Tax planning notes

Tax calculations use **2025 IRS figures** (brackets, contribution limits, LTCG thresholds). All estimates assume federal tax only and do not include state income tax. Always consult a qualified tax professional before making tax decisions.

Key assumptions:
- LTCG rates: 0% / 15% / 20% based on taxable income
- NIIT (3.8%) applies above $200k single / $250k MFJ
- RMD start age: 73 (SECURE 2.0)
- Roth conversion analysis uses standard deduction; itemizers should adjust `current_income` to taxable income

---

## Performance & caching

emoney-mcp maintains two module-level TTL caches (5-minute expiry) to eliminate redundant HTTP calls within a conversation turn:

- **Card cache** — `_get_card()` results are shared across all tools that use the same card. Calling `get_financial_summary` followed by `get_financial_health_score` (both use card 2 for goals) makes only one card request.
- **SNB cache** — the full transaction + category dataset from the SNB API is fetched once. All 9 spending tools (`get_savings_rate`, `get_income_summary`, `get_spending_trends`, etc.) share that single fetch when called in the same session.
- Both caches are cleared automatically on `reset_session`.

Tools with multiple independent data sources use `asyncio.gather()` for parallel fetching (`get_financial_summary` and `get_financial_health_score` cut from ~5 s to ~1.5 s wall-clock time).

---

## First-time login flow

1. Ask Claude anything — e.g. *"What's my net worth?"*
2. A Chrome window opens — log in: username → password → SMS verification code.
3. Once the Emoney home page loads, the session is automatically saved to `~/.emoney_mcp/session.json`.
4. Call your tool again — it works instantly.
5. Subsequent calls work without re-login until the session expires (typically a few hours).

**Tip:** Use `sync_chrome_session` if you are already logged in to Emoney in Chrome — it imports your cookies without opening a new window.

---

## Architecture

```
Claude Desktop
     │  MCP stdio
     ▼
emoney_mcp/server.py         ← tool registration + dispatch (61 tools)
emoney_mcp/scraper.py         ← re-export shim (backward-compatible)
emoney_mcp/scrapers/          ← domain-split scraping package
  ├── _helpers.py             ←   shared URL constants + TTL-cached _get_card()
  ├── accounts.py             ←   balance sheet tools
  ├── investments.py          ←   holdings, performance, transactions
  ├── spending.py             ←   SNB-based cash flow tools + TTL cache
  ├── goals.py                ←   goals, financial summary, health score, monthly review
  ├── tax.py                  ←   2025 IRS tax planning tools
  ├── retirement.py           ←   runway, withdrawal, net worth projection, run_scenario
  ├── portfolio.py            ←   asset location, rebalancing, card discovery
  ├── planning.py             ←   insurance gap analysis
  ├── transactions.py         ←   transaction writes, splits, rules engine (v0.9.0)
  ├── reports.py              ←   report catalog + URL generation (v0.9.0)
  └── explore.py              ←   Emoney site explorer (dev/discovery tool)
emoney_mcp/browser.py         ← session management + nodriver login
     │
     ├── curl_cffi AsyncSession  ← Chrome TLS fingerprint for API calls
     └── nodriver (background thread)  ← Chrome login window when needed
```

**Key design decisions:**
- `nodriver` runs in a separate OS thread with its own `asyncio` event loop to avoid conflicting with the MCP server's event loop
- Two TTL caches (card + SNB) eliminate redundant HTTP calls within a conversation turn; `asyncio.gather()` parallelises independent fetches
- Session cookies are persisted to `~/.emoney_mcp/session.json` — a stable path that works whether running via `uvx`, PyPI, or local clone
- The SNB API JWT token is extracted from the Spending page HTML on each call — no separate auth flow required
- Tax and planning calculations are pure Python — no external API calls, using hardcoded 2025 IRS tables
- Set `EMONEY_DEV=1` to enable hot-reload of scraper modules without restarting Claude Desktop

---

## Internal API endpoints used

### CardSwitcher (Emoney internal dashboard cards)

| Endpoint | Data |
|----------|------|
| `CS/CardSwitcher/GetCard/1` | Account groups with balances |
| `CS/CardSwitcher/GetCard/2` | Financial goals and funding status |
| `CS/CardSwitcher/GetCard/3` | Investment portfolio value + daily change |
| `CS/CardSwitcher/GetCard/4` | Asset allocation model summary |
| `CS/CardSwitcher/GetCard/8` | Net worth + monthly history array |
| `CS/CardSwitcher/GetCard/9` | Net worth, total assets, total liabilities |
| `CS/CardSwitcher/GetCard/11` | Net worth MTD and YTD change |
| `CS/CardSwitcher/GetCard/13` | Cash flow — income, expenses, budget, recent transactions |

### Investments

| Endpoint | Data |
|----------|------|
| `CS/Investments/GetInvestmentData` | Holdings, positions, asset allocation, cost basis |
| `CS/Investments/GetInvestmentTransactions` | Transaction history (POST, requires CSRF token) |

### SNB API (`api.emoneyadvisor.com/snb-api`)

The spending module uses a separate REST API authenticated with a short-lived JWT token embedded in the Spending page HTML. Results are cached for 5 minutes via `_fetch_snb_raw()`.

| Endpoint | Data |
|----------|------|
| `api/values/GetFilteredTransactions` | All bank/CC transactions with `categoryId` (full history, client-side filtered) |
| `api/values/GetCategories` | 114 spending category names mapped by ID |

### CS/Spending (transaction writes, rules, reports)

Write endpoints live on the main emaplan.com host under `/ema/CS/Spending/`. All require an ASP.NET anti-forgery token (`__RequestVerificationToken`) in the POST body and the `X-Requested-With: XMLHttpRequest` header. jQuery bracket notation is used for nested fields (`TransactionID[Value]=...`).

| Endpoint | Operation |
|----------|-----------|
| `CS/Spending/UpdateTransaction` | Edit transaction description and/or category |
| `CS/Spending/HideTransaction` | Exclude a transaction from spending view |
| `CS/Spending/GetTransactionSplits` | Fetch existing splits for a transaction |
| `CS/Spending/UpdateTransactionSplits` | Write split sub-transactions |
| `CS/Spending/GetClassifiableHoldings` | Fetch transaction categorization rules |
| `CS/Spending/AddRule` | Create a new auto-categorization rule |
| `CS/Spending/UpdateRule` | Modify an existing rule |
| `CS/Spending/ApplyRule` | Apply a rule retroactively to history |
| `CS/Reports/GetReportUrl` | Generate a URL for a named Emoney report |

---

## Development & testing

```bash
git clone https://github.com/bluhayz/emoney_mcp.git
cd emoney_mcp

# Install with dev dependencies
uv pip install -e ".[dev]"

# Run tests
uv run pytest tests/ -v

# Syntax check all modules
uv run python -m py_compile src/emoney_mcp/scrapers/_helpers.py \
  src/emoney_mcp/scrapers/spending.py src/emoney_mcp/scraper.py \
  src/emoney_mcp/server.py
```

Tests use fixture JSON files in `tests/fixtures/` and mock HTTP sessions — no live Emoney connection needed.

CI runs on GitHub Actions (Python 3.11, 3.12, 3.13) on every push and pull request. Every push to `main` also auto-publishes to PyPI.

---

## Session file

Cookies are saved to `~/.emoney_mcp/session.json` (`C:\Users\<you>\.emoney_mcp\session.json` on Windows). This path is stable regardless of how the package is installed.

Delete the file (or call `reset_session`) to force a fresh login.

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `mcp` | Model Context Protocol server SDK |
| `nodriver` | Undetected Chrome launcher — bypasses Emoney WAF/TLS fingerprint detection |
| `curl_cffi` | Chrome TLS fingerprint HTTP client for authenticated API calls |
| `pycryptodomex` | AES-GCM decryption for Chrome cookie extraction |
| `beautifulsoup4` | HTML parsing (fallback) |
| `python-dotenv` | Environment variable support |

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for the full version history.
