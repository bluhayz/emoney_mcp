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

get_vault_folder(http_session, folder_path)
    Lists individual files within a specific Vault folder (drill-down).
    ``folder_path`` is a slash-separated path under the Vault root, e.g.
    ``"Tax Documents"`` or ``"Vault/Tax Documents"`` — the ``Vault/`` prefix is
    added automatically when omitted.

Discovered via live network capture (epic #106, discovery pass 2). The listing
endpoint is ``GET /ema/api/v1/vault/<guid>/items?path=<path>&_=<ts>`` returning
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


async def _vault_items(http_session, path: str) -> dict:
    """Shared fetch-and-parse for any vault path; returns raw response dict."""
    base, err = await _vault_api_base(http_session)
    if err:
        return err

    http = await http_session.get_http()
    url = f"{BASE_URL}{base}/items"
    resp = await http.get(
        url,
        params={"path": path, "_": int(time.time() * 1000)},
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"},
        allow_redirects=True,
        timeout=20,
    )
    if resp.status_code != 200 or "json" not in resp.headers.get("content-type", ""):
        return {"error": f"Vault items endpoint returned HTTP {resp.status_code}."}

    data = resp.json()
    if not isinstance(data, dict):
        return {"error": "Vault items endpoint returned an unexpected (non-object) body."}
    return data


async def get_vault_documents(http_session) -> dict:
    """
    List the eMoney Vault's top-level folders with file counts, sizes, sharing
    status, and total storage usage.

    The Vault stores documents shared between the client and advisor (statements,
    estate documents, tax returns, etc.). This returns the top-level folder
    inventory — not the individual files inside them. Use get_vault_folder to
    drill into a specific folder.
    """
    data = await _vault_items(http_session, "Vault")
    if "error" in data:
        return data

    meta     = data.get("metadata") or {}
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
            "Top-level Vault folders only. Use get_vault_folder to list the "
            "individual files inside a folder. The Vault holds documents shared "
            "between you and your advisor. Source: /ema/api/v1/vault/<client>/items."
        ),
    }


async def get_vault_folder(http_session, folder_path: str = "Vault") -> dict:
    """
    List individual files (and sub-folders) within a specific Vault folder.

    ``folder_path`` is the path to the folder, e.g. ``"Tax Documents"`` or the
    full ``"Vault/Tax Documents"``. The ``Vault/`` prefix is added automatically
    when omitted. Returns each item's name, size, creation date, and sharing
    status — "What tax returns are in my vault?" or "List my estate docs".

    Parameters
    ----------
    folder_path : path to the folder (default: "Vault" = top-level listing)
    """
    path = folder_path.strip()
    if not path.lower().startswith("vault"):
        path = "Vault/" + path

    data = await _vault_items(http_session, path)
    if "error" in data:
        return data

    meta     = data.get("metadata") or {}
    children = data.get("children") or []

    folders = [_format_item(c) for c in children if c.get("type") == "folder"]
    files   = [_format_item(c) for c in children if c.get("type") != "folder"]
    folders.sort(key=lambda f: (f.get("name") or "").lower())
    files.sort(key=lambda f: (f.get("created_date") or ""), reverse=True)

    folder_bytes = meta.get("sizeInBytes") or 0

    return {
        "path":         path,
        "folder_name":  meta.get("name"),
        "total_files":  meta.get("fileCount") or len(files),
        "total_bytes":  folder_bytes,
        "total_mb":     round(folder_bytes / 1_048_576, 2),
        "sub_folders":  folders,
        "files":        files,
        "note": (
            f"Contents of vault path '{path}'. Files are sorted newest-first; "
            "sub-folders are sorted alphabetically. Use get_vault_documents for "
            "the top-level folder inventory."
        ),
    }
