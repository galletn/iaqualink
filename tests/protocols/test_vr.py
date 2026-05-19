"""VRProtocol regression tests for story R25-vr.

R25-vr extracted the VR family's cloud behaviour into
``custom_components/iaqualink_robots/protocols/vr.py``:

* Envelope builders (``start_payload`` / ``stop_payload`` /
  ``pause_payload`` / ``return_to_base_payload`` /
  ``clear_desired_payload`` / ``set_fan_speed_payload``).
* Cloud-encoding helpers (``fan_speed_codes`` /
  ``extract_fan_speed_from_response``).
* Status parser (``parse_status``) — lifted verbatim from pre-R25
  ``AqualinkClient._update_vr_robot_data``.

These tests lock the AC #4 contract — byte-identical envelopes and
behaviour-identical parse output vs pre-R25 — by:

* Comparing each envelope builder's output to the dict shape every
  pre-R25 inline branch produced (asserted by direct ``==`` on the
  full envelope dict).
* Running the parser against the per-cycle-state fixture set the
  pre-R25 ``_update_vr_robot_data`` produced; output dict equality.

Plus AC #3 — the dispatch table ``_DEVICE_PROTOCOLS["vr"]`` returns
a ``VRProtocol`` instance.
"""

from __future__ import annotations

from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Fixtures — minimal AqualinkClient shell mirrors the patterns used by
# tests/test_envelope.py and tests/test_stepper.py.
# ---------------------------------------------------------------------------


_ID = "test_user_id"
_AUTH = "test_auth_tok"
_APP = "test_app_client"
_SERIAL = "TEST_SERIAL"
_TOKEN = f"{_ID}|{_AUTH}|{_APP}"


def _make_client() -> Any:
    """Build a minimal ``AqualinkClient`` for the VR protocol tests.

    Uses ``__new__`` to bypass ``__init__`` (which reaches into aiohttp
    + asyncio). The envelope helpers only read four ``str`` attributes
    plus ``_device_type``; the parse helpers additionally need the
    instance attributes the legacy parser mutated (``_activity``,
    ``_fan_speed``) and the helper methods it called
    (``_resolve_cycle_duration``, ``_calculate_times``,
    ``_format_time_human``). The real methods are bound by class so
    they work via ``__new__``-constructed instances.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient.__new__(AqualinkClient)
    client._id = _ID
    client._auth_token = _AUTH
    client._app_client_id = _APP
    client._serial = _SERIAL
    client._device_type = "vr"
    client._fan_speed = None  # populated by parse_status
    client._activity = None  # populated by parse_status
    return client


def _expected_envelope(action: str, equipment: dict[str, Any]) -> dict[str, Any]:
    """Hand-rolled canonical R23 envelope shape — what every VR envelope
    builder should produce after dispatching through ``_build_state_request``.
    """
    return {
        "action": action,
        "version": 1,
        "namespace": "vr",
        "payload": {
            "state": {"desired": {"equipment": equipment}},
            "clientToken": _TOKEN,
        },
        "service": "StateController",
        "target": _SERIAL,
    }


# ---------------------------------------------------------------------------
# AC #3 — dispatch table returns the VR protocol.
# ---------------------------------------------------------------------------


def test_dispatch_returns_vr_protocol_for_vr() -> None:
    """``get_protocol("vr")`` returns a ``VRProtocol`` instance."""
    from custom_components.iaqualink_robots.protocols import (
        VRProtocol,
        get_protocol,
    )

    protocol = get_protocol("vr")
    assert protocol is not None
    assert isinstance(protocol, VRProtocol)
    assert protocol.namespace == "vr"


def test_dispatch_returns_none_for_unmigrated_families() -> None:
    """Until the matching R25 story lands, other families return ``None``.

    Locks the "fall through to inline branch" contract — if a future PR
    populated this table for vortrax/cyclobat/cyclonext/i2d without also
    migrating coordinator.py, that PR's matching test below catches the
    drift.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    for family in ("vortrax", "cyclobat", "cyclonext", "i2d_robot"):
        assert get_protocol(family) is None, (
            f"R25-{family} hasn't shipped yet — dispatch must return None "
            f"and coordinator.py must keep the inline branch for now"
        )


