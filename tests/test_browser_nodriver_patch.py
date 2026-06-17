"""Regression test for the nodriver ``aopen`` listener race.

nodriver 0.50.3's ``Connection.aopen`` is a check-then-act race: under
concurrent ``send()`` calls (its own auto-attach handlers fire during
``browser.get()``), two coroutines can both pass the ``if not self.socket``
check and each spawn a ``_listener`` task on the same websocket. websockets
>= 14 then asserts ``cannot call get() concurrently`` and nodriver loops on it
forever. ``browser._patch_nodriver_aopen_race`` serializes ``aopen`` so only a
single socket + listener is ever created. See browser.py for the full writeup.
"""

import asyncio

import nodriver.core.connection as ndconn

from emoney_mcp import browser


def test_patch_is_idempotent_and_swaps_aopen():
    orig = ndconn.Connection.aopen
    try:
        browser._nodriver_patched = False
        browser._patch_nodriver_aopen_race()
        patched = ndconn.Connection.aopen
        assert patched is not orig
        # Second call must be a no-op (does not re-wrap).
        browser._patch_nodriver_aopen_race()
        assert ndconn.Connection.aopen is patched
    finally:
        ndconn.Connection.aopen = orig
        browser._nodriver_patched = False


def test_concurrent_aopen_creates_single_socket_and_listener(monkeypatch):
    """Five racing aopen() calls must yield exactly one connect + one listener."""
    orig = ndconn.Connection.aopen
    connects = {"n": 0}
    listeners = {"n": 0}

    class _FakeSocket:
        close_code = None

    async def _fake_connect(*_a, **_k):
        connects["n"] += 1
        await asyncio.sleep(0.01)  # the yield window that used to interleave
        return _FakeSocket()

    async def _fake_listener(self):
        listeners["n"] += 1
        await asyncio.sleep(0.02)

    monkeypatch.setattr(ndconn.websockets, "connect", _fake_connect)

    class _Conn(ndconn.Connection):
        def __init__(self):
            self.websocket_url = "ws://test"
            self.socket = None
            self._listener_task = None

        _listener = _fake_listener

    try:
        browser._nodriver_patched = False
        browser._patch_nodriver_aopen_race()

        async def _run():
            conn = _Conn()
            await asyncio.gather(*(conn.aopen() for _ in range(5)))
            await asyncio.sleep(0.05)

        asyncio.run(_run())

        assert connects["n"] == 1
        assert listeners["n"] == 1
    finally:
        ndconn.Connection.aopen = orig
        browser._nodriver_patched = False
