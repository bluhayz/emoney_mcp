# emoney-mcp — AI Developer Context

MCP server bridging Claude (and other MCP clients) to Emoney Advisor (`emaplan.com`). Emoney has no public API; this server uses reverse-engineered internal JSON endpoints + Chrome cookie extraction for auth.

**Current version: 1.0.6 · 82 MCP tools.** Read-only data tools (cards + SNB + Profile), transaction/rules **write** tools, report links, and a large set of pure-Python planning/tax calculators.

---

## Module Map

```
src/emoney_mcp/
├── server.py          # MCP entry point: list_tools(), call_tool(), _call_tool_inner(), private wrappers (82 tools)
├── browser.py         # EmoneyHttpSession (curl_cffi + cookie persistence, 0o600), Chrome cookie extraction, nodriver fallback
├── scraper.py         # Backward-compat shim — re-exports scrapers/*; target of importlib.reload() in EMONEY_DEV mode
└── scrapers/
    ├── __init__.py    # Re-exports all public functions + clear_caches() + clear_cache(module)
    ├── _helpers.py    # URL constants, _get_card() (TTL cache + int coercion), _fmt_dollars()
    ├── accounts.py    # get_accounts, get_retirement_accounts, get_net_worth_breakdown, get_debt_payoff_plan,
    │                  #   get_debt_overview, get_client_profile, get_aggregation_status
    ├── investments.py # get_holdings, get_asset_allocation, get_net_worth_history, get_performance,
    │                  #   get_transactions, get_capital_gains
    ├── spending.py    # SNB API: all transaction/budget/cashflow tools — get_spending(_transactions/_trends/_by_account),
    │                  #   get_income_summary, get_savings_rate, search_transactions, get_recurring_charges,
    │                  #   get_budget_vs_actual, get_year_over_year, get_cash_flow_projection/forecast,
    │                  #   get_unusual_transactions, get_merchant_spending, get_50_30_20_analysis,
    │                  #   get_upcoming_bills, get_categories, explore_snb_write_endpoints (largest module)
    ├── goals.py       # get_goals, get_financial_summary, get_financial_health_score, get_quick_status,
    │                  #   get_college_savings_gap, get_monthly_review
    ├── tax.py         # Tax math tools + IRS 2025 constants — get_tax_loss_harvesting, get_contribution_room,
    │                  #   get_roth_conversion_analysis, get_capital_gains_exposure, get_rmd_estimate,
    │                  #   get_tax_bracket_headroom, get_social_security_optimizer, get_quarterly_estimated_taxes,
    │                  #   get_year_end_checklist, get_annual_tax_advantaged_summary
    ├── retirement.py  # get_retirement_runway, get_withdrawal_rate_analysis, get_net_worth_projection,
    │                  #   run_monte_carlo_retirement, get_dynamic_withdrawal_guardrails, run_scenario,
    │                  #   get_financial_independence_roadmap
    ├── portfolio.py   # get_asset_location_efficiency, get_rebalancing_targets, explore_emoney_cards,
    │                  #   get_available_cards, get_portfolio_concentration, get_net_worth_velocity,
    │                  #   get_tax_drag_analysis
    ├── planning.py    # get_insurance_gap_analysis, get_home_equity, get_fire_number,
    │                  #   get_gifting_and_estate_strategy
    ├── transactions.py# WRITE ops via CS/Spending — update_transaction, hide_transaction,
    │                  #   get/update_transaction_splits, get/add/update/apply_transaction_rule (v0.9.0+)
    ├── reports.py     # get_reports (parse Reports page), get_report_url (CS/Reports/GetReportUrl) (v0.9.0+)
    └── explore.py     # explore_emoney_site — dev/discovery crawler that mines pages for endpoints
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

**Adding a new tool requires touching 6 locations** (if creating a new module, also add to `scrapers/__init__.py` imports + `__all__` before step 2).

| # | File | What to add |
|---|------|-------------|
| 1 | `scrapers/<module>.py` | Implement `async def get_foo(http_session, ...) -> dict` |
| 2 | `scrapers/__init__.py` | Add to imports and `__all__` |
| 3 | `scraper.py` | Add to `from .scrapers import (...)` |
| 4 | `server.py` → `list_tools()` | Add `Tool(name=..., description=..., inputSchema=...)` |
| 5 | `server.py` → `_call_tool_inner()` | Add `elif name == "get_foo": result = await _get_foo(arg)` |
| 6 | `server.py` | Add `async def _get_foo(arg): sess, err = await _get_session_or_err(); if err: return err; return await scraper.get_foo(sess, arg)` |

---

## Infrastructure Extras

**Selective cache invalidation**: `clear_cache(module)` — accepts `'cards'`, `'spending'`, or `'all'`. Also registered as an MCP tool.

**Session health check**: `_get_session_or_err()` in server.py calls `_http_session.is_logged_in()` (throttled, ~every 5 minutes). Returns a `session_warning` error dict if the session is stale before the tool attempt fails.

**Card discovery**: `explore_emoney_cards` probes arbitrary card IDs; `get_available_cards` wraps it with a clean per-card inventory (key names + type hints). Use these to find new card IDs before building a scraper.

**Site discovery**: `explore_emoney_site` (scrapers/explore.py) GET-crawls major Emoney pages and mines HTML/JS for API endpoints, form actions, and nav links — used to reverse-engineer new endpoints.

**Category lookup**: `get_categories` returns the full SNB category name→id map (≈114 categories), backed by the shared SNB cache. Needed to translate `category_id` for `update_transaction` / rules.

---

## Data Sources

### CardSwitcher cards (primary read path)
`GET {BASE_URL}/ema/CS/CardSwitcher/GetCard/{id}?_={ts_ms}`
All card fetches go through `_get_card(http, card_id)` in `_helpers.py` — 300 s TTL cache (30 s on error), and **`card_id` is coerced to `int`** before building the URL (see Security). Always use `_get_card()`.

| Card | Content | Used by |
|------|---------|---------|
| 1 | Account groups with per-account detail | get_accounts |
| 2 | Financial plan goals | goals tools |
| 3 | Investment portfolio value + today's change | performance |
| 4 | Asset allocation model target | get_asset_allocation |
| 6 | Top holdings with tickers (fast) | get_portfolio_concentration |
| 8 | Net worth **History** array (oldest-first / newest-last) + NetWorth | get_net_worth_history, get_net_worth_velocity |
| 9 | Net worth totals (assets / liabilities) | net worth tools |
| 10 | Cash + credit summary | get_home_equity |
| 11 | Net worth change MTD/YTD | performance |
| 13 | Cash flow summary + 5 recent transactions | get_spending |
| 20 | Aggregation status: BrokenConnections + Accounts freshness | get_aggregation_status |

> **Card 8 ordering gotcha:** `History` is **oldest-first**; the newest element is the current month. Current net worth comes from the `NetWorth` field. Mislabeling this order silently reverses trends (fixed in v1.0.5 — `get_net_worth_velocity`).

### SNB API (spending / transactions, read)
`https://api.emoneyadvisor.com/snb-api` — separate host, requires `Authorization: Bearer <jwt>` + `apikey` header scraped from the Spending page HTML via `_get_snb_credentials()`. All SNB reads go through `spending._fetch_snb_data()` / `_fetch_snb_raw()` (300 s cache).

