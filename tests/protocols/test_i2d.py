"""I2DProtocol regression tests for story R25-i2d (with R27 fold).

i2d is the outlier of the 5 device families — it uses HTTP POST not
WebSocket, with hex-encoded status responses (18 bytes, 14 fields).
R25-i2d (which folds R27) extracts:

* ``parse_i2d_payload(hex_str) -> ParsedI2DFrame`` as a **pure**
  function — no I/O, no logging, no globals beyond the module-level
  ``STATE_MAP`` / ``ERROR_MAP`` / ``MODE_MAP`` constants.
* ``I2DProtocol`` with HTTP-body envelope builders (not WS envelopes),
  the ``i2d_url(client)`` helper for the cloud control URL, and a
  ``parse_status`` that wraps the pure parser + does side-effects.

This file independently locks both halves: a parametrized matrix over
the pure parser's inputs (≥10 hex payloads per spec AC #5), and the
protocol class as a whole (envelope shapes, fan-speed encoding,
parse_status behaviour including the i2d-unique "error → activity =
error" mapping).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest


def _make_client() -> Any:
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    c = AqualinkClient.__new__(AqualinkClient)
    c._id = "test_user_id"
    c._auth_token = "test_auth_tok"
    c._app_client_id = "test_app_client"
    c._serial = "SERIAL_X"
    c._device_type = "i2d_robot"
    c._activity = None
    c._fan_speed = None
    c._debug_mode = False
    return c


def _hex_payload(
    state: int = 0x04,
    error: int = 0x00,
    mode: int = 0x00,
    canister_nibble: int = 0x00,
    time_remaining: int = 30,
    uptime: int = 120,
    total_hours: int = 5000,
    hardware_id: str = "aabbcc",
    firmware_id: str = "112233",
) -> str:
    """Build a synthetic 18-byte i2d status payload as a hex string."""
    b = bytearray(18)
    b[0] = 0x00  # header byte 0
    b[1] = 0x00  # header byte 1
    b[2] = state
    b[3] = error
    # Mode byte: canister flag in upper nibble, mode_code in lower nibble.
    b[4] = (canister_nibble << 4) | mode
    b[5] = time_remaining
    b[6:9] = uptime.to_bytes(3, "little")
    b[9:12] = total_hours.to_bytes(3, "little")
    b[12:15] = bytes.fromhex(hardware_id)
    b[15:18] = bytes.fromhex(firmware_id)
    return b.hex()


# ---------------------------------------------------------------------------
# AC #3 — dispatch returns I2DProtocol for "i2d_robot".
# ---------------------------------------------------------------------------


def test_dispatch_returns_i2d_protocol() -> None:
    from custom_components.iaqualink_robots.protocols import (
        I2DProtocol,
        get_protocol,
    )

    protocol = get_protocol("i2d_robot")
    assert protocol is not None
    assert isinstance(protocol, I2DProtocol)
    assert protocol.namespace == "i2d_robot"


def test_post_r25_all_five_families_extracted() -> None:
    """Post-R25-i2d, every supported family has a protocol — the
    "None for unmigrated" path now only fires for unknown device types.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    for family in ("vr", "vortrax", "cyclobat", "cyclonext", "i2d_robot"):
        assert get_protocol(family) is not None, (
            f"All 5 R25 families must have a protocol after R25-i2d; "
            f"{family} returned None"
        )


# ---------------------------------------------------------------------------
# Pure parser — AC #5 requires ≥10 hex payload fixtures.
# ---------------------------------------------------------------------------


def test_parse_payload_active_cleaning() -> None:
    from custom_components.iaqualink_robots.protocols.i2d import (
        ParsedI2DFrame,
        parse_i2d_payload,
    )

    frame = parse_i2d_payload(_hex_payload(state=0x04, mode=0x00))
    assert isinstance(frame, ParsedI2DFrame)
    assert frame.state == "Actively cleaning"
    assert frame.error == "no_error"
    assert frame.cycle == "Quick clean floor only (standard)"


@pytest.mark.parametrize(
    "state_code,expected_display",
    [
        (0x01, "Idle / Docked"),
        (0x02, "Cleaning just started"),
        (0x03, "Finished"),
        (0x04, "Actively cleaning"),
        (0x0C, "Paused"),
        (0x0D, "Error state D"),
        (0x0E, "Error state E"),
    ],
)
def test_parse_payload_all_states(state_code: int, expected_display: str) -> None:
    """Spec AC #5: parametrized over the full STATE_MAP keys."""
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(state=state_code))
    assert frame.state_code == state_code
    assert frame.state == expected_display