# ---------------------------------------------------------------------------
# AC #1 + #4 — envelope builders produce the pre-R25 dict shape.
# ---------------------------------------------------------------------------


def test_start_payload_matches_pre_r25() -> None:
    """``start_payload`` must equal the pre-R25 inline shape:
    ``setCleanerState`` + ``{"state": 1}``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").start_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 1}}
    )


def test_stop_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").stop_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 0}}
    )


def test_pause_payload_matches_pre_r25() -> None:
    """VR's pause: ``robot.state = 2``."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").pause_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 2}}
    )


def test_return_to_base_payload_matches_pre_r25() -> None:
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").return_to_base_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 3}}
    )


def test_clear_desired_payload_matches_pre_r25() -> None:
    """PR #94 natural-completion fix: clear desired.state = 0 (same shape
    as stop_payload — kept distinct so the call site reads honestly).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").clear_desired_payload(client)
    assert envelope == _expected_envelope(
        "setCleanerState", {"robot": {"state": 0}}
    )


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
    """``set_fan_speed_payload`` produces the pre-R25 ``setCleaningMode`` +
    ``{"prCyc": <int>}`` shape for each of the 4 VR fan-speed keys.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").set_fan_speed_payload(client, fan_speed)
    assert envelope == _expected_envelope(
        "setCleaningMode", {"robot": {"prCyc": expected_pr_cyc}}
    )


def test_set_fan_speed_payload_returns_none_for_unknown_key() -> None:
    """Invalid fan-speed key → ``None`` envelope; coordinator falls through
    to the bottom-of-function failure path (matches pre-R25 behaviour).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    envelope = get_protocol("vr").set_fan_speed_payload(client, "nonsense_speed")
    assert envelope is None


def test_fan_speed_codes_contract() -> None:
    """The fan-speed encoding table matches the four VR translation keys
    in CLAUDE.md (vr family supports all 4 cycle modes). The ``wall_only``
    vs ``walls_only`` distinction is part of the contract — VR uses
    ``wall_only`` (singular). Locked here to mirror the
    ``tests/test_vacuum.py::test_wall_only_and_walls_only_are_distinct_keys``
    guard at the protocol layer.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    codes = get_protocol("vr").fan_speed_codes()
    assert codes == {
        "wall_only": 0,
        "floor_only": 1,
        "smart_floor_and_walls": 2,
        "floor_and_walls": 3,
    }
    assert "walls_only" not in codes, (
        "VR uses `wall_only` (singular) — `walls_only` (plural) is the i2d "
        "waterline-only mode and must NOT appear in VR's fan-speed map"
    )


# ---------------------------------------------------------------------------
# extract_fan_speed_from_response — inverse of set_fan_speed_payload.
# ---------------------------------------------------------------------------


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
    """Cloud response carries ``prCyc`` in the equipment.robot block; the
    decoder converts it back to the integration's translation key.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {
                            "robot": {"prCyc": pr_cyc},
                        },
                    },
                },
            },
        },
    }
    decoded = get_protocol("vr").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded == expected_key


def test_extract_fan_speed_returns_requested_when_pr_cyc_unknown() -> None:
    """If the cloud reports a ``prCyc`` value not in the map, fall back
    to the user's requested value rather than masking the cloud anomaly
    with a default. Same behaviour as pre-R25.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {
                            "robot": {"prCyc": 99},
                        },
                    },
                },
            },
        },
    }
    decoded = get_protocol("vr").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded == "floor_only"


