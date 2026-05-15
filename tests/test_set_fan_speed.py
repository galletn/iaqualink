"""Regression tests for `AqualinkClient.set_fan_speed` per-device-type dispatch.

Seeded as part of the issue #76 fix: the cyclonext branch of
`_set_other_fan_speed` had its `cycle_speed_map` keyed by display-name strings
("Floor only", "Floor and walls") even though `vacuum.py::async_set_fan_speed`
forwards the snake_case translation key. The lookup never matched, `request`
stayed None, and the command fell through to a silent
`{"success": False, ..., "error": "No valid request generated"}` return — the
"fan speed no longer propagates from HA to AquaLink" symptom reported on the
RE 4400 iQ (cyclonext).

These tests assert the request-generation contract for each device type
without driving real I/O. They patch the websocket-call path so a successful
`set_fan_speed` return value just confirms the right payload would have been
sent.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


def _build_client(device_type: str):
    """Build a real `AqualinkClient` with just enough state for set_fan_speed.

    We don't go through `__init__` because the production constructor reaches
    into `aiohttp` for header defaults; the bits we need for set_fan_speed
    dispatch are simple instance attributes. Bypassing `__init__` keeps the
    test focused on the dispatch logic under test.
    """
    from custom_components.iaqualinkrobots.coordinator import AqualinkClient

    client = AqualinkClient.__new__(AqualinkClient)
    client._device_type = device_type
    client._serial = "TEST-SERIAL"
    client._id = "user-id"
    client._auth_token = "token"
    client._app_client_id = "app-client"
    client._debug_mode = False
    # Patch the I/O entry point so we can capture the request that would have
    # been sent. Returns a dummy response shaped like the production code's
    # success path so the wrapper unwraps it without errors.
    client.set_cleaner_state = AsyncMock(return_value={"payload": {}})
    return client


# ---------------------------------------------------------------------------
# Cyclonext: the issue #76 regression.
# ---------------------------------------------------------------------------


async def test_cyclonext_floor_only_generates_request() -> None:
    """Pre-fix this lookup returned None (map was keyed on "Floor only", not
    `floor_only`), so `request` stayed None and the command fell through to
    the "No valid request generated" silent-failure branch. Asserts the
    request IS now generated and dispatched.
    """
    client = _build_client("cyclonext")

    result = await client._set_other_fan_speed("floor_only")

    # The I/O call must have happened — that's the bug-fix proof. Pre-fix
    # `set_cleaner_state` was never called for cyclonext.
    client.set_cleaner_state.assert_awaited_once()
    sent_request = client.set_cleaner_state.await_args.args[0]
    assert sent_request["namespace"] == "cyclonext"
    assert sent_request["action"] == "setCleaningMode"
    # cyclonext cloud encoding: 1 = floor_only.
    assert sent_request["payload"]["state"]["desired"]["equipment"]["robot.1"]["cycle"] == "1"
    # Return value must report success, not the silent-failure dict.
    assert result.get("success") is True
    assert result.get("fan_speed") == "floor_only"
    assert "error" not in result


async def test_cyclonext_floor_and_walls_generates_request() -> None:
    """Same check as floor_only, but for `floor_and_walls` → cycle code 3."""
    client = _build_client("cyclonext")

    result = await client._set_other_fan_speed("floor_and_walls")

    client.set_cleaner_state.assert_awaited_once()
    sent_request = client.set_cleaner_state.await_args.args[0]
    assert sent_request["namespace"] == "cyclonext"
    # cyclonext cloud encoding: 3 = floor_and_walls.
    assert sent_request["payload"]["state"]["desired"]["equipment"]["robot.1"]["cycle"] == "3"
    assert result.get("success") is True


async def test_cyclonext_rejects_walls_only() -> None:
    """The issue #76 user-visible bug: cyclonext doesn't support `walls_only`.
    The cycle_speed_map must NOT include it. If a caller somehow gets past
    the vacuum-entity guard and passes `walls_only` to a cyclonext client,
    the silent no-op is acceptable (request stays None → returns the explicit
    `error: "No valid request generated"` dict so the caller knows). This
    test pins that fallback so the cyclonext map can't accidentally start
    accepting `walls_only` later.
    """
    client = _build_client("cyclonext")

    result = await client._set_other_fan_speed("walls_only")

    client.set_cleaner_state.assert_not_awaited()
    assert result.get("success") is False
    assert result.get("error") == "No valid request generated"


# ---------------------------------------------------------------------------
# Non-cyclonext device types — unchanged contract, locked here as a cross-
# check that the per-type dispatch wasn't broken by the cyclonext fix.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device_type, fan_speed, expected_cycle_value",
    [
        ("vr", "floor_only", 1),
        ("vr", "floor_and_walls", 3),
        ("vr", "wall_only", 0),
        ("vr", "smart_floor_and_walls", 2),
        ("vortrax", "floor_only", 1),
        ("vortrax", "floor_and_walls", 3),
    ],
)
async def test_vr_vortrax_cycle_codes_unchanged(
    device_type: str, fan_speed: str, expected_cycle_value: int
) -> None:
    """vr/vortrax use the same `prCyc` int encoding. Locking these so a
    future merge of the four `_set_other_fan_speed` branches into one can't
    silently change the per-type cycle codes.
    """
    client = _build_client(device_type)

    await client._set_other_fan_speed(fan_speed)

    client.set_cleaner_state.assert_awaited_once()
    sent_request = client.set_cleaner_state.await_args.args[0]
    assert sent_request["namespace"] == device_type
    assert sent_request["payload"]["state"]["desired"]["equipment"]["robot"]["prCyc"] == expected_cycle_value


@pytest.mark.parametrize(
    "fan_speed, expected_mode_str",
    [
        ("floor_only", "0"),
        ("floor_and_walls", "1"),
        ("smart_floor_and_walls", "2"),
        ("wall_only", "3"),
    ],
)
async def test_cyclobat_mode_codes_unchanged(fan_speed: str, expected_mode_str: str) -> None:
    """cyclobat encodes mode as a string in `main.mode`. Locking these
    alongside the cyclonext fix so the per-type dispatch table stays
    auditable in one place (the test file).
    """
    client = _build_client("cyclobat")

    await client._set_other_fan_speed(fan_speed)

    client.set_cleaner_state.assert_awaited_once()
    sent_request = client.set_cleaner_state.await_args.args[0]
    assert sent_request["namespace"] == "cyclobat"
    assert sent_request["payload"]["state"]["desired"]["equipment"]["robot"]["main"]["mode"] == expected_mode_str


# ---------------------------------------------------------------------------
# set_fan_speed wrapper: validates against the list before dispatching.
# ---------------------------------------------------------------------------


async def test_set_fan_speed_wrapper_raises_on_value_not_in_list() -> None:
    """The wrapper validates the caller's fan_speed against the list it was
    passed BEFORE dispatching to the per-type implementation. This is the
    second integration-boundary guard (the first being vacuum.py's check) —
    even if a future caller bypasses vacuum.py, the cloud client still
    refuses to send an unknown encoding.
    """
    client = _build_client("cyclonext")

    with pytest.raises(ValueError, match="Invalid fan speed"):
        await client.set_fan_speed("walls_only", ["floor_only", "floor_and_walls"])

    client.set_cleaner_state.assert_not_awaited()