@pytest.mark.parametrize(
    "error_code,expected_key",
    [
        (0x00, "no_error"),
        (0x01, "pump_short_circuit"),
        (0x02, "right_drive_motor_short_circuit"),
        (0x03, "left_drive_motor_short_circuit"),
        (0x04, "pump_motor_overconsumption"),
        (0x05, "right_drive_motor_overconsumption"),
        (0x06, "left_drive_motor_overconsumption"),
        (0x07, "floats_on_surface"),
        (0x08, "running_out_of_water"),
        (0x0A, "communication_error"),
    ],
)
def test_parse_payload_all_errors(error_code: int, expected_key: str) -> None:
    """Spec AC #5: parametrized over ERROR_MAP."""
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(error=error_code))
    assert frame.error_code == error_code
    assert frame.error == expected_key


@pytest.mark.parametrize(
    "mode_code,expected_display",
    [
        (0x00, "Quick clean floor only (standard)"),
        (0x03, "Deep clean floor + walls (high power)"),
        (0x04, "Waterline only (standard)"),
        (0x08, "Quick – floor only (standard)"),
        (0x09, "Custom – floor only (high power)"),
        (0x0A, "Custom – floor + walls (standard)"),
        (0x0B, "Custom – floor + walls (high power)"),
        (0x0D, "Custom – waterline (high power)"),
        (0x0E, "Custom – waterline (standard)"),
    ],
)
def test_parse_payload_all_modes(mode_code: int, expected_display: str) -> None:
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(mode=mode_code))
    assert frame.mode_code == mode_code
    assert frame.cycle == expected_display


def test_parse_payload_canister_full_flag() -> None:
    """The canister-full bit lives in the upper nibble of byte 4 (any
    non-zero value sets the flag). Mode_code is in the lower nibble.
    """
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(canister_nibble=0x0F, mode=0x03))
    assert frame.canister_full is True
    assert frame.mode_code == 0x03


def test_parse_payload_canister_empty_flag() -> None:
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(canister_nibble=0x00, mode=0x00))
    assert frame.canister_full is False


def test_parse_payload_time_uptime_total_hours() -> None:
    """Byte 5 is time_remaining (uint8). Bytes 6-8 are uptime_minutes
    little-endian uint24. Bytes 9-11 are total_hours little-endian uint24.
    """
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(time_remaining=42, uptime=1000, total_hours=9999))
    assert frame.time_remaining == 42
    assert frame.uptime_minutes == 1000
    assert frame.total_hours == 9999


def test_parse_payload_hardware_and_firmware_ids() -> None:
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(
        _hex_payload(hardware_id="deadbe", firmware_id="cafe01")
    )
    assert frame.hardware_id == "deadbe"
    assert frame.firmware_id == "cafe01"


def test_parse_payload_unknown_state_falls_back_to_hex_display() -> None:
    """Unknown state code → ``"Unknown (0xFF)"`` (pre-R25 behaviour
    preserved). Same fallback shape for error and mode.
    """
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(_hex_payload(state=0xFF, error=0xFE, mode=0x0F))
    assert frame.state == "Unknown (0xFF)"
    assert frame.error == "Unknown (0xFE)"
    assert frame.cycle == "Unknown (0x0F)"


def test_parse_payload_tolerates_spaces_in_hex_string() -> None:
    """Real cloud responses include whitespace between hex byte pairs."""
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    frame = parse_i2d_payload(
        "00 00 04 00 00 1E 78 00 00 88 13 00 AA BB CC 11 22 33"
    )
    assert frame.state == "Actively cleaning"
    assert frame.time_remaining == 30


def test_parse_payload_wrong_length_raises_value_error() -> None:
    """Spec AC #5: malformed hex → clean exception."""
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    with pytest.raises(ValueError, match="Expected 36"):
        parse_i2d_payload("00" * 17)  # 34 chars, too short


def test_parse_payload_non_hex_chars_raises_value_error() -> None:
    from custom_components.iaqualink_robots.protocols.i2d import parse_i2d_payload

    with pytest.raises(ValueError):
        parse_i2d_payload("zz" * 18)


def test_module_level_constants_present() -> None:
    """Spec AC #1: STATE_MAP / ERROR_MAP / MODE_MAP are module-level
    constants, not inline dicts. Hoisted out of the pre-R25
    god-function so they're constructed once.
    """
    from custom_components.iaqualink_robots.protocols.i2d import (
        ERROR_MAP,
        MODE_MAP,
        STATE_MAP,
    )

    assert STATE_MAP[0x04] == "Actively cleaning"
    assert ERROR_MAP[0x07] == "floats_on_surface"
    assert MODE_MAP[0x03] == "Deep clean floor + walls (high power)"


# ---------------------------------------------------------------------------
# Envelope builders — HTTP body shape, not WS envelopes.
# ---------------------------------------------------------------------------


