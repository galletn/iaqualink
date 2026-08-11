"""CycloNextProtocol regression tests for story R25-cyclonext.

Cyclonext is the most minimal of the WebSocket families: only 2 fan
speeds, only 2 activity states (cleaning + idle — no returning), and
no return-to-base. It's also the only family that nests under
``equipment.robot.1`` (the ``.1`` suffix) and uses ``mode`` instead of
``state`` for activity decoding.

Locked here:

* Envelope shape — ``namespace=cyclonext``, ``robot_key=robot.1``,
  ``mode`` field with int values (1=start, 0=stop, 2=pause).
* ``return_to_base_payload`` and ``clear_desired_payload`` both return
  ``None`` (family doesn't support them).
* Fan-speed encoding — only 2 keys (``floor_only`` / ``floor_and_walls``)
  with string values (cloud expects strings under ``cycle`` field).
* Issue #76 regression guard — the keys in ``fan_speed_codes()`` MUST
  be snake_case translation keys (matching `vacuum.py`'s
  `_fan_speed_list`), not display names. Pre-fix this drift caused
  silent no-op fan-speed commands on RE 4400 iQ.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

import pytest


_ID = "test_user_id"
_AUTH = "test_auth_tok"
_APP = "test_app_client"
_SERIAL = "TEST_SERIAL"
_TOKEN = f"{_ID}|{_AUTH}|{_APP}"


def _make_client() -> Any:
    from custom_components.iaqualink_robots.client import AqualinkClient

    c = AqualinkClient.__new__(AqualinkClient)
    c._id = _ID
    c._auth_token = _AUTH
    c._app_client_id = _APP
    c._serial = _SERIAL
    c._device_type = "cyclonext"
    c._activity = None
    return c


def _expected_envelope(action: str, equipment: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "version": 1,
        "namespace": "cyclonext",
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


def test_dispatch_returns_cyclonext_protocol() -> None:
    from custom_components.iaqualink_robots.protocols import (
        CycloNextProtocol,
        get_protocol,
    )

    protocol = get_protocol("cyclonext")
    assert protocol is not None
    assert isinstance(protocol, CycloNextProtocol)
    assert protocol.namespace == "cyclonext"


def test_dispatch_returns_none_for_unknown_device_type() -> None:
    """Defence-in-depth: ``get_protocol`` returns ``None`` for an
    unrecognised ``device_type`` (post-Epic-9 every known family
    has a protocol).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    assert get_protocol("not_a_real_family") is None


# ---------------------------------------------------------------------------
# Envelope builders
# ---------------------------------------------------------------------------


def test_start_payload_uses_robot_1_and_mode_int() -> None:
    """Cyclonext nests under ``robot.1`` (not ``robot``) and uses an
    int ``mode`` field for activity commands (not VR's ``state`` field).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclonext").start_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot.1": {"mode": 1}}
    )


def test_stop_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclonext").stop_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot.1": {"mode": 0}}
    )


def test_pause_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclonext").pause_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot.1": {"mode": 2}}
    )


def test_return_to_base_payload_is_none() -> None:
    """Cyclonext family has no return-to-base. Pre-R25 the inline
    branch list ``["vr", "vortrax", "cyclobat"]`` explicitly excluded
    cyclonext; the user-facing return_to_base() call no-ops.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert get_protocol("cyclonext").return_to_base_payload(client) is None


def test_clear_desired_payload_is_none() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert get_protocol("cyclonext").clear_desired_payload(client) is None


@pytest.mark.parametrize(
    "fan_speed,expected_cycle",
    [
        ("floor_only", "1"),
        ("floor_and_walls", "3"),
    ],
)
def test_set_fan_speed_uses_robot_1_and_cycle_string(
    fan_speed: str, expected_cycle: str
) -> None:
    """Cyclonext fan-speed: nested under ``robot.1`` with ``cycle`` field
    carrying a STRING value (the cloud rejects non-string variants on
    some firmware revisions). Action is ``setCleaningMode``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("cyclonext").set_fan_speed_payload(client, fan_speed)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot.1": {"cycle": expected_cycle}}
    )


@pytest.mark.parametrize(
    "invalid_fan_speed",
    ["wall_only", "walls_only", "smart_floor_and_walls"],
)
def test_set_fan_speed_returns_none_for_unsupported_modes(
    invalid_fan_speed: str,
) -> None:
    """Cyclonext only accepts ``floor_only`` and ``floor_and_walls``.
    Any other key MUST return ``None`` — locked here to prevent issue
    #76 regression where ``walls_only`` was accepted by the dispatch
    map but silently no-op'd at the cloud.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert (
        get_protocol("cyclonext").set_fan_speed_payload(client, invalid_fan_speed)
        is None
    )


