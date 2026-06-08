# emoney_mcp

MCP server for [Emoney Advisor](https://wealth.emaplan.com) — exposes your complete financial picture as tools Claude Desktop can call.

> **Ask Claude:** *"How are my finances looking?"* · *"What subscriptions am I paying for?"* · *"What's my savings rate?"* · *"How much have I spent at Costco this year?"* · *"Am I on track for retirement?"*

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
      "args": ["--from", "D:/ClaudeCode/emoney_mcp", "emoney-mcp"],
      "env": {
        "EMONEY_SUBDOMAIN": "wealth"
      }
    }
  }
}
```

---

## Available tools (21 total)

### 🏠 Overview

| Tool | Description |
|------|-------------|
| `get_financial_summary` | **Start here.** Single-call executive dashboard — net worth, portfolio performance, this month's income vs. spending, top 5 categories, and goal status. Best for broad questions like *"How are my finances?"* |

### 💰 Balance Sheet

| Tool | Description |
|------|-------------|
| `get_accounts` | All financial accounts grouped by type (investments, bank, retirement, debt, property) with balances and full net worth summary |
| `get_net_worth` | Current net worth — total assets minus total liabilities |
| `get_net_worth_history` | Monthly net worth trend. Parameter: `months` (default 12, max 60) |
| `get_net_worth_breakdown` | Net worth broken down three ways: by **person** (Drew/Lacey/Joint), by **liquidity** (Liquid/Semi-liquid/Illiquid), and by **tax treatment** (Taxable/Tax-Deferred/Tax-Free) |
| `get_retirement_accounts` | Aggregates all tax-advantaged accounts — 401k, IRA, Roth IRA, annuities, HSA, 529 — with subtotals by category |

### 📈 Investments

| Tool | Description |
|------|-------------|
| `get_holdings` | All investment positions across every account — ticker, units, price, value, cost basis, unrealized gain/loss |
| `get_asset_allocation` | Portfolio asset allocation by asset class (Equities, Fixed Income, Cash, etc.) plus top 10 holdings by weight |
| `get_performance` | Portfolio value change today + MTD net worth change + computed returns from historical data |
| `get_transactions` | Investment transactions (buys, sells, dividends). Parameters: `days` (default 30, max 365), `account_id` (optional GUID) |
| `get_capital_gains` | Realized gains summary — sell proceeds, dividends, interest by tax year. Parameter: `year` (default current) |

### 🎯 Financial Planning

| Tool | Description |
|------|-------------|
| `get_goals` | Financial goals and funding status from Emoney's plan — retirement, education, and spending goals with percent funded |

### 💳 Cash Flow & Spending

| Tool | Description |
|------|-------------|
| `get_spending` | Cash flow summary — income, expenses, net cash flow, savings rate, and 5 most recent transactions. Parameter: `months` (default 1) |
| `get_spending_transactions` | Bank and credit card transactions with **category labels** (Groceries, Dining, Travel, etc.) and **top merchants** with location dedup. Parameter: `days` (default 30, max 365) |
| `get_spending_trends` | Month-over-month category comparison — which categories are trending up/down, plus monthly income vs. spending per month. Parameter: `months` (default 3, max 12) |
| `get_income_summary` | Income sources and monthly income trend — paychecks, direct deposits, dividends, interest grouped by source. Parameter: `days` (default 90, max 365) |
| `get_savings_rate` | Month-by-month savings rate (income minus spending ÷ income). Parameter: `months` (default 6, max 12) |
| `search_transactions` | Search transactions by keyword, category, and/or amount range across up to 365 days. Parameters: `query`, `category`, `days`, `min_amount`, `max_amount` |
| `get_recurring_charges` | Detects subscriptions and recurring bills by analyzing 120 days of transaction patterns. Returns weekly/monthly/quarterly charges and total estimated monthly recurring spend |

### 🔧 Debug & Session Management

| Tool | Description |
|------|-------------|
| `sync_chrome_session` | Pull active Emoney session from a running Chrome browser (no re-login if already logged in) |
| `reset_session` | Clear saved session and force a fresh login on next call |
| `get_version` | Returns installed version, cookie file path, and session status — useful for debugging |

---

## Merchant normalization

All spending tools normalize raw bank descriptions before grouping, so visits to the same merchant at different locations are counted together:

| Raw description | Normalized |
|----------------|------------|
| `APLPAY FOOD LION VA` | `FOOD LION` |
| `COSTCO WHSE STERLING US` | `COSTCO WHSE` |
| `COSTCO WHSE RESTON VA` | `COSTCO WHSE` ← grouped |
| `UNITED AIRLINES HOUSTON TX` | `UNITED AIRLINES` |
| `TST AUSTIN GRILL VA` | `AUSTIN GRILL` |
| `SQ *BLUE BOTTLE COFFEE` | `BLUE BOTTLE COFFEE` |

**Stripped:** payment-network prefixes (`APLPAY`, `SQ *`, `TST`, `PP *`), trailing state abbreviations, city names, country suffixes, ZIP codes, store numbers.

**Protected words** (`MARKET`, `TIMES`, `GRILL`, `STORE`, etc.) are never stripped — preventing false positives like `WHOLE FOODS MARKET` → `WHOLE FOODS`.

---

## First-time login flow

1. Ask Claude anything — e.g. *"What's my net worth?"*
2. A Chrome window opens — log in: username → password → SMS verification code.
3. Once the Emoney home page loads, the session is automatically saved to `~/.emoney_mcp/session.json`.
4. Call your tool again — it works instantly.
5. Subsequent calls work without re-login until the session expires (typically a few hours).

**Tip:** Use `sync_chrome_session` if you are already logged in to Emoney in Chrome — it imports your cookies without opening a new window.

---

## Example questions to ask Claude

### Overview
```
How are my finances looking?
Give me a complete financial summary.
```

### Net Worth & Wealth
```
What's my current net worth?
How has my net worth changed over the last 6 months?
How is our wealth split between Drew and Lacey?
How much of my assets are liquid vs. illiquid?
How much do I have in tax-free vs. tax-deferred accounts?
```

### Investments
```
What are my biggest investment holdings?
How is my portfolio performing this month?
How concentrated am I in any single stock?
What are my realized capital gains this year?
Show me all my buy and sell transactions in the last 90 days.
How much do I have in retirement accounts?
```

### Goals & Planning
```
Am I on track for retirement?
How funded is Parker's 529 education account?
```

### Spending & Cash Flow
```
What did I spend last month vs. what came in?
What are my top spending categories over the last 60 days?
How much did I spend on groceries last month?
Is my dining spending going up or down?
Compare my spending this month vs. last month by category.
```

### Income & Savings
```
What are all my income sources?
How much did Snowflake pay me in the last 90 days?
What is my savings rate over the last 6 months?
Am I saving more or less than last month?
```

### Search & Subscriptions
```
How much have I spent at Costco this year?
Show me all Amazon charges over $50.
What subscriptions am I paying for?
What are my recurring monthly bills?
What is my total monthly recurring spend?
```

---

## Architecture

```
Claude Desktop
     │  MCP stdio
     ▼
