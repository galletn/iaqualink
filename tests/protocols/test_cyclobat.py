"""CycloBatProtocol regression tests for story R25-cyclobat.

Cyclobat is the first family in the R25 series that doesn't share its
common parse slice with VR/Vortrax — different cloud paths for
state/temperature/total_hours. So this test file does NOT exercise the
shared ``apply_common_robot_fields`` helper; the parser is self-contained.

Locked here:

* Envelope shape distinct from VR/Vortrax — ``setCleaningMode`` action
  with ``{"main": {"ctrl": N}}`` and ``namespace=cyclobat``.
* The RTB anomaly — uses flat ``setCleanerState`` + ``robot.state = 3``
  (NOT the ``main.ctrl`` form). Documented inline; locked here so a
  future "fix the inconsistency" PR fails CI rather than silently
  changing the cloud contract.
* Fan-speed map distinct from VR's prCyc — cyclobat's ``main.mode`` uses
  string values with a different number assignment.
* Parser covers the rich cyclobat state — battery / stats / lastCycle /
  cycles sub-dicts, the per-cycle-type cycle_duration picker (4 branches:
  floor / floor+walls / smart / waterline).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest


_ID = "test_user_id"
_AUTH = "test_auth_tok"
_APP = "test_app_client"
_SERIAL = "TEST_SERIAL"
_TOKEN = f"{_ID}|{_AUTH}|{_APP}"


def _make_client() -> Any:
    """Build a minimal ``AqualinkClient`` for cyclobat tests.

    Cyclobat's parser calls ``client._stabilize_status`` which reaches
    into 5 cached attributes the production ``__init__`` sets. The
    fixture stamps each one to match the constructor defaults so the
    method works on a ``__new__``-built instance.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    c = AqualinkClient.__new__(AqualinkClient)
    c._id = _ID
    c._auth_token = _AUTH
    c._app_client_id = _APP
    c._serial = _SERIAL
    c._device_type = "cyclobat"
    c._activity = None
    c._status_history = []
    c._status_stabilization_window = 30
    c._minimum_status_duration = 15
    c._last_status_change_time = 0
    c._stable_status = None
    c._debug_mode = False
    return c


def _expected_envelope(
    action: str, equipment: dict[str, Any], namespace: str = "cyclobat"
) -> dict[str, Any]:
    return {
        "action": action,
        "version": 1,
        "namespace": namespace,
        "payload": {
            "state": {"desired": {"equipment": equipment}},
            "clientToken": _TOKEN,
        },
        "service": "StateController",
        "target": _SERIAL,
    }


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_dispatch_returns_cyclobat_protocol() -> None:
    from custom_components.iaqualink_robots.protocols import (
        CycloBatProtocol,
        get_protocol,
    )

    protocol = get_protocol("cyclobat")
    assert protocol is not None
    assert isinstance(protocol, CycloBatProtocol)
    assert protocol.namespace == "cyclobat"


def test_dispatch_still_returns_none_for_remaining_families() -> None:
    """Cyclonext / i2d remain inline-encoded post-R25-cyclobat."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    for family in ("cyclonext", "i2d_robot"):
        assert get_protocol(family) is None


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def test_start_payload_uses_setcleaningmode_and_main_ctrl() -> None:
    """Cyclobat lifecycle commands use ``setCleaningMode`` + ``main.ctrl``
    nesting (NOT VR's ``setCleanerState`` + ``robot.state``). Distinct
    envelope shape per the family's cloud contract.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclobat").start_payload(client)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot": {"main": {"ctrl": 1}}}
    )


def test_stop_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclobat").stop_payload(client)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot": {"main": {"ctrl": 0}}}
    )


def test_pause_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclobat").pause_payload(client)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot": {"main": {"ctrl": 2}}}
    )


def test_return_to_base_uses_flat_setcleanerstate_anomaly() -> None:
    """RTB anomaly: cyclobat's RTB is the one command that does NOT use
    the ``main.ctrl`` nesting — it uses the same flat
    ``setCleanerState`` + ``robot.state = 3`` shape as VR/Vortrax.
    Documented inline as pre-R25 behaviour the cloud accepts; locked here
    so a "fix the inconsistency" PR can't silently change it.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclobat").return_to_base_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 3}}
    )


def test_clear_desired_payload_is_none_for_cyclobat() -> None:
    """PR #94 was VR-only. Cyclobat inherits the base ``None`` default."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert get_protocol("cyclobat").clear_desired_payload(client) is None