def test_fan_speed_codes_uses_snake_case_translation_keys_only() -> None:
    """Issue #76 regression guard: keys MUST be the snake_case translation
    keys that `vacuum.py::async_set_fan_speed` forwards. Pre-fix this map
    used the legacy display names (``"Floor only"`` / ``"Floor and walls"``)
    and the .get() lookup always returned None — fan-speed commands
    silently no-op'd on RE 4400 iQ.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    codes = get_protocol("cyclonext").fan_speed_codes()
    # Exactly 2 keys; exactly these names; exactly these (string) values.
    assert codes == {"floor_only": "1", "floor_and_walls": "3"}
    assert len(codes) == 2
    # No display-name keys may leak in.
    for forbidden in ("Floor only", "Floor and walls", "wall_only", "walls_only"):
        assert forbidden not in codes, (
            f"Issue #76 regression: cyclonext fan_speed_codes contains "
            f"{forbidden!r} which would silently fail at the cloud"
        )


@pytest.mark.parametrize(
    "cycle,expected_key",
    [(1, "floor_only"), (3, "floor_and_walls")],
)
def test_extract_fan_speed_decodes_cycle(cycle: int, expected_key: str) -> None:
    """Cloud reports ``equipment.robot.1.cycle`` as int; the decoder
    reverses the mapping.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {"robot.1": {"cycle": cycle}},
                    },
                },
            },
        },
    }
    decoded = get_protocol("cyclonext").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded == expected_key


def test_extract_fan_speed_returns_none_when_cycle_missing() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {"state": {"reported": {"equipment": {"robot.1": {}}}}},
        },
    }
    decoded = get_protocol("cyclonext").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded is None


# ---------------------------------------------------------------------------
# parse_status — cyclonext's parser is the simplest WebSocket family.
# ---------------------------------------------------------------------------


def _cyclonext_payload(
    mode: int = 1,
    cycle: int = 1,
    canister: float = 0.4,
    error: str = "0",
    run_time: int = 500,
    cycle_start: int = 1_700_000_000,
) -> dict[str, Any]:
    return {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {
                            "robot.1": {
                                "mode": mode,
                                "canister": canister,
                                "errors": {"code": error},
                                "totRunTime": run_time,
                                "cycle": cycle,
                                "cycleStartTime": cycle_start,
                                # Verbatim from a live cyclonext (RE 4600 iQ).
                                # Was `[60, 90, 120, 150]` -- wrong type and
                                # wrong key count. Note SEVEN keys: cyclonext
                                # adds `scanTim`, which the VR family lacks,
                                # so the two families' durations objects are
                                # not interchangeable.
                                "durations": {
                                    "waterTim": 45,
                                    "quickTim": 90,
                                    "smartTim": 0,
                                    "deepTim": 150,
                                    "customTim": 75,
                                    "firstSmartTim": 150,
                                    "scanTim": 30,
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
    get_protocol("cyclonext").parse_status(client, {}, result)
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


def test_parse_status_missing_robot_1_block_marks_unknown() -> None:
    """Payload without ``equipment.robot.1`` (e.g. another family's
    shape that lands under ``robot``) is rejected — the parser doesn't
    try to fall back to ``robot``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = {
        "payload": {
            "robot": {"state": {"reported": {"equipment": {"robot": {"state": 1}}}}}
        }
    }
    get_protocol("cyclonext").parse_status(client, payload, result)
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


@pytest.mark.parametrize(
    "mode,expected_activity",
    [
        (1, "cleaning"),
        (0, "idle"),
        (2, "idle"),  # cyclonext: anything not 1 is idle
        (3, "idle"),
    ],
)
def test_parse_status_activity_two_states_only(
    mode: int, expected_activity: str
) -> None:
    """Cyclonext has only 2 activity states (cleaning + idle) — no
    ``returning`` because the family has no return-to-base. Locks the
    contract so a future copy-paste of VR's parser logic doesn't
    accidentally introduce a phantom ``returning`` state.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(mode=mode), result
    )
    assert result["activity"] == expected_activity
    # Explicitly: returning state must never appear for cyclonext.
    assert result["activity"] != "returning"


def test_parse_status_canister_scaled() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(canister=0.65), result
    )
    assert result["canister"] == 65.0


def test_parse_status_error_state_from_nested_errors_code() -> None:
    """Cyclonext reads error_state from ``errors.code`` (nested dict),
    not from a flat ``errorState`` field like VR/Vortrax.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(error="E5"), result
    )
    assert result["error_state"] == "E5"


def test_parse_status_total_hours_from_totruntime_field() -> None:
    """``totRunTime`` is the cyclonext field name (not ``totalHours``).
    The pre-R25 except catches missing-field on older models and
    silently returns 0.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(run_time=789), result
    )
    assert result["total_hours"] == 789


def test_parse_status_total_hours_defaults_to_zero_on_old_models() -> None:
    """Older cyclonext models don't expose ``totRunTime``. The parser
    must fall back to 0 silently rather than crashing.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _cyclonext_payload()
    del payload["payload"]["robot"]["state"]["reported"]["equipment"]["robot.1"]["totRunTime"]
    get_protocol("cyclonext").parse_status(client, payload, result)
    assert result["total_hours"] == 0


@pytest.mark.parametrize(
    "raw_mode,expected_activity",
    [
        (1, "cleaning"),
        ("1", "cleaning"),    # issue #100: string-echoing firmware
        (1.0, "cleaning"),
        (0, "idle"),
        ("0", "idle"),
        ("2", "idle"),
    ],
)
def test_parse_status_activity_accepts_string_mode(
    raw_mode: Any, expected_activity: str
) -> None:
    """Issue #100 regression: a CNX 868 iQ stayed **Idle** in HA while
    physically cleaning because the parser tested ``mode == 1`` against a
    value the cloud had echoed back as a string. The reporter's trace
    showed ``None -> Idle`` (not ``unknown``), proving ``mode`` was
    present and merely not an int.

    The cyclobat parser already coerces for exactly this reason; this
    locks the same guard onto cyclonext so the two families can't drift.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(mode=raw_mode), result
    )
    assert result["activity"] == expected_activity


