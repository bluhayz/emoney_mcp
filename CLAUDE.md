# emoney-mcp — AI Developer Context

MCP server bridging Claude (and other MCP clients) to Emoney Advisor (`emaplan.com`). Emoney has no public API; this server uses reverse-engineered internal JSON endpoints + Chrome cookie extraction for auth.

---

## Module Map

```
src/emoney_mcp/
├── server.py          # MCP entry point: list_tools(), call_tool(), _call_tool_inner(), private wrappers
├── browser.py         # EmoneyHttpSession (curl_cffi + cookie persistence), Chrome cookie extraction, nodriver fallback
├── scraper.py         # Backward-compat shim — re-exports scrapers/*; target of importlib.reload() in EMONEY_DEV mode
└── scrapers/
    ├── __init__.py    # Exports all public functions + clear_caches()
    ├── _helpers.py    # URL constants, _get_card() (TTL cache), _fmt_dollars()
    ├── accounts.py    # get_accounts, net worth breakdown, debt payoff plan
    ├── investments.py # holdings, allocation, performance, transactions, capital gains
    ├── spending.py    # SNB API integration: all transaction/budget/cashflow tools (most complex module)
    ├── goals.py       # get_goals, get_financial_summary, get_financial_health_score, get_quick_status
    ├── tax.py         # Tax math tools + IRS 2025 constants (update annually: _TAX_YEAR)
    ├── retirement.py  # Runway, Monte Carlo, withdrawal rate, net worth projection, guardrails
    └── portfolio.py   # Asset location efficiency, rebalancing targets, explore_emoney_cards
```

---

## Tool Dispatch Chain

Every MCP tool call flows through exactly these steps:

```
call_tool(name, args)                  # top-level try/except → JSON error on any exception
  └─ _call_tool_inner(name, args)      # if/elif dispatch tree
       └─ _<tool_name>()               # private wrapper: get session, call scraper
            ├─ _get_session_or_err()   # returns (session, None) or (None, error_dict)
            └─ scraper.<function>(session, ...args)
                 └─ returns dict → JSON string via TextContent
```

**Adding a new tool requires touching 6 locations:**

| # | File | What to add |
|---|------|-------------|
| 1 | `scrapers/<module>.py` | Implement `async def get_foo(http_session, ...) -> dict` |
| 2 | `scrapers/__init__.py` | Add to imports and `__all__` |
| 3 | `scraper.py` | Add to `from .scrapers import (...)` |
| 4 | `server.py` → `list_tools()` | Add `Tool(name=..., description=..., inputSchema=...)` |
| 5 | `server.py` → `_call_tool_inner()` | Add `elif name == "get_foo": result = await _get_foo(arg)` |
| 6 | `server.py` | Add `async def _get_foo(arg): sess, err = await _get_session_or_err(); if err: return err; return await scraper.get_foo(sess, arg)` |

---

## Data Sources

### CardSwitcher cards (primary)
`GET {BASE_URL}/ema/CS/CardSwitcher/GetCard/{id}?_={ts_ms}`  
All card fetches go through `_get_card(http, card_id)` in `_helpers.py` which applies a 300 s TTL cache (30 s on error). Always use `_get_card()` — never call the card URL directly.

| Card | Content |
|------|---------|
| 1 | Account groups with per-account detail |
| 2 | Financial plan goals |
| 3 | Investment portfolio value + today's change |
| 4 | Asset allocation model target |
| 8 | Net worth history array |
| 9 | Net worth totals (assets / liabilities) |
| 11 | Net worth change MTD/YTD |
| 13 | Cash flow summary + 5 recent transactions |

### SNB API (spending/transactions)
`https://api.emoneyadvisor.com/snb-api` — separate host, requires `Authorization: Bearer <jwt>` + `apikey` header scraped from the Spending page HTML. All SNB access goes through `spending._fetch_snb_data(http_session, days)` which caches raw results for 300 s.

