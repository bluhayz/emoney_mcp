"""
eMoney Vault (document storage) — read access.

The Vault client portal page (``/ema/CS/Vault``) embeds a ``vaultApi`` config
object whose ``BaseUrl`` is ``/ema/api/v1/vault/<clientGuid>``. The folder/file
tree is served as JSON from ``<BaseUrl>/items?path=Vault`` (a same-origin,
cookie-authenticated endpoint — no Bearer token needed, unlike the SNB/BFF APIs).

Public functions
----------------
get_vault_documents(http_session)
    Returns the Vault's top-level folders with per-folder file count, size, and
    sharing status, plus total storage usage — for "What documents are in my
    vault?" / "How much have I uploaded?".

Discovered via live network capture (epic #106, discovery pass 2). The listing
endpoint is ``GET /ema/api/v1/vault/<guid>/items?path=Vault&_=<ts>`` returning
``{metadata: {...}, children: [{name,type,fileCount,sizeInBytes,createdDate,
isShared,...}]}``.
"""

import re
import time

from ._helpers import BASE_URL

_VAULT_PAGE = f"{BASE_URL}/ema/CS/Vault"
_VAULT_BASE_RE = re.compile(r'"BaseUrl"\s*:\s*"(/ema/api/v1/vault/[0-9a-fA-F-]+)"')


async def _vault_api_base(http_session) -> tuple[str | None, dict | None]:
    """
    Resolve the per-client Vault API base path by scraping the Vault page.

    Returns ``(base_path, None)`` on success or ``(None, error_dict)`` on failure.
    """
    http = await http_session.get_http()
    resp = await http.get(_VAULT_PAGE, allow_redirects=True, timeout=20)
    if resp.status_code != 200:
        return None, {"error": f"Vault page returned HTTP {resp.status_code}."}
    if "/ema/SignIn" in str(resp.url):
        return None, {"error": "Session expired — call sync_chrome_session or reset_session."}
    m = _VAULT_BASE_RE.search(resp.text)
    if not m:
        return None, {"error": "Could not locate the Vault API base URL on the Vault page."}
    return m.group(1), None


def _format_item(item: dict) -> dict:
    """Project a raw vault tree node down to the fields worth surfacing."""
    size = item.get("sizeInBytes") or 0
    return {
        "name":         item.get("name"),
        "type":         item.get("type"),            # "folder" or "file"
        "file_count":   item.get("fileCount"),
        "size_bytes":   size,
        "size_mb":      round(size / 1_048_576, 2),
        "created_date": item.get("createdDate"),
        "is_shared":    item.get("isShared"),
        "is_private":   item.get("isClientsPrivateFolder"),
    }


async def get_vault_documents(http_session) -> dict:
    """
    List the eMoney Vault's top-level folders with file counts, sizes, sharing
    status, and total storage usage.

    The Vault stores documents shared between the client and advisor (statements,
    estate documents, tax returns, etc.). This returns the top-level folder
    inventory — not the individual files — plus aggregate usage.
    """
    base, err = await _vault_api_base(http_session)
    if err:
        return err

    http = await http_session.get_http()
    url = f"{BASE_URL}{base}/items"
    resp = await http.get(
        url,
        params={"path": "Vault", "_": int(time.time() * 1000)},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        allow_redirects=True,
        timeout=20,
    )
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"Vault items endpoint returned HTTP {resp.status_code}."}

    data = resp.json()
    if not isinstance(data, dict):
        return {"error": "Vault items endpoint returned an unexpected (non-object) body."}

    meta = data.get("metadata") or {}
    children = data.get("children") or []

    folders = [_format_item(c) for c in children if c.get("type") == "folder"]
    files   = [_format_item(c) for c in children if c.get("type") != "folder"]
    folders.sort(key=lambda f: f.get("file_count") or 0, reverse=True)

    total_bytes = meta.get("sizeInBytes") or 0

    return {
        "owner":              meta.get("name"),
        "total_files":        meta.get("fileCount"),
        "total_size_bytes":   total_bytes,
        "total_size_mb":      round(total_bytes / 1_048_576, 2),
        "created_date":       meta.get("createdDate"),
        "folder_count":       len(folders),
        "folders":            folders,
        "root_files":         files,
        "note": (
            "Top-level Vault folders only (not individual files within them); "
            "file_count and size are per-folder aggregates. The Vault holds "
            "documents shared between you and your advisor. Source: "
            "/ema/api/v1/vault/<client>/items."
        ),
    }
