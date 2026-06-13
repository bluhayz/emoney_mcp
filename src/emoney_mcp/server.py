"""Emoney MCP server."""

import importlib
import json
import time

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
                "Shows 2025 IRS annual contribution limits for all tax-advantaged accounts "
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


async def _call_tool_inner(name: str, arguments: dict) -> list[TextContent]:
    # Hot-reload scraper module only in development mode.
    # In production (the common case) this is skipped — the thin shim adds
    # no value once the package is installed, and reloading every call adds
    # measurable overhead.  Set EMONEY_DEV=1 to re-enable hot-reload.
    import os
    if os.environ.get("EMONEY_DEV"):
        importlib.reload(scraper)

    if name == "get_features":
        result = _get_features()
    elif name == "get_financial_summary":
        result = await _get_financial_summary()
    elif name == "get_accounts":
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
    elif name == "search_transactions":
        result = await _search_transactions(
            query=arguments.get("query", ""),
            category=arguments.get("category", ""),
            days=int(arguments.get("days", 365)),
            min_amount=float(arguments.get("min_amount", 0)),
            max_amount=float(arguments["max_amount"]) if "max_amount" in arguments else None,
            max_results=int(arguments.get("max_results", 100)),
        )
    elif name == "get_quick_status":
        result = await _get_quick_status()
    elif name == "get_budget_vs_actual":
        result = await _get_budget_vs_actual(months_avg=int(arguments.get("months_avg", 3)))
    elif name == "get_year_over_year":
        result = await _get_year_over_year()
    elif name == "get_cash_flow_projection":
        result = await _get_cash_flow_projection(months_ahead=int(arguments.get("months_ahead", 6)))
    elif name == "get_recurring_charges":
        result = await _get_recurring_charges()
    elif name == "get_net_worth_breakdown":
        result = await _get_net_worth_breakdown()
    elif name == "get_spending_trends":
        months = int(arguments.get("months", 3))
        result = await _get_spending_trends(months=months)
    elif name == "get_income_summary":
        days = int(arguments.get("days", 90))
        result = await _get_income_summary(days=days)
    elif name == "get_savings_rate":
        months = int(arguments.get("months", 6))
        result = await _get_savings_rate(months=months)
    elif name == "get_spending_transactions":
        days = int(arguments.get("days", 30))
        max_transactions = int(arguments.get("max_transactions", 100))
        result = await _get_spending_transactions(days=days, max_transactions=max_transactions)
    elif name == "get_version":
        result = _get_version()
    elif name == "sync_chrome_session":
        result = await _sync_chrome_session()
    elif name == "reset_session":
        result = await _reset()
    # ── Tax planning ──────────────────────────────────────────────────────
    elif name == "get_tax_loss_harvesting":
        result = await _get_tax_loss_harvesting()
    elif name == "get_contribution_room":
        age = arguments.get("age")
        if age is not None:
            age = int(age)
        result = await _get_contribution_room(
            age=age,
            filing_status=arguments.get("filing_status", "mfj"),
        )
    elif name == "get_roth_conversion_analysis":
        result = await _get_roth_conversion_analysis(
            conversion_amount=float(arguments["conversion_amount"]),
            current_income=float(arguments["current_income"]),
            filing_status=arguments.get("filing_status", "mfj"),
            age=int(arguments["age"]) if "age" in arguments else None,
        )
    elif name == "get_capital_gains_exposure":
        result = await _get_capital_gains_exposure(
            filing_status=arguments.get("filing_status", "mfj"),
            annual_income=float(arguments["annual_income"]) if "annual_income" in arguments else None,
        )
    elif name == "get_rmd_estimate":
        result = await _get_rmd_estimate(birth_year=int(arguments["birth_year"]))
    elif name == "get_tax_bracket_headroom":
        result = await _get_tax_bracket_headroom(
            current_income=float(arguments["current_income"]) if "current_income" in arguments else None,
            filing_status=arguments.get("filing_status", "mfj"),
        )
    # ── Retirement planning ───────────────────────────────────────────────
    elif name == "get_retirement_runway":
        result = await _get_retirement_runway(
            annual_spending=float(arguments["annual_spending"]) if "annual_spending" in arguments else None,
            return_rate=float(arguments.get("return_rate", 0.06)),
        )
    elif name == "get_withdrawal_rate_analysis":
        result = await _get_withdrawal_rate_analysis()
    elif name == "get_net_worth_projection":
        result = await _get_net_worth_projection(
            target_net_worth=float(arguments["target_net_worth"]) if "target_net_worth" in arguments else None,
            annual_return=float(arguments.get("annual_return", 0.07)),
            annual_savings_override=float(arguments["annual_savings_override"]) if "annual_savings_override" in arguments else None,
        )
    # ── Portfolio analysis ────────────────────────────────────────────────
    elif name == "get_asset_location_efficiency":
        result = await _get_asset_location_efficiency()
    elif name == "get_rebalancing_targets":
        result = await _get_rebalancing_targets(
            target_equity_pct=float(arguments.get("target_equity_pct", 60)),
            target_bond_pct=float(arguments.get("target_bond_pct", 30)),
            target_cash_pct=float(arguments.get("target_cash_pct", 10)),
        )
    elif name == "get_financial_health_score":
        result = await _get_financial_health_score()
    elif name == "explore_emoney_site":
        result = await _explore_emoney_site()
    elif name == "explore_snb_write_endpoints":
        result = await _explore_snb_write_endpoints()
    elif name == "explore_emoney_cards":
        card_ids = arguments.get("card_ids")
        if card_ids is not None:
            card_ids = [int(c) for c in card_ids]
        result = await _explore_emoney_cards(card_ids=card_ids)
    elif name == "get_debt_payoff_plan":
        result = await _get_debt_payoff_plan(
            extra_monthly_payment=float(arguments.get("extra_monthly_payment", 0.0)),
            assumed_credit_card_apr=float(arguments.get("assumed_credit_card_apr", 0.22)),
            assumed_loan_apr=float(arguments.get("assumed_loan_apr", 0.07)),
        )
    elif name == "get_college_savings_gap":
        result = await _get_college_savings_gap(
            annual_return=float(arguments.get("annual_return", 0.06)),
            annual_college_inflation=float(arguments.get("annual_college_inflation", 0.05)),
        )
    # ── Advanced retirement simulations ──────────────────────────────────
    elif name == "run_monte_carlo_retirement":
        result = await _run_monte_carlo_retirement(
            simulations=int(arguments.get("simulations", 1_000)),
            years=int(arguments.get("years", 30)),
            annual_spending=float(arguments["annual_spending"]) if "annual_spending" in arguments else None,
            mean_return=float(arguments.get("mean_return", 0.07)),
            std_dev=float(arguments.get("std_dev", 0.15)),
            inflation_mean=float(arguments.get("inflation_mean", 0.03)),
            inflation_std=float(arguments.get("inflation_std", 0.01)),
            social_security_annual=float(arguments.get("social_security_annual", 0.0)),
            withdrawal_rate=float(arguments["withdrawal_rate"]) if "withdrawal_rate" in arguments else None,
        )
    elif name == "get_dynamic_withdrawal_guardrails":
        result = await _get_dynamic_withdrawal_guardrails(
            initial_withdrawal_rate=float(arguments.get("initial_withdrawal_rate", 0.05)),
            raise_ceiling_pct=float(arguments.get("raise_ceiling_pct", 20.0)),
            cut_floor_pct=float(arguments.get("cut_floor_pct", 20.0)),
            raise_guard_pct=float(arguments.get("raise_guard_pct", 20.0)),
            cut_guard_pct=float(arguments.get("cut_guard_pct", 20.0)),
            initial_portfolio_value=float(arguments["initial_portfolio_value"]) if "initial_portfolio_value" in arguments else None,
            current_annual_withdrawal=float(arguments["current_annual_withdrawal"]) if "current_annual_withdrawal" in arguments else None,
        )
    elif name == "get_social_security_optimizer":
        result = await _get_social_security_optimizer(
            birth_year=int(arguments["birth_year"]),
            estimated_monthly_benefit_at_67=float(arguments["estimated_monthly_benefit_at_67"]) if "estimated_monthly_benefit_at_67" in arguments else None,
            filing_status=arguments.get("filing_status", "mfj"),
            spouse_birth_year=int(arguments["spouse_birth_year"]) if "spouse_birth_year" in arguments else None,
            spouse_benefit_at_67=float(arguments["spouse_benefit_at_67"]) if "spouse_benefit_at_67" in arguments else None,
            life_expectancy=int(arguments.get("life_expectancy", 85)),
        )
    elif name == "get_quarterly_estimated_taxes":
        result = await _get_quarterly_estimated_taxes(
            filing_status=arguments.get("filing_status", "mfj"),
            annual_income_override=float(arguments["annual_income_override"]) if "annual_income_override" in arguments else None,
            prior_year_tax=float(arguments["prior_year_tax"]) if "prior_year_tax" in arguments else None,
            expected_withholding=float(arguments["expected_withholding"]) if "expected_withholding" in arguments else None,
        )
    # ── v0.8.0 new tools ──────────────────────────────────────────────────
    elif name == "get_monthly_review":
        result = await _get_monthly_review()
    elif name == "get_unusual_transactions":
        result = await _get_unusual_transactions(
            days=int(arguments.get("days", 90)),
            threshold_pct=float(arguments.get("threshold_pct", 150.0)),
        )
    elif name == "get_merchant_spending":
        result = await _get_merchant_spending(
            days=int(arguments.get("days", 365)),
            merchant=arguments.get("merchant", ""),
            limit=int(arguments.get("limit", 25)),
        )
    elif name == "get_year_end_checklist":
        result = await _get_year_end_checklist(
            age=int(arguments["age"]) if "age" in arguments else None,
            birth_year=int(arguments["birth_year"]) if "birth_year" in arguments else None,
            filing_status=arguments.get("filing_status", "mfj"),
            current_income=float(arguments["current_income"]) if "current_income" in arguments else None,
        )
    elif name == "run_scenario":
        result = await _run_scenario(
            monthly_savings_delta=float(arguments.get("monthly_savings_delta", 0.0)),
            target_net_worth=float(arguments["target_net_worth"]) if "target_net_worth" in arguments else None,
            retirement_age=int(arguments["retirement_age"]) if "retirement_age" in arguments else None,
            annual_return_pct=float(arguments["annual_return_pct"]) if "annual_return_pct" in arguments else None,
        )
    elif name == "get_cash_flow_forecast":
        result = await _get_cash_flow_forecast(months=int(arguments.get("months", 3)))
    elif name == "get_insurance_gap_analysis":
        result = await _get_insurance_gap_analysis(
            income_multiple=float(arguments.get("income_multiple", 10.0)),
            disability_pct=float(arguments.get("disability_pct", 0.65)),
        )
    elif name == "clear_cache":
        result = _clear_cache(module=arguments.get("module", "all"))
    elif name == "get_available_cards":
        card_ids = arguments.get("card_ids")
        if card_ids is not None:
            card_ids = [int(c) for c in card_ids]
        result = await _get_available_cards(card_ids=card_ids)
    else:
        raise ValueError(f"Unknown tool: {name}")

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def _get_session_or_err():
    global _last_health_ts
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


