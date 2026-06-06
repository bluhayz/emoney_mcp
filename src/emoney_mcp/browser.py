"""Browser/session manager for emaplan.com.

Strategy
--------
1. PRIMARY: Read cookies directly from the user's running Chrome browser by
   copying the locked SQLite file via Windows CopyFileW (bypasses the file
   lock), then decrypting via DPAPI + AES-GCM. No browser automation needed.

2. FALLBACK: nodriver opens a real Chrome window in a separate OS thread
   (with its own asyncio event loop) so it doesn't conflict with the MCP
   server's event loop. A watcher loop saves cookies once login completes.

All subsequent scraping uses curl_cffi (Chrome TLS fingerprint).
"""

import asyncio
import ctypes
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

from curl_cffi.requests import AsyncSession

_SUBDOMAIN = os.getenv("EMONEY_SUBDOMAIN", "wealth")
BASE_URL = f"https://{_SUBDOMAIN}.emaplan.com"
LOGIN_URL = f"{BASE_URL}/ema/SignIn"
HOME_URL  = f"{BASE_URL}/ema/CS/Home"

MANUAL_LOGIN_REQUIRED = "MANUAL_LOGIN_REQUIRED"
_DEFAULT_COOKIE_FILE = Path(__file__).parent.parent.parent / ".emoney_session.json"
COOKIE_FILE = Path(os.getenv("EMONEY_SESSION_FILE", str(_DEFAULT_COOKIE_FILE)))

_IMPERSONATE = "chrome120"

# Chrome cookie DB path
_CHROME_COOKIE_SRC = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Google/Chrome/User Data/Default/Network/Cookies"
)
_CHROME_STATE_SRC = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Google/Chrome/User Data/Local State"
)


def _is_signin_url(url: str) -> bool:
    """Return True while the user is still in the login/OAuth flow."""
    low = url.lower()
    return (
        "signin" in low
        or "login" in low
        or "auth." in low          # auth.wealth.emaplan.com OAuth redirect
        or "/oauth" in low
        or "/authorize" in low
        or not url                 # empty URL = not yet loaded
    )


# ---------------------------------------------------------------------------
# Chrome cookie extraction via CopyFileW (no admin needed)
# ---------------------------------------------------------------------------

def _get_chrome_aes_key() -> bytes | None:
    """Derive Chrome's AES-256-GCM key from Local State via DPAPI."""
    try:
        import base64
        import ctypes.wintypes

        state_text = _CHROME_STATE_SRC.read_text(encoding="utf-8")
        state = json.loads(state_text)
        enc_key_b64 = state["os_crypt"]["encrypted_key"]
        enc_key = base64.b64decode(enc_key_b64)[5:]  # strip "DPAPI" prefix

        # DPAPI decrypt
        import ctypes
        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        p_in = ctypes.create_string_buffer(enc_key)
        blob_in = DATA_BLOB(len(enc_key), p_in)
        blob_out = DATA_BLOB()

        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)
        )
        if not ok:
            return None
        key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
        return key
    except Exception:
        return None


def _copy_locked_file(src: Path, dst: str) -> bool:
    """Copy a file that may be locked by another process using CopyFileW."""
    return bool(ctypes.windll.kernel32.CopyFileW(str(src), dst, False))


def extract_chrome_emaplan_cookies() -> dict:
    """
    Copy Chrome's cookie DB, decrypt emaplan.com cookies, return as dict.
    Returns {} on any failure.
    """
    if not _CHROME_COOKIE_SRC.exists():
        return {}

    key = _get_chrome_aes_key()
    if key is None:
        return {}

    tmp = tempfile.mktemp(suffix=".db")
    try:
        if not _copy_locked_file(_CHROME_COOKIE_SRC, tmp):
            return {}

        from Cryptodome.Cipher import AES

        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, encrypted_value, host_key FROM cookies "
            "WHERE host_key LIKE '%emaplan%'"
        ).fetchall()
        conn.close()

        cookies: dict = {}
        for name, enc_val, host in rows:
            try:
                # Chrome AES-GCM: b'\x76\x31\x30' prefix (v10), then 12-byte nonce
                if enc_val[:3] == b"v10":
                    nonce = enc_val[3:15]
                    ct    = enc_val[15:-16]
                    tag   = enc_val[-16:]
                    val   = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct, tag).decode()
                else:
                    # Older DPAPI-encrypted value (rare on modern Chrome)
                    import ctypes.wintypes
                    class DATA_BLOB(ctypes.Structure):
                        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                                    ("pbData", ctypes.POINTER(ctypes.c_char))]
                    p_in = ctypes.create_string_buffer(bytes(enc_val))
                    blob_in = DATA_BLOB(len(enc_val), p_in)
                    blob_out = DATA_BLOB()
                    ctypes.windll.crypt32.CryptUnprotectData(
                        ctypes.byref(blob_in), None, None, None, None, 0,
                        ctypes.byref(blob_out)
                    )
                    val = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode()
                    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
                cookies[name] = val
            except Exception:
                pass

        return cookies
    except Exception:
        return {}
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# curl_cffi HTTP session — used for all scraping after login
# ---------------------------------------------------------------------------

