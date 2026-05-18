"""Session-sharing regression tests for story L19.

Covers:
- AC#1: the 4 cloud REST sites (``send_login``, ``get_devices``,
  ``get_device_features``, ``post_command_i2d``) use HA's shared session
  via ``async_get_clientsession(hass)`` when the client has a hass
  reference, not a freshly-constructed ``aiohttp.ClientSession``.
- AC#2/#3: ``_get_vortrax_model_from_web`` remains isolated — it must
  NOT use the shared HA pool (third-party site; would leak cookies).
- Static-grep guard: every cloud REST site routes through
  ``self._cloud_session()`` rather than ``aiohttp.ClientSession`` directly,
  and the scraper keeps its own isolated ``aiohttp.ClientSession``. Locks
  in the contract the same way M18 / H9b locked theirs.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from custom_components.iaqualink_robots.coordinator import AqualinkClient


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Async-context-manager mock of an aiohttp response."""

    def __init__(self, *, status: int = 200, payload: dict | None = None, text: str = ""):
        self.status = status
        self._payload = payload or {}
        self._text = text
        self.headers: dict = {"Content-Type": "application/json"}
        self.request_info = MagicMock()
        self.history: tuple = ()

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        return self._text

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *args) -> None:
        return None


class _FakeSharedSession:
    """Mock of an HA shared aiohttp session.

    Important: this does NOT have ``__aenter__``/``__aexit__`` — the shared
    session must be used directly, not via ``async with session: ...``
    (that would close HA's session). The ``_cloud_session`` helper handles
    that lifecycle distinction; this mock proves the helper picked the
    shared branch.
    """

    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.get_calls: list[tuple[tuple, dict]] = []
        self.post_calls: list[tuple[tuple, dict]] = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self._response

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self._response


# ---------------------------------------------------------------------------
# AC#1 — the 4 cloud sites use the shared HA session when hass is set.
# ---------------------------------------------------------------------------


async def test_send_login_uses_shared_ha_session(monkeypatch) -> None:
    """``send_login`` must fetch via ``async_get_clientsession(hass)`` —
    not by constructing a fresh ``aiohttp.ClientSession``.
    """
    fake_response = _FakeResponse(status=200, payload={"ok": True})
    shared_session = _FakeSharedSession(fake_response)

    fake_hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: (shared_session if hass is fake_hass else pytest.fail("wrong hass")),
    )

    # Tripwire — if the code falls back to ``aiohttp.ClientSession()`` despite
    # hass being set, this raises and the test fails with a clear message.
    def _no_ephemeral_session(*args, **kwargs):
        raise AssertionError(
            "send_login fell back to aiohttp.ClientSession despite hass being set — "
            "the _cloud_session branch is broken (L19 AC#1)"
        )

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _no_ephemeral_session,
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)

    result = await client.send_login(json.dumps({"email": "u"}), {"X-Test": "1"})

    assert result == {"ok": True}
    assert len(shared_session.post_calls) == 1, "send_login must go through the shared session"
    # Headers must be on the request (not the session) — leakage guard.
    _args, kwargs = shared_session.post_calls[0]
    assert kwargs.get("headers") == {"X-Test": "1"}, (
        "send_login must pass headers per-request when using the shared HA session, "
        "not by mutating the session — auth-token leakage risk"
    )


async def test_get_devices_uses_shared_ha_session(monkeypatch) -> None:
    """``get_devices`` must use the shared HA session."""
    fake_response = _FakeResponse(status=200, payload=[{"serial_number": "X"}])
    shared_session = _FakeSharedSession(fake_response)

    fake_hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: shared_session,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        lambda *a, **kw: pytest.fail("get_devices must not construct ephemeral session"),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)

    result = await client.get_devices({"authentication_token": "T"}, {"X-Test": "1"})

    assert result == [{"serial_number": "X"}]
    assert len(shared_session.get_calls) == 1
    _args, kwargs = shared_session.get_calls[0]
    assert kwargs.get("headers") == {"X-Test": "1"}


async def test_get_device_features_uses_shared_ha_session(monkeypatch) -> None:
    """``get_device_features`` must use the shared HA session, with the
    Authorization header attached per-request (so a retry after force-auth
    picks up the rotated id_token)."""
    fake_response = _FakeResponse(status=200, payload={"model": "M"})
    shared_session = _FakeSharedSession(fake_response)

    fake_hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: shared_session,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        lambda *a, **kw: pytest.fail("get_device_features must not construct ephemeral session"),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)
    client._id_token = "tok-1"

    await client.get_device_features("https://example/features")

    assert len(shared_session.get_calls) == 1
    _args, kwargs = shared_session.get_calls[0]
    assert kwargs.get("headers") == {"Authorization": "tok-1"}


async def test_post_command_i2d_uses_shared_ha_session(monkeypatch) -> None:
    """``post_command_i2d`` must use the shared HA session."""
    fake_response = _FakeResponse(status=200, payload={"command": {"request": "OK"}})
    shared_session = _FakeSharedSession(fake_response)

    fake_hass = MagicMock()
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: shared_session,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        lambda *a, **kw: pytest.fail("post_command_i2d must not construct ephemeral session"),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)
    client._id_token = "tok-1"
    client._api_key = "api-key-1"

    await client.post_command_i2d("https://example/control", {"command": "/x"})

    assert len(shared_session.post_calls) == 1
    _args, kwargs = shared_session.post_calls[0]
    assert kwargs.get("headers") == {"Authorization": "tok-1", "api_key": "api-key-1"}