### Investments endpoint
`POST {BASE_URL}/ema/CS/Investments/GetInvestmentTransactions` — requires `__RequestVerificationToken` (ASP.NET anti-forgery). Token fetched via `http_session.get_csrf_token()`.

---

## Session & Auth

**Normal flow**: `sync_chrome_session` tool reads cookies from live Chrome (Windows: AES-GCM decrypt from SQLite), saves to `~/.emoney_mcp/session.json` (mode 0o600). `curl_cffi.AsyncSession` with `impersonate="chrome120"` loads these cookies on every request.

**Fallback**: If Chrome extraction fails, `nodriver` opens a browser window in a separate OS thread (its own event loop to avoid conflicting with the MCP server loop). User logs in manually; cookies are extracted and saved automatically.

**Session expiry**: Any scraper returning `{"error": "...returned 401..."}` means session is stale → user should call `sync_chrome_session` or `reset_session`.

---

## Error Handling Conventions

All tools return a plain `dict`. On failure, the dict contains an `"error"` key with a human-readable message. Never raise from a scraper function — always return `{"error": "..."}`. The top-level `call_tool` catches any exception as a last resort.

```python
# Scraper pattern
card = await _get_card(http, 9)
if not card:
    return {"error": "Card 9 unavailable. Session may have expired — call reset_session."}
```

---

## Testing

**Framework**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed but adding it is harmless).

**No live network calls** in any test. All tests use `make_mock_http_session()` from `tests/helpers.py`.

```python
from helpers import make_mock_http_session, load_fixture

# Card-based tool
session = make_mock_http_session(card_responses={9: "card9_networth", 1: "card1_accounts"})

# Endpoint-based tool (substring URL match)
session = make_mock_http_session(endpoint_responses={"GetInvestmentData": "investment_data"})

# SNB-based tool (mock the data function directly)
with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw_txns, categories)):
    result = await get_spending_transactions(session, days=30)
```

Fixture files live in `tests/fixtures/<name>.json` containing the `Data` sub-object (the mock wraps it in `{"Data": ...}` automatically for cards).

Run tests: `pytest tests/ -v --tb=short`

---

## Key Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `EMONEY_SUBDOMAIN` | `wealth` | Builds `https://<subdomain>.emaplan.com` |
| `EMONEY_SESSION_FILE` | `~/.emoney_mcp/session.json` | Cookie persistence path |
| `EMONEY_DEV` | unset | `1` = hot-reload `scraper` module on every tool call |

---

## Important Constraints

- **Tax constants are hardcoded for 2025** (`_TAX_YEAR` in `tax.py`). Update `_BRACKETS`, `_CONTRIBUTION_LIMITS`, `_STD_DEDUCTION`, `_LTCG_THRESHOLDS`, `_NIIT_THRESHOLD` each January.
- **`nodriver` runs in its own OS thread** with a separate event loop — do not try to `await` it from the main async context.
- **`curl_cffi` is required** (not `aiohttp`/`httpx`) — Emoney blocks standard Python TLS fingerprints; Chrome impersonation is mandatory.
- **`scraper.py` is the hot-reload target** — `EMONEY_DEV=1` calls `importlib.reload(scraper)` on every tool call. The shim must re-export everything for this to work. Do not add logic to `scraper.py`; put all logic in `scrapers/`.
- **Card IDs are undocumented** — use `explore_emoney_cards` tool to find new card IDs before building a scraper for them.
- **Monte Carlo uses `random.Random(42)`** — results are deterministic/reproducible by design.
- **`asyncio.gather` is used in goals.py** for parallel card fetches — each result must be checked individually for errors before use.

---

## CI / Release

- **CI** (`.github/workflows/ci.yml`): pytest on Python 3.11/3.12/3.13 + `pip-audit` security scan.
- **Publish** (`.github/workflows/publish.yml`): PyPI release triggered by push to `main`. Bump version in `pyproject.toml` before merging.
- **Build system**: `hatchling`. Package root is `src/emoney_mcp/`.