class EmoneyHttpSession:
    def __init__(self):
        self._http: AsyncSession | None = None

    def has_cookies(self) -> bool:
        return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 10

    def load_cookies(self) -> dict:
        if COOKIE_FILE.exists():
            try:
                return json.loads(COOKIE_FILE.read_text())
            except Exception:
                pass
        return {}

    def save_cookies(self, cookies: dict) -> None:
        COOKIE_FILE.write_text(json.dumps(cookies, indent=2))
        self._http = None  # force reload with fresh cookies

    async def get_http(self) -> AsyncSession:
        if self._http is None:
            self._http = AsyncSession(impersonate=_IMPERSONATE)
            for k, v in self.load_cookies().items():
                self._http.cookies.set(k, v, domain=f"{_SUBDOMAIN}.emaplan.com")
        return self._http

    async def is_logged_in(self) -> bool:
        if not self.has_cookies():
            return False
        http = await self.get_http()
        try:
            resp = await http.get(HOME_URL, allow_redirects=True, timeout=20)
            return not _is_signin_url(str(resp.url))
        except Exception:
            return False

    async def get_page_html(self, url: str) -> str:
        http = await self.get_http()
        resp = await http.get(url, allow_redirects=True, timeout=30)
        return resp.text

    def close(self) -> None:
        self._http = None


# ---------------------------------------------------------------------------
# nodriver login session — runs in its own OS thread / event loop
# ---------------------------------------------------------------------------

