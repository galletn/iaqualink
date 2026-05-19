"""VortraxProtocol regression tests for story R25-vortrax.

Mirror the R25-vr test suite for the Vortrax family. The two families
share the lifecycle envelope shapes (start/stop/pause/return-to-base
all use ``setCleanerState`` + ``robot.state``) and the fan-speed cloud
encoding (``setCleaningMode`` + ``prCyc`` 0–3). They differ in:

* Vortrax responses carry a ``product_number`` from
  ``eboxData.completeCleanerPn`` that VR doesn't surface.
* Vortrax has no ``clear_desired_payload`` (PR #94's auto-restart fix
  is VR-only); ``VortraxProtocol.clear_desired_payload`` returns ``None``
  from the abstract base default.
* Vortrax has no stepper UI (no ±15-minute buttons), so the parser
  doesn't read ``stepper`` / ``stepperAdjTime`` and the time math uses
  the simpler stepper-less path on ``_calculate_times``.

These tests lock the byte-identical-envelope contract (AC #4) and the
post-common-refactor parser behaviour. The shared helper
(``apply_common_robot_fields``) has its own test module so the
single-source contract is locked in two places: this file proves
Vortrax's parser uses it correctly, and ``test_common.py`` proves the
helper itself is correct.
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
    """Build a minimal ``AqualinkClient`` for the Vortrax protocol tests."""
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient.__new__(AqualinkClient)
    client._id = _ID
    client._auth_token = _AUTH
    client._app_client_id = _APP
    client._serial = _SERIAL
    client._device_type = "vortrax"
    client._fan_speed = None
    client._activity = None
    return client


def _expected_envelope(action: str, equipment: dict[str, Any]) -> dict[str, Any]:
    """Hand-rolled canonical R23 envelope shape with namespace=vortrax."""
    return {
        "action": action,
        "version": 1,
        "namespace": "vortrax",
        "payload": {
            "state": {"desired": {"equipment": equipment}},
            "clientToken": _TOKEN,
        },
        "service": "StateController",
        "target": _SERIAL,
    }


# ---------------------------------------------------------------------------
# Dispatch — R25-vortrax adds Vortrax to the table.
# ---------------------------------------------------------------------------


def test_dispatch_returns_vortrax_protocol_for_vortrax() -> None:
    from custom_components.iaqualink_robots.protocols import (
        VortraxProtocol,
        get_protocol,
    )

    protocol = get_protocol("vortrax")
    assert protocol is not None
    assert isinstance(protocol, VortraxProtocol)
    assert protocol.namespace == "vortrax"


def test_dispatch_still_returns_none_for_remaining_families() -> None:
    """Cyclobat / cyclonext / i2d remain inline-encoded; their R25 stories
    have not landed. Locks the "fall through to inline" contract.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    for family in ("cyclobat", "cyclonext", "i2d_robot"):
        assert get_protocol(family) is None, (
            f"R25-{family} hasn't shipped yet — dispatch must return None"
        )


# ---------------------------------------------------------------------------
# Envelope builders — byte-identical to pre-R25 inline form.
# ---------------------------------------------------------------------------


def test_start_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vortrax").start_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 1}}
    )


def test_stop_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vortrax").stop_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 0}}
    )


