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
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi.requests import AsyncSession

# Debug-level logger for the auth/cookie paths. Failures here are non-fatal
# (the caller falls back to manual login), but logging the exception type makes
# "it just didn't work" diagnosable. Never log cookie values or tokens.
_log = logging.getLogger("emoney_mcp.browser")

_SUBDOMAIN = os.getenv("EMONEY_SUBDOMAIN", "wealth")
BASE_URL = f"https://{_SUBDOMAIN}.emaplan.com"
LOGIN_URL = f"{BASE_URL}/ema/SignIn"
HOME_URL  = f"{BASE_URL}/ema/CS/Home"

MANUAL_LOGIN_REQUIRED = "MANUAL_LOGIN_REQUIRED"
_DEFAULT_COOKIE_FILE = Path.home() / ".emoney_mcp" / "session.json"
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


def is_emoney_host(url: str) -> bool:
    """Return True if the URL's host is on the trusted emaplan.com domain.

    Used to confirm a response (after following redirects) actually landed on
    Emoney before trusting it for auth checks or HTML/endpoint mining — so a
    redirect through a third-party domain can't be mistaken for Emoney content.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return host == "emaplan.com" or host.endswith(".emaplan.com")


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

        ok = ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
            ctypes.byref(blob_in), None, None, None, None, 0,
            ctypes.byref(blob_out)
        )
        if not ok:
            return None
        key = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]
        return key
    except Exception as e:
        _log.debug("Chrome AES key derivation (Windows) failed: %s", type(e).__name__)
        return None


def _copy_locked_file(src: Path, dst: str) -> bool:
    """Copy a file that may be locked by another process using CopyFileW."""
    return bool(ctypes.windll.kernel32.CopyFileW(str(src), dst, False))  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# macOS Chrome cookie extraction (Keychain + AES-128-CBC)
# ---------------------------------------------------------------------------

_MACOS_CHROME_SUPPORT = Path.home() / "Library/Application Support/Google"


def _macos_cookie_db_candidates() -> list[Path]:
    """Chrome Cookies DBs across channels (stable/Beta/Dev/Canary) and profiles.

    Returns the stable channel's Default profile first, then its other profiles,
    then the other channels — so the most common case is tried first. Mirrors the
    profile-glob discovery used on Windows instead of hardcoding Default only.
    """
    candidates: list[Path] = []
    for channel_dir in sorted(_MACOS_CHROME_SUPPORT.glob("Chrome*")):
        default = channel_dir / "Default" / "Cookies"
        if default.exists():
            candidates.append(default)
        for prof in sorted(channel_dir.glob("Profile */Cookies")):
            candidates.append(prof)
    return candidates


def _read_macos_cookie_rows(db_path: Path) -> list:
    """Read emaplan cookie rows from a Chrome Cookies DB, WAL writes included.

    Copies the DB *and* its -wal/-shm sidecars to a temp dir and opens the copy
    normally (not immutable=1), so the newest cookies still living in the WAL —
    e.g. a session cookie written moments ago — aren't missed.
    """
    tmpdir = tempfile.mkdtemp(prefix="emoney_cookies_")
    try:
        tmp_db = Path(tmpdir) / "Cookies"
        shutil.copy2(db_path, tmp_db)
        for suffix in ("-wal", "-shm"):
            side = db_path.with_name(db_path.name + suffix)
            if side.exists():
                shutil.copy2(side, Path(tmpdir) / ("Cookies" + suffix))
        conn = sqlite3.connect(f"file:{tmp_db}", uri=True)
        try:
            return conn.execute(
                "SELECT name, encrypted_value, host_key FROM cookies "
                "WHERE host_key LIKE '%emaplan%'"
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _log.debug("macOS cookie DB read failed for %s: %s", db_path, type(e).__name__)
        return []
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _get_chrome_macos_key() -> bytes | None:
    """Derive Chrome's AES-128 cookie key on macOS.

    The key is PBKDF2-HMAC-SHA1(password, salt="saltysalt", iterations=1003,
    dkLen=16) where ``password`` is the "Chrome Safe Storage" secret stored in
    the login Keychain. Reading it via the ``security`` CLI triggers a one-time
    Keychain access prompt the user must approve.
    """
    try:
        import subprocess
        from Cryptodome.Protocol.KDF import PBKDF2

        proc = subprocess.run(
            ["security", "find-generic-password", "-w",
             "-s", "Chrome Safe Storage", "-a", "Chrome"],
            capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0 or not proc.stdout:
            return None
        password = proc.stdout.strip().encode("utf-8")
        # pycryptodome PBKDF2 defaults to HMAC-SHA1, which is what Chrome uses.
        return PBKDF2(password, b"saltysalt", dkLen=16, count=1003)
    except Exception as e:
        _log.debug("Chrome Keychain key derivation (macOS) failed: %s", type(e).__name__)
        return None


def _decrypt_macos_cookie(enc_val: bytes, key: bytes) -> str | None:
    """Decrypt a single macOS Chrome ``v10`` cookie value (AES-128-CBC).

    Only the legacy ``v10`` Keychain-derived scheme is supported. Chrome 127+
    (mid-2024) wraps cookies with App-Bound Encryption (``v20`` prefix), which
    needs a separate key path and is not handled here — those return ``None``
    and are reported in aggregate by ``_extract_macos_cookies``.
    """
    try:
        from Cryptodome.Cipher import AES

        if enc_val[:3] != b"v10":
            return None
        iv = b" " * 16
        dec = AES.new(key, AES.MODE_CBC, iv).decrypt(enc_val[3:])
        # Strip PKCS7 padding.
        pad = dec[-1]
        if 1 <= pad <= 16:
            dec = dec[:-pad]
        # Chrome 80+ prepends a 32-byte SHA256(domain) to the plaintext.
        dec = dec[32:]
        for enc in ("ascii", "utf-8"):
            try:
                s = dec.decode(enc)
                return s or None
            except UnicodeDecodeError:
                continue
        return None
    except Exception as e:
        _log.debug("macOS cookie decrypt failed: %s", type(e).__name__)
        return None


def _extract_macos_cookies() -> dict:
    """Read + decrypt emaplan.com cookies from macOS Chrome. {} on any failure.

    Searches every Chrome channel/profile and returns the first one that yields
    emaplan cookies (the profile the user is logged in to).
    """
    candidates = _macos_cookie_db_candidates()
    if not candidates:
        return {}
    key = _get_chrome_macos_key()
    if key is None:
        return {}

    v20_skipped = 0
    for db_path in candidates:
        cookies: dict = {}
        for name, enc_val, host in _read_macos_cookie_rows(db_path):
            enc_bytes = bytes(enc_val)
            if enc_bytes[:3] == b"v20":
                # App-Bound Encryption (Chrome 127+) — not decryptable via the
                # Keychain PBKDF2 path. Count and skip rather than failing silently.
                v20_skipped += 1
                continue
            val = _decrypt_macos_cookie(enc_bytes, key)
            if val is not None:
                cookies[name] = val
        if cookies:
            return cookies

    if v20_skipped:
        _log.warning(
            "macOS Chrome cookies use App-Bound Encryption (v20, Chrome 127+), which "
            "automatic extraction does not support (%d emaplan cookie(s) skipped). "
            "Falling back to manual login — run sync_chrome_session or reset_session "
            "and sign in via the browser window that opens.",
            v20_skipped,
        )
    return {}


def extract_chrome_emaplan_cookies() -> dict:
    """
    Read Chrome's cookie DB, decrypt emaplan.com cookies, return as dict.
    Dispatches by platform (macOS Keychain/CBC vs Windows DPAPI/GCM).
    Returns {} on any failure (caller falls back to nodriver login).
    """
    import sys
    if sys.platform == "darwin":
        return _extract_macos_cookies()

    if not _CHROME_COOKIE_SRC.exists():
        return {}

    key = _get_chrome_aes_key()
    if key is None:
        return {}

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as _f:
        tmp = _f.name
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
                    ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
                        ctypes.byref(blob_in), None, None, None, None, 0,
                        ctypes.byref(blob_out)
                    )
                    val = ctypes.string_at(blob_out.pbData, blob_out.cbData).decode()
                    ctypes.windll.kernel32.LocalFree(blob_out.pbData)  # type: ignore[attr-defined]
                cookies[name] = val
            except Exception as e:
                _log.debug("Skipping undecryptable cookie %r: %s", name, type(e).__name__)

        return cookies
    except Exception as e:
        _log.debug("Chrome cookie extraction (Windows) failed: %s", type(e).__name__)
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
        self._csrf_token: str | None = None

    def has_cookies(self) -> bool:
        return COOKIE_FILE.exists() and COOKIE_FILE.stat().st_size > 10

    def load_cookies(self) -> dict:
        if COOKIE_FILE.exists():
            try:
                return json.loads(COOKIE_FILE.read_text())
            except Exception as e:
                _log.debug("Could not read session file %s: %s", COOKIE_FILE, type(e).__name__)
        return {}

    def save_cookies(self, cookies: dict) -> None:
        # Session cookies are credential-equivalent (anyone who reads them owns
        # the Emoney session), so restrict access to the owner. The mode passed
        # to os.open only applies when the file is *created* — an existing file
        # keeps its old (possibly looser) permissions — so tighten both the
        # directory and the file explicitly on every write.
        COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(COOKIE_FILE.parent, 0o700)
        except OSError:
            pass
        data = json.dumps(cookies, indent=2).encode()
        fd = os.open(str(COOKIE_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            if hasattr(os, "fchmod"):
                try:
                    os.fchmod(fd, 0o600)  # tighten even a pre-existing file
                except OSError:
                    pass
            os.write(fd, data)
        finally:
            os.close(fd)
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
            final_url = str(resp.url)
            # Must land back on emaplan.com (not a third-party SSO/error domain)
            # AND not be sitting in the signin/OAuth flow.
            return is_emoney_host(final_url) and not _is_signin_url(final_url)
        except Exception:
            return False

    async def get_page_html(self, url: str) -> str:
        http = await self.get_http()
        resp = await http.get(url, allow_redirects=True, timeout=30)
        return resp.text

    async def get_csrf_token(self) -> str | None:
        """
        Fetch the Investments page and extract the ASP.NET anti-forgery token
        from the hidden <input> field.  Result is cached for the session lifetime.

        Returns ``None`` (and does not cache) when the token can't be found —
        e.g. the page layout changed or an error page was served — so callers
        can surface a clear, specific error instead of POSTing an empty token
        and triggering a confusing 403/CSRF mismatch.
        """
        if getattr(self, "_csrf_token", None):
            return self._csrf_token
        import re
        http = await self.get_http()
        resp = await http.get(f"{BASE_URL}/ema/CS/Investments", timeout=20)
        match = re.search(
            r'<input[^>]+name=["\']__RequestVerificationToken["\'][^>]+value=["\']([^"\']+)["\']'
            r'|<input[^>]+value=["\']([^"\']+)["\'][^>]+name=["\']__RequestVerificationToken["\']',
            resp.text, re.IGNORECASE
        )
        if not match:
            return None
        token = match.group(1) or match.group(2)
        self._csrf_token = token
        return token

    def close(self) -> None:
        self._http = None
        self._csrf_token = None


# ---------------------------------------------------------------------------
# nodriver login session — runs in its own OS thread / event loop
# ---------------------------------------------------------------------------

class EmoneyLoginSession:
    def __init__(self):
        self._waiting    = False          # True while browser is open
        self._thread: threading.Thread | None = None
        self._done_event = threading.Event()  # set when cookies are saved
        self._error: str | None = None    # last login-thread failure, surfaced to the caller

    def open_login_window(self) -> None:
        """Spawn a background thread that runs its own asyncio event loop."""
        if self._thread and self._thread.is_alive():
            return  # already running
        self._waiting    = True
        self._error      = None           # clear any prior failure for this fresh attempt
        self._done_event.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True, name="nodriver-login")
        self._thread.start()

    def _thread_main(self) -> None:
        """Entry point for the nodriver thread."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._async_main())
        except Exception as e:
            import traceback
            traceback.print_exc()
            # Record the cause so get_authenticated_session can surface it to the
            # MCP caller instead of leaving them with an opaque login timeout.
            self._error = f"{type(e).__name__}: {e}"
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
        await browser.get(LOGIN_URL)  # navigate (returned tab is unused; tabs are polled below)
        log("on login page")

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