@pytest.mark.parametrize(
    "fan_speed,expected_mode",
    [
        ("wall_only", "3"),
        ("floor_only", "0"),
        ("smart_floor_and_walls", "2"),
        ("floor_and_walls", "1"),
    ],
)
def test_set_fan_speed_uses_main_mode_with_string_values(
    fan_speed: str, expected_mode: str
) -> None:
    """Cyclobat's fan-speed uses ``main.mode`` with STRING values (distinct
    from VR's ``prCyc`` with int values). The number assignment also
    differs — VR's 0=wall_only, cyclobat's 3=wall_only.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclobat").set_fan_speed_payload(client, fan_speed)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot": {"main": {"mode": expected_mode}}}
    )


def test_set_fan_speed_returns_none_for_unknown() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert (
        get_protocol("cyclobat").set_fan_speed_payload(client, "nonsense")
        is None
    )


def test_fan_speed_codes_uses_strings_with_distinct_assignment() -> None:
    """Cyclobat's encoding has STRING values (cloud expects strings) and
    a different number assignment than VR — 0/1 swapped and 3 is wall_only
    instead of 0=wall_only. Lock both differences.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    codes = get_protocol("cyclobat").fan_speed_codes()
    assert codes == {
        "wall_only": "3",
        "floor_only": "0",
        "smart_floor_and_walls": "2",
        "floor_and_walls": "1",
    }
    # Sanity: values are strings, not ints.
    for v in codes.values():
        assert isinstance(v, str)


@pytest.mark.parametrize(
    "mode,expected_key",
    [
        (3, "wall_only"),
        (0, "floor_only"),
        (2, "smart_floor_and_walls"),
        (1, "floor_and_walls"),
    ],
)
def test_extract_fan_speed_decodes_main_mode(mode: int, expected_key: str) -> None:
    """The cloud's response carries ``main.mode`` as an integer (decoded
    by the JSON parser). The protocol reverses the encoding table.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {"robot": {"main": {"mode": mode}}},
                    },
                },
            },
        },
    }
    decoded = get_protocol("cyclobat").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded == expected_key


def test_extract_fan_speed_returns_none_when_main_missing() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {"state": {"reported": {"equipment": {"robot": {}}}}},
        },
    }
    decoded = get_protocol("cyclobat").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded is None


# ---------------------------------------------------------------------------
# parse_status — cyclobat has rich nested state (battery / stats /
# lastCycle / cycles).
# ---------------------------------------------------------------------------


def _cyclobat_payload(
    state: int = 1,
    end_cycle_type: int = 0,
    cycle_start: int = 1_700_000_000,
    battery_pct: str = "85",
    raw_status: str = "connected",
) -> dict[str, Any]:
    """Build a complete cyclobat payload with sane defaults."""
    return {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "aws": {"status": raw_status},
                        "equipment": {
                            "robot": {
                                "vr": "1.2.3",
                                "sn": "CB-SERIAL",
                                "main": {
                                    "state": state,
                                    "ctrl": "1",
                                    "mode": "0",
                                    "error": "0",
                                    "cycleStartTime": cycle_start,
                                },
                                "battery": {
                                    "vr": "2.0",
                                    "state": 1,
                                    "userChargePerc": battery_pct,
                                    "userChargeState": 0,
                                    "cycles": 50,
                                    "warning": {"code": 0},
                                },
                                "stats": {
                                    "totRunTime": "1234",
                                    "diagnostic": "ok",
                                    "tmp": "22",
                                    "lastError": {"code": "0", "cycleNb": "10"},
                                },
                                "lastCycle": {
                                    "cycleNb": "11",
                                    "duration": "5400",
                                    "mode": "1",
                                    "endCycleType": end_cycle_type,
                                    "errorCode": "0",
                                },
                                "cycles": {
                                    "floorTim": {"duration": 5400},
                                    "floorWallsTim": {"duration": 7200},
                                    "smartTim": {"duration": 9000},
                                    "waterlineTim": {"duration": 3600},
                                    "firstSmartDone": True,
                                    "liftPatternTim": "100",
                                },
                            }
                        },
                    }
                }
            }
        }
    }


def test_parse_status_no_payload_marks_unknown() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(client, {}, result)
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


@pytest.mark.parametrize(
    "robot_state,expected_activity",
    [(1, "cleaning"), (3, "returning"), (0, "idle"), (2, "idle")],
)
def test_parse_status_activity_mapping(
    robot_state: int, expected_activity: str
) -> None:
    """Cyclobat reads activity from ``main.state`` (not ``robot.state``
    like VR/Vortrax). Same mapping function though: 1→cleaning,
    3→returning, else→idle.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(
        client, _cyclobat_payload(state=robot_state), result
    )
    assert result["activity"] == expected_activity