@pytest.mark.parametrize("raw_mode", ["running", "", None, True, False, [1]])
def test_parse_status_uninterpretable_mode_is_unknown_not_idle(
    raw_mode: Any,
) -> None:
    """A present-but-uninterpretable ``mode`` must report ``unknown``.

    Reporting ``idle`` would reproduce issue #100's core failure: the
    integration confidently showing a wrong state. ``bool`` is included
    because it subclasses ``int``, so ``True`` would otherwise read as
    the cleaning sentinel ``1``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(mode=raw_mode), result
    )
    assert result["activity"] == "unknown"


def test_uninterpretable_mode_warns_once_per_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The coordinator polls roughly every 10s, so a robot stuck on an
    uninterpretable ``mode`` must not emit a WARNING on every poll.
    Rate-limited per distinct raw value per process, mirroring
    ``ws_envelope.py``'s drift-warning sets.
    """
    from custom_components.iaqualink_robots.protocols import cyclonext, get_protocol

    cyclonext._warned_modes.clear()
    protocol = get_protocol("cyclonext")
    client = _make_client()

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            protocol.parse_status(
                client, _cyclonext_payload(mode="bogus"), {}
            )
        # A *different* bad value still warrants its own warning.
        protocol.parse_status(client, _cyclonext_payload(mode="other"), {})

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, (
        f"expected 1 warning per distinct bad mode, got {len(warnings)}"
    )
    cyclonext._warned_modes.clear()


def test_parse_status_resolves_cycle_duration_by_named_key() -> None:
    """Cycle 1 (floor_only) resolves ``durations["quickTim"]`` = 90.

    90 minutes for Floor is the value a live RE 4600 iQ reports, and it
    independently matches what an Alpha-series cyclonext operator reported.

    This is the assertion the suite was missing entirely: 237 protocol
    tests passed while no happy-path test checked ``cycle_duration`` at
    all, so the fixture could carry a payload shape the cloud never sends
    (a 4-element list) without anything noticing.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(mode=1, cycle=1), result
    )
    assert result["cycle"] == 1
    assert result["cycle_duration"] == 90
    assert result["cycle_start_time"] is not None


def test_parse_status_cycle_duration_for_floor_and_walls() -> None:
    """Cycle 3 (floor_and_walls) resolves ``durations["deepTim"]`` = 150.

    Named lookup, not positional. With the real 7-key cyclonext payload
    position 3 and key ``deepTim`` happen to coincide, but only because of
    the cloud's key insertion order; the named map removes that dependency.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(mode=1, cycle=3), result
    )
    assert result["cycle"] == 3
    assert result["cycle_duration"] == 150