# ---------------------------------------------------------------------------
# AC#1 — fallback path: with no hass, falls back to ephemeral ClientSession.
# Critical for config-flow discovery (`AqualinkClient.discover_devices`
# constructs a bare client without hass) and for the existing test_auth.py
# patterns that monkeypatch `aiohttp.ClientSession`.
# ---------------------------------------------------------------------------


async def test_send_login_falls_back_to_ephemeral_without_hass(monkeypatch) -> None:
    """When ``self._hass`` is None, the helper must construct a one-shot
    ``aiohttp.ClientSession`` — required so config-flow discovery still
    works and so the existing ``test_auth.py`` mock pattern stays valid.
    """
    fake_response = _FakeResponse(status=200, payload={"ok": True})
    constructed_sessions: list = []

    class _FakeEphemeralSession:
        def __init__(self, **kwargs) -> None:
            constructed_sessions.append(kwargs)
            self._headers_at_construction = kwargs.get("headers")

        def post(self, *args, **kwargs):
            return fake_response

        def get(self, *args, **kwargs):
            return fake_response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _FakeEphemeralSession,
    )
    # If async_get_clientsession is called when hass is None, that's a bug —
    # the helper must take the fallback branch.
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: pytest.fail("async_get_clientsession called with None hass"),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    assert client._hass is None

    result = await client.send_login(json.dumps({"email": "u"}), {"X-Test": "1"})

    assert result == {"ok": True}
    assert len(constructed_sessions) == 1, (
        "with no hass, send_login must construct exactly one ephemeral aiohttp session"
    )


# ---------------------------------------------------------------------------
# AC#2/#3 — Vortrax scraper stays isolated from the HA shared pool.
# ---------------------------------------------------------------------------


async def test_vortrax_scraper_does_not_use_shared_session(monkeypatch) -> None:
    """``_get_vortrax_model_from_web`` must NOT reach for the shared HA
    pool — that would leak HA cookies/headers (and any iAqualink auth
    tokens routed through the shared pool by mistake) to a third-party
    domain.
    """
    constructed_sessions: list = []

    class _FakeEphemeralSession:
        def __init__(self, **kwargs) -> None:
            constructed_sessions.append(kwargs)

        def get(self, *args, **kwargs):
            return _FakeResponse(status=404)  # forces the all-URLs-fail path

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _FakeEphemeralSession,
    )
    # The scraper must not consult the shared session — any call is a bug.
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: pytest.fail(
            "Vortrax scraper called async_get_clientsession — third-party leak risk (L19 AC#2)"
        ),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    # Even WITH hass set, the scraper must stay isolated — this is the
    # key adversarial check from the story.
    client.set_hass(MagicMock())

    result = await client._get_vortrax_model_from_web("PN-123")

    assert result == "VortraX (PN: PN-123)", "scraper fell off the all-URLs-fail path"
    assert len(constructed_sessions) >= 1, (
        "scraper must construct its own ephemeral aiohttp session, not use the shared pool"
    )


# ---------------------------------------------------------------------------
# Source-level guard: every cloud REST site must go through
# ``self._cloud_session()`` rather than ``aiohttp.ClientSession`` directly.
# The scraper and the WS session are explicitly allowed to keep using
# ``aiohttp.ClientSession`` — they have different lifecycle / isolation
# requirements and are listed by name in the exception set below.
# ---------------------------------------------------------------------------


def test_cloud_sites_routed_through_cloud_session() -> None:
    """Static-grep guard. If a future refactor adds a 5th cloud REST site that
    uses ``aiohttp.ClientSession(`` directly, this test fires.

    Locked-in pattern: only three named sites may construct an
    ``aiohttp.ClientSession`` directly:

    1. ``_cloud_session``        — the helper's own ephemeral fallback for
                                   the no-hass branch (config-flow discovery
                                   + unit tests).
    2. ``_ensure_websocket_connection`` — the persistent websocket session,
                                   owned by the client and closed via
                                   ``_close_websocket``.
    3. ``_get_vortrax_model_from_web`` — third-party Vortrax web scraper,
                                   intentionally isolated to avoid leaking
                                   HA cookies/headers to zodiac-poolcare.com.

    Every other ClientSession() construction must route via
    ``self._cloud_session()`` (story L19).
    """
    coordinator_src = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    ).read_text(encoding="utf-8")

    # Find every line that constructs an aiohttp.ClientSession() (any args).
    # Skip comment lines so docstring/comment references don't trip the guard.
    pattern = re.compile(r"^\s*[^#\n]*aiohttp\.ClientSession\s*\(", re.MULTILINE)
    matches = pattern.findall(coordinator_src)

    # Pre-L19 there were 6 construction sites: WS + 4 cloud + scraper.
    # Post-L19 only 3 are permitted: WS + scraper + the fallback inside
    # ``_cloud_session``. The exact count locks in the contract.
    assert len(matches) == 3, (
        f"expected exactly 3 direct aiohttp.ClientSession constructions in coordinator.py "
        f"(_cloud_session fallback + websocket + scraper), found {len(matches)}: {matches}. "
        f"New cloud REST sites must route through self._cloud_session() — see story L19."
    )

    # Belt and braces: each of the three allowed methods must still exist.
    # If one is renamed, update this test alongside the rename so the
    # diagnostic stays accurate.
    for marker in (
        "async def _cloud_session",
        "async def _ensure_websocket_connection",
        "async def _get_vortrax_model_from_web",
    ):
        assert marker in coordinator_src, (
            f"expected method marker {marker!r} not found — was the method renamed? "
            f"Update this test alongside the rename."
        )
