"""Tests for pure helper functions in browser.py — no network calls needed."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from emoney_mcp.browser import _is_signin_url


class TestIsSigninUrl:
    """_is_signin_url should return True while the user is in the login/OAuth flow."""

    def test_empty_string_is_signin(self):
        assert _is_signin_url("") is True

    def test_signin_in_path(self):
        assert _is_signin_url("https://wealth.emaplan.com/ema/SignIn") is True

    def test_signin_case_insensitive(self):
        assert _is_signin_url("https://wealth.emaplan.com/ema/SIGNIN") is True

    def test_login_in_path(self):
        assert _is_signin_url("https://wealth.emaplan.com/login") is True

    def test_oauth_subdomain(self):
        assert _is_signin_url("https://auth.wealth.emaplan.com/oauth/authorize") is True

    def test_oauth_path(self):
        assert _is_signin_url("https://wealth.emaplan.com/oauth/callback") is True

    def test_authorize_path(self):
        assert _is_signin_url("https://wealth.emaplan.com/authorize?client_id=x") is True

    def test_home_page_is_not_signin(self):
        assert _is_signin_url("https://wealth.emaplan.com/ema/CS/Home") is False

    def test_investments_is_not_signin(self):
        assert _is_signin_url("https://wealth.emaplan.com/ema/CS/Investments") is False

    def test_networth_is_not_signin(self):
        assert _is_signin_url("https://wealth.emaplan.com/ema/CS/NetWorth") is False

    def test_spending_is_not_signin(self):
        assert _is_signin_url("https://wealth.emaplan.com/ema/CS/Spending") is False


class TestSaveCookiesPermissions:
    """save_cookies must keep the session file owner-only (0o600) and the
    directory owner-only (0o700), since session cookies are credential-equivalent.
    Skipped on platforms without POSIX permission semantics."""

    @pytest.mark.skipif(not hasattr(__import__("os"), "fchmod"),
                        reason="POSIX file permissions not enforced on this platform")
    def test_new_file_is_owner_only(self, tmp_path, monkeypatch):
        import os
        import importlib
        import emoney_mcp.browser as browser

        session_file = tmp_path / "nested" / "session.json"
        monkeypatch.setenv("EMONEY_SESSION_FILE", str(session_file))
        importlib.reload(browser)

        browser._http_session.save_cookies({"sess": "secret"})

        assert oct(os.stat(browser.COOKIE_FILE).st_mode & 0o777) == "0o600"
        assert oct(os.stat(browser.COOKIE_FILE.parent).st_mode & 0o777) == "0o700"

        importlib.reload(browser)  # restore module to default env for other tests

    @pytest.mark.skipif(not hasattr(__import__("os"), "fchmod"),
                        reason="POSIX file permissions not enforced on this platform")
    def test_preexisting_loose_file_is_tightened(self, tmp_path, monkeypatch):
        import os
        import importlib
        import emoney_mcp.browser as browser

        session_file = tmp_path / "session.json"
        monkeypatch.setenv("EMONEY_SESSION_FILE", str(session_file))
        importlib.reload(browser)

        # Simulate a pre-existing world-readable session file.
        session_file.write_text("{}")
        os.chmod(session_file, 0o644)
        assert oct(os.stat(session_file).st_mode & 0o777) == "0o644"

        browser._http_session.save_cookies({"sess": "secret"})
        assert oct(os.stat(session_file).st_mode & 0o777) == "0o600"

        importlib.reload(browser)


class TestMacOSCookieDecryption:
    """_decrypt_macos_cookie should round-trip a v10 AES-128-CBC cookie value
    (the Keychain key derivation is not exercised here — only the cipher)."""

    def _encrypt(self, key: bytes, value: bytes, prefix32: bool) -> bytes:
        from Cryptodome.Cipher import AES
        from Cryptodome.Util.Padding import pad
        body = (b"\x11" * 32 + value) if prefix32 else value
        iv = b" " * 16
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(pad(body, 16))
        return b"v10" + ct

    def test_roundtrip_short_value(self):
        from emoney_mcp.browser import _decrypt_macos_cookie
        key = b"0123456789abcdef"  # 16-byte AES-128 key
        enc = self._encrypt(key, b"session-token-xyz", prefix32=True)
        assert _decrypt_macos_cookie(enc, key) == "session-token-xyz"

    def test_roundtrip_long_value(self):
        from emoney_mcp.browser import _decrypt_macos_cookie
        key = b"0123456789abcdef"
        value = b"a" * 200  # spans multiple AES blocks
        enc = self._encrypt(key, value, prefix32=True)
        assert _decrypt_macos_cookie(enc, key) == "a" * 200

    def test_non_v10_returns_none(self):
        from emoney_mcp.browser import _decrypt_macos_cookie
        assert _decrypt_macos_cookie(b"v11garbage", b"0123456789abcdef") is None

    def test_bad_key_does_not_raise(self):
        from emoney_mcp.browser import _decrypt_macos_cookie
        key = b"0123456789abcdef"
        enc = self._encrypt(key, b"value", prefix32=True)
        # Wrong key → garbage/padding error → None, never an exception.
        result = _decrypt_macos_cookie(enc, b"wrongkeywrongkey")
        assert result is None or isinstance(result, str)


class TestAuthDebugLogging:
    """Silent auth/cookie failures should emit a debug log (not crash), and must
    never include cookie values or tokens (#26)."""

    def test_load_cookies_logs_on_corrupt_file(self, tmp_path, monkeypatch, caplog):
        import logging
        import importlib
        import emoney_mcp.browser as browser

        session_file = tmp_path / "session.json"
        session_file.write_text("{ this is not valid json")
        monkeypatch.setenv("EMONEY_SESSION_FILE", str(session_file))
        importlib.reload(browser)

        with caplog.at_level(logging.DEBUG, logger="emoney_mcp.browser"):
            result = browser._http_session.load_cookies()

        assert result == {}  # graceful fallback preserved
        assert any("session file" in r.message for r in caplog.records)
        # The raw (invalid) file contents must not be logged.
        assert all("not valid json" not in r.getMessage() for r in caplog.records)

        importlib.reload(browser)  # restore default env for other tests
