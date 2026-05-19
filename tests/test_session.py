"""Session-sharing regression tests for story L19.

Covers:
- AC#1: the 4 cloud REST sites (``send_login``, ``get_devices``,
  ``get_device_features``, ``post_command_i2d``) reuse HA's pooled
  connector via ``async_get_clientsession(hass).connector`` when the
  client has a hass reference, NOT a freshly-constructed connector.
- AC#2/#3: ``_get_vortrax_model_from_web`` remains isolated — it must
  NOT consult ``async_get_clientsession`` (third-party site; would leak
  cookies AND HA's connector to that domain).
- L19 review F1 (cookie-jar isolation): the cloud-call wrapper session
  has its own ``aiohttp.DummyCookieJar``, NOT HA's shared cookie jar.
  Symmetric to the original AC#1 defence that moved auth headers from
  session-level to per-request — same risk shape, same defence.
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

import aiohttp
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


class _FakeHASession:
    """Mock of the session ``async_get_clientsession(hass)`` returns.

    L19 review F1: the helper no longer yields this session directly —
    it wraps it via ``aiohttp.ClientSession(connector=ha.connector,
    connector_owner=False, cookie_jar=DummyCookieJar())`` so cookies set
    on iAqualink responses cannot persist on HA's shared cookie jar and
    bleed into other integrations. This mock exposes ``connector`` and
    ``cookie_jar`` so tests can assert the wrapper reuses HA's connector
    while having its own private jar.
    """

    def __init__(self) -> None:
        # Use distinct sentinel objects so identity checks are meaningful.
        self.connector = MagicMock(name="ha_connector")
        self.cookie_jar = MagicMock(name="ha_cookie_jar")


def _make_wrapper_factory(fake_response: _FakeResponse, *, constructed: list):
    """Build a stand-in for ``aiohttp.ClientSession``.

    Each construction records its kwargs (so tests can assert
    ``connector`` / ``connector_owner`` / ``cookie_jar``) and records its
    ``get`` / ``post`` calls (so tests can assert headers-per-request).
    The instance supports ``async with`` (the helper enters it that way).
    """

    class _FakeWrapperSession:
        def __init__(self, **kwargs) -> None:
            self.construction_kwargs = kwargs
            self.get_calls: list[tuple[tuple, dict]] = []
            self.post_calls: list[tuple[tuple, dict]] = []
            constructed.append(self)

        def get(self, *args, **kwargs):
            self.get_calls.append((args, kwargs))
            return fake_response

        def post(self, *args, **kwargs):
            self.post_calls.append((args, kwargs))
            return fake_response

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    return _FakeWrapperSession


# ---------------------------------------------------------------------------
# AC#1 — the 4 cloud sites use a wrapper around HA's connector (hass set).
# ---------------------------------------------------------------------------


def _assert_uses_ha_connector_with_private_jar(
    wrapper_kwargs: dict, fake_ha: _FakeHASession
) -> None:
    """Shared assertions for the 4 cloud-site tests.

    Locks the L19 + L19-review-F1 contract: the wrapper session must
    reuse HA's connector (pooling win), explicitly disclaim ownership of
    it (HA still owns lifecycle), and use a private ``DummyCookieJar``
    (cookie-leakage defence — symmetric to the header defence).
    """
    assert wrapper_kwargs.get("connector") is fake_ha.connector, (
        "cloud wrapper must reuse HA's connector — the whole pooling win "
        "depends on this (L19 AC#1)"
    )
    assert wrapper_kwargs.get("connector_owner") is False, (
        "cloud wrapper must NOT own HA's connector — closing the wrapper "
        "must not close HA's connector (L19 AC#1)"
    )
    cookie_jar = wrapper_kwargs.get("cookie_jar")
    assert isinstance(cookie_jar, aiohttp.DummyCookieJar), (
        "cloud wrapper must use DummyCookieJar so cookies set on "
        "iAqualink responses cannot persist on HA's shared cookie jar "
        "(L19 review F1 — symmetric to the header-leak defence)"
    )
    assert cookie_jar is not fake_ha.cookie_jar, (
        "cloud wrapper's cookie jar must NOT be the same object as HA's "
        "shared cookie jar"
    )


async def test_send_login_uses_wrapped_ha_connector(monkeypatch) -> None:
    """``send_login`` must wrap HA's connector + use a private cookie jar."""
    fake_response = _FakeResponse(status=200, payload={"ok": True})
    fake_ha = _FakeHASession()
    constructed: list = []
    fake_hass = MagicMock()

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: (fake_ha if hass is fake_hass else pytest.fail("wrong hass")),
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)

    result = await client.send_login(json.dumps({"email": "u"}), {"X-Test": "1"})

    assert result == {"ok": True}
    assert len(constructed) == 1, "send_login must construct exactly one wrapper session"
    wrapper = constructed[0]
    _assert_uses_ha_connector_with_private_jar(wrapper.construction_kwargs, fake_ha)
    assert len(wrapper.post_calls) == 1, "wrapper.post must be called once"
    _args, kwargs = wrapper.post_calls[0]
    assert kwargs.get("headers") == {"X-Test": "1"}, (
        "headers must travel on the request, not the session — auth-token "
        "leakage guard preserved through the wrapper"
    )


