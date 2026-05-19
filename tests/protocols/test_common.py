"""Shared-helper tests for ``protocols/common.py`` (story R25-vortrax / R29 fold).

``apply_common_robot_fields`` is called by both ``VRProtocol.parse_status``
and ``VortraxProtocol.parse_status``. Each per-family test file
independently asserts that its parser's output is correct end-to-end —
these tests pin the SHARED helper's behaviour in isolation so a future
refactor that changes (e.g.) the temperature fallback ladder fails here
with a clear single-source-of-truth message instead of trip-tripping
multiple per-family tests downstream.

The helper mutates ``result`` in place and updates ``client._activity``
(the VacuumActivity enum mirror — read by the vacuum entity).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from homeassistant.components.vacuum import VacuumActivity

from custom_components.iaqualink_robots.protocols.common import (
    apply_common_robot_fields,
)


def _client() -> Any:
    """MagicMock client with an ``_activity`` attribute the helper writes."""
    c = MagicMock()
    c._activity = None
    return c


def _robot(
    *,
    state: int = 1,
    canister: float = 0.50,
    error_state: int = 0,
    total_hours: int = 100,
    temp_val: str | None = "20.0",
    temp_state: str | None = None,
) -> dict[str, Any]:
    """Build a robot_data dict matching the cloud's ``equipment.robot`` shape."""
    sensors: dict[str, dict[str, str]] = {"sns_1": {}}
    if temp_val is not None:
        sensors["sns_1"]["val"] = temp_val
    if temp_state is not None:
        sensors["sns_1"]["state"] = temp_state
    return {
        "state": state,
        "canister": canister,
        "errorState": error_state,
        "totalHours": total_hours,
        "sensors": sensors,
    }


# ---------------------------------------------------------------------------
# Temperature: .val first, .state fallback, '0' final fallback.
# ---------------------------------------------------------------------------


def test_temperature_from_val() -> None:
    result: dict[str, Any] = {}
    apply_common_robot_fields(_client(), _robot(temp_val="22.5"), result)
    assert result["temperature"] == "22.5"


def test_temperature_falls_back_to_state_when_val_missing() -> None:
    """Some firmware (notably older Vortrax) exposes the temp via ``.state``
    rather than ``.val``; the helper tries both before falling to ``'0'``.
    """
    result: dict[str, Any] = {}
    apply_common_robot_fields(
        _client(), _robot(temp_val=None, temp_state="19.8"), result
    )
    assert result["temperature"] == "19.8"


def test_temperature_falls_back_to_zero_when_no_sensor() -> None:
    """Zodiac XA 5095 iQ has no temp sensor at all — neither ``.val`` nor
    ``.state``. Helper emits the literal string ``'0'`` (matches pre-R25
    behaviour; would be more honest as ``None`` but that's a behaviour
    change outside R25's scope).
    """
    result: dict[str, Any] = {}
    apply_common_robot_fields(
        _client(), _robot(temp_val=None, temp_state=None), result
    )
    assert result["temperature"] == "0"


# ---------------------------------------------------------------------------
# Activity: cloud robot.state → integration raw key + VacuumActivity mirror.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "robot_state,expected_activity,expected_enum",
    [
        (1, "cleaning", VacuumActivity.CLEANING),
        (3, "returning", VacuumActivity.RETURNING),
        (0, "idle", VacuumActivity.IDLE),
        (2, "idle", VacuumActivity.IDLE),  # any non-1/non-3 → idle
        (-1, "idle", VacuumActivity.IDLE),
    ],
)
def test_activity_mapping(
    robot_state: int,
    expected_activity: str,
    expected_enum: VacuumActivity,
) -> None:
    client = _client()
    result: dict[str, Any] = {}
    apply_common_robot_fields(client, _robot(state=robot_state), result)
    assert result["activity"] == expected_activity
    assert client._activity == expected_enum


def test_activity_unknown_when_state_missing() -> None:
    """Missing ``state`` key → activity=unknown. ``client._activity`` is
    NOT touched (preserves the prior enum value so the vacuum entity
    doesn't flip on a single-poll glitch).
    """
    client = _client()
    client._activity = VacuumActivity.CLEANING  # prior state
    result: dict[str, Any] = {}
    robot = _robot()
    del robot["state"]
    apply_common_robot_fields(client, robot, result)
    assert result["activity"] == "unknown"
    assert client._activity == VacuumActivity.CLEANING


# ---------------------------------------------------------------------------
# Canister, error_state, total_hours.
# ---------------------------------------------------------------------------


def test_canister_scaled_to_percent() -> None:
    """Cloud reports canister 0-1; we surface 0-100 for the sensor's %
    unit. Multiplication by 100 — verified for the 4 corner cases.
    """
    for cloud, expected in [(0.0, 0.0), (0.25, 25.0), (0.5, 50.0), (1.0, 100.0)]:
        result: dict[str, Any] = {}
        apply_common_robot_fields(
            _client(), _robot(canister=cloud), result
        )
        assert result["canister"] == expected


def test_canister_defaults_to_zero_when_missing() -> None:
    result: dict[str, Any] = {}
    robot = _robot()
    del robot["canister"]
    apply_common_robot_fields(_client(), robot, result)
    assert result["canister"] == 0


def test_error_state_passes_through() -> None:
    result: dict[str, Any] = {}
    apply_common_robot_fields(_client(), _robot(error_state=42), result)
    assert result["error_state"] == 42


def test_error_state_defaults_to_unknown_when_missing() -> None:
    result: dict[str, Any] = {}
    robot = _robot()
    del robot["errorState"]
    apply_common_robot_fields(_client(), robot, result)
    assert result["error_state"] == "unknown"


def test_total_hours_passes_through() -> None:
    result: dict[str, Any] = {}
    apply_common_robot_fields(_client(), _robot(total_hours=2500), result)
    assert result["total_hours"] == 2500


def test_total_hours_defaults_to_zero_when_missing() -> None:
    result: dict[str, Any] = {}
    robot = _robot()
    del robot["totalHours"]
    apply_common_robot_fields(_client(), robot, result)
    assert result["total_hours"] == 0


# ---------------------------------------------------------------------------
# Helper doesn't touch fields it doesn't own.
# ---------------------------------------------------------------------------


def test_helper_does_not_touch_unrelated_keys() -> None:
    """The helper writes exactly 5 keys: temperature, activity, canister,
    error_state, total_hours. Locks the per-family parser's invariant that
    the helper's output set is fixed — adding a new field requires updating
    each per-family parser too, not just dropping it into the helper.
    """
    result: dict[str, Any] = {"unrelated_key": "preserved"}
    apply_common_robot_fields(_client(), _robot(), result)
    assert result["unrelated_key"] == "preserved"
    written_keys = set(result.keys()) - {"unrelated_key"}
    assert written_keys == {
        "temperature",
        "activity",
        "canister",
        "error_state",
        "total_hours",
    }