def test_start_payload_is_http_body_with_hex_command() -> None:
    """i2d's start_payload returns an HTTP body dict
    (``{command, params, user_id}``), NOT a WS envelope. The
    ``params`` carries the hex command + timeout.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("i2d_robot").start_payload(client)
    assert envelope == {
        "command": "/command",
        "params": "request=0A1240&timeout=800",
        "user_id": "test_user_id",
    }


def test_stop_payload_uses_0A1210() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("i2d_robot").stop_payload(client)
    assert envelope["params"] == "request=0A1210&timeout=800"


def test_pause_payload_equals_stop_payload() -> None:
    """i2d has no dedicated pause command — pre-R25 ``pause_cleaning``
    for i2d delegated to ``stop_cleaning``. The protocol mirrors this
    by having ``pause_payload`` return the same dict as ``stop_payload``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    proto = get_protocol("i2d_robot")
    assert proto.pause_payload(client) == proto.stop_payload(client)


def test_return_to_base_payload_uses_0A1701() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("i2d_robot").return_to_base_payload(client)
    assert envelope["params"] == "request=0A1701&timeout=800"


def test_clear_desired_payload_is_none() -> None:
    """PR #94's auto-restart fix is VR-only."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert get_protocol("i2d_robot").clear_desired_payload(client) is None


def test_i2d_url_helper() -> None:
    """``i2d_url`` is an i2d-only helper (not on the abstract base)
    that builds the cloud control URL from the client's serial.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    url = get_protocol("i2d_robot").i2d_url(client)
    assert url == "https://r-api.iaqualink.net/v2/devices/SERIAL_X/control.json"


# ---------------------------------------------------------------------------
# Fan-speed encoding — walls_only (plural) is i2d-specific.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fan_speed,expected_hex",
    [
        ("walls_only", "0A1284"),
        ("floor_only", "0A1280"),
        ("floor_and_walls", "0A1283"),
    ],
)
def test_set_fan_speed_payload_each_mode(
    fan_speed: str, expected_hex: str
) -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("i2d_robot").set_fan_speed_payload(client, fan_speed)
    assert envelope == {
        "command": "/command",
        "params": f"request={expected_hex}&timeout=800",
        "user_id": "test_user_id",
    }


@pytest.mark.parametrize(
    "invalid_speed",
    ["wall_only", "smart_floor_and_walls", "nonsense"],
)
def test_set_fan_speed_returns_none_for_non_i2d_keys(invalid_speed: str) -> None:
    """Critical: i2d uses ``walls_only`` (plural, waterline mode); the
    ``wall_only`` (singular, VR dedicated wall-scrub) key MUST NOT be
    accepted. This is the parity contract the CLAUDE.md spec calls
    out and the existing ``tests/test_vacuum.py::test_wall_only_and_
    walls_only_are_distinct_keys`` locks at the entity layer.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert (
        get_protocol("i2d_robot").set_fan_speed_payload(client, invalid_speed)
        is None
    )


def test_fan_speed_codes_uses_walls_only_not_wall_only() -> None:
    """Lock the i2d-specific encoding: ``walls_only`` (plural) IS in
    the map; ``wall_only`` (singular, VR/cyclobat dedicated wall-scrub)
    is NOT. Pre-R25 inline check at coordinator level; locked here at
    the protocol layer too.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    codes = get_protocol("i2d_robot").fan_speed_codes()
    assert "walls_only" in codes
    assert "wall_only" not in codes, (
        "i2d uses walls_only (plural, waterline mode), not wall_only "
        "(singular, VR dedicated wall-scrub). See CLAUDE.md device-type "
        "gotcha section."
    )


def test_extract_fan_speed_from_response_returns_none() -> None:
    """i2d HTTP responses don't echo a confirmed fan-speed field the
    way the WS families do. The protocol returns ``None`` to signal
    the caller should use the requested value as-is.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    proto = get_protocol("i2d_robot")
    # Even with a fully-populated response payload, returns None.
    assert proto.extract_fan_speed_from_response({}, "floor_only") is None
    assert (
        proto.extract_fan_speed_from_response(
            {"command": {"response": "00 00 04 00"}}, "floor_only"
        )
        is None
    )


# ---------------------------------------------------------------------------
# parse_status — wraps the pure parser + side-effects.
# ---------------------------------------------------------------------------


def _i2d_response(hex_str: str) -> dict[str, Any]:
    return {"command": {"request": "OA11", "response": hex_str}}


def test_parse_status_active_cleaning_sets_side_effects() -> None:
    """parse_status sets both ``result["activity"]`` (M11 raw key) and
    ``client._activity`` (VacuumActivity enum mirror) for the vacuum
    entity. Same for fan_speed.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response(_hex_payload(state=0x04, mode=0x03, time_remaining=30)),
        result,
    )
    assert result["status"] == "connected"
    assert result["activity"] == "cleaning"
    assert result["fan_speed"] == "floor_and_walls"
    assert client._fan_speed == 3


