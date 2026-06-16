"""
Reports listing and URL fetching.

The Reports page embeds a JSON list of available reports grouped by family.
GetReportUrl generates a time-limited signed URL for a specific report.

Report IDs are strings like "LiquidityReport", "AssetTaxTypeReport", etc.
Use get_reports() to discover all available report IDs, then
get_report_url(report_id) to get a link to view or download the report.
"""

import json
import os
import re

from ._helpers import BASE_URL

_REPORTS_URL = f"{BASE_URL}/ema/CS/Reports"


def _maybe_raw(out: dict, raw) -> dict:
    """Attach the unprocessed API response only when EMONEY_DEV is set."""
    if os.environ.get("EMONEY_DEV"):
        out["raw"] = raw
    return out


async def get_reports(http_session) -> dict:
    """
    Returns all available Emoney reports grouped by family (e.g. Investments,
    Net Worth, Tax, Estate). Each report has an ID, name, short name, and
    description. Pass the report_id to get_report_url() to generate a link.
    """
    http = await http_session.get_http()
    resp = await http.get(_REPORTS_URL, allow_redirects=True, timeout=20)
    if resp.status_code != 200:
        return {"error": f"Reports page returned HTTP {resp.status_code}"}

    html = resp.text
    # Reports are embedded as JSON arrays in the page source inside JS variable
    # assignments or data attributes. Pattern: [{ReportID:...,Name:...}...]
    families: list[dict] = []
    seen_ids: set[str] = set()

    # Match JSON arrays containing ReportID keys
    for blob in re.finditer(r'\[(\{"ReportID"[^]]{20,5000})\]', html):
        try:
            reports = json.loads("[" + blob.group(1) + "]")
        except Exception:
            continue
        for r in reports:
            rid = r.get("ReportID")
            if rid and rid not in seen_ids:
                seen_ids.add(rid)

    # Also try to find family groupings with Name + Reports sublist
    family_blobs = re.finditer(
        r'"Name"\s*:\s*"([^"]+)"[^}]{0,200}"Reports"\s*:\s*(\[[^\]]{10,3000}\])',
        html, re.DOTALL
    )
    for m in family_blobs:
        family_name = m.group(1)
        try:
            reports_list = json.loads(m.group(2))
        except Exception:
            continue
        normalized = []
        for r in reports_list:
            rid = r.get("ReportID")
            if rid:
                seen_ids.add(rid)
                normalized.append({
                    "report_id":   rid,
                    "name":        r.get("Name", ""),
                    "short_name":  r.get("ShortName", ""),
                    "description": r.get("Description", ""),
                })
        if normalized:
            families.append({"family": family_name, "reports": normalized})

    # Fallback: collect any standalone ReportID references
    standalone = []
    for m in re.finditer(r'"ReportID"\s*:\s*"([A-Za-z][A-Za-z0-9]+)"', html):
        rid = m.group(1)
        if rid not in seen_ids:
            seen_ids.add(rid)
            standalone.append({"report_id": rid, "name": rid, "short_name": "", "description": ""})

    if standalone:
        families.append({"family": "Other", "reports": standalone})

    total = sum(len(f["reports"]) for f in families)
    return {
        "families": families,
        "total_reports": total,
        "note": "Use get_report_url(report_id) to get a viewable link for any report.",
    }


async def get_report_url(http_session, report_id: str) -> dict:
    """
    Generate a signed URL to view or download a specific Emoney report.

    report_id — the report identifier string (e.g. "LiquidityReport",
                "AssetTaxTypeReport"). Get the full list from get_reports().

    Returns a URL that can be opened in a browser to view the report as a PDF
    or interactive page.
    """
    http = await http_session.get_http()
    token = await http_session.get_csrf_token()
    if not token:
        return {"error": "Could not obtain CSRF token from Emoney — "
                         "page layout may have changed or the session expired."}

    resp = await http.post(
        f"{_REPORTS_URL}/GetReportUrl",
        data={
            "reportID": report_id,
            "__RequestVerificationToken": token,
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=20,
    )
    if resp.status_code not in (200, 201):
        return {"error": f"GetReportUrl returned HTTP {resp.status_code}"}

    ct = resp.headers.get("content-type", "")
    if "json" not in ct:
        return {"error": "GetReportUrl returned non-JSON — report may not exist or session is stale"}

    data = resp.json()
    # Response is typically a JSON string containing the URL
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            # Raw URL string
            return {"report_id": report_id, "url": data}

    if isinstance(data, dict):
        url = data.get("Url") or data.get("url") or data.get("ReportUrl") or data.get("reportUrl")
        if url:
            return {"report_id": report_id, "url": url}
        if not data.get("Success", True):
            return {"error": data.get("Message", "GetReportUrl failed"), "report_id": report_id}
        return _maybe_raw(
            {"report_id": report_id,
             "error": "GetReportUrl returned no recognizable URL field"},
            data,
        )

    return _maybe_raw(
        {"report_id": report_id,
         "error": "GetReportUrl returned an unexpected response type"},
        data,
    )