| Endpoint | Data |
|----------|------|
| `api/values/GetFilteredTransactions` | All bank/CC transactions with `categoryId` |
| `api/values/GetCategories` | Spending category names by ID |
| `api/values/GetAccounts` | SNB account id→name map (for get_spending_by_account) |

### CS/Spending (transaction + rules WRITE path)
`POST {BASE_URL}/ema/CS/Spending/<action>` — all go through `transactions._csrf_post()`, which adds `__RequestVerificationToken` (ASP.NET anti-forgery) to the body and sets `X-Requested-With: XMLHttpRequest`. Nested fields use jQuery bracket notation (`TransactionID[Value]=...`, `rule[CategoryID][Value]=...`).

| Action | Tool |
|--------|------|
| `UpdateTransaction` | update_transaction |
| `UpdateTransactionHiddenStatus` | hide_transaction |
| `GetAllBankTransactionSplits` / `UpdateTransactionSplits` | get/update_transaction_splits |
| `GetRules` (empty body) / `AddRule` / `UpdateRule` / `ApplyRule` (`{ruleID, transactionID}`) | rules tools |

> **Nexus backend:** these writes are served by Emoney's "Nexus" backend, which periodically returns HTTP 500 with `IsNexusAvailable:false` ("Your data is unavailable due to maintenance"). `_csrf_post` surfaces the body in the error dict. `GetRules` also returns 500 when no rules exist — treated as an empty list. End-to-end write verification is tracked in GitHub issue #19.

### CS/Profile and CS/Reports
- `GET /ema/CS/Profile/GetProfileData` — household identity (names, DOB, ages, dependents, properties) → `get_client_profile`.
- `GET /ema/CS/Reports` (parse embedded JSON) + `POST /ema/CS/Reports/GetReportUrl` → `get_reports` / `get_report_url` (returns a viewable report URL; never auto-followed).

### Investments endpoint
`GET/POST {BASE_URL}/ema/CS/Investments/...` — `GetInvestmentData` (holdings/allocation) and `GetInvestmentTransactions` (POST, requires CSRF token via `http_session.get_csrf_token()`).

---

## Session & Auth

**Normal flow**: `sync_chrome_session` reads cookies from live Chrome (Windows: AES-GCM decrypt of the copied SQLite DB via DPAPI), saves to `~/.emoney_mcp/session.json`. `curl_cffi.AsyncSession` with `impersonate="chrome120"` loads these cookies on every request.

**Fallback**: If Chrome extraction fails, `nodriver` opens a browser window in a separate OS thread (its own event loop) for manual login; cookies are extracted (CDP → JS → temp-profile DB) and saved.

**Session expiry**: scrapers return `{"error": "...401..."}` or a `session_warning` → user should call `sync_chrome_session` or `reset_session`.

---

## Security & Hardening