def test_parse_status_battery_fields_populated() -> None:
    """Cyclobat exposes a richer battery block than any other family."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(
        client, _cyclobat_payload(battery_pct="42"), result
    )
    assert result["battery_level"] == "42"
    assert result["battery_percentage"] == "42"
    assert result["battery_cycles"] == "50"
    assert result["battery_warning_code"] == "0"


def test_parse_status_temperature_from_stats_tmp() -> None:
    """Cyclobat reads temperature from ``stats.tmp`` (not
    ``sensors.sns_1.val`` like VR/Vortrax). The value is wrapped in
    ``str(...)`` per the pre-R25 code.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(client, _cyclobat_payload(), result)
    assert result["temperature"] == "22"


def test_parse_status_total_hours_from_stats_totruntime() -> None:
    """Cyclobat reads total_hours from ``stats.totRunTime`` (not
    ``robot.totalHours``). Confirms the cyclobat parser doesn't use
    the shared common helper (which would read the wrong path).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(client, _cyclobat_payload(), result)
    assert result["total_hours"] == "1234"


@pytest.mark.parametrize(
    "end_cycle_type,expected_duration",
    [
        (0, 5400),   # Floor only → floorTim.duration
        (1, 7200),   # Floor and walls → floorWallsTim.duration
        (2, 9000),   # Smart → smartTim.duration
        (3, 3600),   # Waterline → waterlineTim.duration
    ],
)
def test_parse_status_cycle_duration_picks_correct_slot(
    end_cycle_type: int, expected_duration: int
) -> None:
    """Cyclobat's ``cycle_duration`` is picked from one of 4 per-cycle-type
    nested fields keyed by ``lastCycle.endCycleType``. Lock the mapping.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(
        client, _cyclobat_payload(end_cycle_type=end_cycle_type), result
    )
    assert result["cycle_duration"] == expected_duration


def test_parse_status_never_cleaned() -> None:
    """``main.cycleStartTime == 0`` → cycle_start_time / cycle_end_time /
    estimated_end_time all None; cycle_duration 0.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(
        client, _cyclobat_payload(cycle_start=0), result
    )
    assert result["cycle_start_time"] is None
    assert result["cycle_end_time"] is None
    assert result["estimated_end_time"] is None
    assert result["cycle_duration"] == 0


def test_parse_status_emits_version_and_serial() -> None:
    """Cyclobat-specific: surfaces ``version`` (firmware) and ``serial``
    from the robot block.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(client, _cyclobat_payload(), result)
    assert result["version"] == "1.2.3"
    assert result["serial"] == "CB-SERIAL"


# ---------------------------------------------------------------------------
# Coordinator AST guard
# ---------------------------------------------------------------------------


def _coordinator_method_source(method_name: str) -> str:
    coordinator_path = (
        Path(__file__).parent.parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    )
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "AqualinkClient":
            continue
        for method in class_node.body:
            if (
                isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef))
                and method.name == method_name
            ):
                return ast.unparse(method)
    raise AssertionError(f"AqualinkClient.{method_name} not found")


@pytest.mark.parametrize(
    "method_name,protocol_call",
    [
        ("start_cleaning", "get_protocol('cyclobat').start_payload"),
        ("stop_cleaning", "get_protocol('cyclobat').stop_payload"),
        ("pause_cleaning", "get_protocol('cyclobat').pause_payload"),
        ("return_to_base", "get_protocol('cyclobat').return_to_base_payload"),
        ("_set_other_fan_speed", "get_protocol('cyclobat').set_fan_speed_payload"),
        ("_extract_fan_speed_from_response", "get_protocol('cyclobat').extract_fan_speed_from_response"),
    ],
)
def test_coordinator_cyclobat_branch_routes_through_protocol(
    method_name: str, protocol_call: str
) -> None:
    source = _coordinator_method_source(method_name)
    assert protocol_call in source, (
        f"AqualinkClient.{method_name}'s cyclobat branch must call "
        f"{protocol_call}(self). Got:\n{source}"
    )


def test_update_cyclobat_robot_data_delegates_to_protocol_parse_status() -> None:
    coordinator_path = (
        Path(__file__).parent.parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    )
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))
    method_body = None
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "AqualinkClient":
            continue
        for method in class_node.body:
            if (
                isinstance(method, ast.FunctionDef)
                and method.name == "_update_cyclobat_robot_data"
            ):
                method_body = method
                break
    assert method_body is not None
    assert len(method_body.body) <= 3, (
        f"_update_cyclobat_robot_data has {len(method_body.body)} top-level "
        f"statements post-R25-cyclobat; expected <=3."
    )
    source = ast.unparse(method_body)
    assert "get_protocol('cyclobat').parse_status" in source
