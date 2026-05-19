"""Sensor regression tests — story M11 (raw-key native_value).

Pre-M11 ``AqualinkSensor.native_value`` translated the three enum-valued
sensor keys (``activity``, ``fan_speed``, ``status``) from their raw
cloud values into title-cased English strings before returning. That:

* broke ``{{ is_state('sensor.x_activity', 'cleaning') }}`` (user had
  to write ``'Cleaning'`` instead — non-obvious, locale-fragile);
* caused phantom history-graph transitions when the integration was
  reloaded (raw cached value → titled new value showed up as a state
  change in long-term statistics);
* bypassed the ``translation_key`` already declared on the sensor —
  HA's frontend never saw the raw key, so the locale-aware
  ``entity.sensor.<key>.state.<value>`` translations were dead text.

M11 strips the three display-map blocks in
``sensor.py::native_value``. ``native_value`` now returns the raw
key directly; HA's frontend looks up ``entity.sensor.<key>.state.
<value>`` from the locale file and renders the localized text.

P7 was the prerequisite — it landed the
``entity.sensor.{activity,fan_speed,status}.state.*`` blocks in
``strings.json`` and all 9 locale files. Pre-P7 these tests would have
been a UI regression (raw keys leaking through the frontend).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SENSOR_PY = (
    Path(__file__).parent.parent
    / "custom_components"
    / "iaqualink_robots"
    / "sensor.py"
)


def _make_sensor(key: str, data: dict | None) -> object:
    """Build an ``AqualinkSensor`` shell without running ``__init__``.

    ``__init__`` reaches into HA's ``CoordinatorEntity`` base which
    wants a real coordinator with ``async_add_listener`` etc. — too
    much surface for what we need to test. ``native_value`` only
    reads ``self.coordinator.data`` and ``self._key`` (plus the
    cache attribute ``self._last_value``), so a hand-stitched
    instance is sufficient and faster.
    """
    from custom_components.iaqualink_robots.sensor import AqualinkSensor

    sensor = AqualinkSensor.__new__(AqualinkSensor)
    sensor.coordinator = MagicMock()
    sensor.coordinator.data = data
    sensor._key = key
    sensor._last_value = None
    return sensor


# ---------------------------------------------------------------------------
# AC #1 — native_value returns raw keys for the three enum-valued sensors.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_value",
    ["cleaning", "idle", "error", "returning", "docking", "paused"],
)
def test_activity_sensor_returns_raw_key(raw_value: str) -> None:
    """``sensor.x_activity`` raw state must equal the cloud-side key,
    not a title-cased English string. ``is_state(..., 'cleaning')``
    must match when the robot is cleaning.
    """
    sensor = _make_sensor("activity", {"activity": raw_value})
    assert sensor.native_value == raw_value, (
        f"activity sensor returned {sensor.native_value!r} (titled?) "
        f"instead of the raw key {raw_value!r}. M11 stripped the "
        f"display-map; if this fails the map has been re-inlined."
    )


@pytest.mark.parametrize(
    "raw_value",
    [
        "floor_only",
        "wall_only",
        "walls_only",
        "floor_and_walls",
        "smart_floor_and_walls",
    ],
)
def test_fan_speed_sensor_returns_raw_key(raw_value: str) -> None:
    """``sensor.x_fan_speed`` raw state must equal the cloud-side key.
    The ``wall_only`` (vr/cyclobat dedicated wall-scrub) vs
    ``walls_only`` (i2d waterline-only) distinction matters per
    CLAUDE.md — both must return their respective raw key.
    """
    sensor = _make_sensor("fan_speed", {"fan_speed": raw_value})
    assert sensor.native_value == raw_value


@pytest.mark.parametrize(
    "raw_value", ["connected", "disconnected", "offline", "online"]
)
def test_status_sensor_returns_raw_key(raw_value: str) -> None:
    """``sensor.x_status`` raw state must equal the cloud-side key."""
    sensor = _make_sensor("status", {"status": raw_value})
    assert sensor.native_value == raw_value


# ---------------------------------------------------------------------------
# Non-enum sensors — values pass through unchanged. M11 doesn't touch these.
# ---------------------------------------------------------------------------


def test_battery_level_passes_through_numeric() -> None:
    """Non-enum sensors (numerics, strings, datetimes) pass through
    unchanged. M11 only stripped the three enum display maps; the
    rest of ``native_value`` is unaffected.
    """
    sensor = _make_sensor("battery_level", {"battery_level": 85})
    assert sensor.native_value == 85


def test_serial_number_passes_through_string() -> None:
    sensor = _make_sensor("serial_number", {"serial_number": "ABC123"})
    assert sensor.native_value == "ABC123"


def test_model_passes_through_string() -> None:
    sensor = _make_sensor("model", {"model": "VRX iQ+"})
    assert sensor.native_value == "VRX iQ+"


# ---------------------------------------------------------------------------
# Cache + error-state behaviour preserved. M11 only stripped the display maps;
# the resilient-cache logic for "no_data" / "update_failed" / etc. is intact.
# ---------------------------------------------------------------------------


def test_cached_value_returned_during_connection_failed() -> None:
    """When coordinator data carries an ``error_state`` flag in the
    transient-failure set, native_value returns the last cached value
    instead of the (presumably stale or None) current key value. The
    cache is what keeps sensors stable across short ISP blips.
    """
    sensor = _make_sensor(
        "activity",
        {"activity": "cleaning", "error_state": "connection_failed"},
    )
    sensor._last_value = "idle"
    assert sensor.native_value == "idle"


def test_cached_value_returned_when_current_is_none() -> None:
    """A missing key in ``coordinator.data`` falls back to the
    cached value — protects against transient parse failures where
    one sensor's field is absent for one cycle.
    """
    sensor = _make_sensor("activity", {"other_key": "x"})
    sensor._last_value = "cleaning"
    assert sensor.native_value == "cleaning"


def test_cached_value_returned_when_current_is_unknown() -> None:
    """The literal string ``"unknown"`` from the cloud is treated as
    no-data — cached value wins. Defends against the integration
    surfacing the placeholder text users would otherwise see.
    """
    sensor = _make_sensor("activity", {"activity": "unknown"})
    sensor._last_value = "cleaning"
    assert sensor.native_value == "cleaning"


def test_no_data_returns_cached() -> None:
    """When ``coordinator.data`` itself is None (full outage), the
    sensor surfaces the last cached value rather than going None.
    """
    sensor = _make_sensor("activity", data=None)
    sensor._last_value = "cleaning"
    assert sensor.native_value == "cleaning"


def test_first_poll_no_cache_returns_none() -> None:
    """Cold start: no cache, no data — None is the honest signal."""
    sensor = _make_sensor("activity", data=None)
    assert sensor.native_value is None


def test_last_value_updates_on_successful_read() -> None:
    """Each successful native_value read updates the cache so a
    subsequent transient-failure cycle has something to fall back to.
    """
    sensor = _make_sensor("activity", {"activity": "cleaning"})
    assert sensor.native_value == "cleaning"
    assert sensor._last_value == "cleaning"

    # Switch data; cache should follow.
    sensor.coordinator.data = {"activity": "idle"}
    assert sensor.native_value == "idle"
    assert sensor._last_value == "idle"


# ---------------------------------------------------------------------------
# Regression guard — M11's display maps must NOT come back. The three sites
# in ``sensor.py`` were the only ones doing this title-casing; an AST + grep
# guard catches a future cargo-cult reintroduction.
# ---------------------------------------------------------------------------


def test_no_display_map_for_activity_in_sensor_py() -> None:
    """Source-grep: a literal mapping ``"cleaning": "Cleaning"`` (or
    any other ``"raw": "Titled"`` shape) inside ``native_value`` is
    the exact pattern M11 stripped. A future PR that "improves UX"
    by re-inlining the maps would silently regress
    ``is_state(..., 'cleaning')`` for every user automation.
    """
    source = SENSOR_PY.read_text(encoding="utf-8")
    # Specifically forbid the activity display-map shape pre-M11
    # carried: a dict literal containing both "cleaning": "Cleaning"
    # and "idle": "Idle". We match the substring, not the bytes —
    # whitespace variations would still trip.
    offenders = re.findall(
        r'"cleaning"\s*:\s*"Cleaning"',
        source,
    )
    assert not offenders, (
        "Found `\"cleaning\": \"Cleaning\"` mapping in sensor.py. "
        "M11 removed all three display maps (activity, fan_speed, "
        "status) so `native_value` returns raw keys and HA's "
        "translation_key handles the display. Re-inlining the map "
        "would silently break user automations using "
        "`is_state(..., 'cleaning')`."
    )


def test_no_display_map_for_fan_speed_in_sensor_py() -> None:
    """Same regression guard for ``fan_speed.state``."""
    source = SENSOR_PY.read_text(encoding="utf-8")
    offenders = re.findall(
        r'"floor_only"\s*:\s*"Floor only"',
        source,
    )
    assert not offenders, (
        "Found `\"floor_only\": \"Floor only\"` mapping in sensor.py. "
        "M11 removed this display map."
    )


def test_no_display_map_for_status_in_sensor_py() -> None:
    """Same regression guard for ``status.state``."""
    source = SENSOR_PY.read_text(encoding="utf-8")
    offenders = re.findall(
        r'"connected"\s*:\s*"Connected"',
        source,
    )
    assert not offenders, (
        "Found `\"connected\": \"Connected\"` mapping in sensor.py. "
        "M11 removed this display map."
    )


def test_native_value_does_not_call_any_display_map_helper() -> None:
    """AST guard: ``native_value`` must not invoke any method/function
    whose name suggests title-casing or display-translation. A future
    refactor that extracts the maps into a helper instead of removing
    them would defeat the source-grep guards above — this AST check
    catches the indirection too.
    """
    tree = ast.parse(SENSOR_PY.read_text(encoding="utf-8"))
    suspect_names = {
        "_get_translated_state",
        "_translate_state",
        "_display_state",
        "_titled_state",
    }
    offenders: list[str] = []
    for class_node in ast.walk(tree):
        if (
            not isinstance(class_node, ast.ClassDef)
            or class_node.name != "AqualinkSensor"
        ):
            continue
        for method in class_node.body:
            if not isinstance(method, ast.FunctionDef) or method.name != "native_value":
                continue
            for node in ast.walk(method):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in suspect_names
                ):
                    offenders.append(
                        f"native_value calls self.{node.func.attr}() "
                        f"at line {node.lineno}"
                    )
    assert not offenders, (
        "Found indirection through a translation-helper method in "
        "native_value. M11 removed the display maps; a helper that "
        "re-titles the value would defeat the locale-aware "
        "translation_key path the same way the inline maps did.\n  "
        + "\n  ".join(offenders)
    )