def get_last_login_error() -> str | None:
    """Return the most recent nodriver login-thread failure message, if any."""
    return _login_session._error


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_authenticated_session() -> "EmoneyHttpSession | str":
    """
    Returns an authenticated EmoneyHttpSession or MANUAL_LOGIN_REQUIRED.

    Order of operations:
    1. If saved cookies work, use them (and stop any waiting browser window).
    2. If a browser window is open and waiting, return MANUAL_LOGIN_REQUIRED.
    3. Try extracting cookies directly from the user's running Chrome.
    4. Open nodriver browser window for manual login.
    """
    # 1. Always try saved cookies first — covers: normal resumption, nodriver
    #    just completed, sync_chrome_session was used, or cookies were saved
    #    while a browser window happened to be open (the common stuck state).
    if _http_session.has_cookies():
        if await _http_session.is_logged_in():
            _login_session.stop()          # cancel any pending browser window
            return _http_session
        # Cookies exist but are stale — fall through to re-authenticate

    # 2. Browser window is open — user still logging in
    if _login_session._waiting:
        return MANUAL_LOGIN_REQUIRED

    # 3. Try to extract from the user's live Chrome session (Windows only)
    chrome_cookies = extract_chrome_emaplan_cookies()
    if chrome_cookies:
        _http_session.save_cookies(chrome_cookies)
        if await _http_session.is_logged_in():
            return _http_session

    # 4. Need fresh login via nodriver
    _login_session.open_login_window()
    return MANUAL_LOGIN_REQUIRED


async def close_session() -> None:
    _http_session.close()
    _login_session.stop()
