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
        Tool(
            name="get_accounts",
            description=(
                "Returns all financial accounts and net worth from Emoney Advisor. "
                "On first use (or after session expiry) it will try to read your "
                "Chrome session automatically. If that fails, a Chrome window opens "
                "— log in, then call this tool again."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_net_worth",
            description="Returns current net worth (assets minus liabilities) from Emoney Advisor.",
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
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
            name="get_transactions",
            description=(
                "Returns investment transactions (buys, sells, dividends, etc.) for a "
                "date range. Optional parameters: days (default 30, max 365) and "
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
    elif name == "get_holdings":
        result = await _get_holdings()
    elif name == "get_transactions":
        days = int(arguments.get("days", 30))
        account_id = arguments.get("account_id")
        result = await _get_transactions(days=days, account_id=account_id)
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
        "net_worth": result.get("net_worth"),
        "account_count": len(result.get("accounts", [])),
    }


async def _get_holdings() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_holdings(sess)


async def _get_transactions(days: int = 30, account_id: str | None = None) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_transactions(sess, days=days, account_id=account_id)


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