emoney_mcp/server.py       ← tool registration + dispatch (21 tools)
emoney_mcp/scraper.py      ← Emoney internal API calls (hot-reloaded)
emoney_mcp/browser.py      ← session management + nodriver login
     │
     ├── curl_cffi AsyncSession  ← Chrome TLS fingerprint for API calls
     └── nodriver (background thread)  ← Chrome login window when needed
```

**Key design decisions:**
- `nodriver` runs in a separate OS thread with its own `asyncio` event loop to avoid conflicting with the MCP server's event loop
- `importlib.reload(scraper)` on every tool call enables hot-reload — edit `scraper.py` and changes take effect immediately without restarting Claude Desktop
- Session cookies are persisted to `~/.emoney_mcp/session.json` — a stable path that works whether running via `uvx`, PyPI, or local clone
- The SNB API JWT token is extracted from the Spending page HTML on each call — no separate auth flow required

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
| `CS/CardSwitcher/GetCard/13` | Cash flow — income, expenses, recent transactions |

### Investments

| Endpoint | Data |
|----------|------|
| `CS/Investments/GetInvestmentData` | Holdings, positions, asset allocation, cost basis |
| `CS/Investments/GetInvestmentTransactions` | Transaction history (POST, requires CSRF token) |

### SNB API (`api.emoneyadvisor.com/snb-api`)

The spending module uses a separate REST API authenticated with a short-lived JWT token embedded in the Spending page HTML.

| Endpoint | Data |
|----------|------|
| `api/values/GetFilteredTransactions` | All bank/CC transactions with `categoryId` (up to 2,000 most recent) |
| `api/values/GetCategories` | 114 spending category names mapped by ID |
| `api/values/GetAccounts` | Linked bank and credit card accounts |

---

## Development & testing

```bash
git clone https://github.com/bluhayz/emoney_mcp.git
cd emoney_mcp

# Install with dev dependencies
uv pip install -e ".[dev]"

# Run tests
uv run pytest tests/ -v

# Syntax check
uv run python -m py_compile src/emoney_mcp/scraper.py src/emoney_mcp/server.py
```

Tests use fixture JSON files in `tests/fixtures/` and mock HTTP sessions — no live Emoney connection needed.

CI runs on GitHub Actions (Python 3.11, 3.12, 3.13) on every push and pull request. Every push to `main` also auto-publishes to PyPI.

---

## Session file

Cookies are saved to `~/.emoney_mcp/session.json` (`C:\Users\<you>\.emoney_mcp\session.json` on Windows). This path is stable regardless of how the package is installed.

Delete the file to force a fresh login.

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
