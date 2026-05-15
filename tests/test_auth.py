"""Auth-failure regression tests for story H9b.

Covers:
- AC#1: REST 401 raises ``ConfigEntryAuthFailed`` from the coordinator boundary.
- AC#3: WS handshake 401 raises ``ConfigEntryAuthFailed`` via the coordinator —
  exercises the real ``aiohttp.WSServerHandshakeError(status=401)`` branch in
  ``_ensure_websocket_connection``, not just the coordinator translation. The
  fragile ``"401" in str(e)`` substring match is locked out via a source-grep
  guard (same pattern M18 used for the wall_only/walls_only distinction).
- Absorbed H9a finding: ``_authenticate`` is serialised under an
  ``asyncio.Lock`` so two concurrent callers don't both hit Cognito. The
  serialization test uses an ``asyncio.Event`` gate so the assertion would
  fail if the lock were removed (the in-method recheck alone is not enough).
- Absorbed H9a finding: the JWT-exp fallback WARN is rate-limited (one-shot
  then DEBUG) to prevent log flooding on a persistently-malformed token shape.
- H9b review D2: REST 401 retry-once mitigation actually fires a real
  ``_authenticate(force=True)`` round-trip, not a recheck-skipped no-op.
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.iaqualinkrobots.coordinator import (
    AqualinkClient,
    AqualinkDataUpdateCoordinator,
    AuthFailedError,
)


def _make_jwt(payload: dict) -> str:
    """Build a syntactically-valid JWT with the given payload claims.

    Matches the helper in tests/test_jwt.py — kept local here to keep this
    suite freestanding (no cross-test imports).
    """
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.fake-signature"


def _fake_login_response() -> dict:
    """Build the dict shape that `send_login` returns on success."""
    return {
        "first_name": "T",
        "last_name": "U",
        "id": "1",
        "authentication_token": "fresh-auth-tok",
        "userPoolOAuth": {"IdToken": _make_jwt({"exp": 2_524_608_000})},
        "cognitoPool": {"appClientId": "cid"},
    }


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _build_mock_client(*, fetch_status_side_effect=None) -> MagicMock:
    """Build a MagicMock AqualinkClient with the minimum surface the
    coordinator's ``_async_update_data`` exercises.
    """
    client = MagicMock()
    client.robot_id = "test_robot"
    client._device_type = "vr"
    client._pending_stop_reset = False
    client.set_hass = MagicMock()
    client.set_coordinator_callback = MagicMock()
    client.set_coordinator_reference = MagicMock()
    client.fetch_status = AsyncMock(side_effect=fetch_status_side_effect)
    client.clear_desired_state = AsyncMock()
    client._reset_websocket_failures = MagicMock()
    return client


@pytest.fixture
def coordinator_with_client(hass: HomeAssistant):
    """Build a coordinator + mock client wired the way the production setup does."""

    def _make(fetch_status_side_effect):
        client = _build_mock_client(fetch_status_side_effect=fetch_status_side_effect)
        coord = AqualinkDataUpdateCoordinator(hass, client, interval=3.0)
        coord._last_data = {}
        coord._setup_complete = True  # skip the first-call branch
        coord._update_polling_interval = MagicMock()
        return coord, client

    return _make


# ---------------------------------------------------------------------------
# AC#1 — REST 401 translates to ConfigEntryAuthFailed at the coordinator.
# ---------------------------------------------------------------------------


async def test_401_raises_config_entry_auth_failed(coordinator_with_client) -> None:
    """A REST layer raising ``AuthFailedError`` (e.g. from ``get_devices``)
    must surface as ``ConfigEntryAuthFailed`` so HA dispatches reauth.
    """
    coord, _client = coordinator_with_client(
        fetch_status_side_effect=AuthFailedError("401 Unauthorized on /get_devices"),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


# ---------------------------------------------------------------------------
# AC#3 — WS handshake 401 drives the REAL `_ensure_websocket_connection`
# code path, not just the coordinator-level translation. H9b review P3.
# ---------------------------------------------------------------------------


def _make_handshake_error(status: int) -> aiohttp.WSServerHandshakeError:
    """Construct a typed `aiohttp.WSServerHandshakeError` with the given status."""
    return aiohttp.WSServerHandshakeError(
        request_info=MagicMock(),
        history=(),
        status=status,
        message=f"HTTP {status}",
        headers={},
    )


async def test_ws_handshake_401_drives_auth_failed_after_retry(monkeypatch) -> None:
    """End-to-end: a real `WSServerHandshakeError(status=401)` raised by
    `ws_connect` must trip the typed-check branch, force a re-auth, retry, and
    on a second 401 raise `AuthFailedError`. Coordinator boundary then
    translates that to `ConfigEntryAuthFailed` (covered by the other test).
    """
    client = AqualinkClient(username="u@example.com", password="pw", api_key="k")
    # Pre-populate a locally-valid token so the entry gate at the top of
    # _ensure_websocket_connection doesn't pre-emptively re-auth — we want to
    # exercise the WS handshake retry branch specifically.
    client._id_token = _make_jwt({"exp": 2_524_608_000})
    client._auth_token = "fake-auth"
    client._token_expires_at = (
        datetime.datetime.now() + datetime.timedelta(hours=1)
    )

    # Mock send_login so the WS 401 retry's _authenticate(force=True) doesn't
    # actually hit Cognito.
    send_login_calls = 0

    async def fake_send_login(data, headers):
        nonlocal send_login_calls
        send_login_calls += 1
        return _fake_login_response()

    monkeypatch.setattr(client, "send_login", fake_send_login)

    # Build a fake aiohttp.ClientSession whose ws_connect raises 401 every time.
    fake_session = MagicMock()
    fake_session.ws_connect = AsyncMock(side_effect=_make_handshake_error(401))
    fake_session.closed = False
    fake_session.close = AsyncMock()

    def fake_session_factory(*args, **kwargs):
        return fake_session

    monkeypatch.setattr(aiohttp, "ClientSession", fake_session_factory)
    # Patch asyncio.sleep to no-op so the test doesn't wait for backoff.
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with pytest.raises(AuthFailedError):
        await client._ensure_websocket_connection()

    # The retry path must have forced a Cognito refresh — proves the
    # force=True wiring is exercised end-to-end.
    assert send_login_calls >= 1, "WS 401 retry should have forced a re-auth"


def test_string_match_removed() -> None:
    """Source-level guard: ``"401" in <expr>`` must not reappear in coordinator.py.

    Locks in the AC#3 contract the same way M18 locked the wall_only /
    walls_only distinction — if a future refactor reintroduces the fragile
    substring match, this test fails loudly.

    Sign-off P3: regex covers BOTH single- and double-quoted variants so a
    quote-style refactor cannot accidentally bypass the guard.
    """
    import re

    coordinator_src = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    ).read_text(encoding="utf-8")

    # Matches: "401" in <expr>, '401' in <expr>, "401" in str(...), etc.
    # The bracket character class catches both quote styles in one pattern.
    pattern = re.compile(r"""['"]401['"]\s+in\s+""")
    matches = pattern.findall(coordinator_src)
    assert not matches, (
        "Substring match on '401' was reintroduced — H9b AC#3 requires the typed "
        "aiohttp.WSServerHandshakeError(status=401) check instead. "
        f"Found {len(matches)} matche(s)."
    )


