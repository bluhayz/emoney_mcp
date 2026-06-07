"""Emoney MCP server."""

import importlib
import json

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .browser import (
    get_authenticated_session,
    close_session,
    MANUAL_LOGIN_REQUIRED,
    COOKIE_FILE,
    _http_session,
    extract_chrome_emaplan_cookies,
)
from . import scraper

load_dotenv()

app = Server("emoney-mcp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ── Core balance sheet ────────────────────────────────────────────
        Tool(
            name="get_accounts",
            description=(
                "Returns all financial accounts grouped by type (investments, bank, "
                "retirement, debt, property) with balances and net worth summary. "
                "On first use (or after session expiry) a Chrome login window may open."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_net_worth",
            description="Returns current net worth (total assets minus total liabilities) from Emoney Advisor.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_net_worth_history",
            description=(
                "Returns the monthly net worth trend — how your total wealth has changed over time. "
                "Optional parameter: months (default 12, max 60). "
                "Useful for answering 'How has my net worth grown this year?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Number of months of history to return (default 12, max 60)",
                        "default": 12,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_retirement_accounts",
            description=(
                "Aggregates all tax-advantaged retirement and savings accounts: 401k, IRA, Roth IRA, "
                "annuities, HSA, 529 education accounts. Returns totals by category and individual "
                "account balances. Useful for 'How much do I have saved for retirement?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Investments ───────────────────────────────────────────────────
        Tool(
            name="get_holdings",
            description=(
                "Returns all investment positions (holdings) across every brokerage, "
                "retirement, and investment account — ticker, description, units, price, "
                "current value, cost basis, and unrealized gain/loss."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_asset_allocation",
            description=(
                "Returns the portfolio asset allocation breakdown by asset class "
                "(Equities, Fixed Income, Cash, etc.) with percentages and values. "
                "Also shows top 10 holdings by size for concentration risk. "
                "Useful for 'Am I properly diversified?' or 'How much am I in equities vs bonds?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_performance",
            description=(
                "Returns portfolio performance — value change across standard time periods "
                "(MTD, YTD, 1-year, etc.) in dollars and percent. "
                "Useful for 'How is my portfolio performing this year?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_transactions",
            description=(
                "Returns investment transactions (buys, sells, dividends, etc.) for a "
                "date range. Optional: days (default 30, max 365) and "
                "account_id (Emoney account GUID to filter to one account)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days back to fetch (default 30, max 365)",
                        "default": 30,
                    },
                    "account_id": {
                        "type": "string",
                        "description": "Optional Emoney AccountID GUID to filter to one account",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_capital_gains",
            description=(
                "Summarizes realized capital gains from sell transactions for a given year. "
                "Returns total proceeds, sell transaction detail, dividends, and interest received. "
                "Optional parameter: year (default current year). "
                "Useful for 'What are my realized gains this year for taxes?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "year": {
                        "type": "integer",
                        "description": "Tax year to summarize (default: current year)",
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_goals",
            description=(
                "Returns financial goals and their funding status from Emoney's financial plan. "
                "Includes retirement goal (start year, funded %), education funding, and "
                "other spending goals with projected cost vs. current funding. "
                "Useful for 'Am I on track for retirement?' or 'How funded is Parker's education goal?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Spending / Cash flow ──────────────────────────────────────────
        Tool(
            name="get_spending",
            description=(
                "Returns spending by category for recent months from all linked bank and "
                "credit card accounts. Optional parameter: months (default 1). "
                "Useful for 'How much did I spend on dining last month?' or "
                "'What are my biggest spending categories?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Number of months to include (default 1, max 12)",
                        "default": 1,
                    }
                },
                "required": [],
            },
        ),
        # ── Session management ────────────────────────────────────────────
        Tool(
            name="sync_chrome_session",
            description=(
                "Try to pull the current Emoney session from your running Chrome "
                "browser automatically (no login needed if you are already logged "
                "in to Emoney in Chrome)."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="reset_session",
            description="Clear the saved session and force a fresh login on the next call.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    importlib.reload(scraper)

    if name == "get_accounts":
        result = await _get_accounts()
    elif name == "get_net_worth":
        result = await _get_net_worth()
    elif name == "get_net_worth_history":
        months = int(arguments.get("months", 12))
        result = await _get_net_worth_history(months=months)
    elif name == "get_retirement_accounts":
        result = await _get_retirement_accounts()
    elif name == "get_holdings":
        result = await _get_holdings()
    elif name == "get_asset_allocation":
        result = await _get_asset_allocation()
    elif name == "get_performance":
        result = await _get_performance()
    elif name == "get_transactions":
        days = int(arguments.get("days", 30))
        account_id = arguments.get("account_id")
        result = await _get_transactions(days=days, account_id=account_id)
    elif name == "get_goals":
        result = await _get_goals()
    elif name == "get_capital_gains":
        year = arguments.get("year")
        if year is not None:
            year = int(year)
        result = await _get_capital_gains(year=year)
    elif name == "get_spending":
        months = int(arguments.get("months", 1))
        result = await _get_spending(months=months)
    elif name == "sync_chrome_session":
        result = await _sync_chrome_session()
    elif name == "reset_session":
        result = await _reset()
    else:
        raise ValueError(f"Unknown tool: {name}")

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _get_session_or_err():
    sess = await get_authenticated_session()
    if sess == MANUAL_LOGIN_REQUIRED:
        return None, {
            "login_required": True,
            "message": (
                "Could not find an active Emoney session in Chrome. "
                "Try sync_chrome_session first (make sure you are logged in to "
                "Emoney in Chrome). Otherwise a Chrome window has been opened — "
                "log in manually, then call get_accounts again."
            ),
        }
    return sess, None


# ── Core balance sheet ─────────────────────────────────────────────────────

async def _get_accounts() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_accounts(sess)


async def _get_net_worth() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    result = await scraper.get_accounts(sess)
    if "error" in result:
        return result
    return {
        "net_worth":         result.get("net_worth"),
        "total_assets":      result.get("total_assets"),
        "total_liabilities": result.get("total_liabilities"),
    }


async def _get_net_worth_history(months: int = 12) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_net_worth_history(sess, months=months)


async def _get_retirement_accounts() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_retirement_accounts(sess)


# ── Investments ────────────────────────────────────────────────────────────

async def _get_holdings() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_holdings(sess)


async def _get_asset_allocation() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_asset_allocation(sess)


async def _get_performance() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_performance(sess)


async def _get_transactions(days: int = 30, account_id: str | None = None) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_transactions(sess, days=days, account_id=account_id)


async def _get_capital_gains(year: int | None = None) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_capital_gains(sess, year=year)


# ── Spending ───────────────────────────────────────────────────────────────

async def _get_goals() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_goals(sess)


async def _get_spending(months: int = 1) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_spending(sess, months=months)


# ── Session management ─────────────────────────────────────────────────────

async def _sync_chrome_session() -> dict:
    """Explicitly try to pull cookies from the user's running Chrome."""
    cookies = extract_chrome_emaplan_cookies()
    if not cookies:
        return {
            "success": False,
            "message": (
                "No emaplan.com cookies found in Chrome. "
                "Make sure you are logged in to Emoney in Chrome, then try again."
            ),
        }
    _http_session.save_cookies(cookies)
    # Verify they actually work
    if await _http_session.is_logged_in():
        return {
            "success": True,
            "cookie_count": len(cookies),
            "message": "Session synced from Chrome. You can now call get_accounts.",
        }
    else:
        return {
            "success": False,
            "cookie_count": len(cookies),
            "message": (
                "Found cookies in Chrome but they don't authenticate. "
                "Please log in to Emoney in Chrome first, then try again."
            ),
        }


async def _reset() -> dict:
    import emoney_mcp.browser as bmod
    from .browser import EmoneyHttpSession, EmoneyLoginSession
    try:
        if COOKIE_FILE.exists():
            COOKIE_FILE.unlink()
        bmod._http_session = EmoneyHttpSession()
        bmod._login_session = EmoneyLoginSession()
        return {"success": True, "message": "Session cleared. Call get_accounts to log in again."}
    except Exception as e:
        return {"error": str(e)}


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(read_stream, write_stream, app.create_initialization_options())
        finally:
            await close_session()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
