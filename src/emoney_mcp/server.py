"""Emoney MCP server."""

import importlib
import inspect
import json
import time

from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .browser import (
    get_authenticated_session,
    close_session,
    get_last_login_error,
    MANUAL_LOGIN_REQUIRED,
    COOKIE_FILE,
    _http_session,
    extract_chrome_emaplan_cookies,
)
from . import scraper

load_dotenv()

app = Server("emoney-mcp")

# Session health check — tracks last time we verified the session is live.
# Checked every 5 minutes to proactively warn before tools fail.
_last_health_ts: float = 0.0
_HEALTH_CHECK_INTERVAL: float = 300.0  # seconds


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
        Tool(
            name="get_financial_summary",
            description=(
                "Returns a compact executive dashboard — net worth, portfolio performance, "
                "this month's income vs. spending, top 5 spending categories, and goal status. "
                "Best first tool to call for broad questions like 'How are my finances?' or "
                "'Give me a financial overview.' No parameters required."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_quick_status",
            description=(
                "Returns a 5-number financial snapshot: net worth, portfolio today's change, "
                "this month's savings rate, top spending category, and goal on-track status. "
                "Designed for quick-check queries with minimal token usage. "
                "Useful for 'How am I doing today?' or 'Quick financial check.'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="search_transactions",
            description=(
                "Search spending transactions by keyword, category, or amount. "
                "Parameters: query (text to match in description), category (e.g. 'Groceries'), "
                "days (default 365), min_amount, max_amount. "
                "Useful for 'How much did I spend at Costco this year?' or 'Show me all Amazon charges.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query":      {"type": "string",  "description": "Text to search in transaction description"},
                    "category":   {"type": "string",  "description": "Category name to filter by (partial match)"},
                    "days":       {"type": "integer", "description": "Days back to search (default 365)", "default": 365},
                    "min_amount": {"type": "number",  "description": "Minimum transaction amount"},
                    "max_amount":   {"type": "number",  "description": "Maximum transaction amount"},
                    "max_results":  {"type": "integer", "description": "Cap on returned transactions (default 100; pass 0 for all)", "default": 100},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_recurring_charges",
            description=(
                "Detects recurring and subscription charges by analyzing transaction patterns "
                "over the last 120 days. Identifies weekly, biweekly, monthly, and quarterly "
                "charges and estimates total monthly recurring spend. "
                "Useful for 'What subscriptions am I paying for?' or 'What are my recurring bills?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_net_worth_breakdown",
            description=(
                "Breaks down net worth by three lenses: (1) by person — Drew, Lacey, Joint/Family; "
                "(2) by liquidity — Liquid, Semi-liquid, Illiquid; "
                "(3) by tax treatment — Taxable, Tax-Deferred, Tax-Free. "
                "Useful for 'How much of our wealth is Lacey's?' or 'How much is in tax-advantaged accounts?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_spending_trends",
            description=(
                "Returns month-over-month spending trends by category — which categories "
                "are going up, down, or stable, plus monthly income vs. spending summary. "
                "Optional parameter: months (default 3, max 12). "
                "Useful for 'Is my dining spending going up?' or 'Compare my last 3 months of spending.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Number of months to compare (default 3, max 12)",
                        "default": 3,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_income_summary",
            description=(
                "Returns income sources and monthly income trend for the last N days. "
                "Identifies paychecks, direct deposits, dividends, and interest income "
                "grouped by source. Optional parameter: days (default 90, max 365). "
                "Useful for 'How much did I earn last month?' or 'What are my income sources?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days back to include (default 90, max 365)",
                        "default": 90,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_savings_rate",
            description=(
                "Returns month-by-month savings rate — income minus spending divided by income. "
                "Shows how much of your income you are actually saving each month. "
                "Optional parameter: months (default 6, max 12). "
                "Useful for 'What is my savings rate?' or 'Am I saving more or less than last month?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {
                        "type": "integer",
                        "description": "Number of months to include (default 6, max 12)",
                        "default": 6,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_spending_transactions",
            description=(
                "Returns bank and credit card transactions with category labels (Groceries, Dining, "
                "Travel, Shopping, etc.) for the last N days. Unlike get_transactions (which covers "
                "investment activity), this covers everyday spending from linked bank/CC accounts. "
                "Optional: days (default 30, max 365), max_transactions (default 100; pass 0 for all). "
                "Useful for 'What did I spend on groceries last month?' or 'Show me my dining expenses.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "integer",
                        "description": "Number of days back to fetch (default 30, max 365)",
                        "default": 30,
                    },
                    "max_transactions": {
                        "type": "integer",
                        "description": "Cap on returned transactions (default 100; pass 0 for all)",
                        "default": 100,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_budget_vs_actual",
            description=(
                "Compares this month's actual spending to the rolling average of the prior N months "
                "by category. Flags categories that are tracking >15% above their average ('over_budget'). "
                "Also compares to any total monthly budget configured in Emoney. "
                "Optional: months_avg (default 3). "
                "Useful for 'Am I over budget this month?' or 'Which categories are overspending?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months_avg": {
                        "type": "integer",
                        "description": "Prior months to use as the rolling average benchmark (default 3)",
                        "default": 3,
                    }
                },
                "required": [],
            },
        ),
        Tool(
            name="get_year_over_year",
            description=(
                "Compares this year's spending and income to the same period last year. "
                "Shows total year-to-date change in dollars and percent plus a per-category breakdown "
                "of biggest increases and decreases. Requires 2 years of transaction history. "
                "No parameters required. "
                "Useful for 'Am I spending more this year than last year?' or 'How has my dining changed?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_cash_flow_projection",
            description=(
                "Projects monthly cash flow for the next N months based on actual income and spending "
                "averages from the last 90 days. Shows projected monthly surplus/deficit and a running "
                "balance estimate. Optional: months_ahead (default 6, max 24). "
                "Useful for 'Will I have enough to cover a big expense in 3 months?' or "
                "'What does my cash flow look like through year-end?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months_ahead": {
                        "type": "integer",
                        "description": "Months to project forward (default 6, max 24)",
                        "default": 6,
                    }
                },
                "required": [],
            },
        ),
        # ── Tax planning ──────────────────────────────────────────────────
        Tool(
            name="get_tax_loss_harvesting",
            description=(
                "Identifies investment positions with unrealized losses in taxable accounts "
                "that are candidates for tax-loss harvesting. Cross-references account type so "
                "only losses in taxable brokerage accounts (not IRAs or 401ks) are flagged as "
                "harvestable. Returns positions sorted by loss size plus estimated tax savings "
                "at 15%, 20%, and 23.8% LTCG+NIIT rates. "
                "Useful for 'Where can I harvest losses?' or 'What's my tax-loss harvesting opportunity?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_contribution_room",
            description=(
                "Shows 2026 IRS annual contribution limits for all tax-advantaged accounts "
                "(401k, IRA, HSA, SIMPLE IRA, SEP IRA, 529) alongside your current balances. "
                "Adjusts for catch-up contributions based on age. "
                "Parameters: age (integer, optional), filing_status ('single', 'mfj', 'hoh', default 'mfj'). "
                "Useful for 'How much can I still contribute to my IRA this year?' or "
                "'Am I maxing out my HSA?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "age":          {"type": "integer", "description": "Your age (determines catch-up eligibility)"},
                    "filing_status": {"type": "string",  "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_roth_conversion_analysis",
            description=(
                "Estimates the federal tax cost and long-term benefit of converting a specified "
                "dollar amount from pre-tax (traditional IRA/401k) to Roth this year. "
                "Shows bracket-by-bracket tax impact, effective rate on the conversion, "
                "projected tax-free growth, and whether the conversion is tax-favored given "
                "your current vs. expected future marginal rate. "
                "Required parameters: conversion_amount (dollars), current_income (annual gross income). "
                "Optional: filing_status ('mfj', 'single', 'hoh'), age. "
                "Useful for 'Should I do a Roth conversion?' or 'What does it cost to convert $100k to Roth?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "conversion_amount": {"type": "number",  "description": "Dollar amount to convert to Roth"},
                    "current_income":    {"type": "number",  "description": "Annual gross income before conversion (wages, RMDs, SS, etc.)"},
                    "filing_status":     {"type": "string",  "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                    "age":               {"type": "integer", "description": "Your age (optional, used for context)"},
                },
                "required": ["conversion_amount", "current_income"],
            },
        ),
        Tool(
            name="get_capital_gains_exposure",
            description=(
                "Identifies all investment positions with large unrealized gains in taxable accounts "
                "and estimates the federal tax liability if those positions were sold today. "
                "Distinguishes taxable vs. tax-deferred/free accounts, applies LTCG rates and NIIT "
                "based on income level. "
                "Parameters: filing_status ('mfj', 'single', 'hoh'), annual_income (optional — inferred from transactions if omitted). "
                "Useful for 'What's my capital gains tax exposure?' or 'Which positions would trigger the biggest tax bill if sold?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filing_status": {"type": "string", "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                    "annual_income": {"type": "number", "description": "Annual income override (inferred from transactions if omitted)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_rmd_estimate",
            description=(
                "Estimates Required Minimum Distributions (RMDs) from pre-tax retirement accounts "
                "(traditional IRA, 401k) using the IRS Uniform Lifetime Table. "
                "RMDs start at age 73 under SECURE 2.0. Returns current-year RMD (if applicable) "
                "and a 10-year projected RMD schedule with estimated account balances. "
                "Required parameter: birth_year (e.g. 1955). "
                "Useful for 'When do I have to start taking RMDs?' or 'How much will my RMD be at 75?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "birth_year": {"type": "integer", "description": "Your year of birth (e.g. 1955)"},
                },
                "required": ["birth_year"],
            },
        ),
        Tool(
            name="get_tax_bracket_headroom",
            description=(
                "Shows how much additional income can be earned before crossing into the next "
                "federal tax bracket. Infers current income from 12-month transaction history "
                "if not supplied. Also shows LTCG bracket headroom. "
                "Optional: current_income (dollars), filing_status ('mfj', 'single', 'hoh'). "
                "Useful for 'How much can I convert to Roth without hitting the next bracket?' or "
                "'How much freelance income can I take this year at my current rate?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "current_income":  {"type": "number", "description": "Estimated annual gross income (inferred if omitted)"},
                    "filing_status":   {"type": "string", "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                },
                "required": [],
            },
        ),
        # ── Retirement planning ───────────────────────────────────────────
        Tool(
            name="get_retirement_runway",
            description=(
                "Models how many years the current portfolio can sustain withdrawals under "
                "conservative (4%), base (6%), and optimistic (8%) return scenarios. "
                "Also shows sustainable withdrawal amounts at 3.5%, 4%, and 4.5% SWR. "
                "If annual_spending is not provided, uses actual 12-month spending from linked accounts. "
                "Parameters: annual_spending (optional dollars), return_rate (optional, default 0.06). "
                "Useful for 'Can I afford to retire now?' or 'How long will my money last?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "annual_spending": {"type": "number", "description": "Annual spending override in dollars (default: actual 12-month spend)"},
                    "return_rate":     {"type": "number", "description": "Base-case annual return assumption (default 0.06)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_withdrawal_rate_analysis",
            description=(
                "Analyzes safe withdrawal rates in the context of your Emoney retirement goal. "
                "Projects portfolio value to retirement start year, then shows annual and monthly "
                "income at 3%, 3.5%, 4%, 4.5%, and 5% withdrawal rates with estimated years funded. "
                "No parameters required — uses retirement goal start/end year from Emoney. "
                "Useful for 'How much can I spend in retirement?' or 'What does a 4% withdrawal rate give me?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_net_worth_projection",
            description=(
                "Projects net worth forward using the current balance plus actual average monthly savings, "
                "compounding at an assumed annual return. Shows milestone years ($500k, $1M, $2M, $5M, $10M) "
                "and a 30-year snapshot table. Optional: target_net_worth (dollars to find the specific year for), "
                "annual_return (default 0.07), annual_savings_override. "
                "Useful for 'When will I hit $2M?' or 'How is my net worth projected to grow?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_net_worth":       {"type": "number", "description": "Target to find the year for (e.g. 2000000)"},
                    "annual_return":          {"type": "number", "description": "Annual return assumption (default 0.07)", "default": 0.07},
                    "annual_savings_override":{"type": "number", "description": "Override the inferred annual savings amount"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_debt_payoff_plan",
            description=(
                "Models debt payoff using the avalanche (highest-rate first) and snowball (smallest-balance first) "
                "strategies. Identifies all non-mortgage debt accounts from Emoney, assumes typical APRs by account "
                "type, and shows months to payoff and total interest for each strategy. "
                "Optional: extra_monthly_payment, assumed_credit_card_apr (default 0.22), assumed_loan_apr (default 0.07). "
                "Useful for 'When will I be debt-free?' or 'Which debt payoff strategy saves the most interest?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "extra_monthly_payment":   {"type": "number", "description": "Extra monthly payment above minimums (default $0)"},
                    "assumed_credit_card_apr": {"type": "number", "description": "Assumed APR for credit cards (default 0.22)"},
                    "assumed_loan_apr":        {"type": "number", "description": "Assumed APR for non-mortgage loans (default 0.07)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_college_savings_gap",
            description=(
                "Estimates the gap between current 529 savings and projected college costs for education goals "
                "in the Emoney financial plan. Shows required monthly contributions to close any gap by the "
                "goal start year. Optional: annual_return (default 0.06), annual_college_inflation (default 0.05). "
                "Useful for 'Are we on track for Parker's college?' or 'How much do we need to save monthly for 529?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "annual_return":           {"type": "number", "description": "Expected 529 portfolio return (default 0.06)"},
                    "annual_college_inflation":{"type": "number", "description": "College cost inflation rate (default 0.05)"},
                },
                "required": [],
            },
        ),
        # ── Portfolio analysis ─────────────────────────────────────────────
        Tool(
            name="get_asset_location_efficiency",
            description=(
                "Grades how well assets are positioned across account types for tax efficiency. "
                "Tax-inefficient assets (bonds, REITs, TIPS) should be in tax-deferred/free accounts; "
                "tax-efficient assets (index funds, growth stocks) can be in taxable accounts. "
                "Returns an A-F letter grade, per-position ratings, and specific swap suggestions. "
                "Useful for 'Are my assets in the right accounts?' or 'How tax-efficient is my portfolio?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_rebalancing_targets",
            description=(
                "Computes the exact dollar amounts to buy and sell in each asset class to reach "
                "a target allocation. Classifies current holdings into equity, bond, and cash buckets "
                "and shows the drift from target. "
                "Parameters: target_equity_pct (default 60), target_bond_pct (default 30), "
                "target_cash_pct (default 10). "
                "Useful for 'How do I rebalance to 60/40?' or 'How far off target is my portfolio?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target_equity_pct": {"type": "number", "description": "Target equity percentage (default 60)"},
                    "target_bond_pct":   {"type": "number", "description": "Target bond percentage (default 30)"},
                    "target_cash_pct":   {"type": "number", "description": "Target cash percentage (default 10)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_financial_health_score",
            description=(
                "Returns a 0-100 composite financial health score with an A-F letter grade. "
                "Combines six dimensions: savings rate (25%), goal funding (25%), debt-to-asset ratio (20%), "
                "emergency fund coverage (15%), portfolio diversification (10%), and net worth trend (5%). "
                "Each component is scored and explained. "
                "Useful for 'How healthy are my finances overall?' or 'What should I focus on improving?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Transaction writes ────────────────────────────────────────────
        Tool(
            name="update_transaction",
            description=(
                "Update a spending transaction's category and/or user-visible description. "
                "transaction_id is the SNB transaction ID (visible in get_spending_transactions). "
                "category_id is the numeric category ID string (e.g. '65' for Food & Dining). "
                "At least one of category_id or description must be provided. "
                "Useful for 'Recategorize this charge' or 'Rename this transaction'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string", "description": "SNB transaction ID"},
                    "category_id":    {"type": "string", "description": "Numeric category ID to assign (optional)"},
                    "description":    {"type": "string", "description": "User-visible description override (optional)"},
                },
                "required": ["transaction_id"],
            },
        ),
        Tool(
            name="hide_transaction",
            description=(
                "Hide or un-hide a transaction from spending views and reports. "
                "Hidden transactions are excluded from budgets and category totals. "
                "Useful for 'Hide this transfer' or 'Exclude this refund from spending'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string", "description": "SNB transaction ID"},
                    "hidden":         {"type": "boolean", "description": "True to hide, False to un-hide (default True)"},
                },
                "required": ["transaction_id"],
            },
        ),
        Tool(
            name="get_transaction_splits",
            description=(
                "Return the current splits for a transaction — how it is divided "
                "across multiple categories (e.g. a Costco run split into Groceries and Gas). "
                "Returns splits array with category and amount per split."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_id": {"type": "string", "description": "SNB transaction ID"},
                },
                "required": ["transaction_id"],
            },
        ),
        Tool(
            name="update_transaction_splits",
            description=(
                "Replace all splits on a transaction — divide one transaction across "
                "multiple categories with specific amounts. Amounts must sum to the total. "
                "Each split needs CategoryID (numeric string) and SplitAmount (negative for expenses). "
                "Useful for 'Split this Costco charge: $80 groceries and $40 gas'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "transaction_splits": {
                        "type": "array",
                        "description": "List of split objects: [{CategoryID:{Value:'65'}, SplitAmount:-80.00, UserDescription:'Groceries'}, ...]",
                        "items": {"type": "object"},
                    },
                },
                "required": ["transaction_splits"],
            },
        ),
        # ── Rules engine ──────────────────────────────────────────────────
        Tool(
            name="get_categories",
            description=(
                "Return all SNB spending category names and their numeric IDs. "
                "Use this to look up the category_id needed by update_transaction, "
                "add_transaction_rule, and update_transaction_rule. "
                "Useful for 'What category ID is Groceries?' or 'List all spending categories.'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_transaction_rules",
            description=(
                "Return all auto-categorization rules. Rules automatically assign a category "
                "to transactions whose description contains a specific string. "
                "Each rule has: rule_id, description_contains, category_id, user_description. "
                "Useful for 'What categorization rules do I have?' or before adding a new rule."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="add_transaction_rule",
            description=(
                "Create a new auto-categorization rule. Any transaction whose description "
                "contains description_contains will be assigned the given category. "
                "Optionally apply to a specific transaction_id that triggered this rule. "
                "Useful for 'Automatically categorize all Starbucks charges as Coffee'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description_contains": {"type": "string", "description": "Substring to match in transaction description (case-insensitive)"},
                    "category_id":          {"type": "string", "description": "Numeric category ID to assign"},
                    "user_description":     {"type": "string", "description": "Display label for the rule (defaults to description_contains)"},
                    "transaction_id":       {"type": "string", "description": "Optional: SNB transaction ID that triggered this rule"},
                    "min_amount":           {"type": "number", "description": "Optional: only match transactions above this amount"},
                    "max_amount":           {"type": "number", "description": "Optional: only match transactions below this amount"},
                },
                "required": ["description_contains", "category_id"],
            },
        ),
        Tool(
            name="update_transaction_rule",
            description=(
                "Edit an existing auto-categorization rule. Fetches the current rule and "
                "applies only the fields you provide. "
                "rule_id comes from get_transaction_rules. "
                "Useful for 'Change the Starbucks rule to use a different category'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id":              {"type": "string", "description": "Rule ID from get_transaction_rules"},
                    "description_contains": {"type": "string", "description": "New description substring to match (optional)"},
                    "category_id":          {"type": "string", "description": "New category ID to assign (optional)"},
                    "user_description":     {"type": "string", "description": "New display label (optional)"},
                    "min_amount":           {"type": "number", "description": "New minimum amount filter (optional)"},
                    "max_amount":           {"type": "number", "description": "New maximum amount filter (optional)"},
                    "transaction_id":       {"type": "string", "description": "Optional context transaction ID"},
                },
                "required": ["rule_id"],
            },
        ),
        Tool(
            name="apply_transaction_rule",
            description=(
                "Apply an existing rule to all past transactions that match its description pattern. "
                "This bulk-recategorizes historical transactions. "
                "rule_id comes from get_transaction_rules. "
                "Useful for 'Apply the Starbucks rule to all past transactions'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "rule_id":        {"type": "string", "description": "Rule ID from get_transaction_rules"},
                    "transaction_id": {"type": "string", "description": "Optional: limit to a specific transaction"},
                },
                "required": ["rule_id"],
            },
        ),
        # ── Reports ───────────────────────────────────────────────────────
        Tool(
            name="get_reports",
            description=(
                "List all available Emoney reports grouped by family (Investments, Net Worth, "
                "Tax, Estate, etc.). Each report has a report_id, name, and description. "
                "Use get_report_url(report_id) to generate a viewable link for any report. "
                "Useful for 'What reports are available?' or finding a specific report."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_report_url",
            description=(
                "Generate a signed URL to view or download a specific Emoney report as PDF. "
                "report_id is the identifier string from get_reports() (e.g. 'LiquidityReport'). "
                "The URL can be opened in a browser to view the report. "
                "Useful for 'Get me a link to the Asset Allocation report'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "report_id": {"type": "string", "description": "Report identifier from get_reports (e.g. 'LiquidityReport')"},
                },
                "required": ["report_id"],
            },
        ),
        Tool(
            name="explore_emoney_site",
            description=(
                "Crawls all major Emoney sections (Home, Accounts, Investments, Spending, Cash Flow, "
                "Goals, Insurance, Tax, Reports, Documents, Tasks, Messages, Estate, Education, "
                "Scenario, Monte Carlo, Social Security, Profile) using the authenticated session "
                "and mines each page's HTML/JS for API endpoints, form actions, AJAX URLs, and nav links. "
                "No data is modified — all requests are GET only. "
                "Use this to discover new data sources and plan new tools."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="explore_snb_write_endpoints",
            description=(
                "Probes the SNB API (api.emoneyadvisor.com/snb-api) for write endpoints that "
                "might support transaction category updates, description edits, splits, or deletes. "
                "Sends OPTIONS and GET requests to ~20 candidate endpoint patterns and reports "
                "which ones exist (401/403/405 = present) vs. are absent (404). "
                "No data is modified — all probes are read-only. "
                "Use this to investigate whether transaction recategorization is feasible before "
                "building a write tool. No parameters required."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="explore_emoney_cards",
            description=(
                "Probes unexplored Emoney CardSwitcher endpoints (cards 5, 6, 7, 10, 12, 14–16) "
                "to discover what additional data is available (insurance, tax projection, estate, etc.). "
                "Returns the full payload of any available cards. "
                "Optional parameter: card_ids (list of integers to probe). "
                "Useful for 'What other Emoney data can we access?' or debugging new endpoints."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "card_ids": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Card IDs to probe (default: [5,6,7,10,12,14,15,16])",
                    },
                },
                "required": [],
            },
        ),
        # ── Advanced retirement simulations ──────────────────────────────
        Tool(
            name="run_monte_carlo_retirement",
            description=(
                "Runs Monte Carlo retirement simulations (default 1,000 paths) using stochastic "
                "annual returns and inflation to estimate the probability that a portfolio survives "
                "a given retirement horizon. Returns probability of success, median/10th/90th percentile "
                "ending balances, worst-case depletion year, and a year-by-year percentile table. "
                "Also finds the safe withdrawal rate at 90% success. "
                "Optional: simulations (default 1000), years (default 30), annual_spending (inferred if omitted), "
                "mean_return (default 0.07), std_dev (default 0.15), inflation_mean (default 0.03), "
                "inflation_std (default 0.01), social_security_annual (default 0), withdrawal_rate. "
                "Useful for 'What are my odds of not running out of money?' or 'How safe is a 4% withdrawal rate?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "simulations":            {"type": "integer", "description": "Number of simulation paths (default 1000, max 10000)", "default": 1000},
                    "years":                  {"type": "integer", "description": "Retirement horizon in years (default 30)", "default": 30},
                    "annual_spending":        {"type": "number",  "description": "Annual spending/withdrawal in dollars (default: actual 12-month spend)"},
                    "mean_return":            {"type": "number",  "description": "Mean annual portfolio return (default 0.07 = 7%)", "default": 0.07},
                    "std_dev":                {"type": "number",  "description": "Annual return standard deviation (default 0.15; use 0.18-0.20 for all-equity)", "default": 0.15},
                    "inflation_mean":         {"type": "number",  "description": "Mean annual inflation rate (default 0.03)", "default": 0.03},
                    "inflation_std":          {"type": "number",  "description": "Inflation standard deviation (default 0.01)", "default": 0.01},
                    "social_security_annual": {"type": "number",  "description": "Annual Social Security or pension income to offset withdrawals (default 0)", "default": 0},
                    "withdrawal_rate":        {"type": "number",  "description": "Override spending with a portfolio percentage (e.g. 0.04 = 4%)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_dynamic_withdrawal_guardrails",
            description=(
                "Applies Guyton-Klinger guardrail rules to determine whether to raise, hold, or cut "
                "the current withdrawal amount based on how the portfolio is performing relative to "
                "its starting value. If portfolio outperforms, a 10% raise is triggered; if it "
                "underperforms past the lower guardrail, a 10% cut is recommended. Returns the "
                "adjusted annual and monthly withdrawal and whether a change is needed. "
                "Optional: initial_withdrawal_rate (default 0.05), raise_ceiling_pct (default 20), "
                "cut_floor_pct (default 20), initial_portfolio_value, current_annual_withdrawal. "
                "Useful for 'Should I adjust my retirement withdrawals this year?' or "
                "'Am I hitting a guardrail on my spending?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "initial_withdrawal_rate":   {"type": "number",  "description": "Withdrawal rate at retirement start (default 0.05 = 5%)", "default": 0.05},
                    "raise_ceiling_pct":         {"type": "number",  "description": "Max % above initial a raise can go (default 20)", "default": 20.0},
                    "cut_floor_pct":             {"type": "number",  "description": "Max % below initial a cut can go (default 20)", "default": 20.0},
                    "raise_guard_pct":           {"type": "number",  "description": "Rate must drop this % below initial to trigger a raise (default 20)", "default": 20.0},
                    "cut_guard_pct":             {"type": "number",  "description": "Rate must rise this % above initial to trigger a cut (default 20)", "default": 20.0},
                    "initial_portfolio_value":   {"type": "number",  "description": "Portfolio value at retirement start (optional; uses current if omitted)"},
                    "current_annual_withdrawal": {"type": "number",  "description": "Override inferred annual withdrawal (optional)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_social_security_optimizer",
            description=(
                "Computes the optimal Social Security claiming age by comparing lifetime benefits "
                "at age 62, Full Retirement Age (FRA), and 70. Shows monthly benefit at each age, "
                "breakeven ages (when claiming later surpasses claiming earlier), and which strategy "
                "maximizes lifetime benefits at a given life expectancy. Includes spousal analysis "
                "if spouse parameters are provided. "
                "Required: birth_year. Optional: estimated_monthly_benefit_at_67 (from ssa.gov — "
                "uses $2,000 placeholder if omitted), filing_status, spouse_birth_year, "
                "spouse_benefit_at_67, life_expectancy (default 85). "
                "Useful for 'Should I claim Social Security at 62 or wait until 70?' or "
                "'What is the breakeven age for delaying Social Security?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "birth_year":                      {"type": "integer", "description": "Your year of birth (e.g. 1962)"},
                    "estimated_monthly_benefit_at_67": {"type": "number",  "description": "Your estimated monthly SS benefit at age 67 (from ssa.gov — uses placeholder if omitted)"},
                    "filing_status":                   {"type": "string",  "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                    "spouse_birth_year":               {"type": "integer", "description": "Spouse year of birth (optional — enables spousal analysis)"},
                    "spouse_benefit_at_67":            {"type": "number",  "description": "Spouse monthly benefit at FRA (optional)"},
                    "life_expectancy":                 {"type": "integer", "description": "Assumed age at death for lifetime value calculation (default 85)", "default": 85},
                },
                "required": ["birth_year"],
            },
        ),
        Tool(
            name="get_quarterly_estimated_taxes",
            description=(
                "Calculates quarterly federal estimated tax payment amounts and due dates for the "
                "current year. Uses two methods — current-year annualized (based on estimated income) "
                "and IRS safe harbor (100%/110% of prior-year tax) — and recommends the lower one to "
                "avoid underpayment penalties. Infers income from 12-month transaction history if not supplied. "
                "Optional: filing_status, annual_income_override, prior_year_tax, expected_withholding. "
                "Useful for 'How much do I owe in estimated taxes each quarter?' or "
                "'What are my Q2 estimated tax payments?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filing_status":          {"type": "string", "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                    "annual_income_override": {"type": "number", "description": "Override inferred annual income (dollars)"},
                    "prior_year_tax":         {"type": "number", "description": "Total federal tax paid last year (for safe harbor calculation)"},
                    "expected_withholding":   {"type": "number", "description": "Expected W-2 withholding this year (reduces estimated payments needed)"},
                },
                "required": [],
            },
        ),
        # ── Help ──────────────────────────────────────────────────────────
        Tool(
            name="get_features",
            description=(
                "Lists all available emoney-mcp tools grouped by category, with a short "
                "description and example questions for each. Call this to discover what "
                "you can ask — no parameters required."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        # ── Debug ─────────────────────────────────────────────────────────
        Tool(
            name="get_version",
            description=(
                "Returns the installed version of emoney-mcp, the cookie file path, "
                "and whether a saved session file exists. Useful for debugging."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
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
        # ── v0.8.0 new tools ──────────────────────────────────────────────
        Tool(
            name="get_monthly_review",
            description=(
                "Compiles a structured monthly financial report in a single call — net worth change, "
                "investment performance, this month's income vs. spending, top categories, "
                "savings rate, goal status, and a short list of action items. "
                "No parameters required. "
                "Useful for 'Give me my monthly financial review' or end-of-month check-ins."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_unusual_transactions",
            description=(
                "Flags transactions that are unusually large compared to the merchant's or "
                "category's historical average. Helps catch one-off large charges, billing errors, "
                "or potential fraud. "
                "Optional: days (look-back window, default 90), threshold_pct (% above avg to flag, default 150). "
                "Useful for 'Are there any unusual charges this month?' or 'Flag any big unexpected purchases.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days":          {"type": "integer", "description": "Look-back window in days (default 90)", "default": 90},
                    "threshold_pct": {"type": "number",  "description": "Flag transactions exceeding this % of merchant average (default 150)", "default": 150},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_merchant_spending",
            description=(
                "Returns total spending grouped by normalized merchant name for the last N days. "
                "Shows total, transaction count, average transaction, and date range per merchant. "
                "Optional: days (default 365), merchant (substring filter), limit (top N merchants, default 25). "
                "Useful for 'How much did I spend at Amazon this year?' or 'What are my top 10 merchants?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days":     {"type": "integer", "description": "Look-back window in days (default 365)", "default": 365},
                    "merchant": {"type": "string",  "description": "Optional merchant name substring filter (case-insensitive)"},
                    "limit":    {"type": "integer", "description": "Number of merchants to return (default 25)", "default": 25},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_year_end_checklist",
            description=(
                "Generates a year-end tax planning checklist with status (action_needed / opportunity / done) "
                "and dollar amounts. Covers contribution room, tax-loss harvesting, capital gains exposure, "
                "bracket headroom, and RMDs. "
                "Optional: age, birth_year (for RMD check), filing_status (default 'mfj'), current_income. "
                "Useful for 'What tax actions should I take before year-end?' or 'Year-end tax checklist.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "age":            {"type": "integer", "description": "Your age (determines catch-up contribution eligibility)"},
                    "birth_year":     {"type": "integer", "description": "Your birth year (enables RMD check)"},
                    "filing_status":  {"type": "string",  "description": "'single', 'mfj', or 'hoh' (default 'mfj')"},
                    "current_income": {"type": "number",  "description": "Annual gross income (inferred from transactions if omitted)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="run_scenario",
            description=(
                "Runs a what-if projection alongside a baseline and shows the difference. "
                "Useful for 'If I save $500/month more, when do I retire?' or "
                "'What if I assume 8% returns instead of 7%?' "
                "Optional: monthly_savings_delta (e.g. 500 or -200), target_net_worth, "
                "retirement_age, annual_return_pct (e.g. 8 for 8%). "
                "All parameters have sensible defaults — calling with no args shows baseline vs. no-change."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "monthly_savings_delta": {"type": "number",  "description": "Change in monthly savings vs. current (e.g. 500 to save $500/month more)", "default": 0},
                    "target_net_worth":      {"type": "number",  "description": "Target balance to reach (defaults to retirement goal value)"},
                    "retirement_age":        {"type": "integer", "description": "Override retirement age for comparison year"},
                    "annual_return_pct":     {"type": "number",  "description": "Override assumed annual return (e.g. 8 for 8%; default 7)"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_cash_flow_forecast",
            description=(
                "Projects future monthly cash flow broken into recurring fixed costs and discretionary "
                "spending, using actual detected recurring charges and recent income/spending history. "
                "More structured than get_cash_flow_projection — separates fixed from variable costs. "
                "Optional: months (1–6, default 3). "
                "Useful for 'What will my monthly cash flow look like next quarter?' or "
                "'How much recurring vs. discretionary spending do I have?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {"type": "integer", "description": "Months to forecast (1–6, default 3)", "default": 3},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_insurance_gap_analysis",
            description=(
                "Estimates life insurance need, disability coverage need, and emergency fund adequacy "
                "using standard financial planning rules applied to your actual income and assets from Emoney. "
                "Shows the gap between what you likely need and your liquid assets. "
                "Optional: income_multiple (default 10× income for life), disability_pct (default 65% of income). "
                "Useful for 'Am I adequately insured?' or 'How much life insurance do I need?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "income_multiple": {"type": "number", "description": "Life insurance = this × annual income (default 10)", "default": 10},
                    "disability_pct":  {"type": "number", "description": "Recommended disability benefit as fraction of income (default 0.65)", "default": 0.65},
                },
                "required": [],
            },
        ),
        # ── v1.0.0 Family Financial Planning Tools ─────────────────────────
        Tool(
            name="get_home_equity",
            description=(
                "Returns home equity and loan-to-value (LTV) ratio for all real-estate holdings. "
                "Identifies property accounts and mortgage/HELOC liabilities from Emoney's account list. "
                "Useful for 'What is our home equity?' or 'What is our mortgage LTV?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_fire_number",
            description=(
                "Computes the Financial Independence (FI) number — the portfolio size needed to retire on investment returns alone. "
                "Uses 12-month actual spending from linked accounts as the baseline. "
                "Returns the FI number, gap from current investable assets, percent of the way there, "
                "years-to-FI at current savings rate, and monthly savings needed to FI in 15/20/25 years. "
                "Optional: swr (safe withdrawal rate, default 0.04), annual_return (default 0.07). "
                "Useful for 'How far are we from financial independence?' or 'What is our FIRE number?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "swr":           {"type": "number", "description": "Safe withdrawal rate (default 0.04 = 4%)", "default": 0.04},
                    "annual_return": {"type": "number", "description": "Expected annual portfolio return (default 0.07 = 7%)", "default": 0.07},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_gifting_and_estate_strategy",
            description=(
                "Estate snapshot and gifting strategy recommendations using 2026 IRS constants. "
                "Shows federal estate tax exposure, annual gift exclusion capacity, 529 superfunding opportunity, "
                "and a prioritized list of estate-reduction strategies. "
                "Optional: num_recipients (people to gift to, default 2), filing_status ('mfj' or 'single'). "
                "Useful for 'How much can we gift tax-free?' or 'Are we exposed to estate taxes?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "num_recipients": {"type": "integer", "description": "Number of people to gift to per year (default 2)", "default": 2},
                    "filing_status":  {"type": "string",  "description": "'mfj' or 'single' (default 'mfj')", "default": "mfj"},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_debt_overview",
            description=(
                "Consolidated view of all debts with estimated interest costs and payoff dates. "
                "Classifies every negative-balance account by type (mortgage, credit card, auto, student, other), "
                "estimates monthly and annual interest, and projects payoff date at minimum payments. "
                "Useful for 'How much are we paying in interest each year?' or 'When will each debt be paid off?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "assumed_mortgage_apr": {"type": "number", "description": "APR for mortgage/HELOC (default 0.065)", "default": 0.065},
                    "assumed_cc_apr":       {"type": "number", "description": "APR for credit cards (default 0.22)", "default": 0.22},
                    "assumed_auto_apr":     {"type": "number", "description": "APR for auto loans (default 0.07)", "default": 0.07},
                    "assumed_student_apr":  {"type": "number", "description": "APR for student loans (default 0.055)", "default": 0.055},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_50_30_20_analysis",
            description=(
                "Classifies spending into Needs (50%), Wants (30%), and Savings (20%) buckets and compares "
                "actual percentages against the 50/30/20 guideline. Returns monthly averages per bucket, "
                "status (on_track / slightly_over / over_target), and actionable recommendations. "
                "Optional: months (number of complete months to average, default 3). "
                "Useful for 'Are we following the 50/30/20 rule?' or 'Where should we cut spending?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {"type": "integer", "description": "Months to average (default 3, max 12)", "default": 3},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_spending_by_account",
            description=(
                "Breaks down spending by which linked bank or credit card account generated each transaction. "
                "Useful for families with multiple cards to see which account is being used for which categories. "
                "Optional: days (look-back window, default 30). "
                "Useful for 'Which card are we using the most?' or 'How much does each person spend?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days": {"type": "integer", "description": "Look-back window in days (default 30, max 365)", "default": 30},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_upcoming_bills",
            description=(
                "Projects recurring bill charges expected in the next N days based on 120-day charge history. "
                "Flags any charge that is overdue (expected but not yet seen in the transaction feed). "
                "Optional: days_ahead (forecast horizon in days, default 30). "
                "Useful for 'What bills are due this month?' or 'Are any subscriptions overdue?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "integer", "description": "Forecast horizon in days (default 30)", "default": 30},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_portfolio_concentration",
            description=(
                "Identifies overly concentrated investment positions and scores overall portfolio diversification A-F. "
                "Flags any single position above the concentration threshold. Returns top-10 holdings by size, "
                "single-stock vs. fund breakdown, and recommendations. "
                "Optional: concentration_threshold_pct (default 10%). "
                "Useful for 'Am I too concentrated in any stock?' or 'How diversified is our portfolio?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "concentration_threshold_pct": {"type": "number", "description": "Flag positions above this % of portfolio (default 10)", "default": 10.0},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_net_worth_velocity",
            description=(
                "Computes the rate of net worth growth from Card 8 historical data. "
                "Returns monthly change, rolling average, year-over-year comparison, trend (accelerating/stable/decelerating), "
                "and 12-month forward projection at current velocity. "
                "Optional: months (history to analyse, default 12, max 60). "
                "Useful for 'Is our net worth growing faster than last year?' or 'How fast are we building wealth?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "months": {"type": "integer", "description": "Months of history to analyse (default 12, max 60)", "default": 12},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_tax_drag_analysis",
            description=(
                "Quantifies the annual dollar cost of holding tax-inefficient assets (bonds, REITs) in taxable accounts. "
                "Estimates annual tax drag per position and shows the highest-priority swaps to tax-deferred accounts. "
                "Optional: marginal_rate (default 0.32), ltcg_rate (default 0.15). "
                "Useful for 'How much are misplaced assets costing us in taxes?' or 'What should we move to our IRA?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "marginal_rate": {"type": "number", "description": "Federal marginal ordinary income tax rate (default 0.32)", "default": 0.32},
                    "ltcg_rate":     {"type": "number", "description": "Long-term capital gains rate (default 0.15)", "default": 0.15},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_financial_independence_roadmap",
            description=(
                "Shows progress against Fidelity's salary-multiple retirement milestones (1× by 30, 3× by 40, 6× by 50, 10× by 65) "
                "and computes the Coast FI number — the portfolio value needed today so growth alone reaches FI. "
                "Optional: current_age (enables age-specific milestone lookup), retirement_age (default 65). "
                "Useful for 'Are we on track for retirement by Fidelity benchmarks?' or 'What is our Coast FI number?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "current_age":    {"type": "integer", "description": "Your current age (enables age-based milestone lookup)"},
                    "retirement_age": {"type": "integer", "description": "Target retirement age for Coast FI (default 65)", "default": 65},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_annual_tax_advantaged_summary",
            description=(
                "Shows 2026 IRS annual contribution limits for 401k, IRA, HSA, and 529 alongside current account balances. "
                "Adjusts for catch-up contributions based on age. Shows key deadlines and remaining days in the tax year. "
                "Optional: age (determines catch-up eligibility). "
                "Useful for 'How much can I still contribute to my IRA this year?' or 'Am I maxing out my tax-advantaged accounts?'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "age": {"type": "integer", "description": "Your age (determines catch-up contribution eligibility)"},
                },
                "required": [],
            },
        ),
        # ── v1.0.2 Live endpoint discoveries ──────────────────────────────
        Tool(
            name="get_client_profile",
            description=(
                "Returns household profile: names, dates of birth, ages, and family members "
                "from Emoney's Profile page. Includes Drew, Lacey, and dependents (e.g. Parker). "
                "Use the returned age and birth_year values to auto-populate retirement and tax tools "
                "instead of passing them manually. "
                "Useful for 'How old is Drew?' or 'What are our dates of birth?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_aggregation_status",
            description=(
                "Returns the health and freshness status of all linked account aggregations. "
                "Shows which institution connections are broken or disconnected, preventing data refresh. "
                "Useful for 'Why is my Chase balance stale?' or 'Which accounts need re-authentication?'"
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="clear_cache",
            description=(
                "Selectively clears the in-memory data cache to force fresh data on the next call. "
                "Use 'cards' to clear CardSwitcher responses, 'spending' to clear SNB transaction data, "
                "or 'all' to clear everything. "
                "Optional: module ('cards', 'spending', or 'all'; default 'all'). "
                "Useful when you need up-to-the-minute data without doing a full session reset."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "module": {
                        "type":        "string",
                        "description": "'cards', 'spending', or 'all' (default 'all')",
                        "enum":        ["cards", "spending", "all"],
                        "default":     "all",
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_available_cards",
            description=(
                "Returns a clean inventory of all Emoney CardSwitcher card IDs (1–16) that respond, "
                "showing which keys each card returns and a type fingerprint. "
                "Useful before building new scrapers or exploring what data Emoney exposes. "
                "Optional: card_ids (list of integers; default probes 1–16). "
                "Useful for 'What Emoney data cards are available?' or 'Discover new data sources.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "card_ids": {
                        "type":  "array",
                        "items": {"type": "integer"},
                        "description": "Card IDs to probe (default: 1–16)",
                    },
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        return await _call_tool_inner(name, arguments)
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e), "tool": name}, indent=2))]


# ---------------------------------------------------------------------------
# Tool dispatch registry
# ---------------------------------------------------------------------------
#
# Single source of truth for routing. Each tool maps to a handler taking the raw
# `arguments` dict and returning a dict (or an awaitable of one). Pure tools use
# `_passthru`, which resolves the session and forwards converted kwargs to the
# scraper function (looked up by name at call time so EMONEY_DEV hot-reload keeps
# working). A few tools with bespoke behaviour reference dedicated wrappers.

_REQ = object()  # sentinel: argument is required


def _A(name, conv=str, default=_REQ, *, optional=False):
    """Declare one argument: name + how to pull/convert it from `arguments`.

    optional=True  -> conv(value) if present and not None, else None
    default given  -> conv(arguments.get(name, default))
    neither        -> conv(arguments[name])  (required; KeyError if missing)
    """
    return (name, conv, default, optional)


def _ints(v):
    return [int(c) for c in v]


def _identity(v):
    return v


def _kwargs(specs, args: dict) -> dict:
    out = {}
    for name, conv, default, optional in specs:
        if optional:
            out[name] = conv(args[name]) if (name in args and args[name] is not None) else None
        elif default is _REQ:
            # Required argument: give a clear, actionable message instead of the
            # bare KeyError (which renders as just the quoted key name and is
            # indistinguishable from a real bug in the top-level handler).
            if name not in args:
                raise ValueError(f"Missing required argument: '{name}'")
            out[name] = conv(args[name])
        else:
            out[name] = conv(args.get(name, default))
    return out


def _passthru(fn_name: str, *specs):
    """Build a handler that calls scraper.<fn_name>(sess, **converted_kwargs)."""
    async def handler(arguments: dict):
        sess, err = await _get_session_or_err()
        if err:
            return err
        fn = getattr(scraper, fn_name)
        return await fn(sess, **_kwargs(specs, arguments))
    return handler


_DISPATCH = {
    # ── Overview & dashboards ─────────────────────────────────────────────
    "get_features":                  lambda a: _get_features(),
    "get_financial_summary":         _passthru("get_financial_summary"),
    "get_financial_health_score":    _passthru("get_financial_health_score"),
    "get_quick_status":              _passthru("get_quick_status"),
    "get_monthly_review":            _passthru("get_monthly_review"),
    # ── Balance sheet ─────────────────────────────────────────────────────
    "get_accounts":                  _passthru("get_accounts"),
    "get_net_worth":                 lambda a: _get_net_worth(),
    "get_net_worth_history":         _passthru("get_net_worth_history", _A("months", int, 12)),
    "get_net_worth_breakdown":       _passthru("get_net_worth_breakdown"),
    "get_retirement_accounts":       _passthru("get_retirement_accounts"),
    "get_client_profile":            _passthru("get_client_profile"),
    "get_aggregation_status":        _passthru("get_aggregation_status"),
    "get_home_equity":               _passthru("get_home_equity"),
    # ── Investments ───────────────────────────────────────────────────────
    "get_holdings":                  _passthru("get_holdings"),
    "get_asset_allocation":          _passthru("get_asset_allocation"),
    "get_performance":               _passthru("get_performance"),
    "get_transactions":              _passthru("get_transactions", _A("days", int, 30),
                                               _A("account_id", str, optional=True)),
    "get_capital_gains":             _passthru("get_capital_gains", _A("year", int, optional=True)),
    "get_asset_location_efficiency": _passthru("get_asset_location_efficiency"),
    "get_rebalancing_targets":       _passthru("get_rebalancing_targets",
                                               _A("target_equity_pct", float, 60),
                                               _A("target_bond_pct", float, 30),
                                               _A("target_cash_pct", float, 10)),
    "get_portfolio_concentration":   _passthru("get_portfolio_concentration",
                                               _A("concentration_threshold_pct", float, 10.0)),
    "get_net_worth_velocity":        _passthru("get_net_worth_velocity", _A("months", int, 12)),
    "get_tax_drag_analysis":         _passthru("get_tax_drag_analysis",
                                               _A("marginal_rate", float, 0.32),
                                               _A("ltcg_rate", float, 0.15)),
    # ── Goals ─────────────────────────────────────────────────────────────
    "get_goals":                     _passthru("get_goals"),
    "get_college_savings_gap":       _passthru("get_college_savings_gap",
                                               _A("annual_return", float, 0.06),
                                               _A("annual_college_inflation", float, 0.05)),
    # ── Spending & cash flow ──────────────────────────────────────────────
    "get_spending":                  _passthru("get_spending", _A("months", int, 1)),
    "get_spending_transactions":     _passthru("get_spending_transactions", _A("days", int, 30),
                                               _A("max_transactions", int, 100)),
    "get_spending_trends":           _passthru("get_spending_trends", _A("months", int, 3)),
    "get_budget_vs_actual":          _passthru("get_budget_vs_actual", _A("months_avg", int, 3)),
    "get_year_over_year":            _passthru("get_year_over_year"),
    "get_cash_flow_projection":      _passthru("get_cash_flow_projection", _A("months_ahead", int, 6)),
    "get_cash_flow_forecast":        _passthru("get_cash_flow_forecast", _A("months", int, 3)),
    "get_income_summary":            _passthru("get_income_summary", _A("days", int, 90)),
    "get_savings_rate":              _passthru("get_savings_rate", _A("months", int, 6)),
    "get_recurring_charges":         _passthru("get_recurring_charges"),
    "get_unusual_transactions":      _passthru("get_unusual_transactions", _A("days", int, 90),
                                               _A("threshold_pct", float, 150.0)),
    "get_merchant_spending":         _passthru("get_merchant_spending", _A("days", int, 365),
                                               _A("merchant", str, ""), _A("limit", int, 25)),
    "get_50_30_20_analysis":         _passthru("get_50_30_20_analysis", _A("months", int, 3)),
    "get_spending_by_account":       _passthru("get_spending_by_account", _A("days", int, 30)),
    "get_upcoming_bills":            _passthru("get_upcoming_bills", _A("days_ahead", int, 30)),
    "get_categories":                _passthru("get_categories"),
    "search_transactions":           _passthru("search_transactions", _A("query", str, ""),
                                               _A("category", str, ""), _A("days", int, 365),
                                               _A("min_amount", float, 0),
                                               _A("max_amount", float, optional=True),
                                               _A("max_results", int, 100)),
    # ── Transaction writes & rules ────────────────────────────────────────
    "update_transaction":            _passthru("update_transaction", _A("transaction_id", str),
                                               _A("category_id", str, optional=True),
                                               _A("description", str, optional=True)),
    "hide_transaction":              _passthru("hide_transaction", _A("transaction_id", str),
                                               _A("hidden", bool, True)),
    "get_transaction_splits":        _passthru("get_transaction_splits", _A("transaction_id", str)),
    "update_transaction_splits":     _passthru("update_transaction_splits",
                                               _A("transaction_splits", _identity)),
    "get_transaction_rules":         _passthru("get_transaction_rules"),
    "add_transaction_rule":          _passthru("add_transaction_rule",
                                               _A("description_contains", str),
                                               _A("category_id", str),
                                               _A("user_description", str, optional=True),
                                               _A("transaction_id", str, optional=True),
                                               _A("min_amount", float, optional=True),
                                               _A("max_amount", float, optional=True)),
    "update_transaction_rule":       _passthru("update_transaction_rule", _A("rule_id", str),
                                               _A("description_contains", str, optional=True),
                                               _A("category_id", str, optional=True),
                                               _A("user_description", str, optional=True),
                                               _A("min_amount", float, optional=True),
                                               _A("max_amount", float, optional=True),
                                               _A("transaction_id", str, optional=True)),
    "apply_transaction_rule":        _passthru("apply_transaction_rule", _A("rule_id", str),
                                               _A("transaction_id", str, optional=True)),
    # ── Reports ───────────────────────────────────────────────────────────
    "get_reports":                   _passthru("get_reports"),
    "get_report_url":                _passthru("get_report_url", _A("report_id", str)),
    # ── Tax planning ──────────────────────────────────────────────────────
    "get_tax_loss_harvesting":       _passthru("get_tax_loss_harvesting"),
    "get_contribution_room":         _passthru("get_contribution_room", _A("age", int, optional=True),
                                               _A("filing_status", str, "mfj")),
    "get_roth_conversion_analysis":  _passthru("get_roth_conversion_analysis",
                                               _A("conversion_amount", float),
                                               _A("current_income", float),
                                               _A("filing_status", str, "mfj"),
                                               _A("age", int, optional=True)),
    "get_capital_gains_exposure":    _passthru("get_capital_gains_exposure",
                                               _A("filing_status", str, "mfj"),
                                               _A("annual_income", float, optional=True)),
    "get_rmd_estimate":              _passthru("get_rmd_estimate", _A("birth_year", int)),
    "get_tax_bracket_headroom":      _passthru("get_tax_bracket_headroom",
                                               _A("current_income", float, optional=True),
                                               _A("filing_status", str, "mfj")),
    "get_social_security_optimizer": _passthru("get_social_security_optimizer", _A("birth_year", int),
                                               _A("estimated_monthly_benefit_at_67", float, optional=True),
                                               _A("filing_status", str, "mfj"),
                                               _A("spouse_birth_year", int, optional=True),
                                               _A("spouse_benefit_at_67", float, optional=True),
                                               _A("life_expectancy", int, 85)),
    "get_quarterly_estimated_taxes": _passthru("get_quarterly_estimated_taxes",
                                               _A("filing_status", str, "mfj"),
                                               _A("annual_income_override", float, optional=True),
                                               _A("prior_year_tax", float, optional=True),
                                               _A("expected_withholding", float, optional=True)),
    "get_year_end_checklist":        _passthru("get_year_end_checklist", _A("age", int, optional=True),
                                               _A("birth_year", int, optional=True),
                                               _A("filing_status", str, "mfj"),
                                               _A("current_income", float, optional=True)),
    "get_annual_tax_advantaged_summary": _passthru("get_annual_tax_advantaged_summary",
                                                   _A("age", int, optional=True)),
    # ── Retirement & long-range ───────────────────────────────────────────
    "get_retirement_runway":         _passthru("get_retirement_runway",
                                               _A("annual_spending", float, optional=True),
                                               _A("return_rate", float, 0.06)),
    "get_withdrawal_rate_analysis":  _passthru("get_withdrawal_rate_analysis"),
    "get_net_worth_projection":      _passthru("get_net_worth_projection",
                                               _A("target_net_worth", float, optional=True),
                                               _A("annual_return", float, 0.07),
                                               _A("annual_savings_override", float, optional=True)),
    "get_debt_payoff_plan":          _passthru("get_debt_payoff_plan",
                                               _A("extra_monthly_payment", float, 0.0),
                                               _A("assumed_credit_card_apr", float, 0.22),
                                               _A("assumed_loan_apr", float, 0.07)),
    "get_debt_overview":             _passthru("get_debt_overview",
                                               _A("assumed_mortgage_apr", float, 0.065),
                                               _A("assumed_cc_apr", float, 0.22),
                                               _A("assumed_auto_apr", float, 0.07),
                                               _A("assumed_student_apr", float, 0.055)),
    "run_monte_carlo_retirement":    _passthru("run_monte_carlo_retirement",
                                               _A("simulations", int, 1_000), _A("years", int, 30),
                                               _A("annual_spending", float, optional=True),
                                               _A("mean_return", float, 0.07), _A("std_dev", float, 0.15),
                                               _A("inflation_mean", float, 0.03),
                                               _A("inflation_std", float, 0.01),
                                               _A("social_security_annual", float, 0.0),
                                               _A("withdrawal_rate", float, optional=True)),
    "get_dynamic_withdrawal_guardrails": _passthru("get_dynamic_withdrawal_guardrails",
                                                   _A("initial_withdrawal_rate", float, 0.05),
                                                   _A("raise_ceiling_pct", float, 20.0),
                                                   _A("cut_floor_pct", float, 20.0),
                                                   _A("raise_guard_pct", float, 20.0),
                                                   _A("cut_guard_pct", float, 20.0),
                                                   _A("initial_portfolio_value", float, optional=True),
                                                   _A("current_annual_withdrawal", float, optional=True)),
    "run_scenario":                  _passthru("run_scenario",
                                               _A("monthly_savings_delta", float, 0.0),
                                               _A("target_net_worth", float, optional=True),
                                               _A("retirement_age", int, optional=True),
                                               _A("annual_return_pct", float, optional=True)),
    "get_fire_number":               _passthru("get_fire_number", _A("swr", float, 0.04),
                                               _A("annual_return", float, 0.07)),
    "get_financial_independence_roadmap": _passthru("get_financial_independence_roadmap",
                                                    _A("current_age", int, optional=True),
                                                    _A("retirement_age", int, 65)),
    # ── Planning ──────────────────────────────────────────────────────────
    "get_insurance_gap_analysis":    _passthru("get_insurance_gap_analysis",
                                               _A("income_multiple", float, 10.0),
                                               _A("disability_pct", float, 0.65)),
    "get_gifting_and_estate_strategy": _passthru("get_gifting_and_estate_strategy",
                                                 _A("num_recipients", int, 2),
                                                 _A("filing_status", str, "mfj")),
    # ── Discovery / debug / session ───────────────────────────────────────
    "explore_emoney_site":           _passthru("explore_emoney_site"),
    "explore_snb_write_endpoints":   _passthru("explore_snb_write_endpoints"),
    "explore_emoney_cards":          _passthru("explore_emoney_cards", _A("card_ids", _ints, optional=True)),
    "get_available_cards":           _passthru("get_available_cards", _A("card_ids", _ints, optional=True)),
    "get_version":                   lambda a: _get_version(),
    "sync_chrome_session":           lambda a: _sync_chrome_session(),
    "reset_session":                 lambda a: _reset(),
    "clear_cache":                   lambda a: _clear_cache(module=a.get("module", "all")),
}


async def _call_tool_inner(name: str, arguments: dict) -> list[TextContent]:
    # Hot-reload the scraper module only in development mode (EMONEY_DEV=1).
    # In production this is skipped — the thin shim adds no value once installed
    # and reloading on every call has measurable overhead. _passthru resolves the
    # scraper function by name at call time, so reload is picked up.
    import os
    if os.environ.get("EMONEY_DEV"):
        importlib.reload(scraper)

    try:
        handler = _DISPATCH[name]
    except KeyError:
        raise ValueError(f"Unknown tool: {name}")

    result = handler(arguments)
    if inspect.isawaitable(result):
        result = await result
    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _get_session_or_err():
    global _last_health_ts
    sess = await get_authenticated_session()
    if sess == MANUAL_LOGIN_REQUIRED:
        login_err = get_last_login_error()
        message = (
            "Could not find an active Emoney session in Chrome. "
            "Try sync_chrome_session first (make sure you are logged in to "
            "Emoney in Chrome). Otherwise a Chrome window has been opened — "
            "log in manually, then call get_accounts again."
        )
        if login_err:
            # The background login attempt failed outright (e.g. Chrome missing) —
            # surface the cause instead of an opaque "waiting for login".
            message += f" Note: the last automatic login attempt failed ({login_err})."
        return None, {
            "login_required": True,
            "login_error": login_err,
            "message": message,
        }

    # Proactive health check every 5 minutes — non-blocking warning if session stale.
    now = time.time()
    if now - _last_health_ts >= _HEALTH_CHECK_INTERVAL:
        _last_health_ts = now
        try:
            logged_in = await _http_session.is_logged_in()
            if not logged_in:
                return None, {
                    "session_warning": True,
                    "message": (
                        "Session health check failed — Emoney session appears stale. "
                        "Run sync_chrome_session to refresh, then retry."
                    ),
                }
        except Exception:
            pass   # health check failures are non-fatal

    return sess, None


# ── Core balance sheet ─────────────────────────────────────────────────────



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






# ── Investments ────────────────────────────────────────────────────────────











# ── Spending ───────────────────────────────────────────────────────────────





















# ── Help ──────────────────────────────────────────────────────────────────

def _get_features() -> dict:
    from importlib.metadata import version, PackageNotFoundError
    try:
        ver = version("emoney-mcp")
    except PackageNotFoundError:
        ver = "unknown (dev install)"
    categories = {
            "Overview & Dashboard": {
                "tools": {
                    "get_quick_status": {
                        "description": "5-number snapshot: net worth, portfolio change, savings rate, top spending category, goal status. Minimal tokens.",
                        "examples": ["How am I doing today?", "Quick financial check."],
                        "parameters": "none",
                    },
                    "get_financial_summary": {
                        "description": "Executive dashboard — net worth, performance, income vs. spending, top 5 spending categories, goal status.",
                        "examples": ["How are my finances?", "Give me a financial overview."],
                        "parameters": "none",
                    },
                    "get_financial_health_score": {
                        "description": "Composite 0–100 financial health score (A–F) across savings rate, goal funding, debt ratio, emergency fund, diversification, and net worth trend.",
                        "examples": ["How healthy are my finances?", "What should I focus on improving?"],
                        "parameters": "none",
                    },
                    "get_features": {
                        "description": "Lists all available tools grouped by category (this tool).",
                        "examples": ["What can you do?", "What features are available?"],
                        "parameters": "none",
                    },
                },
            },
            "Balance Sheet & Net Worth": {
                "tools": {
                    "get_accounts": {
                        "description": "All financial accounts grouped by type (investments, bank, retirement, debt, property) with balances.",
                        "examples": ["Show all my accounts.", "What are my account balances?"],
                        "parameters": "none",
                    },
                    "get_net_worth": {
                        "description": "Current net worth (total assets minus total liabilities).",
                        "examples": ["What is my net worth?"],
                        "parameters": "none",
                    },
                    "get_net_worth_history": {
                        "description": "Monthly net worth trend over time.",
                        "examples": ["How has my net worth grown this year?"],
                        "parameters": "months (default 12, max 60)",
                    },
                    "get_net_worth_breakdown": {
                        "description": "Net worth split by person, by liquidity, and by tax treatment.",
                        "examples": ["How much of our wealth is in tax-advantaged accounts?", "How much is illiquid?"],
                        "parameters": "none",
                    },
                    "get_retirement_accounts": {
                        "description": "Aggregates all tax-advantaged retirement and savings accounts (401k, IRA, Roth, HSA, 529).",
                        "examples": ["How much do I have saved for retirement?"],
                        "parameters": "none",
                    },
                },
            },
            "Investments & Portfolio": {
                "tools": {
                    "get_holdings": {
                        "description": "All investment positions — ticker, units, price, value, cost basis, unrealized gain/loss.",
                        "examples": ["What positions do I hold?", "Show my unrealized gains."],
                        "parameters": "none",
                    },
                    "get_asset_allocation": {
                        "description": "Portfolio allocation by asset class (equities, bonds, cash) with top 10 holdings.",
                        "examples": ["Am I properly diversified?", "How much am I in equities vs bonds?"],
                        "parameters": "none",
                    },
                    "get_performance": {
                        "description": "Portfolio value change across MTD, YTD, 1-year, and longer periods.",
                        "examples": ["How is my portfolio performing this year?"],
                        "parameters": "none",
                    },
                    "get_transactions": {
                        "description": "Investment transactions (buys, sells, dividends) for a date range.",
                        "examples": ["What investment transactions happened last month?"],
                        "parameters": "days (default 30, max 365), account_id (optional)",
                    },
                    "get_capital_gains": {
                        "description": "Realized capital gains summary for a given tax year.",
                        "examples": ["What are my realized gains this year?"],
                        "parameters": "year (default: current year)",
                    },
                    "get_rebalancing_targets": {
                        "description": "Exact buy/sell amounts to reach a target equity/bond/cash allocation.",
                        "examples": ["How do I rebalance to 60/40?", "How far off target is my portfolio?"],
                        "parameters": "target_equity_pct (default 60), target_bond_pct (default 30), target_cash_pct (default 10)",
                    },
                    "get_asset_location_efficiency": {
                        "description": "Grades how well assets are positioned across account types for tax efficiency (A–F) with swap suggestions.",
                        "examples": ["Are my assets in the right accounts?", "How tax-efficient is my portfolio?"],
                        "parameters": "none",
                    },
                },
            },
            "Spending & Cash Flow": {
                "tools": {
                    "get_spending": {
                        "description": "Spending by category for recent months from all linked bank and credit card accounts.",
                        "examples": ["How much did I spend on dining last month?", "What are my biggest spending categories?"],
                        "parameters": "months (default 1, max 12)",
                    },
                    "get_spending_transactions": {
                        "description": "Bank and credit card transactions with category labels for everyday spending.",
                        "examples": ["Show me my dining expenses.", "What did I spend on groceries last month?"],
                        "parameters": "days (default 30, max 365), max_transactions (default 100; 0=all)",
                    },
                    "get_spending_trends": {
                        "description": "Month-over-month spending trends by category — which are trending up, down, or stable.",
                        "examples": ["Is my dining spending going up?", "Compare my last 3 months of spending."],
                        "parameters": "months (default 3, max 12)",
                    },
                    "get_budget_vs_actual": {
                        "description": "Compares this month's actual spending to the rolling average by category. Flags overspend categories.",
                        "examples": ["Am I over budget this month?", "Which categories are overspending?"],
                        "parameters": "months_avg (default 3)",
                    },
                    "get_year_over_year": {
                        "description": "Compares this year's spending and income to the same period last year with per-category breakdown.",
                        "examples": ["Am I spending more this year?", "How has my grocery spending changed year-over-year?"],
                        "parameters": "none",
                    },
                    "get_cash_flow_projection": {
                        "description": "Projects monthly cash flow for the next N months using actual income/spending averages.",
                        "examples": ["Will I have enough cash in 3 months?", "What does my cash flow look like through year-end?"],
                        "parameters": "months_ahead (default 6, max 24)",
                    },
                    "search_transactions": {
                        "description": "Search spending transactions by keyword, category, and/or amount range.",
                        "examples": ["How much did I spend at Costco this year?", "Show me all Amazon charges."],
                        "parameters": "query, category, days (default 365), min_amount, max_amount, max_results (default 100)",
                    },
                    "get_recurring_charges": {
                        "description": "Detects recurring/subscription charges from 120-day transaction patterns.",
                        "examples": ["What subscriptions am I paying for?", "What are my recurring bills?"],
                        "parameters": "none",
                    },
                    "get_income_summary": {
                        "description": "Income sources and monthly income trend; identifies paychecks, dividends, and interest grouped by source.",
                        "examples": ["How much did I earn last month?", "What are my income sources?"],
                        "parameters": "days (default 90, max 365)",
                    },
                    "get_savings_rate": {
                        "description": "Month-by-month savings rate — income minus spending divided by income.",
                        "examples": ["What is my savings rate?", "Am I saving more than last month?"],
                        "parameters": "months (default 6, max 12)",
                    },
                },
            },
            "Goals": {
                "tools": {
                    "get_goals": {
                        "description": "Financial goals and funding status from the Emoney plan (retirement, education, spending goals).",
                        "examples": ["Am I on track for retirement?", "How funded is the education goal?"],
                        "parameters": "none",
                    },
                },
            },
            "Tax Planning": {
                "tools": {
                    "get_tax_bracket_headroom": {
                        "description": "Shows how much more income can be earned before crossing into the next federal bracket. Also shows LTCG bracket headroom.",
                        "examples": ["How much can I convert to Roth without a bracket jump?", "How much freelance income can I take this year?"],
                        "parameters": "current_income (optional — inferred if omitted), filing_status (default 'mfj')",
                    },
                    "get_tax_loss_harvesting": {
                        "description": "Identifies taxable positions with unrealized losses, ranked by size with estimated tax savings.",
                        "examples": ["Where can I harvest losses?", "What's my tax-loss harvesting opportunity?"],
                        "parameters": "none",
                    },
                    "get_capital_gains_exposure": {
                        "description": "Identifies taxable positions with large unrealized gains and estimates tax liability if sold today.",
                        "examples": ["What's my capital gains tax exposure?", "Which positions would trigger the biggest tax bill?"],
                        "parameters": "filing_status ('mfj', 'single', 'hoh'), annual_income (optional)",
                    },
                    "get_contribution_room": {
                        "description": "Shows 2026 IRS contribution limits for all tax-advantaged accounts alongside current balances.",
                        "examples": ["How much can I still contribute to my IRA?", "Am I maxing out my HSA?"],
                        "parameters": "age (optional), filing_status (default 'mfj')",
                    },
                    "get_roth_conversion_analysis": {
                        "description": "Estimates tax cost and long-term benefit of a Roth conversion with bracket-by-bracket detail.",
                        "examples": ["Should I do a Roth conversion?", "What does it cost to convert $100k to Roth?"],
                        "parameters": "conversion_amount (required), current_income (required), filing_status, age",
                    },
                    "get_rmd_estimate": {
                        "description": "Estimates Required Minimum Distributions using the IRS Uniform Lifetime Table with 10-year projection.",
                        "examples": ["When do I have to start taking RMDs?", "How much will my RMD be at 75?"],
                        "parameters": "birth_year (required)",
                    },
                    "get_social_security_optimizer": {
                        "description": "Compares SS claiming at 62, FRA, and 70 — monthly benefit, lifetime value, and breakeven ages. Includes spousal analysis.",
                        "examples": ["Should I claim Social Security at 62 or wait until 70?", "What is the SS breakeven age?"],
                        "parameters": "birth_year (required), estimated_monthly_benefit_at_67 (optional), filing_status, spouse_birth_year, spouse_benefit_at_67, life_expectancy (default 85)",
                    },
                    "get_quarterly_estimated_taxes": {
                        "description": "Calculates Q1–Q4 estimated federal tax payments using current-year annualized and safe-harbor methods. Shows due dates.",
                        "examples": ["How much do I owe in estimated taxes?", "What are my Q2 estimated tax payments?"],
                        "parameters": "filing_status, annual_income_override, prior_year_tax, expected_withholding",
                    },
                },
            },
            "Retirement Planning": {
                "tools": {
                    "get_retirement_runway": {
                        "description": "Models how many years the portfolio sustains withdrawals under conservative, base, and optimistic return scenarios.",
                        "examples": ["Can I afford to retire now?", "How long will my money last?"],
                        "parameters": "annual_spending (optional), return_rate (default 0.06)",
                    },
                    "get_withdrawal_rate_analysis": {
                        "description": "Projects portfolio to retirement year and shows annual/monthly income at 3–5% withdrawal rates.",
                        "examples": ["How much can I spend in retirement?", "What does a 4% withdrawal rate give me?"],
                        "parameters": "none",
                    },
                    "get_net_worth_projection": {
                        "description": "Projects net worth forward and answers 'When will I hit $X?' — shows milestone years and 30-year snapshot.",
                        "examples": ["When will I hit $2M?", "How is my net worth projected to grow?"],
                        "parameters": "target_net_worth (optional), annual_return (default 0.07), annual_savings_override",
                    },
                    "get_debt_payoff_plan": {
                        "description": "Models avalanche vs. snowball debt payoff strategies — months to payoff and total interest for each.",
                        "examples": ["When will I be debt-free?", "Which payoff strategy saves the most interest?"],
                        "parameters": "extra_monthly_payment, assumed_credit_card_apr (default 0.22), assumed_loan_apr (default 0.07)",
                    },
                    "get_college_savings_gap": {
                        "description": "Estimates 529 funding gap for education goals — current trajectory vs. projected college costs with monthly savings needed.",
                        "examples": ["Are we on track for Parker's college?", "How much do we need to save monthly for 529?"],
                        "parameters": "annual_return (default 0.06), annual_college_inflation (default 0.05)",
                    },
                    "run_monte_carlo_retirement": {
                        "description": "Monte Carlo simulation (1,000+ paths) with stochastic returns and inflation — probability of success, median/10th/90th ending balances, safe withdrawal rate.",
                        "examples": ["What are my odds of not running out of money?", "How safe is a 4% withdrawal rate for 30 years?"],
                        "parameters": "simulations (default 1000), years (default 30), annual_spending, mean_return (default 0.07), std_dev (default 0.15), inflation_mean, inflation_std, social_security_annual, withdrawal_rate",
                    },
                    "get_dynamic_withdrawal_guardrails": {
                        "description": "Guyton-Klinger guardrail rules — raises withdrawals 10% when portfolio outperforms or cuts 10% when it underperforms vs. the starting value.",
                        "examples": ["Should I adjust my retirement withdrawals this year?", "Am I hitting a guardrail on my spending?"],
                        "parameters": "initial_withdrawal_rate (default 0.05), raise_ceiling_pct, cut_floor_pct, initial_portfolio_value, current_annual_withdrawal",
                    },
                },
            },
            "Session & Debug": {
                "tools": {
                    "sync_chrome_session": {
                        "description": "Pulls the active Emoney session from your running Chrome browser — no re-login needed if already logged in.",
                        "examples": ["Sync my Chrome session."],
                        "parameters": "none",
                    },
                    "reset_session": {
                        "description": "Clears the saved session and forces a fresh login on the next call.",
                        "examples": ["Reset my session.", "Log me out."],
                        "parameters": "none",
                    },
                    "get_version": {
                        "description": "Returns installed version, cookie file path, and session status for debugging.",
                        "examples": ["What version is emoney-mcp?"],
                        "parameters": "none",
                    },
                    "explore_emoney_cards": {
                        "description": "Probes unexplored Emoney CardSwitcher endpoints to discover additional data.",
                        "examples": ["What other Emoney data can we access?"],
                        "parameters": "card_ids (optional list of integers)",
                    },
                    "get_available_cards": {
                        "description": "Clean inventory of all responding card IDs (1–16) with data-shape fingerprint.",
                        "examples": ["What Emoney data cards are available?", "Discover new data sources."],
                        "parameters": "card_ids (optional list of integers)",
                    },
                    "clear_cache": {
                        "description": "Selectively purge in-memory data caches to force fresh data.",
                        "examples": ["Clear spending cache.", "Force fresh data from Emoney."],
                        "parameters": "module ('cards', 'spending', or 'all'; default 'all')",
                    },
                },
            },
    }

    # Derive counts from the dispatch registry (single source of truth) so the
    # advertised total can't drift from the tools actually registered, and
    # surface any tools missing from the hand-maintained category map.
    listed = {tool for cat in categories.values() for tool in cat.get("tools", {})}
    registered = set(_DISPATCH)
    return {
        "version": ver,
        "total_tools": len(registered),
        "categorized_tools": len(listed & registered),
        "uncategorized_tools": sorted(registered - listed),
        "categories": categories,
    }


# ── Session management ─────────────────────────────────────────────────────

def _get_version() -> dict:
    from importlib.metadata import version, PackageNotFoundError
    try:
        ver = version("emoney-mcp")
    except PackageNotFoundError:
        ver = "unknown (dev install)"
    return {
        "version": ver,
        "cookie_file": str(COOKIE_FILE),
        "session_exists": COOKIE_FILE.exists(),
    }


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
        # Purge in-memory caches so stale data is never returned after login
        scraper.clear_caches()
        return {"success": True, "message": "Session cleared. Call get_accounts to log in again."}
    except Exception as e:
        return {"error": str(e)}


# ── Tax planning ───────────────────────────────────────────────────────────











# ── Retirement planning ────────────────────────────────────────────────────





# ── Portfolio analysis ─────────────────────────────────────────────────────













# ── New Sprint 2 & 3 handlers ──────────────────────────────────────────────

















# ── Advanced retirement simulations ───────────────────────────────────────









# ── v0.8.0 new tool wrappers ───────────────────────────────────────────────















# ── v1.0.0 private wrappers ─────────────────────────────────────────────────





























def _clear_cache(module: str = "all") -> dict:
    return scraper.clear_cache(module=module)




# ── Transaction writes ─────────────────────────────────────────────────────









# ── Rules engine ───────────────────────────────────────────────────────────











# ── Reports ────────────────────────────────────────────────────────────────





async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        try:
            await app.run(read_stream, write_stream, app.create_initialization_options())
        finally:
            await close_session()


def run() -> None:
    """Sync entry point for `uvx` / `[project.scripts]`."""
    import asyncio
    asyncio.run(main())


if __name__ == "__main__":
    run()
