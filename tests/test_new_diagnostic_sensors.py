"""The four diagnostic sensors added from the live-capture audit.

Each reads a field the cloud already sends on every poll that no sensor
exposed. Values below are verbatim from four live captures (2x vr,
2x cyclonext).

`last_cloud_contact` is populated by the client (it lives in the shadow
envelope's `aws` block, not the per-family robot block), so it is tested
against the client helper rather than a protocol parser.
"""

from __future__ import annotations

import datetime
from typing import Any

import pytest

from custom_components.iaqualink_robots.client import AqualinkClient
from custom_components.iaqualink_robots.protocols import get_protocol
from custom_components.iaqualink_robots.sensor import (
    _DIAGNOSTIC_SENSOR_KEYS,
    _TIMESTAMP_SENSOR_KEYS,
    ALL_SENSOR_TYPES,
)


def _client(device_type: str) -> Any:
    c = AqualinkClient.__new__(AqualinkClient)
    c._id = "u"
    c._auth_token = "t"
    c._app_client_id = "a"
    c._serial = "S"
    c._device_type = device_type
    c._activity = None
    c._fan_speed = None
    c._include_seconds_remaining = False
    c._ebox_data = {}
    return c


# Verbatim from the live RA 6570 iQ capture.
VR_ROBOT = {
    "vr": "V32E56", "state": 1, "prCyc": 1, "cycleStartTime": 1786199969,
    "canister": 0, "errorState": 0, "errorCode": 0, "diagnostic": 0,
    "stepper": 0, "stepperAdjTime": 15, "totalHours": 833,
    "durations": {"waterTim": 45, "quickTim": 75, "smartTim": 0,
                  "deepTim": 165, "customTim": 150, "firstSmartTim": 150},
    "sensors": {"sns_1": {"type": "temperature", "state": 0, "val": 27}},
}

# Verbatim from the live CNX 50 iQ capture.
CYCLONEXT_ROBOT = {
    "vr": "V21E27", "mode": 1, "cycle": 1, "cycleStartTime": 1786214968,
    "canister": 0, "stepper": 0, "stepperAdjTime": 15, "diagnostic": 0,
    "durations": {"waterTim": 45, "quickTim": 90, "smartTim": 0,
                  "deepTim": 150, "customTim": 75, "firstSmartTim": 150,
                  "scanTim": 30},
    "errors": {"timestamp": 0, "code": 0},
}


def _envelope(robot: dict[str, Any], key: str) -> dict[str, Any]:
    return {"payload": {"robot": {"state": {"reported": {
        "equipment": {key: robot}}}}}}


