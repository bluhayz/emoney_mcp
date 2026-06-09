# emoney-mcp

Connect your [Emoney Advisor](https://wealth.emaplan.com) account to Claude Desktop.
Ask Claude questions about your finances in plain English — no spreadsheets, no dashboards.

> *"How are my finances looking?"* · *"Am I over budget this month?"* · *"When will I hit $2M?"*  
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
Give me a full financial summary.
What's my financial health score?
```

### Net worth & accounts
```
What's my current net worth?
How has my net worth changed over the last 6 months?
When will I hit $2 million?
How much do I have in tax-free vs. tax-deferred accounts?
```

### Spending & budgets
```
Am I over budget this month?
What are my biggest spending categories?
Am I spending more this year than last year?
What subscriptions am I paying for?
How much have I spent at Costco this year?
Show me all Amazon charges over $50.
```

### Cash flow
```
Will I have enough cash to cover a big purchase in 3 months?
What does my cash flow look like through year-end?
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
How much can I convert to Roth without crossing the next bracket?
Where can I harvest tax losses this year?
What would it cost to convert $150,000 to Roth?
What's my capital gains tax exposure if I sell my concentrated positions?
How much can I still contribute to my IRA and HSA this year?
When do I have to start taking RMDs, and how much will they be?
Should I claim Social Security at 62 or wait until 70?
How much do I owe in estimated taxes each quarter?
```

### Retirement planning
```
Can I afford to retire now?
How long will my money last at different withdrawal rates?
What are the odds my portfolio lasts 30 years?
Run a Monte Carlo simulation on my retirement plan.
Should I adjust my withdrawals this year given market performance?
When will I be debt-free?
Are we on track for college savings?
```

---

## Available tools (42 total)

### Overview & Dashboard
| Tool | What it answers |
|------|----------------|
| `get_quick_status` | 5-number snapshot: net worth, portfolio change, savings rate, top spending category, goal status |
| `get_financial_summary` | Full dashboard: net worth, performance, income vs. spending, top 5 categories, goal status |
| `get_financial_health_score` | 0–100 composite score (A–F) across savings rate, goal funding, debt ratio, emergency fund, diversification, net worth trend |

### Balance Sheet
| Tool | What it answers |
|------|----------------|
| `get_accounts` | All accounts by type with balances and net worth |
| `get_net_worth` | Current net worth (assets minus liabilities) |
| `get_net_worth_history` | Monthly net worth trend (up to 60 months) |
| `get_net_worth_breakdown` | Net worth by person, liquidity, and tax treatment |
| `get_retirement_accounts` | All tax-advantaged accounts (401k, IRA, Roth, HSA, 529) with subtotals |

### Investments
| Tool | What it answers |
|------|----------------|
| `get_holdings` | All positions: ticker, value, cost basis, unrealized gain/loss |
| `get_asset_allocation` | Allocation by asset class plus top 10 holdings |
| `get_performance` | Portfolio return today, MTD, YTD |
| `get_transactions` | Investment transactions (buys, sells, dividends) |
| `get_capital_gains` | Realized gains by tax year |
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
| `get_income_summary` | Income sources grouped by payee |
| `get_savings_rate` | Month-by-month savings rate |
| `search_transactions` | Search by keyword, category, or amount range |
| `get_recurring_charges` | Detected subscriptions and recurring bills |

### Goals
| Tool | What it answers |
|------|----------------|
| `get_goals` | Retirement, education, and spending goals with % funded |

### Tax Planning
| Tool | What it answers |
|------|----------------|
| `get_tax_bracket_headroom` | Room remaining in your current bracket before the next threshold |
| `get_tax_loss_harvesting` | Taxable positions with unrealized losses ranked by size |
| `get_contribution_room` | 2025 IRS limits for 401k, IRA, HSA, SIMPLE IRA, SEP IRA, 529 |
| `get_roth_conversion_analysis` | Tax cost and long-term benefit of a Roth conversion |
| `get_capital_gains_exposure` | Estimated tax bill on unrealized gains if sold today |
| `get_rmd_estimate` | Required Minimum Distribution schedule (10-year projection) |
| `get_social_security_optimizer` | SS benefit at 62 / FRA / 70 with breakeven ages and spousal analysis |
| `get_quarterly_estimated_taxes` | Q1–Q4 estimated federal payment amounts and due dates |

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

### Session Management
| Tool | What it does |
|------|-------------|
| `sync_chrome_session` | Import your active Emoney session from Chrome (no re-login) |
| `reset_session` | Clear saved session and force a fresh login |
| `get_features` | List all tools with descriptions and example questions |

---

## Tax planning disclaimer

Tax estimates use 2025 IRS figures (brackets, limits, LTCG thresholds) and cover federal tax only. Always consult a qualified tax professional before making tax decisions.

---

## Changelog

See the [full changelog](https://github.com/bluhayz/emoney_mcp/blob/main/CHANGELOG.md) for version history.