- **`card_id` is coerced to `int`** in `_get_card()` before URL interpolation; non-coercible values return `None` with no request. Prevents path/query injection (e.g. `"8/../SignOut"`) via the user/model-supplied `card_ids` list in `explore_emoney_cards`/`get_available_cards`. (v1.0.6)
- **Session cookie file is owner-only**: `save_cookies()` sets the directory to `0o700` and the file to `0o600` via `os.fchmod` on every write (so a pre-existing looser file is tightened, not just newly created ones). Guarded for platforms without `os.fchmod` (Windows). (v1.0.6)
- **Secrets never logged or persisted in plaintext logs**: the nodriver logger prints cookie *counts* and *key names* only; SNB JWT / apikey live in request headers, never on disk or stdout.
- **No TLS bypass, no `eval`/`exec`/`subprocess`/`shell`/`pickle`** anywhere in the codebase.
- HTML-parsing regexes run only on responses from the trusted emaplan.com host.

---

## Error Handling Conventions

All tools return a plain `dict`. On failure, the dict contains an `"error"` key with a human-readable message. Never raise from a scraper function — always return `{"error": "..."}`. The top-level `call_tool` catches any exception as a last resort. Write helpers (`_csrf_post`) include the response body snippet in the error for diagnosis.

```python
card = await _get_card(http, 9)
if not card:
    return {"error": "Card 9 unavailable. Session may have expired — call reset_session."}
```

---

## Testing

**Framework**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). **19 test files, 369 tests, no live network calls.** All tests use `make_mock_http_session()` from `tests/helpers.py`, or patch `_get_card` / `_fetch_snb_raw` / `_csrf_post` directly.

```python
from helpers import make_mock_http_session, load_fixture

# Card-based tool
session = make_mock_http_session(card_responses={9: "card9_networth", 1: "card1_accounts"})

# Endpoint-based tool (substring URL match)
session = make_mock_http_session(endpoint_responses={"GetInvestmentData": "investment_data"})

# SNB-based tool (mock the data function directly)
with patch("emoney_mcp.scrapers.spending._fetch_snb_raw", return_value=(True, raw_txns, categories)):
    result = await get_spending_transactions(session, days=30)

# Write tool (patch the CSRF poster)
with patch("emoney_mcp.scrapers.transactions._csrf_post", new=AsyncMock(return_value={...})):
    result = await update_transaction(session, transaction_id="123", category_id="22")
```

**Fixtures must match the real payload shape.** A fixture that encodes the wrong shape will hide bugs — e.g. the Card 8 net-worth-velocity bug (v1.0.5) survived because its test fixture was a bare newest-first list instead of the real `{NetWorth, History[]}` dict. When mocking a card or endpoint, mirror the live response.

Run tests: `pytest tests/ -v --tb=short`

---

## Key Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `EMONEY_SUBDOMAIN` | `wealth` | Builds `https://<subdomain>.emaplan.com` |
| `EMONEY_SESSION_FILE` | `~/.emoney_mcp/session.json` | Cookie persistence path (written 0o600) |
| `EMONEY_DEV` | unset | `1` = hot-reload `scraper` module on every tool call (off by default for performance) |

---

## Important Constraints

- **Tax constants are hardcoded for 2025** (`_TAX_YEAR` in `tax.py`). Update `_BRACKETS`, `_CONTRIBUTION_LIMITS`, `_STD_DEDUCTION`, `_LTCG_THRESHOLDS`, `_NIIT_THRESHOLD` each January.
- **Card 8 `History` is oldest-first**; current value is the `NetWorth` field. Don't assume newest-first.
- **CS/Spending writes go through the Nexus backend**, which can be in maintenance (`IsNexusAvailable:false`) — surface the error, don't treat it as a code bug.
- **`nodriver` runs in its own OS thread** with a separate event loop — do not `await` it from the main async context.
- **`curl_cffi` is required** (not `aiohttp`/`httpx`) — Emoney blocks standard Python TLS fingerprints; Chrome impersonation is mandatory.
- **`scraper.py` is the hot-reload target** — keep it a thin re-export shim; put all logic in `scrapers/`.
- **Monte Carlo uses `random.Random(42)`** — deterministic by design.
- **`asyncio.gather` is used** in goals.py / portfolio.py / planning.py for parallel card fetches — check each result for errors before use.

---

## CI / Release

- **Single workflow** (`.github/workflows/ci.yml`) with three jobs:
  - `test` — pytest on Python 3.11 / 3.12 / 3.13 + syntax check.
  - `security` — `pip-audit` (advisory only, `continue-on-error`).
  - `publish` — **gated on `needs: test`** and `if: push to main`. Builds with `uv` and publishes to PyPI **only when the `pyproject.toml` version changed** (compares against `HEAD~1`); otherwise it logs a skip. A failing test suite blocks the release.
- **Always bump `version` in `pyproject.toml`** when you want a release — the publish job no-ops on an unchanged version, and Claude Desktop only picks up new builds from PyPI.
- Actions pinned to Node.js 24 majors: `actions/checkout@v5`, `actions/setup-python@v6`, `astral-sh/setup-uv@v6`.
- **Build system**: `hatchling`. Package root is `src/emoney_mcp/`. `readme = "README_PYPI.md"`.