def test_parse_status_duration_survives_reordered_durations_keys() -> None:
    """The whole point of the named map: a cloud that reorders its JSON
    keys must not shift every duration.

    Under the old positional lookup this returned ``waterTim`` (45) for
    cycle 1 instead of ``quickTim`` (75).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    payload = _cyclonext_payload(mode=1, cycle=1)
    robot = payload["payload"]["robot"]["state"]["reported"]["equipment"]["robot.1"]
    robot["durations"] = {
        "quickTim": 90,      # deliberately first now
        "waterTim": 45,
        "deepTim": 150,
        "smartTim": 0,
    }

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(client, payload, result)
    assert result["cycle_duration"] == 90


def test_parse_status_string_cycle_still_resolves_duration() -> None:
    """A string ``cycle`` must still resolve a duration.

    ``resolve_cycle_duration`` rejects non-int cycle codes by design, so
    without coercion at the parser boundary a string-echoing firmware
    would silently get ``cycle_duration = 0``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(mode="1", cycle="3"), result
    )
    assert result["cycle"] == 3
    assert result["cycle_duration"] == 150


@pytest.mark.parametrize("cycle", ["1", "3"])
def test_extract_fan_speed_decodes_string_cycle(cycle: str) -> None:
    """The cloud may echo ``cycle`` back as the string we sent it —
    ``set_fan_speed_payload`` deliberately sends strings. The decode map
    is int-keyed, so an uncoerced lookup silently fell through to the
    requested value and masked real drift.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {"equipment": {"robot.1": {"cycle": cycle}}},
                },
            },
        },
    }
    decoded = get_protocol("cyclonext").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded == _CYCLONEXT_EXPECTED[cycle]


_CYCLONEXT_EXPECTED = {"1": "floor_only", "3": "floor_and_walls"}


def test_parse_status_never_cleaned() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("cyclonext").parse_status(
        client, _cyclonext_payload(cycle_start=0), result
    )
    assert result["cycle_start_time"] is None
    assert result["cycle_end_time"] is None
    assert result["cycle"] == 0
    assert result["cycle_duration"] == 0


# ---------------------------------------------------------------------------
# Coordinator AST guard
# ---------------------------------------------------------------------------


def _coordinator_method_source(method_name: str) -> str:
    client_path = (
        Path(__file__).parent.parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "client.py"
    )
    tree = ast.parse(client_path.read_text(encoding="utf-8"))
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "AqualinkClient":
            continue
        for method in class_node.body:
            if (
                isinstance(method, (ast.AsyncFunctionDef, ast.FunctionDef))
                and method.name == method_name
            ):
                return ast.unparse(method)
    raise AssertionError(f"AqualinkClient.{method_name} not found in client.py")


@pytest.mark.parametrize(
    "method_name,protocol_call",
    [
        ("start_cleaning", "_require_protocol('cyclonext').start_payload"),
        ("stop_cleaning", "_require_protocol('cyclonext').stop_payload"),
        ("pause_cleaning", "_require_protocol('cyclonext').pause_payload"),
        ("_set_other_fan_speed", "_require_protocol('cyclonext').set_fan_speed_payload"),
    ],
)
def test_coordinator_cyclonext_branch_routes_through_protocol(
    method_name: str, protocol_call: str
) -> None:
    """Each cyclonext command-method branch must dispatch through the
    protocol. ``_extract_fan_speed_from_response`` is NOT in this list
    because R25-i2d collapsed it to a generic dispatch (single
    ``get_protocol(self._device_type)`` call serving all 5 families).
    """
    source = _coordinator_method_source(method_name)
    assert protocol_call in source, (
        f"AqualinkClient.{method_name}'s cyclonext branch must call "
        f"{protocol_call}(self). Got:\n{source}"
    )


def test_update_cyclonext_robot_data_delegates_to_protocol() -> None:
    client_path = (
        Path(__file__).parent.parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "client.py"
    )
    tree = ast.parse(client_path.read_text(encoding="utf-8"))
    method_body = None
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "AqualinkClient":
            continue
        for method in class_node.body:
            if (
                isinstance(method, ast.FunctionDef)
                and method.name == "_update_cyclonext_robot_data"
            ):
                method_body = method
                break
    assert method_body is not None
    assert len(method_body.body) <= 3, (
        f"_update_cyclonext_robot_data has {len(method_body.body)} top-level "
        f"statements post-R25-cyclonext; expected <=3."
    )
    source = ast.unparse(method_body)
    assert "_require_protocol('cyclonext').parse_status" in source
