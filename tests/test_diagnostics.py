"""Diagnostics tests (story P5).

Locks in three properties of the diagnostics dump:

1. Every credential / token / PII field in ``TO_REDACT`` is replaced with
   ``"**REDACTED**"`` in the rendered output.
2. The serial number is partially redacted (first 3 + last 3 chars) — full
   redaction breaks cross-issue triage; full disclosure breaks the user.
3. The shape contract from the story (entry / coordinator / client blocks)
   is stable, including the mid-setup case where ``coordinator`` / ``client``
   may be missing from ``hass.data``.

The forward-defence test ``test_no_token_or_password_key_appears_unredacted``
walks the rendered payload recursively and fails if any key matching
``*token*``, ``*password*``, or ``username`` carries a non-redacted value.
That is the long-term net against drift — adding a new credential field to
the codebase without updating ``TO_REDACT`` would surface here.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import MagicMock

from custom_components.iaqualink_robots.const import DOMAIN
from custom_components.iaqualink_robots.diagnostics import (
    TO_REDACT,
    _partial_redact,
    async_get_config_entry_diagnostics,
)
from tests.const import (
    MOCK_DEVICE_TYPE,
    MOCK_ENTRY_DATA,
    MOCK_PASSWORD,
    MOCK_SERIAL,
    MOCK_USERNAME,
)

REDACTED = "**REDACTED**"


def _entry(data: dict | None = None, options: dict | None = None) -> MagicMock:
    """Build a MagicMock ConfigEntry with the surface diagnostics reads."""
    entry = MagicMock()
    entry.entry_id = "test-entry-id"
    entry.data = data if data is not None else dict(MOCK_ENTRY_DATA)
    entry.options = options if options is not None else {}
    entry.version = 3
    entry.title = "Test Pool Robot"
    entry.state = "loaded"
    return entry


def _coordinator(
    data: dict | None = None,
    consecutive_failures: int = 0,
    last_update_success: bool = True,
    update_interval_seconds: float | None = 3.0,
) -> MagicMock:
    """Build a MagicMock coordinator with the surface diagnostics reads."""
    coordinator = MagicMock()
    coordinator.data = data if data is not None else {
        "model": "VRX iQ+",
        "fan_speed": "floor_only",
        "status": "connected",
    }
    coordinator._consecutive_failures = consecutive_failures
    coordinator.last_update_success = last_update_success
    if update_interval_seconds is None:
        coordinator.update_interval = None
    else:
        coordinator.update_interval = dt.timedelta(seconds=update_interval_seconds)
    return coordinator


def _client(
    *,
    serial: str = MOCK_SERIAL,
    device_type: str = MOCK_DEVICE_TYPE,
    ws_consecutive_failures: int = 0,
    token_expires_at: dt.datetime | None = None,
    model: str | None = "VRX iQ+",
) -> MagicMock:
    """Build a MagicMock client with the surface diagnostics reads."""
    client = MagicMock()
    client.serial = serial
    client.device_type = device_type
    client._ws_consecutive_failures = ws_consecutive_failures
    client._token_expires_at = token_expires_at
    client._model = model
    return client


def _hass(coordinator: Any = None, client: Any = None, entry_id: str = "test-entry-id") -> MagicMock:
    """Build a MagicMock hass with ``hass.data[DOMAIN][entry_id]`` populated."""
    hass = MagicMock()
    if coordinator is None and client is None:
        hass.data = {}
    else:
        hass.data = {DOMAIN: {entry_id: {"coordinator": coordinator, "client": client}}}
    return hass


def _walk_strings(obj: Any):
    """Yield every string value reachable from ``obj`` (dict / list / scalars)."""
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)
    elif isinstance(obj, str):
        yield obj


def _walk_keys_with_values(obj: Any, path: tuple = ()):
    """Yield ``(path, key, value)`` for every dict entry reachable from ``obj``."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield path, k, v
            yield from _walk_keys_with_values(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_keys_with_values(v, path + (i,))


# ---------------------------------------------------------------------------
# _partial_redact — small pure helper, exhaustive cases
# ---------------------------------------------------------------------------


def test_partial_redact_none_returns_empty_string() -> None:
    assert _partial_redact(None) == ""


def test_partial_redact_short_value_collapses_to_double_asterisk() -> None:
    """Values <= 6 chars expose too high a fraction; collapse instead."""
    for short in ("", "a", "abc", "abcdef"):
        assert _partial_redact(short) == "**", (
            f"len-{len(short)} value should collapse to '**', got {_partial_redact(short)!r}"
        )


def test_partial_redact_long_value_shows_first_three_and_last_three() -> None:
    """The 3-on / 3-off shape keeps enough entropy for triage."""
    assert _partial_redact("R23X12345678") == "R23***678"
    assert _partial_redact("abcdefg") == "abc***efg"  # exactly 7 chars


def test_partial_redact_coerces_non_string_to_string() -> None:
    """A bug elsewhere could hand us an int — we should not crash."""
    assert _partial_redact(1234567890) == "123***890"


# ---------------------------------------------------------------------------
# async_get_config_entry_diagnostics — happy path
# ---------------------------------------------------------------------------


async def test_diagnostics_redacts_known_credentials_in_entry_data() -> None:
    """AC #2: username, password, api_key are redacted in entry.data."""
    hass = _hass(_coordinator(), _client())
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["entry"]["data"]["username"] == REDACTED
    assert payload["entry"]["data"]["password"] == REDACTED
    assert payload["entry"]["data"]["api_key"] == REDACTED
    # Non-sensitive fields remain visible.
    assert payload["entry"]["data"]["serial_number"] == MOCK_SERIAL
    assert payload["entry"]["data"]["device_type"] == MOCK_DEVICE_TYPE


async def test_diagnostics_includes_runtime_state() -> None:
    """AC #3: coordinator.data + counters + last_update_success are present."""
    coordinator = _coordinator(
        data={"status": "connected", "fan_speed": "floor_only", "model": "RA 6500 iQ"},
        consecutive_failures=2,
        last_update_success=True,
        update_interval_seconds=3.0,
    )
    hass = _hass(coordinator, _client())
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["coordinator"]["data"]["status"] == "connected"
    assert payload["coordinator"]["data"]["fan_speed"] == "floor_only"
    assert payload["coordinator"]["consecutive_failures"] == 2
    assert payload["coordinator"]["last_update_success"] is True
    assert payload["coordinator"]["update_interval_seconds"] == 3.0


async def test_diagnostics_serial_is_partially_redacted() -> None:
    """AC #2: serial appears in first-3-***-last-3 form, not in full."""
    client = _client(serial="R23X12345678")
    hass = _hass(_coordinator(), client)
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["client"]["serial_redacted"] == "R23***678"
    # And the full serial does not leak elsewhere in the client block.
    for s in _walk_strings(payload["client"]):
        assert "R23X12345678" not in s, (
            f"full serial leaked through client block: {s!r}"
        )


async def test_diagnostics_token_expires_at_serialised_as_iso() -> None:
    """``_token_expires_at`` is an aware-UTC datetime post-H8 — needs JSON-safe shape."""
    expires = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
    client = _client(token_expires_at=expires)
    hass = _hass(_coordinator(), client)
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["client"]["token_expires_at"] == "2026-06-01T12:00:00+00:00"


async def test_diagnostics_token_expires_at_none_is_passed_through() -> None:
    """Pre-auth path: token_expires_at is None; payload must not crash."""
    client = _client(token_expires_at=None)
    hass = _hass(_coordinator(), client)
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["client"]["token_expires_at"] is None


# ---------------------------------------------------------------------------
# Forward-defence test — the long-term safety net
# ---------------------------------------------------------------------------


async def test_no_token_or_password_key_appears_unredacted() -> None:
    """No key matching ``*token*`` / ``*password*`` / ``username`` may carry a non-redacted value.

    This is the future-proofing test: if a developer adds a new credential
    field to entry.data (say ``"refresh_jwt"``) without updating
    ``TO_REDACT``, this test will catch the leak as long as the new
    field has a recognisable name. Falls short for credentials with
    opaque names — those need to be added to ``TO_REDACT`` explicitly.
    """
    entry_data = {
        **MOCK_ENTRY_DATA,
        "id_token": "eyJhbGciOiJIUzI1NiJ9.fake.token",
        "refresh_token": "rt-secret-xyz",
        "auth_token": "auth-secret-abc",
    }
    coordinator = _coordinator(
        data={
            "status": "connected",
            "username": MOCK_USERNAME,  # If a future change leaks this here
            "password": MOCK_PASSWORD,  # ... or this, the redactor must catch it
        },
    )
    hass = _hass(coordinator, _client())
    entry = _entry(data=entry_data)

    payload = await async_get_config_entry_diagnostics(hass, entry)

    for path, key, value in _walk_keys_with_values(payload):
        k_lower = key.lower() if isinstance(key, str) else ""
        if not isinstance(value, str):
            continue
        if "token" in k_lower or "password" in k_lower or k_lower == "username":
            assert value == REDACTED, (
                f"Sensitive key {'/'.join(map(str, path + (key,)))!r} "
                f"appeared unredacted with value {value!r}"
            )


async def test_no_raw_password_substring_anywhere_in_payload() -> None:
    """Defence-in-depth: the raw password literal must not appear anywhere."""
    hass = _hass(_coordinator(), _client())
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    for s in _walk_strings(payload):
        assert MOCK_PASSWORD not in s, (
            f"raw password leaked through payload value: {s!r}"
        )


# ---------------------------------------------------------------------------
# Mid-setup safety — entry exists but coordinator/client not yet stored
# ---------------------------------------------------------------------------


async def test_diagnostics_without_coordinator_or_client_returns_entry_only() -> None:
    """If the integration is mid-setup, ``hass.data[DOMAIN][entry_id]`` may be absent.

    A diagnostic in that state should still surface entry.data (redacted) so
    users with "stuck loading" reports can attach something useful.
    """
    hass = _hass(coordinator=None, client=None)  # hass.data = {}
    entry = _entry()

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert "entry" in payload
    assert payload["entry"]["data"]["username"] == REDACTED
    assert "coordinator" not in payload
    assert "client" not in payload


# ---------------------------------------------------------------------------
# TO_REDACT contract — keep the set documented and exhaustive
# ---------------------------------------------------------------------------


def test_to_redact_includes_known_credential_field_names() -> None:
    """Trip wire: if anyone removes a credential field from TO_REDACT, fail loudly."""
    required = {
        "username",
        "password",
        "api_key",
        "id_token",
        "auth_token",
        "refresh_token",
    }
    missing = required - TO_REDACT
    assert not missing, (
        f"TO_REDACT must include all credential field names; missing: {missing}"
    )


# ---------------------------------------------------------------------------
# AsyncMock smoke test — confirm the function is async and awaitable
# ---------------------------------------------------------------------------


async def test_async_get_config_entry_diagnostics_is_awaitable() -> None:
    """Verify the function signature matches HA's expected async hook shape."""
    hass = _hass(_coordinator(), _client())
    entry = _entry()

    result = async_get_config_entry_diagnostics(hass, entry)
    # Must return a coroutine that we can await.
    assert hasattr(result, "__await__"), (
        "async_get_config_entry_diagnostics must be async (HA requires an awaitable)"
    )
    payload = await result
    assert isinstance(payload, dict)
