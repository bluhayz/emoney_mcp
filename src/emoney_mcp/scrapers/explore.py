"""
Site-wide exploration tool.

Fetches each major Emoney section using the authenticated curl_cffi session and
mines the HTML/JS for:
  - API endpoints (fetch/$.ajax/XMLHttpRequest/axios URLs)
  - Form actions
  - JS config objects (apiUrl, baseUrl, endpoint, etc.)
  - Nav links discovered in each page
  - Page titles / H1s

No data is modified — all requests are GET only.
"""

import re
from urllib.parse import urljoin

from ..browser import is_emoney_host
from ._helpers import BASE_URL

# ---------------------------------------------------------------------------
# Pages to probe
# ---------------------------------------------------------------------------

_PAGES = [
    ("Home / Dashboard",        f"{BASE_URL}/ema/CS/Home"),
    ("Accounts",                f"{BASE_URL}/ema/CS/Accounts"),
    ("Net Worth",               f"{BASE_URL}/ema/CS/NetWorth"),
    ("Investments",             f"{BASE_URL}/ema/CS/Investments"),
    ("Spending",                f"{BASE_URL}/ema/CS/Spending"),
    ("Cash Flow",               f"{BASE_URL}/ema/CS/CashFlow"),
    ("Goals / Plan",            f"{BASE_URL}/ema/CS/Goals"),
    ("Insurance",               f"{BASE_URL}/ema/CS/Insurance"),
    ("Tax",                     f"{BASE_URL}/ema/CS/Tax"),
    ("Reports",                 f"{BASE_URL}/ema/CS/Reports"),
    ("Documents / Vault",       f"{BASE_URL}/ema/CS/Documents"),
    ("Tasks",                   f"{BASE_URL}/ema/CS/Tasks"),
    ("Messages",                f"{BASE_URL}/ema/CS/Messages"),
    ("Profile / Settings",      f"{BASE_URL}/ema/CS/Profile"),
    ("Estate",                  f"{BASE_URL}/ema/CS/Estate"),
    ("Education",               f"{BASE_URL}/ema/CS/Education"),
    ("Scenario / What-If",      f"{BASE_URL}/ema/CS/Scenario"),
    ("Monte Carlo",             f"{BASE_URL}/ema/CS/MonteCarlo"),
    ("Social Security",         f"{BASE_URL}/ema/CS/SocialSecurity"),
]

# ---------------------------------------------------------------------------
# Regex patterns for mining HTML/JS
# ---------------------------------------------------------------------------

# Quoted strings that look like relative or absolute URLs in JS
_URL_PATTERNS = [
    # fetch("…") / fetch('…')
    re.compile(r"""fetch\s*\(\s*['"`]([^'"`\s]{4,200})['"`]"""),
    # $.ajax({ url: "…" }) / url: '…'
    re.compile(r"""url\s*:\s*['"`]([^'"`\s]{4,200})['"`]"""),
    # axios.get("…") / axios.post("…")
    re.compile(r"""axios\s*\.\s*(?:get|post|put|patch|delete)\s*\(\s*['"`]([^'"`\s]{4,200})['"`]"""),
    # XMLHttpRequest open("GET", "…")
    re.compile(r"""\.open\s*\(\s*['"`]\w+['"`]\s*,\s*['"`]([^'"`\s]{4,200})['"`]"""),
    # action="…" on forms
    re.compile(r"""action=['"]([^'"]{4,200})['"]"""),
    # data-url="…" / data-endpoint="…" / data-src="…"
    re.compile(r"""data-(?:url|endpoint|src|action|href)=['"]([^'"]{4,200})['"]"""),
    # apiUrl: "…" / endpoint: "…" / baseUrl: "…"
    re.compile(r"""(?:apiUrl|endpoint|baseUrl|apiBase|serviceUrl)\s*[=:]\s*['"`]([^'"`\s]{4,200})['"`]"""),
    # /ema/ or /snb-api or /api/ paths anywhere in JS strings
    re.compile(r"""['"`](/(?:ema|api|snb-api)[^'"`\s]{2,150})['"`]"""),
]

