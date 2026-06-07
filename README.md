# emoney_mcp

MCP server for [Emoney Advisor](https://wealth.emaplan.com) — exposes your complete financial picture as tools Claude Desktop can call.

> **Ask Claude:** *"What's my net worth?"* · *"How is my portfolio performing?"* · *"Am I on track for retirement?"*

---

## How it works

1. On first use, a Chrome window opens (via [nodriver](https://github.com/ultrafunkamsterdam/nodriver)) — log in normally including SMS MFA.
2. The server saves your session cookies to a local file.
3. All subsequent data fetches use [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprint) to call Emoney's internal JSON APIs — no browser needed until the session expires.

Emoney has no public API, so this uses browser automation for login and reverse-engineered internal endpoints for data.

---

## Prerequisites

- Python 3.11+
- Google Chrome installed at the default path
- Claude Desktop

---

## Installation

```bash
git clone https://github.com/bluhayz/emoney_mcp.git
cd emoney_mcp
py -m pip install -e .
```

---

## Claude Desktop configuration

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "emoney": {
      "command": "py",
      "args": ["-m", "emoney_mcp.server"],
      "cwd": "D:\\ClaudeCode\\emoney_mcp\\src",
      "env": {
        "EMONEY_SUBDOMAIN": "wealth"
      }
    }
  }
}
```

Adjust `cwd` to wherever you cloned the repo. Restart Claude Desktop after saving.

---

## Available tools (13 total)

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
| `get_asset_allocation` | Top 10 holdings by portfolio weight for concentration risk analysis |
| `get_performance` | Portfolio value change today + MTD net worth change + 1-month and 3-month computed returns |
| `get_transactions` | Investment transactions (buys, sells, dividends). Parameters: `days` (default 30, max 365), `account_id` (optional) |
| `get_capital_gains` | Realized gains summary — sell proceeds, dividends, interest by tax year. Parameter: `year` (default current) |

### 🎯 Financial Planning

| Tool | Description |
|------|-------------|
| `get_goals` | Financial goals and funding status from Emoney's plan — retirement, education, and spending goals with percent funded |

### 💳 Cash Flow

| Tool | Description |
|------|-------------|
| `get_spending` | Cash flow for last 30 days — income, expenses, net, savings rate, and 5 most recent transactions |

### 🔧 Session Management

| Tool | Description |
|------|-------------|
| `sync_chrome_session` | Pull active Emoney session from a running Chrome browser (no re-login if already logged in) |
| `reset_session` | Clear saved session and force a fresh login on next call |

---

## First-time login flow

1. Ask Claude anything — e.g. *"What's my net worth?"*
2. A Chrome window opens — log in: username → password → SMS verification code.
3. Once the Emoney home page loads, the session is automatically saved.
4. Call your tool again — it works instantly.
5. Subsequent calls work without re-login until the session expires (typically a few hours).

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
```

---

## Architecture

```
Claude Desktop
     │  MCP stdio
     ▼
emoney_mcp/server.py       ← tool registration + dispatch
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

---

## Internal API endpoints used

| Endpoint | Data |
|----------|------|
| `CardSwitcher/GetCard/1` | Account groups with balances |
| `CardSwitcher/GetCard/2` | Financial goals and funding status |
| `CardSwitcher/GetCard/3` | Investment portfolio value + daily change |
| `CardSwitcher/GetCard/8` | Net worth + monthly history |
| `CardSwitcher/GetCard/11` | Net worth MTD and YTD change |
| `CardSwitcher/GetCard/13` | Cash flow — income, expenses, recent transactions |
| `Investments/GetInvestmentData` | Holdings, positions, cost basis |
| `Investments/GetInvestmentTransactions` | Transaction history (POST, requires CSRF token) |

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