async def test_get_devices_uses_wrapped_ha_connector(monkeypatch) -> None:
    """``get_devices`` must wrap HA's connector + use a private cookie jar."""
    fake_response = _FakeResponse(status=200, payload=[{"serial_number": "X"}])
    fake_ha = _FakeHASession()
    constructed: list = []
    fake_hass = MagicMock()

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: fake_ha,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)

    result = await client.get_devices({"authentication_token": "T"}, {"X-Test": "1"})

    assert result == [{"serial_number": "X"}]
    assert len(constructed) == 1
    wrapper = constructed[0]
    _assert_uses_ha_connector_with_private_jar(wrapper.construction_kwargs, fake_ha)
    assert len(wrapper.get_calls) == 1
    _args, kwargs = wrapper.get_calls[0]
    assert kwargs.get("headers") == {"X-Test": "1"}


async def test_get_device_features_uses_wrapped_ha_connector(monkeypatch) -> None:
    """``get_device_features`` must wrap HA's connector + use a private cookie
    jar; Authorization header travels per-request so the retry path picks
    up the rotated ``_id_token`` cleanly."""
    fake_response = _FakeResponse(status=200, payload={"model": "M"})
    fake_ha = _FakeHASession()
    constructed: list = []
    fake_hass = MagicMock()

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: fake_ha,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)
    client._id_token = "tok-1"

    await client.get_device_features("https://example/features")

    assert len(constructed) == 1
    wrapper = constructed[0]
    _assert_uses_ha_connector_with_private_jar(wrapper.construction_kwargs, fake_ha)
    assert len(wrapper.get_calls) == 1
    _args, kwargs = wrapper.get_calls[0]
    assert kwargs.get("headers") == {"Authorization": "tok-1"}


async def test_post_command_i2d_uses_wrapped_ha_connector(monkeypatch) -> None:
    """``post_command_i2d`` must wrap HA's connector + use a private cookie jar."""
    fake_response = _FakeResponse(status=200, payload={"command": {"request": "OK"}})
    fake_ha = _FakeHASession()
    constructed: list = []
    fake_hass = MagicMock()

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: fake_ha,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)
    client._id_token = "tok-1"
    client._api_key = "api-key-1"

    await client.post_command_i2d("https://example/control", {"command": "/x"})

    assert len(constructed) == 1
    wrapper = constructed[0]
    _assert_uses_ha_connector_with_private_jar(wrapper.construction_kwargs, fake_ha)
    assert len(wrapper.post_calls) == 1
    _args, kwargs = wrapper.post_calls[0]
    assert kwargs.get("headers") == {"Authorization": "tok-1", "api_key": "api-key-1"}


# ---------------------------------------------------------------------------
# L19 review F1 — dedicated cookie-jar isolation test.
#
# This is the test that pins the cookie-leakage defence in isolation
# from the per-site tests above. Two sequential cloud calls produce two
# wrappers, each with its OWN DummyCookieJar — so any Set-Cookie on
# call N can never affect call N+1, and neither can affect HA's pool.
# ---------------------------------------------------------------------------


async def test_cloud_wrappers_have_isolated_private_cookie_jars(monkeypatch) -> None:
    """Two consecutive cloud calls must construct two wrapper sessions,
    each with its OWN ``DummyCookieJar`` instance — never HA's shared
    jar, never the same jar across calls. Guards the symmetric leak to
    the header-leak defence (story L19 review finding F1)."""
    fake_response = _FakeResponse(status=200, payload={"ok": True})
    fake_ha = _FakeHASession()
    constructed: list = []
    fake_hass = MagicMock()

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.async_get_clientsession",
        lambda hass: fake_ha,
    )
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
    )

    client = AqualinkClient(username="u", password="p", api_key="k")
    client.set_hass(fake_hass)

    # Two cloud calls.
    await client.send_login(json.dumps({"email": "u"}), {"X-1": "1"})
    await client.get_devices({"authentication_token": "T"}, {"X-2": "2"})

    assert len(constructed) == 2, "expected one wrapper per cloud call"
    jars = [w.construction_kwargs.get("cookie_jar") for w in constructed]
    for jar in jars:
        assert isinstance(jar, aiohttp.DummyCookieJar), (
            "every cloud wrapper must use a DummyCookieJar — no exceptions"
        )
        assert jar is not fake_ha.cookie_jar, (
            "wrapper jar must never be HA's shared cookie jar"
        )
    assert jars[0] is not jars[1], (
        "wrappers must have DISTINCT DummyCookieJar instances — sharing "
        "the same instance across calls would re-introduce cookie "
        "persistence and defeat the isolation"
    )