def test_pause_payload_matches_pre_r25() -> None:
    """Vortrax pause: same robot.state = 2 as VR."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vortrax").pause_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 2}}
    )


def test_return_to_base_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vortrax").return_to_base_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 3}}
    )


def test_clear_desired_payload_is_none_for_vortrax() -> None:
    """Vortrax has no documented auto-restart bug (PR #94 was VR-only).
    The base class returns ``None`` by default; VortraxProtocol doesn't
    override. If a future Vortrax operator reports the symptom, override
    here and update the test.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    assert get_protocol("vortrax").clear_desired_payload(client) is None


@pytest.mark.parametrize(
    "fan_speed,expected_pr_cyc",
    [
        ("wall_only", 0),
        ("floor_only", 1),
        ("smart_floor_and_walls", 2),
        ("floor_and_walls", 3),
    ],
)
def test_set_fan_speed_payload_matches_pre_r25(
    fan_speed: str, expected_pr_cyc: int
) -> None:
    """All 4 prCyc values are encoded the same as VR — the cloud accepts
    them, even though ``vacuum.py``'s ``_fan_speed_list`` only exposes
    2 modes to the user. The 2-vs-4 restriction lives at the UI layer.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vortrax").set_fan_speed_payload(client, fan_speed)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot": {"prCyc": expected_pr_cyc}}
    )


def test_set_fan_speed_payload_returns_none_for_unknown_key() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vortrax").set_fan_speed_payload(client, "nonsense")
    assert envelope is None


def test_fan_speed_codes_matches_vr_encoding() -> None:
    """Vortrax cloud encoding == VR cloud encoding; the families differ
    only in which subset ``vacuum.py`` exposes to the user. Lock the
    full 4-key map here so a firmware-only refactor that enables all 4
    modes on the Vortrax UI doesn't need a protocol change.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    codes = get_protocol("vortrax").fan_speed_codes()
    assert codes == {
        "wall_only": 0,
        "floor_only": 1,
        "smart_floor_and_walls": 2,
        "floor_and_walls": 3,
    }


@pytest.mark.parametrize(
    "pr_cyc,expected_key",
    [
        (0, "wall_only"),
        (1, "floor_only"),
        (2, "smart_floor_and_walls"),
        (3, "floor_and_walls"),
    ],
)
def test_extract_fan_speed_decodes_pr_cyc(pr_cyc: int, expected_key: str) -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {"robot": {"prCyc": pr_cyc}},
                    },
                },
            },
        },
    }
    decoded = get_protocol("vortrax").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded == expected_key


def test_extract_fan_speed_returns_none_when_missing() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {"robot": {"state": {"reported": {"equipment": {"robot": {}}}}}},
    }
    decoded = get_protocol("vortrax").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded is None


# ---------------------------------------------------------------------------
# parse_status — common-helper + Vortrax-specific fields.
# ---------------------------------------------------------------------------


def _vortrax_payload(**overrides: Any) -> dict[str, Any]:
    """Build a complete Vortrax StateReported payload with sane defaults."""
    robot = {
        "state": 1,
        "errorState": 0,
        "canister": 0.50,
        "totalHours": 123,
        "prCyc": 1,
        "cycleStartTime": 1_700_000_000,
        "durations": [60, 90, 120, 150],
        "sensors": {"sns_1": {"val": "21.5"}},
    }
    ebox = {"completeCleanerPn": "VTX-PN-001"}

    robot_overrides = overrides.pop("robot", {})
    ebox_overrides = overrides.pop("ebox", None)

    robot.update(robot_overrides)
    if ebox_overrides is None:
        ebox_block: dict[str, Any] | None = ebox
    elif ebox_overrides is False:
        ebox_block = None
    else:
        ebox_block = dict(ebox)
        ebox_block.update(ebox_overrides)

    reported: dict[str, Any] = {"equipment": {"robot": robot}}
    if ebox_block is not None:
        reported["eboxData"] = ebox_block

    return {"payload": {"robot": {"state": {"reported": reported}}}}


def test_parse_status_no_payload_marks_unknown() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("vortrax").parse_status(client, {}, result)
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


def test_parse_status_malformed_payload_marks_unknown() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("vortrax").parse_status(
        client, {"payload": {"oops": True}}, result
    )
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


def test_parse_status_extracts_product_number() -> None:
    """Vortrax-specific: ``eboxData.completeCleanerPn`` is surfaced as
    ``result["product_number"]``. Distinguishes Vortrax from VR in the
    parse layer.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _vortrax_payload(ebox={"completeCleanerPn": "VTX-12345"})
    get_protocol("vortrax").parse_status(client, payload, result)
    assert result["product_number"] == "VTX-12345"


def test_parse_status_product_number_none_when_missing() -> None:
    """Missing ``eboxData`` block → product_number is None (Vortrax-specific
    field with explicit None fallback, not 'unknown' string).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _vortrax_payload(ebox=False)
    get_protocol("vortrax").parse_status(client, payload, result)
    assert result["product_number"] is None


