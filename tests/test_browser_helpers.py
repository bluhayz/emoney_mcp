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