# ---------------------------------------------------------------------------
# Sign-off P1 — send_login must surface Cognito-side credential rejection as
# AuthFailedError so the coordinator boundary triggers reauth. Three rejection
# shapes are covered: explicit 401, explicit 403, and a 200-shaped response
# whose body is missing the expected ``userPoolOAuth``/``authentication_token``
# envelope (Cognito's ``NotAuthorizedException`` response).
# ---------------------------------------------------------------------------


async def test_send_login_401_raises_auth_failed(monkeypatch) -> None:
    """A 401 response from Cognito must raise ``AuthFailedError`` so the
    coordinator translates it to ``ConfigEntryAuthFailed`` and HA dispatches
    reauth.
    """
    client = AqualinkClient(username="u", password="p", api_key="k")

    class _FakeResponse:
        status = 401
        headers: dict = {}
        request_info = MagicMock()
        history: tuple = ()

        async def text(self) -> str:
            return ""

        async def json(self) -> dict:
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    class _FakeSession:
        def __init__(self, **kwargs) -> None:
            pass

        def post(self, *args, **kwargs):
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _FakeSession,
    )

    with pytest.raises(AuthFailedError, match="401"):
        await client.send_login(json.dumps({"email": "u"}), {})


async def test_send_login_403_raises_auth_failed(monkeypatch) -> None:
    """A 403 response from Cognito must raise ``AuthFailedError`` (not
    ``ClientResponseError`` — that was the pre-sign-off behaviour and meant
    the coordinator broad-except absorbed it as transient noise).
    """
    client = AqualinkClient(username="u", password="p", api_key="k")

    class _FakeResponse:
        status = 403
        headers: dict = {}
        request_info = MagicMock()
        history: tuple = ()

        async def text(self) -> str:
            return ""

        async def json(self) -> dict:
            return {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    class _FakeSession:
        def __init__(self, **kwargs) -> None:
            pass

        def post(self, *args, **kwargs):
            return _FakeResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

    monkeypatch.setattr(
        "custom_components.iaqualink_robots.coordinator.aiohttp.ClientSession",
        _FakeSession,
    )

    with pytest.raises(AuthFailedError, match="403"):
        await client.send_login(json.dumps({"email": "u"}), {})


async def test_authenticate_missing_keys_raises_auth_failed(monkeypatch) -> None:
    """A 200-shaped Cognito response with a body that is missing the expected
    envelope (e.g. ``NotAuthorizedException``) must surface as
    ``AuthFailedError`` from ``_authenticate``, not propagate as ``KeyError``.

    This closes the password-rotation reauth gap: before sign-off P1, a
    rotated-password retry path landed in ``_authenticate`` → KeyError →
    coordinator broad-except → transient-failure ladder → no reauth.
    """
    client = AqualinkClient(username="u", password="p", api_key="k")

    async def fake_send_login(data, headers):
        # Cognito's NotAuthorizedException response shape — JSON but
        # without the expected ``userPoolOAuth`` / ``authentication_token``
        # keys.
        return {"__type": "NotAuthorizedException", "message": "Incorrect username or password."}

    monkeypatch.setattr(client, "send_login", fake_send_login)

    with pytest.raises(AuthFailedError, match="unexpected body shape"):
        await client._authenticate(force=True)


# ---------------------------------------------------------------------------
# Absorbed H9a finding — concurrent _authenticate is serialised under a lock.
# H9b review P5 — strengthened test: uses an asyncio.Event gate so the second
# caller is actually waiting on the LOCK, not on the in-method recheck.
# ---------------------------------------------------------------------------


async def test_authenticate_serialized_under_lock(monkeypatch) -> None:
    """Two concurrent _authenticate() callers must serialise on the lock.

    The test holds the first caller inside `send_login` via an asyncio.Event
    and assigns work to the second caller while the first is still mid-flight.
    If the lock were removed, the second caller's `send_login` would also be
    invoked (we'd see send_login_calls == 2 before releasing the gate). With
    the lock in place, the second waits at `async with self._auth_lock:` and
    only proceeds after the first completes — by which time the in-method
    recheck makes it a no-op (send_login_calls stays at 1).
    """
    client = AqualinkClient(
        username="user@example.com",
        password="pw",
        api_key="key",
    )
    assert not client._auth_token
    assert client._is_token_expired()

    send_login_calls = 0
    enter_event = asyncio.Event()  # first caller signals it has entered send_login
    release_event = asyncio.Event()  # test releases first caller

    async def fake_send_login(data, headers):
        nonlocal send_login_calls
        send_login_calls += 1
        enter_event.set()
        await release_event.wait()  # block until the test says go
        return _fake_login_response()

    monkeypatch.setattr(client, "send_login", fake_send_login)

    # Schedule both tasks. First task enters send_login and blocks on
    # release_event. Second task hits the lock and blocks there.
    task1 = asyncio.create_task(client._authenticate())
    await enter_event.wait()
    # Now first task is inside send_login; second task is queued behind the lock.
    task2 = asyncio.create_task(client._authenticate())
    # Give task2 a chance to attempt the lock and block. If the lock weren't
    # there, task2 would enter send_login here and bump send_login_calls to 2
    # BEFORE we release the gate.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert send_login_calls == 1, (
        f"Second caller entered send_login while the first was still running "
        f"(calls={send_login_calls}). The asyncio.Lock around _authenticate "
        f"is missing or broken — the in-method recheck cannot catch this case "
        f"because the first caller has not yet written _id_token."
    )

    # Release first caller; second caller now proceeds, sees a valid token via
    # the in-method recheck, and short-circuits.
    release_event.set()
    await asyncio.gather(task1, task2)

    assert send_login_calls == 1, (
        f"After release, second caller should have short-circuited via the "
        f"in-method recheck (got {send_login_calls} send_login calls)"
    )