class EmoneyLoginSession:
    def __init__(self):
        self._waiting    = False          # True while browser is open
        self._thread: threading.Thread | None = None
        self._done_event = threading.Event()  # set when cookies are saved

    def open_login_window(self) -> None:
        """Spawn a background thread that runs its own asyncio event loop."""
        if self._thread and self._thread.is_alive():
            return  # already running
        self._waiting    = True
        self._done_event.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="nodriver-login")
        self._thread.start()

    def _thread_main(self) -> None:
        """Entry point for the nodriver thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as exc:
            import traceback
            traceback.print_exc()
        finally:
            loop.close()
            self._waiting = False

    async def _async_main(self) -> None:
        import nodriver as nd
        import sys

        def log(msg):
            print(f"[nodriver-thread] {msg}", file=sys.stderr, flush=True)

        chrome = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
        log("starting nd.start()")
        browser = await nd.start(
            headless=False,
            browser_executable_path=chrome if os.path.exists(chrome) else None,
        )
        log("browser started, navigating to login")
        tab = await browser.get(LOGIN_URL)
        log(f"on login page")

        # Poll ALL open tabs until one leaves the signin/OAuth flow (up to 10 min)
        for i in range(300):
            await asyncio.sleep(2)
            try:
                for t in list(browser.tabs):
                    try:
                        url = str(await t.evaluate("window.location.href") or "")
                    except Exception:
                        continue
                    log(f"[{i}] tab url={url[:80]}")
                    if url and not _is_signin_url(url) and "emaplan.com" in url:
                        log("login detected! extracting cookies...")
                        cookies = await self._extract_cookies(browser, t)
                        log(f"extracted {len(cookies)} cookies: {list(cookies.keys())[:10]}")
                        if cookies:
                            _http_session.save_cookies(cookies)
                            self._done_event.set()
                            log("cookies saved!")
                        else:
                            log("WARNING: no cookies extracted")
                        return
            except Exception as exc:
                log(f"[{i}] poll error: {exc}")

        try:
            browser.stop()
        except Exception:
            pass
        self._waiting = False
        log("thread done")

    async def _extract_cookies(self, browser, tab) -> dict:
        """Extract emaplan.com cookies from the browser via multiple methods."""
        import sys

        def log(msg):
            print(f"[nodriver-thread] {msg}", file=sys.stderr, flush=True)

        # Method 1: CDP get_all_cookies
        try:
            import nodriver.cdp.network as cdp_net
            all_cookies = await tab.send(cdp_net.get_all_cookies())
            result = {
                c.name: c.value
                for c in (all_cookies or [])
                if c.domain and "emaplan" in c.domain
            }
            log(f"CDP got {len(result)} cookies")
            if result:
                return result
        except Exception as e:
            log(f"CDP method failed: {e}")

        # Method 2: CDP get_cookies for the specific URL
        try:
            import nodriver.cdp.network as cdp_net
            site_cookies = await tab.send(cdp_net.get_cookies([BASE_URL, f"{BASE_URL}/ema/CS/Home"]))
            result = {c.name: c.value for c in (site_cookies or [])}
            log(f"CDP get_cookies got {len(result)} cookies")
            if result:
                return result
        except Exception as e:
            log(f"CDP get_cookies failed: {e}")

        # Method 3: JS document.cookie (non-HttpOnly only)
        try:
            raw = await tab.evaluate("document.cookie")
            out: dict = {}
            for part in (raw or "").split(";"):
                part = part.strip()
                if "=" in part:
                    k, v = part.split("=", 1)
                    out[k.strip()] = v.strip()
            log(f"JS document.cookie got {len(out)} cookies")
            if out:
                return out
        except Exception as e:
            log(f"JS cookie method failed: {e}")

        # Method 4: Read from nodriver's temp Chrome profile directly
        try:
            cookies = self._read_profile_cookies(browser)
            log(f"Profile read got {len(cookies)} cookies")
            if cookies:
                return cookies
        except Exception as e:
            log(f"Profile read failed: {e}")

        return {}

    def _read_profile_cookies(self, browser) -> dict:
        """Read cookies from nodriver's temporary Chrome profile (not locked)."""
        import glob as _glob

        # nodriver stores temp profile in a predictable location
        profile_dir = None
        try:
            # Try to get profile dir from browser config
            cfg = getattr(browser, "config", None)
            if cfg:
                profile_dir = getattr(cfg, "user_data_dir", None)
        except Exception:
            pass

        if not profile_dir:
            # Search common temp locations
            import tempfile
            tmp = Path(tempfile.gettempdir())
            candidates = list(tmp.glob("nodriver_*")) + list(tmp.glob("pyppeteer_*")) + list(tmp.glob("uc_*"))
            for c in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
                cookie_db = c / "Default" / "Network" / "Cookies"
                if cookie_db.exists():
                    profile_dir = str(c)
                    break

        if not profile_dir:
            return {}

        cookie_db = Path(profile_dir) / "Default" / "Network" / "Cookies"
        if not cookie_db.exists():
            cookie_db = Path(profile_dir) / "Default" / "Cookies"
        if not cookie_db.exists():
            return {}

        # This profile isn't locked by another process — read directly
        conn = sqlite3.connect(str(cookie_db))
        rows = conn.execute(
            "SELECT name, value, host_key FROM cookies WHERE host_key LIKE '%emaplan%'"
        ).fetchall()
        conn.close()
        return {name: value for name, value, host in rows}

    def stop(self) -> None:
        self._waiting = False


# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

_http_session   = EmoneyHttpSession()
_login_session  = EmoneyLoginSession()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_authenticated_session() -> "EmoneyHttpSession | str":
    """
    Returns an authenticated EmoneyHttpSession or MANUAL_LOGIN_REQUIRED.

    Order of operations:
    1. If the nodriver thread just finished (cookies saved), use them.
    2. If saved cookies still work, use them.
    3. Try extracting cookies directly from the user's running Chrome.
    4. Open nodriver browser window for manual login.
    """
    # 1. Nodriver thread finished since last check → reload session
    if not _login_session._waiting and _http_session.has_cookies():
        if await _http_session.is_logged_in():
            return _http_session
        # Cookies exist but invalid → fall through to re-login

    # 2. Browser open — still waiting for login
    if _login_session._waiting:
        return MANUAL_LOGIN_REQUIRED

    # 3. Try saved cookies
    if _http_session.has_cookies():
        if await _http_session.is_logged_in():
            return _http_session

    # 4. Try to extract from the user's live Chrome session
    chrome_cookies = extract_chrome_emaplan_cookies()
    if chrome_cookies:
        _http_session.save_cookies(chrome_cookies)
        if await _http_session.is_logged_in():
            return _http_session

    # 5. Need fresh login via nodriver
    _login_session.open_login_window()
    return MANUAL_LOGIN_REQUIRED


async def close_session() -> None:
    _http_session.close()
    _login_session.stop()
