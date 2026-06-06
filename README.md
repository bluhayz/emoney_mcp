# emoney_mcp

MCP server for [Emoney Advisor](https://wealth.emaplan.com) — exposes your
financial planning data as tools Claude Desktop can call.

> **Ask Claude:** *"What's my net worth?"* and get a real answer from your Emoney account.

## How it works

1. On first use, a Chrome window opens (via [nodriver](https://github.com/ultrafunkamsterdam/nodriver)) — log in normally including SMS MFA.
2. The server saves your session cookies to a local file.
3. All subsequent data fetches use [curl_cffi](https://github.com/yifeikong/curl_cffi) (Chrome TLS fingerprint) to call Emoney's internal JSON API — no browser needed until the session expires.

Emoney has no public API, so this uses browser automation for login and direct HTTP calls for data.

## Prerequisites

- Python 3.11+
- Google Chrome installed at the default path
- Claude Desktop

## Installation

```bash
cd emoney_mcp
py -m pip install -e .
```

## Claude Desktop config

Add to `%APPDATA%\Claude\claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "emoney": {
      "command": "py",
      "args": ["-m", "emoney_mcp.server"],
      "cwd": "D:\\ClaudeCode\\emoney_mcp\\src",
      "env": {
        "EMONEY_SUBDOMAIN": "wealth"
      }
    }
  }
}
```

Adjust `cwd` to wherever you cloned the repo.

## Available tools

| Tool | Description |
|---|---|
| `get_accounts` | All accounts grouped by type, with balances and net worth |
| `get_net_worth` | Net worth, total assets, total liabilities |
| `sync_chrome_session` | Pull session from your running Chrome (if cookies are accessible) |
| `reset_session` | Clear saved session and force a fresh login |

## First-time login flow

1. Ask Claude: *"What's my net worth?"*
2. A Chrome window opens — log in with username → Next → password → SMS code.
3. Once on the Emoney home page, call `get_accounts` again — cookies are auto-saved.
4. Subsequent calls work instantly until the session expires (typically a few hours).

## Session file

Cookies are saved to `emoney_mcp/.emoney_session.json`. This file is in `.gitignore`
and should never be committed. Delete it to force a fresh login.

## Dependencies

- `mcp` — Model Context Protocol server SDK
- `nodriver` — undetected Chrome launcher for login
- `curl_cffi` — Chrome TLS fingerprint HTTP client
- `beautifulsoup4` — HTML parsing (fallback)
- `pycryptodomex` — AES-GCM decryption for Chrome cookie extraction
- `python-dotenv` — env var support