async def test_force_authenticate_bypasses_recheck(monkeypatch) -> None:
    """`_authenticate(force=True)` must do a Cognito round-trip even when the
    local token still looks valid — the spec's "refresh-then-retry" mitigation
    depends on this (a cloud-side revocation looks locally-valid until the
    response says otherwise).
    """
    client = AqualinkClient(username="u", password="p", api_key="k")
    # Pre-populate a locally-valid token. The default recheck would skip auth.
    client._id_token = _make_jwt({"exp": 2_524_608_000})
    client._auth_token = "stale-but-locally-valid"
    client._token_expires_at = (
        datetime.datetime.now() + datetime.timedelta(hours=1)
    )

    send_login_calls = 0

    async def fake_send_login(data, headers):
        nonlocal send_login_calls
        send_login_calls += 1
        return _fake_login_response()

    monkeypatch.setattr(client, "send_login", fake_send_login)

    # Without force — recheck short-circuits, no Cognito call.
    await client._authenticate()
    assert send_login_calls == 0, "recheck should have skipped Cognito call"

    # With force — bypasses recheck, hits Cognito.
    await client._authenticate(force=True)
    assert send_login_calls == 1, "force=True should force a Cognito round-trip"


# ---------------------------------------------------------------------------
# Absorbed H9a finding — JWT exp WARN log is rate-limited.
# ---------------------------------------------------------------------------


def test_jwt_exp_warn_rate_limited(caplog) -> None:
    """The fallback WARN fires once per process; subsequent fallbacks log at
    DEBUG. Stops a persistently-malformed Cognito response from flooding the
    operator log every hour.
    """
    from custom_components.iaqualinkrobots import coordinator as coord_mod

    # Reset the module-level flag so this test is deterministic regardless
    # of test ordering — other tests may have already tripped the warn.
    coord_mod._JWT_EXP_FALLBACK_WARNED = False

    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.iaqualink_robots.coordinator",
    )

    coord_mod._resolve_token_expiry("garbage")
    coord_mod._resolve_token_expiry("still-garbage")
    coord_mod._resolve_token_expiry("yet-more-garbage")

    warning_records = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "JWT exp claim missing or unparseable" in r.message
    ]
    debug_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG
        and "JWT exp claim missing or unparseable" in r.message
    ]

    assert len(warning_records) == 1, (
        f"expected exactly 1 WARNING-level fallback log, got {len(warning_records)} "
        f"({[r.message for r in warning_records]})"
    )
    assert len(debug_records) >= 2, (
        f"subsequent fallbacks must downgrade to DEBUG; got {len(debug_records)}"
    )
