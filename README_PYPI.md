# emoney-mcp

Connect your [Emoney Advisor](https://wealth.emaplan.com) account to Claude Desktop.
Ask Claude questions about your finances in plain English — no spreadsheets, no dashboards.

> *"How are my finances looking?"* · *"Am I over budget this month?"* · *"When will I hit $2M?"*  
> *"What's my year-end tax checklist?"* · *"If I save $500/month more, when do I retire?"* · *"Flag any unusual charges this month."*  
> *"What's my tax bracket headroom for a Roth conversion?"* · *"What are my odds of not running out of money in retirement?"* · *"Should I claim Social Security at 62 or 70?"*

---

## Prerequisites

- Google Chrome installed at its default location
- [uv](https://docs.astral.sh/uv/getting-started/installation/) — free, one-line install
- [Claude Desktop](https://claude.ai/download)
- An active Emoney Advisor account

---

## Install uv (if you don't have it)

**macOS / Linux**
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

---

## Add to Claude Desktop

Open your Claude Desktop config file:

- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add the following (create the file if it doesn't exist):

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

Restart Claude Desktop. The first startup takes ~30 seconds while dependencies download; subsequent starts are instant.

> **Note:** If your Emoney login URL is not `wealth.emaplan.com`, replace `"wealth"` with the subdomain from your own URL (e.g. `"client"` for `client.emaplan.com`).

---

## First use

1. Ask Claude anything — e.g. *"What's my net worth?"*
2. A Chrome window opens automatically — log in with your Emoney username, password, and SMS code.
3. Your session is saved. All future questions work instantly without re-login.

**Already logged into Emoney in Chrome?** Ask Claude to `sync_chrome_session` and it will import your existing session — no new login required.

---

## What you can ask

### Quick overview
```
How am I doing today?
Give me my monthly review.
Give me a full financial summary.
What's my financial health score?
```

### Net worth & accounts
```
What's my current net worth?
How has my net worth changed over the last 6 months?
When will I hit $2 million?
If I save $500/month more, when will I hit $2 million?
How much do I have in tax-free vs. tax-deferred accounts?
```

### Spending & budgets
```
Am I over budget this month?
Are there any unusual charges this month?
What are my biggest spending categories?
How much have I spent at Amazon this year?
What are my top merchants by total spending?
Am I spending more this year than last year?
What subscriptions am I paying for?
Show me all Amazon charges over $50.
```

### Cash flow
```
Will I have enough cash to cover a big purchase in 3 months?
What does my monthly cash flow look like next quarter?
How much of my spending is fixed vs. discretionary?
What are my recurring monthly bills?
```

### Investments
```
What are my biggest holdings?
How is my portfolio performing this year?
How do I rebalance to a 60/40 allocation?
What are my realized capital gains this year?
```

### Tax planning
```
What are my year-end tax action items?
How much can I convert to Roth without crossing the next bracket?
Where can I harvest tax losses this year?
What would it cost to convert $150,000 to Roth?
What's my capital gains tax exposure if I sell my concentrated positions?
How much can I still contribute to my IRA and HSA this year?
When do I have to start taking RMDs, and how much will they be?
Should I claim Social Security at 62 or wait until 70?
How much do I owe in estimated taxes each quarter?
```

### Retirement & scenario planning
```
Can I afford to retire now?
If I save $1,000/month more, how much sooner can I retire?
What if I assume 8% returns — when do I hit $3M?
How long will my money last at different withdrawal rates?
What are the odds my portfolio lasts 30 years?
Run a Monte Carlo simulation on my retirement plan.
Should I adjust my withdrawals this year given market performance?
When will I be debt-free?
Are we on track for college savings?
```

### Insurance & protection
```
Am I adequately insured?
How much life insurance do I need?
Do I have enough set aside for an emergency?
```

### Transaction management
```
Rename the Starbucks transaction from yesterday to "Coffee with client".
Recategorize that Amazon charge as Office Supplies.
Hide the duplicate charge from last week.
Split the $200 Target purchase between Groceries and Household Supplies.
Show me all my auto-categorization rules.
What Emoney reports are available?
Get me a link to the Liquidity Report.
```

---

## Available tools (93 total)

### Overview & Dashboard
| Tool | What it answers |
|------|----------------|
| `get_quick_status` | 5-number snapshot: net worth, portfolio change, savings rate, top spending category, goal status |
| `get_financial_summary` | Full dashboard: net worth, performance, income vs. spending, top 5 categories, goal status |
| `get_financial_health_score` | 0–100 composite score (A–F) across savings rate, goal funding, debt ratio, emergency fund, diversification, net worth trend |
| `get_monthly_review` | **New.** Structured monthly report — net worth change, investments, spending breakdown, savings rate, goal status, and action items in one call |

### Balance Sheet
| Tool | What it answers |
|------|----------------|
| `get_accounts` | All accounts by type with balances and net worth |
| `get_net_worth` | Current net worth (assets minus liabilities) |
| `get_net_worth_history` | Monthly net worth trend (up to 60 months) |
| `get_net_worth_breakdown` | Net worth by person, liquidity, and tax treatment |
| `get_retirement_accounts` | All tax-advantaged accounts (401k, IRA, Roth, HSA, 529) with subtotals |
| `get_client_profile` | Household names, dates of birth, ages, and dependents (auto-fills birth_year/age for other tools) |

### Investments
| Tool | What it answers |
|------|----------------|
| `get_holdings` | All positions: ticker, value, cost basis, unrealized gain/loss |
| `get_asset_allocation` | Allocation by asset class plus top 10 holdings |
| `get_performance` | Portfolio return today, MTD, YTD |
| `get_transactions` | Investment transactions (buys, sells, dividends) |
| `get_capital_gains` | Sell proceeds, dividends, interest by tax year (not realized gain/loss) |
| `get_asset_location_efficiency` | A–F tax efficiency grade with swap suggestions |
| `get_rebalancing_targets` | Exact buy/sell amounts to hit a target allocation |

### Spending & Cash Flow
| Tool | What it answers |
|------|----------------|
| `get_spending` | Spending by category for the last N months |
| `get_spending_transactions` | Bank/CC transactions with category labels |
| `get_spending_trends` | Which categories are trending up, down, or stable |
| `get_budget_vs_actual` | This month vs. rolling average; flags overspend categories |
| `get_year_over_year` | This year's YTD vs. last year same period |
| `get_cash_flow_projection` | Monthly cash flow forecast up to 24 months ahead |
| `get_cash_flow_forecast` | **New.** Projected cash flow broken into recurring fixed costs vs. discretionary spending |
| `get_income_summary` | Income sources grouped by payee |
| `get_savings_rate` | Month-by-month savings rate |
| `search_transactions` | Search by keyword, category, or amount range |
| `get_recurring_charges` | Detected subscriptions and recurring bills |
| `get_unusual_transactions` | **New.** Flag charges unusually large vs. merchant or category historical average |
| `get_merchant_spending` | **New.** Total spending grouped by normalized merchant name |
| `get_categories` | Full spending-category name→ID map (look up the `category_id` used when recategorizing) |

### Goals
| Tool | What it answers |
|------|----------------|
| `get_goals` | Retirement, education, and spending goals with % funded |

### Tax Planning
| Tool | What it answers |
|------|----------------|
| `get_tax_bracket_headroom` | Room remaining in your current bracket before the next threshold |
| `get_tax_loss_harvesting` | Taxable positions with unrealized losses ranked by size |
| `get_contribution_room` | 2026 IRS limits for 401k, IRA, HSA, SIMPLE IRA, SEP IRA, 529 |
| `get_roth_conversion_analysis` | Tax cost and long-term benefit of a Roth conversion |
| `get_capital_gains_exposure` | Estimated tax bill on unrealized gains if sold today |
| `get_rmd_estimate` | Required Minimum Distribution schedule (10-year projection) |
| `get_social_security_optimizer` | SS benefit at 62 / FRA / 70 with breakeven ages and spousal analysis |
| `get_quarterly_estimated_taxes` | Q1–Q4 estimated federal payment amounts and due dates |
| `get_year_end_checklist` | **New.** Prioritized year-end tax action list with status and dollar amounts |

### Retirement & Long-range Planning
| Tool | What it answers |
|------|----------------|
| `get_retirement_runway` | Years portfolio sustains withdrawals at 3 return scenarios |
| `get_withdrawal_rate_analysis` | Annual income at 3–5% withdrawal rates projected to retirement |
| `get_net_worth_projection` | When you'll hit $500k / $1M / $2M / $5M / $10M |
| `get_debt_payoff_plan` | Avalanche vs. snowball payoff with months and total interest |
| `get_college_savings_gap` | 529 funding gap and monthly contribution needed to close it |
| `run_monte_carlo_retirement` | Probability of success across 1,000+ stochastic retirement paths |
| `get_dynamic_withdrawal_guardrails` | Whether to raise, hold, or cut withdrawals (Guyton-Klinger rules) |
| `run_scenario` | **New.** What-if projection: compare baseline vs. savings increase, return override, or retirement age change |

### Planning
| Tool | What it answers |
|------|----------------|
| `get_insurance_gap_analysis` | Life insurance need, disability coverage need, and emergency fund adequacy from your actual income and assets |

### Family Financial Planning (v1.0.0 — 12 new tools)
| Tool | What it answers |
|------|----------------|
| `get_home_equity` | What is our home equity and LTV? |
| `get_fire_number` | What is our FI number? How many years until we're financially independent? |
| `get_financial_independence_roadmap` | Are we on track by Fidelity's retirement benchmarks? What is our Coast FI number? |
| `get_debt_overview` | How much are we paying in interest each year? When will each debt be paid off? |
| `get_gifting_and_estate_strategy` | How much can we gift tax-free? Are we exposed to estate taxes? |
| `get_50_30_20_analysis` | What is our needs/wants/savings split? Are we following the 50/30/20 rule? |
| `get_spending_by_account` | Which card are we using most? How does each person spend? |
| `get_upcoming_bills` | What bills are due this month? Are any subscriptions overdue? |
| `get_portfolio_concentration` | Are we too concentrated in any single stock? How diversified is our portfolio? |
| `get_net_worth_velocity` | Is our net worth growing faster than last year? |
| `get_tax_drag_analysis` | How much are misplaced assets costing us in taxes? What should we move to our IRA? |
| `get_annual_tax_advantaged_summary` | How much can we still contribute to our IRA/401k/HSA this year? |

### Transaction Management
| Tool | What it answers |
|------|----------------|
| `update_transaction` | **New.** Rename a transaction or change its category |
| `hide_transaction` | **New.** Exclude a transaction from your spending view |
| `get_transaction_splits` | **New.** View how a transaction is split across categories |
| `update_transaction_splits` | **New.** Split a transaction across multiple categories and amounts |

### Transaction Rules
| Tool | What it answers |
|------|----------------|
| `get_transaction_rules` | **New.** List all Emoney auto-categorization rules |
| `add_transaction_rule` | **New.** Create a rule to auto-categorize future transactions |
| `update_transaction_rule` | **New.** Modify an existing categorization rule |
| `apply_transaction_rule` | **New.** Apply a rule retroactively to existing transactions |

### Reports
| Tool | What it answers |
|------|----------------|
| `get_reports` | **New.** List all available Emoney reports by family |
| `get_report_url` | **New.** Generate a URL to open a specific Emoney report |

### Session Management
| Tool | What it does |
|------|-------------|
| `sync_chrome_session` | Import your active Emoney session from Chrome (no re-login) |
| `reset_session` | Clear saved session and force a fresh login |
| `clear_cache` | **New.** Selectively clear card or spending data cache to force fresh data |
| `get_available_cards` | **New.** Inventory of all responding Emoney data cards with key-type fingerprints |
| `get_aggregation_status` | **New.** Which linked institutions are broken/disconnected and each account's last-updated date |
| `get_vault_documents` | **New.** eMoney Vault top-level folders (file count, size, sharing) + total storage usage |
| `get_all_goals_funding_status` | **New.** Plan goals (retirement, leave-to-heirs, education/spending) with Monte Carlo success probability, surplus/shortfall, and status |
| `get_lifetime_cash_flow_projection` | **New.** Year-by-year lifetime cash flow — inflow/outflow/net, portfolio value, net worth, withdrawals + peak/depletion summary |
| `get_version` | Installed version, cookie file path, and session status |
| `get_features` | List all tools with descriptions and example questions |
| `explore_emoney_cards` · `explore_emoney_site` · `explore_snb_write_endpoints` | Developer/discovery probes for finding new Emoney data sources and endpoints |

### Advanced Planning Calculators (v1.0.19 — 11 new tools)
| Tool | What it answers |
|------|----------------|
| `get_multi_year_tax_projection` | Taxable income, bracket, and tax over N years; flags low-bracket "conversion window" years |
| `get_roth_conversion_ladder` | Multi-year Roth conversion plan filling each year's bracket up to a target rate |
| `get_irmaa_analysis` | Medicare IRMAA surcharge tier, distance to the next cliff, and the cost of crossing it |
| `get_withdrawal_sequencing_strategy` | Tax-efficient drawdown order vs. proportional, with estimated lifetime tax saved |
| `get_retirement_income_plan` | Year-by-year guaranteed income vs. spending need, withdrawal rate, and depletion age |
| `get_emergency_fund_analysis` | Months of expenses covered vs. a target, with surplus/shortfall |
| `get_idle_cash_optimization` | Low-yield cash and the annual income uplift from HYSA/MMF/T-bills |
| `get_mortgage_amortization_schedule` | Interest vs. principal over time, total interest, payoff date (+ extra payments) |
| `get_mortgage_refinance_analysis` | Monthly savings, break-even month, and lifetime interest change for a refi |
| `get_mortgage_payoff_vs_invest` | Paying down the mortgage vs. investing the difference, after tax |
| `get_financial_alerts` | One prioritized "what needs attention" list across accounts, spending, bills, and portfolio |

### Advanced Planning Calculators (v1.0.21 — 5 new tools)
| Tool | What it answers |
|------|----------------|
| `get_charitable_giving_strategy` | Best giving vehicle — QCD, donor-advised-fund bunching, or in-kind appreciated stock — with estimated tax benefit and which lots to gift |
| `get_tax_gain_harvesting` | Room in the 0% long-term capital-gains bracket and which lots to sell to reset basis tax-free |
| `get_state_tax_estimate` | State income tax on a Roth conversion, capital gain, or withdrawal (50 states + DC; the first non-federal tax modeling) |
| `get_healthcare_cost_projection` | Lifetime retirement healthcare costs, split pre-65 (ACA) and post-65 (Medicare), inflated |
| `get_hsa_optimization` | HSA triple-tax framing, invest-vs-spend, and balance trajectory to a target age |

### Advanced Planning Calculators (v1.0.22 — 4 new tools)
| Tool | What it answers |
|------|----------------|
| `get_income_sources_timeline` | Chronological view of when each income stream switches on (SS, pension, annuity, RMDs at 73) and when the mortgage frees cash flow; flags bridge gap years |
| `get_portfolio_risk_metrics` | Annualized volatility, max drawdown, Sharpe, and estimated beta from portfolio value history |
| `get_benchmark_comparison` | Portfolio return vs. a blended stock/bond benchmark's long-run expected return |
| `get_sequence_of_returns_stress_test` | Portfolio survival under an adverse early-returns sequence (2000/2008 starts) vs. the average case — the risk Monte Carlo averages away |

### Advanced Planning Calculators (v1.0.23 — 2 new tools)
| Tool | What it answers |
|------|----------------|
| `model_life_event_scenario` | "What happens to the plan if ___?" — early retirement, home purchase, new child, job loss, downsizing, or a market crash, baseline vs. scenario |
| `get_estate_liquidity_analysis` | Can the estate pay its tax + debts + final expenses without a forced sale? Flags illiquid-heavy estates at risk |

---

## Tax planning disclaimer

Tax estimates use 2026 IRS figures (brackets, limits, LTCG thresholds). Federal tax is the default; `get_state_tax_estimate` adds an approximate state layer (top marginal rate, 2025 figures). Always consult a qualified tax professional before making tax decisions.

---

## Changelog

See the [full changelog](https://github.com/bluhayz/emoney_mcp/blob/main/CHANGELOG.md) for version history.