async def _get_financial_summary() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_financial_summary(sess)


async def _search_transactions(
    query: str = "",
    category: str = "",
    days: int = 365,
    min_amount: float = 0.0,
    max_amount: float | None = None,
    max_results: int = 100,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.search_transactions(
        sess, query=query, category=category,
        days=days, min_amount=min_amount, max_amount=max_amount,
        max_results=max_results,
    )


async def _get_recurring_charges() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_recurring_charges(sess)


async def _get_net_worth_breakdown() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_net_worth_breakdown(sess)


async def _get_spending_trends(months: int = 3) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_spending_trends(sess, months=months)


async def _get_income_summary(days: int = 90) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_income_summary(sess, days=days)


async def _get_savings_rate(months: int = 6) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_savings_rate(sess, months=months)


async def _get_spending_transactions(days: int = 30, max_transactions: int = 100) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_spending_transactions(sess, days=days, max_transactions=max_transactions)


# ── Help ──────────────────────────────────────────────────────────────────

def _get_features() -> dict:
    from importlib.metadata import version, PackageNotFoundError
    try:
        ver = version("emoney-mcp")
    except PackageNotFoundError:
        ver = "unknown (dev install)"
    return {
        "version": ver,
        "total_tools": 51,
        "categories": {
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
                        "description": "Shows 2025 IRS contribution limits for all tax-advantaged accounts alongside current balances.",
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
        },
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

async def _get_tax_loss_harvesting() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_tax_loss_harvesting(sess)


async def _get_contribution_room(age: int | None = None, filing_status: str = "mfj") -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_contribution_room(sess, age=age, filing_status=filing_status)


async def _get_roth_conversion_analysis(
    conversion_amount: float,
    current_income: float,
    filing_status: str = "mfj",
    age: int | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_roth_conversion_analysis(
        sess,
        conversion_amount=conversion_amount,
        current_income=current_income,
        filing_status=filing_status,
        age=age,
    )


async def _get_capital_gains_exposure(
    filing_status: str = "mfj",
    annual_income: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_capital_gains_exposure(
        sess, filing_status=filing_status, annual_income=annual_income
    )


async def _get_rmd_estimate(birth_year: int) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_rmd_estimate(sess, birth_year=birth_year)


# ── Retirement planning ────────────────────────────────────────────────────

async def _get_retirement_runway(
    annual_spending: float | None = None,
    return_rate: float = 0.06,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_retirement_runway(
        sess, annual_spending=annual_spending, return_rate=return_rate
    )


async def _get_withdrawal_rate_analysis() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_withdrawal_rate_analysis(sess)


# ── Portfolio analysis ─────────────────────────────────────────────────────

async def _get_asset_location_efficiency() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_asset_location_efficiency(sess)


async def _get_rebalancing_targets(
    target_equity_pct: float = 60.0,
    target_bond_pct:   float = 30.0,
    target_cash_pct:   float = 10.0,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_rebalancing_targets(
        sess,
        target_equity_pct=target_equity_pct,
        target_bond_pct=target_bond_pct,
        target_cash_pct=target_cash_pct,
    )


async def _get_financial_health_score() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_financial_health_score(sess)


async def _explore_emoney_site() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.explore_emoney_site(sess)


async def _explore_snb_write_endpoints() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.explore_snb_write_endpoints(sess)


async def _explore_emoney_cards(card_ids: list[int] | None = None) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.explore_emoney_cards(sess, card_ids=card_ids)


# ── New Sprint 2 & 3 handlers ──────────────────────────────────────────────

async def _get_quick_status() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_quick_status(sess)


async def _get_budget_vs_actual(months_avg: int = 3) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_budget_vs_actual(sess, months_avg=months_avg)


async def _get_year_over_year() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_year_over_year(sess)


async def _get_cash_flow_projection(months_ahead: int = 6) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_cash_flow_projection(sess, months_ahead=months_ahead)


async def _get_tax_bracket_headroom(
    current_income: float | None = None,
    filing_status: str = "mfj",
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_tax_bracket_headroom(
        sess, current_income=current_income, filing_status=filing_status
    )


async def _get_net_worth_projection(
    target_net_worth: float | None = None,
    annual_return: float = 0.07,
    annual_savings_override: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_net_worth_projection(
        sess,
        target_net_worth=target_net_worth,
        annual_return=annual_return,
        annual_savings_override=annual_savings_override,
    )


async def _get_debt_payoff_plan(
    extra_monthly_payment: float = 0.0,
    assumed_credit_card_apr: float = 0.22,
    assumed_loan_apr: float = 0.07,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_debt_payoff_plan(
        sess,
        extra_monthly_payment=extra_monthly_payment,
        assumed_credit_card_apr=assumed_credit_card_apr,
        assumed_loan_apr=assumed_loan_apr,
    )


async def _get_college_savings_gap(
    annual_return: float = 0.06,
    annual_college_inflation: float = 0.05,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_college_savings_gap(
        sess,
        annual_return=annual_return,
        annual_college_inflation=annual_college_inflation,
    )


# ── Advanced retirement simulations ───────────────────────────────────────

async def _run_monte_carlo_retirement(
    simulations: int = 1_000,
    years: int = 30,
    annual_spending: float | None = None,
    mean_return: float = 0.07,
    std_dev: float = 0.15,
    inflation_mean: float = 0.03,
    inflation_std: float = 0.01,
    social_security_annual: float = 0.0,
    withdrawal_rate: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.run_monte_carlo_retirement(
        sess,
        simulations=simulations,
        years=years,
        annual_spending=annual_spending,
        mean_return=mean_return,
        std_dev=std_dev,
        inflation_mean=inflation_mean,
        inflation_std=inflation_std,
        social_security_annual=social_security_annual,
        withdrawal_rate=withdrawal_rate,
    )


async def _get_dynamic_withdrawal_guardrails(
    initial_withdrawal_rate: float = 0.05,
    raise_ceiling_pct: float = 20.0,
    cut_floor_pct: float = 20.0,
    raise_guard_pct: float = 20.0,
    cut_guard_pct: float = 20.0,
    initial_portfolio_value: float | None = None,
    current_annual_withdrawal: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_dynamic_withdrawal_guardrails(
        sess,
        initial_withdrawal_rate=initial_withdrawal_rate,
        raise_ceiling_pct=raise_ceiling_pct,
        cut_floor_pct=cut_floor_pct,
        raise_guard_pct=raise_guard_pct,
        cut_guard_pct=cut_guard_pct,
        initial_portfolio_value=initial_portfolio_value,
        current_annual_withdrawal=current_annual_withdrawal,
    )


async def _get_social_security_optimizer(
    birth_year: int,
    estimated_monthly_benefit_at_67: float | None = None,
    filing_status: str = "mfj",
    spouse_birth_year: int | None = None,
    spouse_benefit_at_67: float | None = None,
    life_expectancy: int = 85,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_social_security_optimizer(
        sess,
        birth_year=birth_year,
        estimated_monthly_benefit_at_67=estimated_monthly_benefit_at_67,
        filing_status=filing_status,
        spouse_birth_year=spouse_birth_year,
        spouse_benefit_at_67=spouse_benefit_at_67,
        life_expectancy=life_expectancy,
    )


async def _get_quarterly_estimated_taxes(
    filing_status: str = "mfj",
    annual_income_override: float | None = None,
    prior_year_tax: float | None = None,
    expected_withholding: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_quarterly_estimated_taxes(
        sess,
        filing_status=filing_status,
        annual_income_override=annual_income_override,
        prior_year_tax=prior_year_tax,
        expected_withholding=expected_withholding,
    )


# ── v0.8.0 new tool wrappers ───────────────────────────────────────────────

async def _get_monthly_review() -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_monthly_review(sess)


async def _get_unusual_transactions(days: int = 90, threshold_pct: float = 150.0) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_unusual_transactions(sess, days=days, threshold_pct=threshold_pct)


async def _get_merchant_spending(
    days: int = 365,
    merchant: str = "",
    limit: int = 25,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_merchant_spending(sess, days=days, merchant=merchant, limit=limit)


async def _get_year_end_checklist(
    age: int | None = None,
    birth_year: int | None = None,
    filing_status: str = "mfj",
    current_income: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_year_end_checklist(
        sess,
        age=age,
        birth_year=birth_year,
        filing_status=filing_status,
        current_income=current_income,
    )


async def _run_scenario(
    monthly_savings_delta: float = 0.0,
    target_net_worth: float | None = None,
    retirement_age: int | None = None,
    annual_return_pct: float | None = None,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.run_scenario(
        sess,
        monthly_savings_delta=monthly_savings_delta,
        target_net_worth=target_net_worth,
        retirement_age=retirement_age,
        annual_return_pct=annual_return_pct,
    )


async def _get_cash_flow_forecast(months: int = 3) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_cash_flow_forecast(sess, months=months)


async def _get_insurance_gap_analysis(
    income_multiple: float = 10.0,
    disability_pct: float = 0.65,
) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_insurance_gap_analysis(
        sess, income_multiple=income_multiple, disability_pct=disability_pct
    )


def _clear_cache(module: str = "all") -> dict:
    return scraper.clear_cache(module=module)


async def _get_available_cards(card_ids: list[int] | None = None) -> dict:
    sess, err = await _get_session_or_err()
    if err:
        return err
    return await scraper.get_available_cards(sess, card_ids=card_ids)


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
