# emoney-mcp — AI Developer Context

MCP server bridging Claude (and other MCP clients) to Emoney Advisor (`emaplan.com`). Emoney has no public API; this server uses reverse-engineered internal JSON endpoints + Chrome cookie extraction for auth.

**Current version: 1.0.34 · 113 MCP tools.** Read-only data tools (cards + SNB + Profile + Vault + plan goals/cash flow + investment depth), transaction/rules **write** tools, an aggregation-refresh **ops** tool (#103), report links, and a large set of pure-Python planning/tax calculators (IRS 2026 figures).

---

## Module Map

```
src/emoney_mcp/
├── server.py          # MCP entry point: list_tools(), call_tool(), _call_tool_inner(), private wrappers (93 tools)
├── browser.py         # EmoneyHttpSession (curl_cffi + cookie persistence, 0o600), Chrome cookie extraction, nodriver fallback
├── scraper.py         # Backward-compat shim — re-exports scrapers/*; target of importlib.reload() in EMONEY_DEV mode
└── scrapers/
    ├── __init__.py    # Re-exports all public functions + clear_caches() + clear_cache(module)
    ├── _helpers.py    # URL constants, _get_card() (TTL cache + int coercion), _fmt_dollars()
    ├── accounts.py    # get_accounts, get_retirement_accounts, get_net_worth_breakdown, get_debt_payoff_plan,
    │                  #   get_debt_overview, get_client_profile, get_aggregation_status
    ├── investments.py # get_holdings, get_asset_allocation, get_net_worth_history, get_performance,
    │                  #   get_transactions, get_capital_gains, get_dividend_income_analysis (#92),
    │                  #   get_sector_geographic_allocation (#93)
    ├── spending.py    # SNB API: all transaction/budget/cashflow tools — get_spending(_transactions/_trends/_by_account),
    │                  #   get_income_summary, get_savings_rate, search_transactions, get_recurring_charges,
    │                  #   get_budget_vs_actual, get_year_over_year, get_cash_flow_projection/forecast,
    │                  #   get_unusual_transactions, get_merchant_spending, get_50_30_20_analysis,
    │                  #   get_upcoming_bills, get_categories, explore_snb_write_endpoints (largest module)
    ├── goals.py       # get_goals, get_financial_summary, get_financial_health_score, get_quick_status,
    │                  #   get_college_savings_gap, get_monthly_review, get_emergency_fund_analysis,
    │                  #   get_idle_cash_optimization, get_financial_alerts (orchestrator)
    ├── tax.py         # Tax math tools + IRS 2026 constants — get_tax_loss_harvesting, get_contribution_room,
    │                  #   get_roth_conversion_analysis, get_capital_gains_exposure, get_rmd_estimate,
    │                  #   get_tax_bracket_headroom, get_social_security_optimizer, get_quarterly_estimated_taxes,
    │                  #   get_year_end_checklist, get_annual_tax_advantaged_summary,
    │                  #   get_multi_year_tax_projection, get_roth_conversion_ladder, get_irmaa_analysis,
    │                  #   get_charitable_giving_strategy, get_tax_gain_harvesting, get_state_tax_estimate
    ├── retirement.py  # get_retirement_runway, get_withdrawal_rate_analysis, get_net_worth_projection,
    │                  #   run_monte_carlo_retirement, get_dynamic_withdrawal_guardrails, run_scenario,
    │                  #   get_financial_independence_roadmap, get_withdrawal_sequencing_strategy,
    │                  #   get_retirement_income_plan, get_income_sources_timeline (#85),
    │                  #   get_sequence_of_returns_stress_test (#98), model_life_event_scenario (#97)
    ├── portfolio.py   # get_asset_location_efficiency, get_rebalancing_targets, explore_emoney_cards,
    │                  #   get_available_cards, get_portfolio_concentration, get_net_worth_velocity,
    │                  #   get_tax_drag_analysis, get_portfolio_risk_metrics, get_benchmark_comparison (#94)
    ├── planning.py    # get_insurance_gap_analysis, get_home_equity, get_fire_number,
    │                  #   get_gifting_and_estate_strategy, get_mortgage_amortization_schedule,
    │                  #   get_mortgage_refinance_analysis, get_mortgage_payoff_vs_invest,
    │                  #   get_healthcare_cost_projection, get_hsa_optimization (#102),
    │                  #   get_estate_liquidity_analysis (#81),
    │                  #   get_long_term_care_analysis (#78),
    │                  #   get_real_estate_investment_analysis (#100)
    ├── transactions.py# WRITE ops via CS/Spending — update_transaction, hide_transaction,
    │                  #   get/update_transaction_splits, get/add/update/apply_transaction_rule (v0.9.0+)
    ├── reports.py     # get_reports (parse Reports page), get_report_url (CS/Reports/GetReportUrl) (v0.9.0+)
    ├── vault.py       # get_vault_documents (#104) — scrapes vaultApi.BaseUrl from /ema/CS/Vault,
    │                  #   then GETs /ema/api/v1/vault/<guid>/items?path=Vault (same-origin JSON, cookie auth)
    ├── plan_api.py    # get_all_goals_funding_status (#96) — internal-api BFF (api.emoneyadvisor.com),
    │                  #   Bearer JWT + apikey (reuses _get_snb_credentials); clientId/planId from MyPlan HTML
    ├── aggregation_api.py # refresh_account_aggregation (#103) — aggapi service (api.emoneyadvisor.com),
    │                  #   Bearer from /ema/CS/Aggregation/GetToken + DISTINCT aggApiKey scraped from
    │                  #   Organizer/Accounts; POST /users/<guid>/connections/<id>/refresh → 202 {activityId}
    └── explore.py     # explore_emoney_site — dev/discovery crawler that mines pages for endpoints
```

---

## Tool Dispatch Chain

Dispatch is **registry-driven** (a single `_DISPATCH` dict in `server.py`), not an
if/elif tree. Every MCP tool call flows through:

```
call_tool(name, args)                  # top-level try/except → JSON error on any exception
  └─ _call_tool_inner(name, args)      # looks up _DISPATCH[name]; raises ValueError if missing
       └─ handler(args)                # registry handler
            ├─ pure tools: _passthru("scraper_fn", _A(...), ...)
            │     ├─ _get_session_or_err()        # (session, None) or (None, error_dict)
            │     ├─ _kwargs(specs, args)         # pull + convert each argument
            │     └─ getattr(scraper, fn)(session, **kwargs)   # name lookup → hot-reload safe
            └─ special tools: a lambda calling a dedicated wrapper
                  (get_net_worth, get_features, get_version,
                   sync_chrome_session, reset_session, clear_cache)
       └─ returns dict → JSON string via TextContent
```

**Argument specs** — `_A(name, conv=str, default=_REQ, *, optional=False)`:
- `default` given → `conv(args.get(name, default))`
- `optional=True` → `conv(args[name])` if present & not None, else `None`
- neither → `conv(args[name])` (required; a missing arg raises `ValueError("Missing required argument: '<name>'")`)
Special converters: `_ints` (list→[int]), `_identity` (pass through), `_bool` (safe bool coercion — `bool("false")` would otherwise be `True`).

**Adding a new tool now touches 4 locations** (one new file split adds the
`scrapers/__init__.py` import + `__all__`):

| # | File | What to add |
|---|------|-------------|
| 1 | `scrapers/<module>.py` | Implement `async def get_foo(http_session, ...) -> dict` |
| 2 | `scrapers/__init__.py` | Add to imports and `__all__` |
| 3 | `scraper.py` | Add to `from .scrapers import (...)` |
| 4 | `server.py` → `list_tools()` **and** `_DISPATCH` | Add the `Tool(...)` schema and a registry entry, e.g. `"get_foo": _passthru("get_foo", _A("days", int, 30))` |

A test (`tests/test_server_dispatch.py`) asserts every advertised tool is in
`_DISPATCH` (and routes), so a forgotten registry entry fails CI rather than
404-ing at runtime. Bespoke tools register a `lambda a: _my_wrapper(...)` instead
of `_passthru`.

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

> **Card 8 ordering gotcha:** `History` is **oldest-first**; the newest element is the current month. Current net worth comes from the `NetWorth` field. Mislabeling this order silently reverses trends (fixed in v1.0.5 — `get_net_worth_velocity`). Both `get_net_worth_history` and `get_net_worth_velocity` now slice/label Card 8 through the shared `_parse_card8_history()` helper in `_helpers.py` (drift-free `_month_offset` labels), so the two can't diverge.

### SNB API (spending / transactions, read)
`https://api.emoneyadvisor.com/snb-api` — separate host, requires `Authorization: Bearer <jwt>` + `apikey` header scraped from the Spending page HTML via `_get_snb_credentials()`. All SNB reads go through `spending._fetch_snb_data()` / `_fetch_snb_raw()` (300 s cache).

| Endpoint | Data |
|----------|------|
| `api/values/GetFilteredTransactions` | All bank/CC transactions with `categoryId` |
| `api/values/GetCategories` | Spending category names by ID |
| `api/values/GetAccounts` | SNB account id→name map (for get_spending_by_account) |

### Transaction + rules WRITE path — SNB API (modern) vs. legacy CS/Spending
The live web UI writes through the **SNB API** (`api.emoneyadvisor.com/snb-api/api/values/<action>`, Bearer JWT + `apikey` via `_get_snb_credentials`, JSON body) — `transactions._snb_post()` / `_snb_get()`. The legacy `/ema/CS/Spending/<action>` path (`_csrf_post`, ASP.NET anti-forgery + jQuery bracket notation) is served by the retired "Nexus" backend, which returns `IsNexusAvailable:false` / HTTP 500 for writes — **dead, not in maintenance** (retrying never succeeds).

| Action | Tool | Status |
|--------|------|--------|
| `UpdateTransaction` (SNB) | update_transaction | ✅ SNB, live-verified |
| `GetBankTransactionRules` (SNB GET) | get_transaction_rules | ✅ SNB, live-verified |
| `CreateRule` / `UpdateRule` (SNB, `{Rule, TransactionID}`) | add/update_transaction_rule | ✅ SNB, live-verified (#121) |
| `SetRules` (CS/Spending bulk-replace) | delete_transaction_rule | ✅ live-verified (#121) |
| `ToggleTransactionVisibility` (SNB) | hide_transaction | ✅ SNB, live-verified (#121) |
| `GetBankTransactionSplits` (SNB GET) | get_transaction_splits | ✅ SNB, live-verified (#121) |
| `updateTransactionSplits` (SNB) | update_transaction_splits | ✅ live-verified (#121) — POST a bare ARRAY of split objects |
| `ApplyRule` | apply_transaction_rule | ⏳ dead legacy path; no standalone SNB ApplyRule (folds into Create/UpdateRule's TransactionID) — effectively deprecated |

> **Rule/ID shapes (#121, verified live 2026-06-18):** SNB serializes `ruleID`/`categoryID` (and split `categoryID`/`transactionID`) as WCF complex types `{"value":"123"}`, NOT bare strings — Create/Update/SetRules *require* the wrapped `{Value}` form (flat → HTTP 400/500). Use `transactions._unwrap_id`/`_wrap_id`. **CreateRule must OMIT `RuleID`** on create (sending `{Value:null}` → 500 — the bug that shipped in 1.0.31). **Rule delete has no single endpoint**: the UI bulk-replaces the whole collection via `POST /ema/CS/Spending/SetRules {rules:[...]}` (the one *live* CS/Spending route — the rest of that path is dead Nexus), CSRF token in the `__RequestVerificationToken` header (`_csrf_post_json`). hide = SNB `ToggleTransactionVisibility {hideTransaction, transactionId}`.
>
> **Splits write (#121):** `update_transaction_splits(transaction_id, splits)` POSTs a **bare JSON array** to SNB `updateTransactionSplits`. The first split is the parent (`transactionID:{value}`, `parentTransactionID:null`); each additional split is a child (`transactionID:null`, `parentTransactionID:{value}`, `identity:N`); `splitAmount` is a string; transaction metadata (descriptions/dates) is carried over from `GetBankTransactionSplits`. Pass a single split to un-split. Contract captured live (split + revert observed).

### CS/Profile and CS/Reports
- `GET /ema/CS/Profile/GetProfileData` — household identity (names, DOB, ages, dependents, properties) → `get_client_profile`.
- `GET /ema/CS/Reports` (parse embedded JSON) + `POST /ema/CS/Reports/GetReportUrl` → `get_reports` / `get_report_url` (returns a viewable report URL; never auto-followed).

### Investments endpoint
`GET/POST {BASE_URL}/ema/CS/Investments/...` — `GetInvestmentData` (holdings/allocation) and `GetInvestmentTransactions` (POST, requires CSRF token via `http_session.get_csrf_token()`).

### internal-api BFF (My Plan — goals, projections, cash flow)
`https://api.emoneyadvisor.com/internal-api/api/clients/<clientId>/plans/<planId>/...` — the My Plan SPA's data API, **Apigee-gated with the SAME auth as the SNB API**: `Authorization: Bearer <jwt>` + `apikey` header, both from `_get_snb_credentials()` (scraped from the Spending page). `clientId`/`planId` are embedded in the My Plan page HTML (`clientId":"..."` / `planId":"..."`) — see `plan_api._get_plan_ids`. Discovered via live network capture (epic #106). Known sub-paths (mine more by capturing XHRs on `/ema/CS/MyPlan`):

| Path (under `/plans/<plan>`) | Data | Tool / roadmap |
|------|------|------|
| `/projection/montecarlo/goals` | per-goal probability of success + surplus/shortfall | `get_all_goals_funding_status` (#96) |
| `/projection/goalfunding/retirement` | retirement funding $ vs expense $ | #96 |
| `/projection/linear/cashflow/details` | year-by-year lifetime cash flow | `get_lifetime_cash_flow_projection` (#82) |
| `/projection/montecarlo/probabilityofsuccess`, `/projection/retirement`, `/projection/montecarlo/assetspread` | plan success / retirement projection | future |
| `/expenses`, `/expenses/education`, `/expenses/spending`, `/expenses/funding`, `/assumptions` | goal definitions + plan inputs | future |
| (client-level) `/calculatednetworth`, `/investments/total`, `/plans/<plan>/assetallocation/details/...` | investment depth | #91/#92/#93/#95 |

> The Apigee gateway returns `401 {"fault":...FailedToResolveAPIKey}` if the `apikey` header is missing — both Bearer JWT and `apikey` are required.

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

**Framework**: `pytest` + `pytest-asyncio` (`asyncio_mode = "auto"`). **27 test files, 473 tests, no live network calls.** All tests use `make_mock_http_session()` from `tests/helpers.py`, or patch `_get_card` / `_fetch_snb_raw` / `_csrf_post` directly.

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

- **Tax constants are hardcoded for 2026** (`_TAX_YEAR` in `tax.py`). Update `_BRACKETS`, `_CONTRIBUTION_LIMITS`, `_STD_DEDUCTION`, `_LTCG_THRESHOLDS`, `_NIIT_THRESHOLD` each January.
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
- Actions pinned to Node.js 24: `actions/checkout@v5`, `actions/setup-python@v6`, `astral-sh/setup-uv@v8.2.0` (setup-uv stopped publishing a moving `v8` major tag at v8 — "immutable releases", so pin the full version).
- **Build system**: `hatchling`. Package root is `src/emoney_mcp/`. `readme = "README_PYPI.md"`.