def test_extract_fan_speed_returns_none_when_pr_cyc_missing() -> None:
    """No ``prCyc`` in the response → ``None`` (the caller decides whether
    to keep the previous value or surface the gap).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    response_data = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {"equipment": {"robot": {}}},
                },
            },
        },
    }
    decoded = get_protocol("vr").extract_fan_speed_from_response(
        response_data, requested_fan_speed="floor_only"
    )
    assert decoded is None


# ---------------------------------------------------------------------------
# AC #1 + #4 — parse_status mirrors pre-R25 _update_vr_robot_data.
# ---------------------------------------------------------------------------


def _full_vr_payload(**robot_overrides: Any) -> dict[str, Any]:
    """Build a complete VR ``StateReported`` payload with sane defaults
    for every field the parser reads. Overrides for the field-under-test
    in each parametrised case.
    """
    robot = {
        "state": 1,
        "errorState": 0,
        "canister": 0.50,
        "totalHours": 123,
        "stepper": 5,
        "stepperAdjTime": 15,
        "prCyc": 1,
        "cycleStartTime": 1_700_000_000,
        "durations": [60, 90, 120, 150],
        "sensors": {"sns_1": {"val": "21.5"}},
    }
    robot.update(robot_overrides)
    return {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {"robot": robot},
                    },
                },
            },
        },
    }


def test_parse_status_no_payload_marks_unknown() -> None:
    """Missing top-level ``payload`` → activity=unknown, error_state=no_data."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("vr").parse_status(client, {}, result)
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


def test_parse_status_malformed_payload_marks_unknown() -> None:
    """Payload without the expected ``equipment.robot`` nesting → same
    no-data signal. The KeyError/TypeError is caught and silenced.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    get_protocol("vr").parse_status(client, {"payload": {"oops": True}}, result)
    assert result["activity"] == "unknown"
    assert result["error_state"] == "no_data"


@pytest.mark.parametrize(
    "robot_state,expected_activity",
    [
        (1, "cleaning"),
        (3, "returning"),
        (0, "idle"),
        (2, "idle"),  # any non-1/non-3 maps to idle
    ],
)
def test_parse_status_activity_mapping(
    robot_state: int, expected_activity: str
) -> None:
    """``robot.state`` maps to the integration's activity raw key (post-M11
    these are lowercase machine keys, not title-cased English).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _full_vr_payload(state=robot_state)
    get_protocol("vr").parse_status(client, payload, result)
    assert result["activity"] == expected_activity


@pytest.mark.parametrize(
    "pr_cyc,expected_fan_speed",
    [
        (0, "wall_only"),
        (1, "floor_only"),
        (2, "smart_floor_and_walls"),
        (3, "floor_and_walls"),
        (99, "floor_only"),  # unknown prCyc → "floor_only" default
    ],
)
def test_parse_status_fan_speed_mapping(
    pr_cyc: int, expected_fan_speed: str
) -> None:
    """``prCyc`` decodes to the fan-speed translation key. Unknown values
    fall back to ``floor_only`` (the safe default).
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _full_vr_payload(prCyc=pr_cyc)
    get_protocol("vr").parse_status(client, payload, result)
    assert result["fan_speed"] == expected_fan_speed
    assert client._fan_speed == expected_fan_speed


def test_parse_status_canister_scaled_to_percent() -> None:
    """``canister`` is 0-1 from the cloud; surfaced as 0-100."""
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _full_vr_payload(canister=0.75)
    get_protocol("vr").parse_status(client, payload, result)
    assert result["canister"] == 75.0


def test_parse_status_temperature_fallback_to_state() -> None:
    """Some firmware exposes temp via ``sensors.sns_1.state`` instead of
    ``.val``. The parser tries ``.val`` first, then ``.state``, then ``'0'``.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _full_vr_payload(sensors={"sns_1": {"state": "19.8"}})
    get_protocol("vr").parse_status(client, payload, result)
    assert result["temperature"] == "19.8"


