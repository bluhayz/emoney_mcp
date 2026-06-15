#!/usr/bin/env python3
"""
Live smoke test for emoney-mcp read-only tools.

Runs every read-only scraper against a REAL authenticated Emoney session and
flags any tool that errors, raises, or leaks a raw passthrough — then runs a
handful of cross-checks that catch shape/ordering bugs unit tests can't (these
are exactly the failure modes that have bitten this reverse-engineered API).

This is intentionally NOT a pytest test: it needs live auth and hits the
network. Run it manually after a change that touches data parsing:

    uv run python scripts/smoke.py

Exit code: 0 = all good, 1 = one or more failures, 2 = no live session.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.browser import get_authenticated_session, MANUAL_LOGIN_REQUIRED  # noqa: E402
from emoney_mcp import scraper  # noqa: E402

GREEN, RED, YEL, DIM, RST = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

# Read-only tools called with safe defaults. Tools requiring caller-specific
# arguments (e.g. get_rmd_estimate(birth_year)) are intentionally omitted.
READONLY = [
    # balance sheet
    ("get_accounts",               lambda s: scraper.get_accounts(s)),
    ("get_net_worth_breakdown",    lambda s: scraper.get_net_worth_breakdown(s)),
    ("get_retirement_accounts",    lambda s: scraper.get_retirement_accounts(s)),
    ("get_debt_payoff_plan",       lambda s: scraper.get_debt_payoff_plan(s)),
    ("get_debt_overview",          lambda s: scraper.get_debt_overview(s)),
    ("get_client_profile",         lambda s: scraper.get_client_profile(s)),
    ("get_aggregation_status",     lambda s: scraper.get_aggregation_status(s)),
    # investments
    ("get_holdings",               lambda s: scraper.get_holdings(s)),
    ("get_asset_allocation",       lambda s: scraper.get_asset_allocation(s)),
    ("get_net_worth_history",      lambda s: scraper.get_net_worth_history(s)),
    ("get_performance",            lambda s: scraper.get_performance(s)),
    ("get_transactions",           lambda s: scraper.get_transactions(s)),
    ("get_capital_gains",          lambda s: scraper.get_capital_gains(s)),
    # spending / cash flow
    ("get_spending",               lambda s: scraper.get_spending(s)),
    ("get_spending_transactions",  lambda s: scraper.get_spending_transactions(s)),
    ("get_spending_trends",        lambda s: scraper.get_spending_trends(s)),
    ("get_income_summary",         lambda s: scraper.get_income_summary(s)),
    ("get_savings_rate",           lambda s: scraper.get_savings_rate(s)),
    ("get_recurring_charges",      lambda s: scraper.get_recurring_charges(s)),
    ("get_budget_vs_actual",       lambda s: scraper.get_budget_vs_actual(s)),
    ("get_year_over_year",         lambda s: scraper.get_year_over_year(s)),
    ("get_cash_flow_projection",   lambda s: scraper.get_cash_flow_projection(s)),
    ("get_cash_flow_forecast",     lambda s: scraper.get_cash_flow_forecast(s)),
    ("get_unusual_transactions",   lambda s: scraper.get_unusual_transactions(s)),
    ("get_merchant_spending",      lambda s: scraper.get_merchant_spending(s)),
    ("get_50_30_20_analysis",      lambda s: scraper.get_50_30_20_analysis(s)),
    ("get_spending_by_account",    lambda s: scraper.get_spending_by_account(s)),
    ("get_upcoming_bills",         lambda s: scraper.get_upcoming_bills(s)),
    ("get_categories",             lambda s: scraper.get_categories(s)),
    # goals / dashboards
    ("get_goals",                  lambda s: scraper.get_goals(s)),
    ("get_financial_summary",      lambda s: scraper.get_financial_summary(s)),
    ("get_financial_health_score", lambda s: scraper.get_financial_health_score(s)),
    ("get_quick_status",           lambda s: scraper.get_quick_status(s)),
    ("get_college_savings_gap",    lambda s: scraper.get_college_savings_gap(s)),
    ("get_monthly_review",         lambda s: scraper.get_monthly_review(s)),
    # portfolio
    ("get_asset_location_efficiency", lambda s: scraper.get_asset_location_efficiency(s)),
    ("get_rebalancing_targets",    lambda s: scraper.get_rebalancing_targets(s)),
    ("get_available_cards",        lambda s: scraper.get_available_cards(s)),
    ("get_portfolio_concentration", lambda s: scraper.get_portfolio_concentration(s)),
    ("get_net_worth_velocity",     lambda s: scraper.get_net_worth_velocity(s)),
    ("get_tax_drag_analysis",      lambda s: scraper.get_tax_drag_analysis(s)),
    # planning
    ("get_home_equity",            lambda s: scraper.get_home_equity(s)),
    ("get_insurance_gap_analysis", lambda s: scraper.get_insurance_gap_analysis(s)),
    ("get_fire_number",            lambda s: scraper.get_fire_number(s)),
    ("get_financial_independence_roadmap", lambda s: scraper.get_financial_independence_roadmap(s)),
    # reports
    ("get_reports",                lambda s: scraper.get_reports(s)),
]


async def _run_tool(sess, name, fn):
    """Returns (status, detail). status in {ok, ERROR, RAW_LEAK, EXC}."""
    try:
        r = await fn(sess)
    except Exception as e:  # noqa: BLE001 — smoke test wants to keep going
        return "EXC", repr(e)
    if isinstance(r, dict) and "error" in r:
        return "ERROR", str(r["error"])[:120]
    if isinstance(r, dict) and "raw" in r:
        return "RAW_LEAK", "tool returned a 'raw' passthrough"
    return "ok", r


async def main() -> int:
    sess = await get_authenticated_session()
    if isinstance(sess, str) or sess is MANUAL_LOGIN_REQUIRED:
        print(f"{RED}No live Emoney session.{RST} Log in (sync_chrome_session) and retry.")
        return 2

    print(f"{DIM}Running {len(READONLY)} read-only tools against the live session…{RST}\n")
    outputs, failures = {}, []
    for name, fn in READONLY:
        status, detail = await _run_tool(sess, name, fn)
        outputs[name] = detail
        if status == "ok":
            print(f"  {GREEN}✓{RST} {name}")
        else:
            failures.append((name, status, detail))
            print(f"  {RED}✗ {name}{RST}  {YEL}{status}{RST}: {detail}")

    # ---- cross-checks (catch shape/ordering bugs) --------------------------
    print(f"\n{DIM}Cross-checks…{RST}")
    checks = []

    acct = outputs.get("get_accounts")
    vel  = outputs.get("get_net_worth_velocity")
    hist = outputs.get("get_net_worth_history")

    def _is_dict(x):
        return isinstance(x, dict) and "error" not in x

    # 1. velocity current net worth must match the balance sheet (the v1.0.5 bug)
    if _is_dict(acct) and _is_dict(vel):
        a, v = acct.get("net_worth"), vel.get("current_net_worth")
        ok = a is not None and a == v
        checks.append(("velocity current_net_worth == get_accounts net_worth", ok,
                       f"{v} vs {a}"))

    # 2. history and velocity must agree on the newest data point (ordering)
    if _is_dict(hist) and _is_dict(vel):
        h = (hist.get("history") or [])
        m = (vel.get("monthly_history") or [])
        ok = bool(h) and bool(m) and h[-1].get("net_worth") == m[-1].get("net_worth")
        checks.append(("net_worth_history[-1] == velocity.monthly_history[-1]", ok,
                       f"{(m[-1].get('net_worth') if m else None)} vs {(h[-1].get('net_worth') if h else None)}"))

    # 3. velocity series must be chronological (oldest-first / non-reversed)
    if _is_dict(vel):
        months = [r.get("month") for r in (vel.get("monthly_history") or [])]
        ok = months == sorted(months)
        checks.append(("velocity.monthly_history is chronological", ok, str(months[:3]) + "…"))

    for label, ok, detail in checks:
        mark = f"{GREEN}✓{RST}" if ok else f"{RED}✗{RST}"
        print(f"  {mark} {label}  {DIM}({detail}){RST}")
        if not ok:
            failures.append((label, "CHECK_FAILED", detail))

    # ---- summary -----------------------------------------------------------
    total = len(READONLY) + len(checks)
    print(f"\n{'-'*60}")
    if failures:
        print(f"{RED}FAIL{RST}: {len(failures)} of {total} checks failed.")
        return 1
    print(f"{GREEN}PASS{RST}: all {total} read-only tools and cross-checks succeeded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