def test_parse_status_error_flips_activity_to_error() -> None:
    """i2d-unique: a non-zero error_code flips ``result["activity"]`` to
    ``"error"`` (none of the other 4 families do this — they leave
    activity at idle/cleaning/returning and route errors through the
    error_state field).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response(_hex_payload(state=0x01, error=0x07)), result
    )
    assert result["activity"] == "error"
    assert result["error_state"] == "floats_on_surface"


def test_parse_status_idle() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response(_hex_payload(state=0x01, error=0x00)), result
    )
    assert result["activity"] == "idle"
    assert result["error_state"] == "no_error"


def test_parse_status_just_started_cleaning_counts_as_cleaning() -> None:
    """state_code 0x02 ("Cleaning just started") also maps to
    activity=cleaning (the cloud reports 0x02 briefly at the start of
    a cycle before flipping to 0x04). Pre-R25 logic preserved.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response(_hex_payload(state=0x02, time_remaining=60)), result
    )
    assert result["activity"] == "cleaning"


def test_parse_status_non_status_request_returns_without_parsing() -> None:
    """When the response is from a non-status request (e.g. a
    fan-speed set command), parse_status bails after seeding the
    default keys. The body isn't a status hex frame so it can't be
    parsed.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client,
        {"command": {"request": "0A1240", "response": "ack"}},
        result,
    )
    assert result["status"] == "offline"
    assert "activity" not in result


def test_parse_status_malformed_hex_marks_update_failed() -> None:
    """Pre-R25 caught both wrong-length and non-hex errors in the
    same outer except and set ``error_state=update_failed``. Preserved.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response("not_hex_at_all"), result
    )
    assert result["error_state"] == "update_failed"


def test_parse_status_estimated_end_time_set_only_when_cleaning() -> None:
    """The H8-aware-UTC estimated_end_time is only set when the robot
    is actively cleaning AND has remaining minutes > 0. Idle / error
    states leave ``estimated_end_time`` unset.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    # Idle → no estimated_end_time.
    result: dict[str, Any] = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response(_hex_payload(state=0x01, time_remaining=30)), result
    )
    assert "estimated_end_time" not in result

    # Cleaning with time_remaining > 0 → estimated_end_time set.
    result = {}
    get_protocol("i2d_robot").parse_status(
        client, _i2d_response(_hex_payload(state=0x04, time_remaining=30)), result
    )
    assert "estimated_end_time" in result


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
    "method_name,protocol_method",
    [
        ("start_cleaning", "start_payload"),
        ("stop_cleaning", "stop_payload"),
        ("return_to_base", "return_to_base_payload"),
        ("_set_i2d_fan_speed", "set_fan_speed_payload"),
    ],
)
def test_coordinator_i2d_branch_routes_through_protocol(
    method_name: str, protocol_method: str
) -> None:
    """Each i2d command method must reference both ``get_protocol(
    'i2d_robot')`` and the matching protocol method. The coordinator
    pattern is ``i2d = get_protocol(...); i2d.<method>(self)`` rather
    than the inline single-call form the WS families use, so the
    check verifies both halves appear in the same method body.
    """
    source = _coordinator_method_source(method_name)
    assert "get_protocol('i2d_robot')" in source, (
        f"AqualinkClient.{method_name}'s i2d branch must reference "
        f"get_protocol('i2d_robot')"
    )
    assert f".{protocol_method}(" in source, (
        f"AqualinkClient.{method_name}'s i2d branch must call "
        f".{protocol_method}(...)"
    )


def test_update_i2d_robot_reduced_to_under_30_statements() -> None:
    """Spec AC #2: ``_update_i2d_robot`` must be ≤ 30 lines (HTTP
    fetch + delegate to protocol). Pre-R25-i2d was 138 lines including
    the inline lookup tables.
    """
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
                isinstance(method, ast.AsyncFunctionDef)
                and method.name == "_update_i2d_robot"
            ):
                method_body = method
                break
    assert method_body is not None
    stmt_count = len(method_body.body)
    assert stmt_count <= 30, (
        f"_update_i2d_robot has {stmt_count} top-level statements "
        f"post-R25-i2d (spec budget ≤30; pre-R25 was ~138)."
    )
    source = ast.unparse(method_body)
    # Body should delegate to protocol's parse_status — either via local
    # variable (``i2d.parse_status``) or directly through get_protocol.
    assert "parse_status" in source, (
        "_update_i2d_robot must delegate to the i2d protocol's parse_status"
    )