# ---------------------------------------------------------------------------
# AC#1 — fallback path: with no hass, falls back to ephemeral ClientSession
# (no connector, no DummyCookieJar). Required so config-flow discovery
# (AqualinkClient.discover_devices) and existing test_auth.py monkeypatch
# patterns keep working.
# ---------------------------------------------------------------------------


async def test_send_login_falls_back_to_ephemeral_without_hass(monkeypatch) -> None:
    """When ``self._hass`` is None, the helper must construct a one-shot
    ``aiohttp.ClientSession`` with NO ``connector`` kwarg (so it builds
    its own) and NO ``cookie_jar`` kwarg (uses aiohttp's default — fine
    for ephemeral sessions whose jar dies on close)."""
    fake_response = _FakeResponse(status=200, payload={"ok": True})
    constructed: list = []

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
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
    assert len(constructed) == 1, (
        "with no hass, send_login must construct exactly one ephemeral aiohttp session"
    )
    wrapper = constructed[0]
    # Fallback path takes the no-connector branch — no HA pool to reuse.
    assert "connector" not in wrapper.construction_kwargs, (
        "fallback session must NOT reuse a connector (no hass means no HA pool)"
    )
    assert "cookie_jar" not in wrapper.construction_kwargs, (
        "fallback session uses aiohttp's default cookie jar — fine because "
        "the session closes on context exit so the jar can't persist"
    )


# ---------------------------------------------------------------------------
# AC#2/#3 — Vortrax scraper stays isolated from the HA shared pool.
# ---------------------------------------------------------------------------


async def test_vortrax_scraper_does_not_use_shared_session(monkeypatch) -> None:
    """``_get_vortrax_model_from_web`` must NOT consult
    ``async_get_clientsession`` — that would leak HA's connector AND
    cookies/headers to a third-party domain (zodiac-poolcare.com).

    The scraper constructs its own ``aiohttp.ClientSession(timeout=...)``
    with NO ``connector`` kwarg (so it doesn't share HA's pool) and
    NO ``cookie_jar`` kwarg (the ephemeral default is fine — closes on
    exit).
    """
    fake_response = _FakeResponse(status=404)  # all-URLs-fail path
    constructed: list = []

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _make_wrapper_factory(fake_response, constructed=constructed),
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
    assert len(constructed) >= 1, (
        "scraper must construct its own ephemeral aiohttp session, not use the shared pool"
    )
    # The scraper's session has neither connector nor cookie_jar — pure ephemeral.
    for wrapper in constructed:
        assert "connector" not in wrapper.construction_kwargs, (
            "scraper session must NOT share HA's connector"
        )


# ---------------------------------------------------------------------------
# Source-level guard: every cloud REST site must go through
# ``self._cloud_session()`` rather than ``aiohttp.ClientSession`` directly.
# Pre-L19: 6 construction sites (4 cloud + WS + scraper).
# Post-L19: 4 (WS + scraper + helper's 2 branches — shared-wrapper + ephemeral).
# ---------------------------------------------------------------------------


def test_cloud_sites_routed_through_cloud_session() -> None:
    """Static-grep guard. If a future refactor adds a 5th cloud REST site that
    uses ``aiohttp.ClientSession(`` directly, this test fires.

    Locked-in pattern: only four named sites may construct an
    ``aiohttp.ClientSession`` directly:

    1. ``_cloud_session`` (shared branch) — the wrapper that reuses HA's
                                   connector with a private DummyCookieJar.
                                   New site added by L19 review F1.
    2. ``_cloud_session`` (fallback branch) — the no-hass ephemeral
                                   path for config-flow discovery and
                                   unit tests.
    3. ``_ensure_websocket_connection`` — the persistent websocket
                                   session, owned by the client and
                                   closed via ``_close_websocket``.
    4. ``_get_vortrax_model_from_web`` — third-party Vortrax web
                                   scraper, intentionally isolated to
                                   avoid leaking HA cookies/headers/
                                   connector to zodiac-poolcare.com.

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
    # Post-L19 (with cookie-jar isolation) only 4 are permitted: WS +
    # scraper + helper's two branches (shared-wrapper + ephemeral fallback).
    # The exact count locks in the contract.
    assert len(matches) == 4, (
        f"expected exactly 4 direct aiohttp.ClientSession constructions in coordinator.py "
        f"(_cloud_session shared-wrapper + _cloud_session fallback + websocket + scraper), "
        f"found {len(matches)}: {matches}. "
        f"New cloud REST sites must route through self._cloud_session() — see story L19."
    )

    # Belt and braces: each of the allowed methods must still exist.
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