def _parse(device_type: str, robot: dict[str, Any], key: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    get_protocol(device_type).parse_status(
        _client(device_type), _envelope(robot, key), result
    )
    return result


# ---------------------------------------------------------------------------
# Firmware version — present on every family
# ---------------------------------------------------------------------------


def test_vr_reports_firmware_version() -> None:
    """``robot.vr`` is the firmware revision, not the device type.

    Unfortunate collision: the field is named ``vr`` on every family,
    including the family also called ``vr``.
    """
    assert _parse("vr", VR_ROBOT, "robot")["version"] == "V32E56"


def test_cyclonext_reports_firmware_version() -> None:
    assert _parse("cyclonext", CYCLONEXT_ROBOT, "robot.1")["version"] == "V21E27"


def test_missing_firmware_is_omitted_not_defaulted() -> None:
    """An absent field must be left out so the sensor reads Unknown, rather
    than defaulted to something that looks like real data.
    """
    robot = {k: v for k, v in VR_ROBOT.items() if k != "vr"}
    assert "version" not in _parse("vr", robot, "robot")


# ---------------------------------------------------------------------------
# Error / diagnostic codes
# ---------------------------------------------------------------------------


def test_vr_reports_error_and_diagnostic_codes() -> None:
    result = _parse("vr", VR_ROBOT, "robot")
    assert result["error_code"] == 0
    assert result["diagnostic_code"] == 0


def test_cyclonext_has_no_separate_error_code() -> None:
    """Cyclonext reports ``errors.code`` (already surfaced as ``error_state``)
    and no ``errorCode``. Confirmed absent on both captured units, which is
    why ``error_code`` is excluded for this family in sensor.py.
    """
    result = _parse("cyclonext", CYCLONEXT_ROBOT, "robot.1")
    assert "error_code" not in result
    assert result["diagnostic_code"] == 0


def test_dict_valued_field_is_not_emitted_as_a_code() -> None:
    """Guard the shape difference between families: cyclonext nests
    ``errors`` as a dict. A dict must never land in a numeric code sensor.
    """
    robot = dict(VR_ROBOT, errorCode={"nested": 1}, diagnostic=[1, 2])
    result = _parse("vr", robot, "robot")
    assert "error_code" not in result
    assert "diagnostic_code" not in result


# ---------------------------------------------------------------------------
# Last cloud contact — set by the client, not a protocol parser
# ---------------------------------------------------------------------------


def test_last_cloud_contact_parses_millisecond_epoch() -> None:
    """``aws.timestamp`` is epoch MILLISECONDS (13 digits), unlike
    ``cycleStartTime`` which is seconds. Getting this wrong would render a
    date in the year 58,000.
    """
    client = _client("vr")
    result: dict[str, Any] = {}
    data = {"payload": {"robot": {"state": {"reported": {
        "aws": {"status": "connected", "timestamp": 1786201126595},
    }}}}}
    client._apply_last_cloud_contact(data, result)

    stamp = result["last_cloud_contact"]
    assert isinstance(stamp, datetime.datetime)
    assert stamp.tzinfo is not None, "must be aware-UTC (H8 convention)"
    assert stamp.year == 2026, f"got {stamp!r} — seconds/ms confusion?"


@pytest.mark.parametrize(
    "timestamp", [None, 0, -1, "not-a-number", True, {}, []],
)
def test_last_cloud_contact_ignores_unusable_timestamps(timestamp: Any) -> None:
    """Bad values leave the key absent rather than emitting an epoch-1970
    date. ``True`` is included because bool subclasses int.
    """
    client = _client("vr")
    result: dict[str, Any] = {}
    data = {"payload": {"robot": {"state": {"reported": {
        "aws": {"timestamp": timestamp},
    }}}}}
    client._apply_last_cloud_contact(data, result)
    assert "last_cloud_contact" not in result


def test_last_cloud_contact_tolerates_missing_aws_block() -> None:
    client = _client("vr")
    result: dict[str, Any] = {}
    client._apply_last_cloud_contact({"payload": {}}, result)
    assert "last_cloud_contact" not in result


def _client_with_block() -> Any:
    c = _client("vr")
    c._last_robot_block = {}
    return c


def test_raw_robot_block_cached_from_ws_shape() -> None:
    """Diagnostics needs the RAW cloud values, not just the parsed result.

    Issue #100 stalled because a report could show `activity: idle` without
    revealing the cloud value the parser derived it from. Caching the block
    lets a reporter attach exactly what the cloud sent, with no script and
    no credential sharing.
    """
    client = _client_with_block()
    data = {"payload": {"robot": {"state": {"reported": {
        "aws": {"timestamp": 1786201126595},
        "equipment": {"robot": {"mode": 1, "cycle": 3}},
    }}}}}
    client._apply_last_cloud_contact(data, {})
    assert client._last_robot_block == {"mode": 1, "cycle": 3}


def test_raw_robot_block_cached_from_dotted_key() -> None:
    """Cyclonext over WebSocket nests under ``robot.1``, not ``robot``."""
    client = _client_with_block()
    data = {"payload": {"robot": {"state": {"reported": {
        "equipment": {"robot.1": {"mode": 0, "cycle": 1}},
    }}}}}
    client._apply_last_cloud_contact(data, {})
    assert client._last_robot_block == {"mode": 0, "cycle": 1}


def test_raw_robot_block_cached_from_rest_list_shape() -> None:
    """Over the REST shadow, cyclonext sends ``robot`` as ``[null, {...}]``.

    That list-with-a-leading-null is where the ``robot.1`` key name comes
    from. A naive ``isinstance(block, dict)`` check would silently skip it.
    """
    client = _client_with_block()
    data = {"payload": {"robot": {"state": {"reported": {
        "equipment": {"robot": [None, {"mode": 1, "cycle": 3}]},
    }}}}}
    client._apply_last_cloud_contact(data, {})
    assert client._last_robot_block == {"mode": 1, "cycle": 3}


def test_raw_robot_block_never_captures_the_envelope() -> None:
    """Only the robot block is kept — the envelope carries ``clientToken``,
    which embeds the auth token, and ``async_redact_data`` matches dict keys
    so it would not scrub a credential nested inside a stringified blob.
    """
    client = _client_with_block()
    data = {"payload": {
        "clientToken": "user|SECRET_AUTH_TOKEN|app",
        "robot": {"state": {"reported": {
            "equipment": {"robot": {"mode": 1}},
        }}},
    }}
    client._apply_last_cloud_contact(data, {})
    assert "SECRET_AUTH_TOKEN" not in repr(client._last_robot_block)
    assert "clientToken" not in client._last_robot_block


def test_raw_robot_block_left_alone_when_absent() -> None:
    """A payload with no equipment must not clear a previously good block."""
    client = _client_with_block()
    client._last_robot_block = {"mode": 1}
    client._apply_last_cloud_contact(
        {"payload": {"robot": {"state": {"reported": {}}}}}, {}
    )
    assert client._last_robot_block == {"mode": 1}


def test_ebox_data_is_cached_for_diagnostics() -> None:
    """The same shadow read caches equipment identity so diagnostics.py can
    include part numbers without issuing its own request (issue #88).
    """
    client = _client("vr")
    result: dict[str, Any] = {}
    data = {"payload": {"robot": {"state": {"reported": {
        "aws": {"timestamp": 1786201126595},
        "eboxData": {"completeCleanerPn": "WR000219",
                     "controlBoxPn": "30098900"},
    }}}}}
    client._apply_last_cloud_contact(data, result)
    assert client._ebox_data["completeCleanerPn"] == "WR000219"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_new_sensors_are_declared_and_categorised() -> None:
    """All four must be declared, and all four are Diagnostic-category so
    they don't crowd the primary controls on the device page.
    """
    declared = {k for k, _ in ALL_SENSOR_TYPES}
    for key in ("version", "error_code", "diagnostic_code",
                "last_cloud_contact"):
        assert key in declared, f"{key} missing from ALL_SENSOR_TYPES"
        assert key in _DIAGNOSTIC_SENSOR_KEYS, f"{key} not diagnostic-category"

    # The timestamp one needs device_class=TIMESTAMP for relative rendering.
    assert "last_cloud_contact" in _TIMESTAMP_SENSOR_KEYS


def test_every_sensor_key_has_a_translation() -> None:
    """A key with no strings.json entry renders as raw text in the UI."""
    import json
    from pathlib import Path

    strings = json.loads(
        (Path(__file__).parent.parent / "custom_components"
         / "iaqualink_robots" / "strings.json").read_text(encoding="utf-8")
    )
    translated = strings["entity"]["sensor"]
    for key in ("version", "error_code", "diagnostic_code",
                "last_cloud_contact"):
        assert key in translated, f"{key} has no strings.json entry"
        assert translated[key].get("name")