@pytest.mark.parametrize(
    "robot_state,expected_activity",
    [
        (1, "cleaning"),
        (3, "returning"),
        (0, "idle"),
        (2, "idle"),
    ],
)
def test_parse_status_activity_mapping(
    robot_state: int, expected_activity: str
) -> None:
    """The activity mapping is shared with VR (via
    ``apply_common_robot_fields``). This test independently locks the
    Vortrax side so a future refactor that breaks one parser's path
    without the other can't slip through.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _vortrax_payload(robot={"state": robot_state})
    get_protocol("vortrax").parse_status(client, payload, result)
    assert result["activity"] == expected_activity


def test_parse_status_canister_via_common() -> None:
    """``canister`` 0-1 → 0-100 % via the shared helper."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _vortrax_payload(robot={"canister": 0.75})
    get_protocol("vortrax").parse_status(client, payload, result)
    assert result["canister"] == 75.0


def test_parse_status_never_cleaned_clears_cycle_and_times() -> None:
    """Vortrax-specific: when ``cycleStartTime == 0`` the parser clears
    cycle/cycle_duration/time_remaining AND also resets ``cycle = 0``
    (VR's parser doesn't clear ``cycle`` in this branch; the divergence
    is intentional and pre-R25).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _vortrax_payload(robot={"cycleStartTime": 0})
    get_protocol("vortrax").parse_status(client, payload, result)
    assert result["cycle_start_time"] is None
    assert result["cycle_end_time"] is None
    assert result["estimated_end_time"] is None
    assert result["cycle"] == 0
    assert result["cycle_duration"] == 0
    assert result["time_remaining"] == 0


def test_parse_status_does_not_write_stepper_or_fan_speed() -> None:
    """Vortrax has no stepper UI; the parser must NOT touch ``stepper``
    or ``stepper_adj_time`` (which are VR-only fields). Also: Vortrax
    doesn't write ``fan_speed`` from the prCyc field — the polled-path
    fan_speed comes from the cycle map, not from a per-poll read.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _vortrax_payload()
    get_protocol("vortrax").parse_status(client, payload, result)
    assert "stepper" not in result
    assert "stepper_adj_time" not in result
    # ``fan_speed`` is not set by Vortrax's parser; VR's parser does set it.
    assert "fan_speed" not in result


# ---------------------------------------------------------------------------
# Coordinator AST guard — Vortrax branches route through the protocol.
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
                isinstance(method, ast.AsyncFunctionDef)
                and method.name == method_name
            ):
                return ast.unparse(method)
    raise AssertionError(f"AqualinkClient.{method_name} not found")


@pytest.mark.parametrize(
    "method_name,protocol_call",
    [
        ("start_cleaning", "get_protocol('vortrax').start_payload"),
        ("stop_cleaning", "get_protocol('vortrax').stop_payload"),
        ("pause_cleaning", "get_protocol('vortrax').pause_payload"),
        ("return_to_base", "get_protocol('vortrax').return_to_base_payload"),
        ("_set_other_fan_speed", "get_protocol('vortrax').set_fan_speed_payload"),
    ],
)
def test_coordinator_vortrax_branch_routes_through_protocol(
    method_name: str, protocol_call: str
) -> None:
    """Each command-method's Vortrax branch must dispatch through the
    protocol, not via re-inlined ``_build_state_request(...)``. AST-level
    so the check is whitespace-insensitive but shape-strict.
    """
    source = _coordinator_method_source(method_name)
    assert protocol_call in source, (
        f"AqualinkClient.{method_name}'s Vortrax branch must call "
        f"{protocol_call}(self). Got:\n{source}"
    )


def test_update_vortrax_robot_data_delegates_to_protocol_parse_status() -> None:
    """``_update_vortrax_robot_data`` is now a 2-statement delegate to
    ``VortraxProtocol.parse_status`` — same shape as ``_update_vr_robot_data``.
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
                isinstance(method, ast.FunctionDef)
                and method.name == "_update_vortrax_robot_data"
            ):
                method_body = method
                break
    assert method_body is not None
    assert len(method_body.body) <= 3, (
        f"_update_vortrax_robot_data has {len(method_body.body)} top-level "
        f"statements post-R25-vortrax; expected <=3 (docstring + delegate)."
    )
    source = ast.unparse(method_body)
    assert "get_protocol('vortrax').parse_status" in source
