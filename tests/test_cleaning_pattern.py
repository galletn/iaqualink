"""Cleaning-pattern (`route`) support — parse, envelope, and select entity.

Verified end-to-end against a live cyclobat (firmware V22E3.25.3):

* the four cycle durations in the payload match the iAquaLink app exactly
  (SMART 124 min = 02:04, Floor 90 = 01:30, Floor/Wall 150 = 02:30,
  Waterline 45 = 00:45), which is what identified `route` as the pattern;
* `floorTim.route` was 1 with S-Pattern shown as selected in the app;
* writing `setCleaningMode` with `cycles.floorTim.route` flipped the value
  1 -> 0, the cloud echoed it back over StateStreamer, and writing the
  original value restored it.

`route` is PER CYCLE, and only cycles carrying the key support it — on the
captured device floorTim/floorWallsTim/smartTim did, waterlineTim/wallsTim
did not.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.iaqualink_robots.client import AqualinkClient
from custom_components.iaqualink_robots.const import (
    PATTERN_BY_ROUTE,
    PATTERN_CYCLE_NAMES,
    PATTERN_FEATURE,
    PLATFORMS,
    ROUTE_BY_PATTERN,
)
from custom_components.iaqualink_robots.protocols import get_protocol
from custom_components.iaqualink_robots.select import AqualinkPatternSelect

# Verbatim `cycles` block from the live capture.
REAL_CYCLES = {
    "floorTim": {"duration": 90, "type": 0, "route": 1},
    "floorWallsTim": {"duration": 150, "type": 0, "route": 0},
    "smartTim": {"duration": 124, "type": 0, "route": 0},
    "waterlineTim": {"duration": 45, "type": 0},
    "firstSmartDone": 1,
    "liftPatternTim": 10,
    "wallsTim": {"duration": 90, "type": 0},
}


def _client() -> Any:
    """Minimal cyclobat client.

    Cyclobat's parser calls ``_stabilize_status``, which reaches into five
    cached attributes the production ``__init__`` sets; the same set is
    stamped by ``tests/protocols/test_cyclobat.py::_make_client``, whose
    docstring documents why. Kept in step with that fixture.
    """
    c = AqualinkClient.__new__(AqualinkClient)
    c._id = "uid"
    c._auth_token = "tok"
    c._app_client_id = "app"
    c._serial = "SER"
    c._device_type = "cyclobat"
    c._activity = None
    c._fan_speed = None
    c._include_seconds_remaining = False
    c._features = [PATTERN_FEATURE]
    c._status_history = []
    c._status_stabilization_window = 30
    c._minimum_status_duration = 15
    c._last_status_change_time = 0
    c._stable_status = None
    return c


def _payload(cycles: dict[str, Any]) -> dict[str, Any]:
    return {"payload": {"robot": {"state": {"reported": {
        "aws": {"status": "connected"},
        "equipment": {"robot": {
            "vr": "V22E3.25.3",
            "main": {"state": 0, "ctrl": 0, "mode": 0, "error": 0,
                     "cycleStartTime": 1785876705},
            "cycles": cycles,
            "battery": {"userChargePerc": 100, "state": 2,
                        "userChargeState": 4, "cycles": 57,
                        "warning": {"code": 0}},
            "lastCycle": {"cycleNb": 98, "duration": 90, "mode": 0,
                          "endCycleType": 0, "errorCode": 0},
            "stats": {"totRunTime": 9119, "diagnostic": 0, "tmp": 5,
                      "lastError": {"code": 0, "cycleNb": 0}},
        }},
    }}}}}


def _parse(cycles: dict[str, Any] = REAL_CYCLES) -> dict[str, Any]:
    result: dict[str, Any] = {}
    get_protocol("cyclobat").parse_status(_client(), _payload(cycles), result)
    return result


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_patterns_parsed_only_for_cycles_that_have_route() -> None:
    """waterlineTim and wallsTim carry no `route`, so they must be absent —
    that is what stops a select being created for them.
    """
    patterns = _parse()["cleaning_patterns"]
    assert patterns == {"floorTim": 1, "floorWallsTim": 0, "smartTim": 0}
    assert "waterlineTim" not in patterns
    assert "wallsTim" not in patterns


def test_pattern_is_per_cycle_not_global() -> None:
    """The captured device had Floor on S-Pattern and the others on Standard
    simultaneously, so a single global value would be wrong.
    """
    patterns = _parse()["cleaning_patterns"]
    assert len(set(patterns.values())) > 1


def test_no_patterns_key_when_device_has_none() -> None:
    """A robot without pattern support must not get an empty dict, which
    would still be truthy enough to mislead the platform's guard.
    """
    cycles = {k: ({i: j for i, j in v.items() if i != "route"}
                  if isinstance(v, dict) else v)
              for k, v in REAL_CYCLES.items()}
    assert "cleaning_patterns" not in _parse(cycles)


def test_bool_route_rejected() -> None:
    """`bool` subclasses `int`; True must not read as S-Pattern."""
    cycles = dict(REAL_CYCLES, floorTim={"duration": 90, "route": True})
    assert "floorTim" not in _parse(cycles).get("cleaning_patterns", {})


def test_walls_duration_surfaced() -> None:
    """`wallsTim` was observed on this firmware with no counterpart in the
    app's mode list — surfaced rather than silently dropped.
    """
    assert _parse()["walls_duration"] == "90"


def test_durations_match_the_app() -> None:
    """Cross-check against the app screenshot that identified `route`."""
    result = _parse()
    assert result["smart_duration"] == "124"      # 02:04
    assert result["floor_duration"] == "90"       # 01:30
    assert result["floor_walls_duration"] == "150"  # 02:30
    assert result["waterline_duration"] == "45"   # 00:45


# ---------------------------------------------------------------------------
# Write envelope — the shape verified against live hardware
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", [0, 1])
def test_set_pattern_envelope(route: int) -> None:
    """Locks the exact frame that was confirmed to work.

    Note the nesting is under `cycles.<cycleKey>`, unlike every other
    cyclobat command which nests under `main`.
    """
    envelope = get_protocol("cyclobat").set_pattern_payload(
        _client(), "floorTim", route
    )
    assert envelope == {
        "action": "setCleaningMode",
        "version": 1,
        "namespace": "cyclobat",
        "payload": {
            "state": {"desired": {"equipment": {"robot": {
                "cycles": {"floorTim": {"route": route}},
            }}}},
            "clientToken": "uid|tok|app",
        },
        "service": "StateController",
        "target": "SER",
    }


def test_set_pattern_targets_one_cycle_only() -> None:
    """Writing one cycle's pattern must not carry the others, or it would
    overwrite them.
    """
    envelope = get_protocol("cyclobat").set_pattern_payload(
        _client(), "smartTim", 1
    )
    cycles = (envelope["payload"]["state"]["desired"]["equipment"]["robot"]
              ["cycles"])
    assert list(cycles) == ["smartTim"]


def test_other_families_have_no_pattern_builder() -> None:
    """No vr/cyclonext payload captured carries a `route` key, so they must
    not silently accept a pattern write (the issue #76 failure mode).
    """
    for family in ("vr", "vortrax", "cyclonext", "i2d_robot"):
        protocol = get_protocol(family)
        assert getattr(protocol, "set_pattern_payload", None) is None, (
            f"{family} gained a pattern builder without a verified envelope"
        )


# ---------------------------------------------------------------------------
# Mappings + wiring
# ---------------------------------------------------------------------------


def test_route_mapping_round_trips() -> None:
    assert PATTERN_BY_ROUTE == {0: "standard", 1: "s_pattern"}
    for route, name in PATTERN_BY_ROUTE.items():
        assert ROUTE_BY_PATTERN[name] == route


def test_select_platform_registered() -> None:
    from homeassistant.const import Platform

    assert Platform.SELECT in PLATFORMS


def test_every_named_cycle_has_a_translation() -> None:
    """A select whose translation key is missing renders as raw text."""
    import json
    from pathlib import Path

    strings = json.loads(
        (Path(__file__).parent.parent / "custom_components"
         / "iaqualink_robots" / "strings.json").read_text(encoding="utf-8")
    )
    select = strings["entity"]["select"]
    for sub_name in PATTERN_CYCLE_NAMES.values():
        key = f"pattern_{sub_name}"
        assert key in select, f"{key} missing from strings.json"
        assert select[key].get("name")
        assert set(select[key]["state"]) == set(ROUTE_BY_PATTERN)


# ---------------------------------------------------------------------------
# Select entity behaviour
# ---------------------------------------------------------------------------


def _select(cycle_key: str, data: dict[str, Any]) -> AqualinkPatternSelect:
    from unittest.mock import MagicMock

    coordinator = MagicMock()
    coordinator.data = data
    sel = AqualinkPatternSelect.__new__(AqualinkPatternSelect)
    sel.coordinator = coordinator
    sel.client = _client()
    sel._cycle_key = cycle_key
    sel._attr_options = list(ROUTE_BY_PATTERN)
    return sel


def test_current_option_reflects_route() -> None:
    data = {"cleaning_patterns": {"floorTim": 1, "smartTim": 0}}
    assert _select("floorTim", data).current_option == "s_pattern"
    assert _select("smartTim", data).current_option == "standard"


def test_current_option_none_for_unknown_route() -> None:
    """An unmapped value reports None ("Unknown") rather than defaulting to
    Standard, which would misreport the robot's actual state.
    """
    sel = _select("floorTim", {"cleaning_patterns": {"floorTim": 7}})
    assert sel.current_option is None


def test_current_option_none_when_missing() -> None:
    assert _select("floorTim", {}).current_option is None