# Patterns that look like internal Emoney API endpoints (filter noise)
_INTERESTING = re.compile(
    r"(/ema/|/api/|snb-api|emoneyadvisor\.com|emaplan\.com)",
    re.IGNORECASE,
)

# Nav links: href="/ema/CS/…"
_NAV_LINK = re.compile(r"""href=['"](/ema/CS/[^'"?#\s]{2,80})['"]""")

# Page title
_TITLE = re.compile(r"<title[^>]*>([^<]{1,120})</title>", re.IGNORECASE)
_H1    = re.compile(r"<h1[^>]*>([^<]{1,120})</h1>",    re.IGNORECASE)


def _mine_endpoints(html: str, page_url: str) -> list[str]:
    """Extract interesting endpoint URLs from a page's HTML/JS."""
    found: set[str] = set()
    for pat in _URL_PATTERNS:
        for m in pat.finditer(html):
            raw = m.group(1).strip()
            if not _INTERESTING.search(raw):
                continue
            # Make absolute
            if raw.startswith("/"):
                raw = urljoin(BASE_URL, raw)
            found.add(raw)
    return sorted(found)


def _mine_nav_links(html: str) -> list[str]:
    """Extract /ema/CS/… nav links from a page."""
    return sorted({m.group(1) for m in _NAV_LINK.finditer(html)})


def _page_title(html: str) -> str:
    m = _TITLE.search(html) or _H1.search(html)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

async def explore_emoney_site(http_session) -> dict:
    """
    Crawl major Emoney pages and mine them for API endpoints and structure.

    Returns a dict with:
      - pages_visited: list of {section, url, title, status, endpoints, nav_links}
      - all_endpoints: deduplicated sorted list of every endpoint found
      - nav_map: all /ema/CS/ paths discovered (union across all pages)
      - summary: counts
    """
    http = await http_session.get_http()

    pages_visited = []
    all_endpoints: set[str] = set()
    all_nav: set[str] = set()

    for section, url in _PAGES:
        try:
            resp = await http.get(url, allow_redirects=True, timeout=20)
            status = resp.status_code
            final_url = str(resp.url)

            # If we got redirected to a signin page, mark as auth-required
            if any(x in final_url.lower() for x in ("signin", "login", "/auth")):
                pages_visited.append({
                    "section":    section,
                    "url":        url,
                    "status":     status,
                    "note":       "redirected to login — page may not exist or requires different permissions",
                    "endpoints":  [],
                    "nav_links":  [],
                    "title":      "",
                })
                continue

            # Never mine HTML/endpoints from a response that redirected off the
            # trusted emaplan.com host (SSO provider, error page, misconfigured
            # redirect) — those endpoints would not be legitimate Emoney internals.
            if not is_emoney_host(final_url):
                pages_visited.append({
                    "section":    section,
                    "url":        url,
                    "status":     status,
                    "note":       f"redirected off emaplan.com to {final_url} — content not mined",
                    "endpoints":  [],
                    "nav_links":  [],
                    "title":      "",
                })
                continue

            html = resp.text
            endpoints = _mine_endpoints(html, url)
            nav_links = _mine_nav_links(html)
            title = _page_title(html)

            all_endpoints.update(endpoints)
            all_nav.update(nav_links)

            pages_visited.append({
                "section":   section,
                "url":       url,
                "final_url": final_url if final_url != url else None,
                "status":    status,
                "title":     title,
                "endpoints": endpoints,
                "nav_links": nav_links,
            })

        except Exception as exc:
            pages_visited.append({
                "section":  section,
                "url":      url,
                "status":   None,
                "error":    str(exc),
                "endpoints": [],
                "nav_links": [],
                "title":    "",
            })

    return {
        "pages_visited":  pages_visited,
        "all_endpoints":  sorted(all_endpoints),
        "nav_map":        sorted(all_nav),
        "summary": {
            "pages_probed":       len(_PAGES),
            "pages_ok":           sum(1 for p in pages_visited if p.get("status") == 200),
            "total_endpoints":    len(all_endpoints),
            "total_nav_sections": len(all_nav),
        },
    }
