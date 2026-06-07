# emoney_mcp

MCP server for [Emoney Advisor](https://wealth.emaplan.com) — exposes your complete financial picture as tools Claude Desktop can call.

> **Ask Claude:** *"What's my net worth?"* · *"How is my portfolio performing?"* · *"Am I on track for retirement?"* · *"What did I spend at Costco last month?"*

---

## How it works

1. On first use, a Chrome window opens (via [nodriver](https://github.com/ultrafunkamsterdam/nodriver)) — log in normally including SMS MFA.
2. The server saves your session cookies to a local file.
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
      "args": [
        "--from", "git+https://github.com/bluhayz/emoney_mcp",
        "emoney-mcp"
      ],
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
      "args": [
        "--from", "D:/ClaudeCode/emoney_mcp",
        "emoney-mcp"
      ],
      "env": {
        "EMONEY_SUBDOMAIN": "wealth"
      }
    }
  }
}
```

Or use the traditional `py -m` approach:

```json
{
  "mcpServers": {
    "emoney": {
      "command": "py",
      "args": ["-m", "emoney_mcp.server"],
      "cwd": "D:\\ClaudeCode\\emoney_mcp\\src",
      "env": { "EMONEY_SUBDOMAIN": "wealth" }
    }
  }
}
```

---

## Available tools (14 total)

### 💰 Balance Sheet

| Tool | Description |
|------|-------------|
| `get_accounts` | All financial accounts grouped by type (investments, bank, retirement, debt, property) with balances and full net worth summary |
| `get_net_worth` | Current net worth — total assets minus total liabilities |
| `get_net_worth_history` | Monthly net worth trend. Parameter: `months` (default 12, max 60) |
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
| `get_spending` | Cash flow summary for recent months — income, expenses, net cash flow, savings rate, and 5 most recent transactions. Parameter: `months` (default 1) |
| `get_spending_transactions` | Bank and credit card transactions with **category labels** (Groceries, Dining, Travel, etc.) and **top merchants**. Parameter: `days` (default 30, max 365) |

### 🔧 Session Management

| Tool | Description |
|------|-------------|
| `sync_chrome_session` | Pull active Emoney session from a running Chrome browser (no re-login if already logged in) |
| `reset_session` | Clear saved session and force a fresh login on next call |

---

## `get_spending_transactions` — merchant dedup

The spending transactions tool normalizes raw bank descriptions before grouping by merchant, so visits to the same store at different locations are counted together:

| Raw description | Normalized merchant |
|----------------|---------------------|
| `APLPAY FOOD LION VA` | `FOOD LION` |
| `COSTCO WHSE STERLING US` | `COSTCO WHSE` |
| `COSTCO WHSE RESTON VA` | `COSTCO WHSE` ← grouped |
| `UNITED AIRLINES HOUSTON TX` | `UNITED AIRLINES` |
| `TST AUSTIN GRILL VA` | `AUSTIN GRILL` |
| `SQ *BLUE BOTTLE COFFEE` | `BLUE BOTTLE COFFEE` |

**What gets stripped:** payment-network prefixes (`APLPAY`, `SQ *`, `TST`, `PP *`), trailing state abbreviations (from a fixed 50-state list), city names, country suffixes (`US`, `USA`), ZIP codes, and store numbers.

**Protected words** (`MARKET`, `TIMES`, `GRILL`, `STORE`, etc.) are never stripped, preventing false positives like `WHOLE FOODS MARKET` → `WHOLE FOODS`.

Internal financial flows (transfers, payroll, credit card payments, investment income) are excluded from the merchant list so it shows real spending only.

---

## First-time login flow

1. Ask Claude anything — e.g. *"What's my net worth?"*
2. A Chrome window opens — log in: username → password → SMS verification code.
3. Once the Emoney home page loads, the session is automatically saved.
4. Call your tool again — it works instantly.
5. Subsequent calls work without re-login until the session expires (typically a few hours).

**Tip:** Use `sync_chrome_session` if you are already logged in to Emoney in Chrome — it imports your cookies without opening a new window.

---

## Example questions to ask Claude

```
What's my current net worth?
How has my net worth changed over the last 6 months?
What are my biggest investment holdings?
How is my portfolio performing this month?
Am I on track for retirement?
How much do I have in tax-advantaged retirement accounts?
What did I spend last month vs. what came in?
What are my realized capital gains this year?
Show me all my buy and sell transactions in the last 90 days.
How concentrated am I in any single stock?

What did I spend on groceries last month?
What are my top spending categories over the last 60 days?
Which merchants did I spend the most at?
How much have I spent at Costco this year?
Show me my dining and restaurant expenses.
```

---

## Architecture

```
Claude Desktop
     │  MCP stdio
     ▼
emoney_mcp/server.py       ← tool registration + dispatch (14 tools)
emoney_mcp/scraper.py      ← Emoney internal API calls (hot-reloaded)
emoney_mcp/browser.py      ← session management + nodriver login
     │
     ├── curl_cffi AsyncSession  ← Chrome TLS fingerprint for API calls
     └── nodriver (background thread)  ← Chrome login window when needed
```

**Key design decisions:**
- `nodriver` runs in a separate OS thread with its own `asyncio` event loop to avoid conflicting with the MCP server's event loop
- `importlib.reload(scraper)` on every tool call enables hot-reload — edit `scraper.py` and changes take effect immediately without restarting Claude Desktop
- Session cookies are persisted to `.emoney_session.json` so login is only needed once per session
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

CI runs on GitHub Actions (Python 3.11, 3.12, 3.13) on every push and pull request.

---

## Session file

Cookies are saved to `.emoney_session.json` at the project root. This file is in `.gitignore` and should **never be committed** — it contains live session credentials.

Delete it to force a fresh login.

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