def test_parse_status_never_cleaned_robot_emits_none() -> None:
    """``cycleStartTime == 0`` means the robot has never run a cycle.
    Pre-H8-review the parser emitted a 1970-epoch datetime (HA's frontend
    rendered "55 years ago"); post-H8 it emits ``None`` so the frontend
    shows "Unknown". R25-vr preserves that.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    # client._format_time_human is the real bound method; works via __new__.
    result: dict[str, Any] = {}
    payload = _full_vr_payload(cycleStartTime=0)
    get_protocol("vr").parse_status(client, payload, result)
    assert result["cycle_start_time"] is None
    assert result["cycle_end_time"] is None
    assert result["estimated_end_time"] is None
    assert result["cycle_duration"] == 0
    assert result["time_remaining"] == 0


def test_parse_status_missing_stepper_uses_defaults() -> None:
    """No stepper in payload → stepper=0, stepper_adj_time=15. Matches the
    pre-R25 except-handler defaults.
    """
    from custom_components.iaqualink_robots.protocols import get_protocol

    client = _make_client()
    result: dict[str, Any] = {}
    payload = _full_vr_payload()
    del payload["payload"]["robot"]["state"]["reported"]["equipment"]["robot"]["stepper"]
    get_protocol("vr").parse_status(client, payload, result)
    assert result["stepper"] == 0
    assert result["stepper_adj_time"] == 15


# ---------------------------------------------------------------------------
# AC #2 — coordinator routes VR commands through the protocol (not inline).
# AST-based; mirrors the pattern in tests/test_coordinator.py and
# tests/test_stepper.py.
# ---------------------------------------------------------------------------


def _coordinator_method_calls(method_name: str) -> str:
    """Return the source text of an ``AqualinkClient`` async method."""
    import ast
    from pathlib import Path

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
        ("start_cleaning", "get_protocol('vr').start_payload"),
        ("stop_cleaning", "get_protocol('vr').stop_payload"),
        ("pause_cleaning", "get_protocol('vr').pause_payload"),
        ("return_to_base", "get_protocol('vr').return_to_base_payload"),
        ("clear_desired_state", "get_protocol('vr').clear_desired_payload"),
        ("_set_other_fan_speed", "get_protocol('vr').set_fan_speed_payload"),
    ],
)
def test_coordinator_vr_branch_routes_through_protocol(
    method_name: str, protocol_call: str
) -> None:
    """Each command-method's VR branch must dispatch through the protocol,
    not the inline ``_build_state_request(...)`` form. AST-level so the
    check is whitespace-insensitive but shape-strict — if a future PR
    bypasses the protocol by re-inlining the VR envelope, this test fails.
    """
    source = _coordinator_method_calls(method_name)
    # ``ast.unparse`` normalises quote style — both ``"vr"`` and ``'vr'``
    # render as ``'vr'`` in the unparsed source.
    assert protocol_call in source, (
        f"AqualinkClient.{method_name}'s VR branch must call {protocol_call}"
        f"(self). Got:\n{source}"
    )


def test_update_vr_robot_data_delegates_to_protocol_parse_status() -> None:
    """The legacy ``_update_vr_robot_data`` method on ``AqualinkClient``
    is kept as a stable seam (called by both the polled path and the
    push path) but its body must delegate to VRProtocol.parse_status —
    the duplicated ~120-line parser body is gone.
    """
    import ast
    from pathlib import Path

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
                and method.name == "_update_vr_robot_data"
            ):
                method_body = method
                break
    assert method_body is not None, "AqualinkClient._update_vr_robot_data missing"
    # Body should be just a docstring + one delegating call expression.
    # The check is structural: <=3 top-level statements (docstring + delegate
    # + optional trailing blank). Pre-R25 had ~30 top-level statements.
    assert len(method_body.body) <= 3, (
        f"_update_vr_robot_data has {len(method_body.body)} top-level "
        f"statements post-R25; expected <=3 (docstring + delegate). Did a "
        f"regression re-inline the parser body?"
    )
    source = ast.unparse(method_body)
    assert "get_protocol('vr').parse_status" in source, (
        "_update_vr_robot_data must delegate to get_protocol('vr').parse_status"
    )
