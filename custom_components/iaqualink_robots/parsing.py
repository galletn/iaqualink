"""Pure date/time/duration helpers used by the client and protocol parsers (R26).

These were previously instance methods on ``AqualinkClient`` but read no
client state beyond two flags (``_include_seconds_remaining`` / ``_debug_mode``)
that are passed through verbatim. R26 splits them into this module so the
protocol parsers in ``protocols/`` can import them as free functions rather
than reach across the ``client.parse_status(...)`` boundary on every call
site (``client._calculate_times`` / ``client._format_time_human`` etc.).

Module is import-time pure — no I/O, no Home Assistant imports beyond the
``dt_util`` aware-UTC clock used by ``calculate_times`` for "time remaining"
arithmetic.
"""
from __future__ import annotations

import datetime
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from .client import AqualinkClient

_LOGGER = logging.getLogger(__name__)


def format_time_human(hours: int, minutes: int, seconds: int) -> str:
    """Format an ``HH/MM/SS`` triple as a human-readable string.

    Story C3 stripped the dead ``hass.localize`` fallback that pre-dated
    the discovery that ``localize`` is a frontend-JS API absent from
    Python ``HomeAssistant`` objects. The localised display-language
    fix belongs in ``translations/*.json`` + entity ``translation_key``,
    not the coordinator data layer (cross-refs: M11, P7).
    """
    return f"{hours} Hour(s) {minutes} Minute(s) {seconds} Second(s)"


def resolve_cycle_duration(
    durations: Mapping[str, int] | Sequence[int],
    cycle: int,
    key_map: Mapping[int, str] | None = None,
) -> int:
    """Look up the cycle duration (in minutes) for cycle code ``cycle``.

    Pre-C5, the three callers each did
    ``list(durations.values())[result["cycle"]]`` — a positional lookup
    that silently raised ``IndexError`` (masked by the surrounding
    broad-except) when ``cycle`` was unexpected. C5 extracted the lookup
    so out-of-range / non-int cycle codes fall back to 0 with an
    explicit DEBUG breadcrumb instead of an exception nobody sees.

    **Verified payload shape.** C5 deferred "switch to named keys" until
    a real API sample existed. One was captured from a live VR
    (RA 6570 iQ) via ``tools/capture_payload.py``::

        durations = {"waterTim": 45, "quickTim": 75, "smartTim": 0,
                     "deepTim": 165, "customTim": 150,
                     "firstSmartTim": 150}

    Three things that sample settled:

    * It is a JSON **object**, not an array, and it has **six** keys —
      not the four the earlier docstring here assumed.
    * Positional indexing was *accidentally* correct: ``prCyc`` 0/1/2/3
      lines up with ``waterTim``/``quickTim``/``smartTim``/``deepTim``
      purely because of the cloud's key insertion order, which is not an
      API contract. Pass ``key_map`` to resolve by name instead.
    * The positional form also accepted codes 4 and 5, silently
      resolving them to ``customTim`` / ``firstSmartTim``. The payload
      carries ``customCyc`` and ``customIntensity`` fields, so a custom
      cycle really exists and those codes are not hypothetical.

    ``smartTim`` reads 0 while the robot is idle and is populated by the
    cloud once a smart cycle actually starts, so a 0 here is not
    treated as an error.

    ``bool`` is filtered out via the ``not isinstance(cycle, bool)``
    clause: in Python, ``bool`` subclasses ``int``, so ``True`` would
    otherwise pass the ``isinstance(cycle, int)`` guard and resolve as
    cycle 1.

    ``str``/``bytes`` are rejected explicitly: both are ``Sequence``s, so
    a cloud that ever sent ``durations`` as a string would otherwise
    index out a single character and return it as a "duration". Lists are
    tolerated defensively — no live payload has been seen using one.
    """
    if isinstance(durations, (str, bytes)) or not isinstance(
        durations, (Mapping, Sequence)
    ):
        _LOGGER.debug(
            "Unusable durations container %r (type %s); falling back to 0",
            durations, type(durations).__name__,
        )
        return 0

    if not isinstance(cycle, int) or isinstance(cycle, bool):
        _LOGGER.debug(
            "Unexpected cycle code %r for durations=%r; falling back to 0",
            cycle, durations,
        )
        return 0

    # Preferred path: resolve by the cloud's own key name.
    if key_map is not None and isinstance(durations, Mapping):
        name = key_map.get(cycle)
        if name is not None:
            value = durations.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
            _LOGGER.debug(
                "durations is missing mapped key %r for cycle %r "
                "(keys=%s); falling back to positional lookup",
                name, cycle, list(durations),
            )
        else:
            _LOGGER.debug(
                "No durations key mapped for cycle %r (map=%s); "
                "falling back to positional lookup",
                cycle, dict(key_map),
            )

    # Legacy positional fallback, for families whose real payload keys
    # haven't been captured yet.
    if not (0 <= cycle < len(durations)):
        _LOGGER.debug(
            "Unexpected cycle code %r for durations=%r; falling back to 0",
            cycle, durations,
        )
        return 0
    ordered = list(durations.values()) if isinstance(durations, Mapping) else durations
    value = ordered[cycle]
    return int(value) if isinstance(value, (int, float)) else 0


def add_minutes_to_datetime(
    dt: datetime.datetime, minutes: int
) -> datetime.datetime:
    """Add minutes to an aware datetime object.

    H8: rejects naive datetimes at the boundary so a future regression
    anywhere upstream surfaces here rather than silently producing
    wrong-timezone arithmetic far away from the bug site.

    H8 review follow-up: raise ``TypeError`` rather than ``assert`` —
    ``assert`` is stripped under ``python -O`` / ``PYTHONOPTIMIZE=1``,
    which defeats the fail-fast goal in optimized builds.
    """
    if dt.tzinfo is None:
        raise TypeError(
            "add_minutes_to_datetime requires a tz-aware datetime (H8)"
        )
    return dt + datetime.timedelta(minutes=minutes)


def calculate_times(
    client: "AqualinkClient",
    start_time: datetime.datetime,
    duration_minutes: int,
    result: dict[str, Any],
    robot_data: dict[str, Any] | None = None,
) -> None:
    """Calculate end time and remaining time using stepper-based adjustments.

    Mutates ``result`` in place — sets ``cycle_end_time``,
    ``estimated_end_time``, ``time_remaining``, ``time_remaining_human``,
    ``base_cycle_duration``, ``adjusted_cycle_duration``,
    ``stepper_value``, and ``stepper_adjustment_minutes``.

    Reads ``client._include_seconds_remaining`` (gates seconds in the
    human-readable string) and ``client._debug_mode`` (verbose error
    logging) — these are the only client deps so the function stays
    decoupled from the rest of the client surface.
    """
    try:
        stepper_value = 0
        stepper_adj_time = 15  # Default 15 minutes per step

        if robot_data:
            stepper_value = robot_data.get('stepper', 0)
            stepper_adj_time = robot_data.get('stepperAdjTime', 15)

            _LOGGER.debug(
                f"Stepper calculation: base_duration={duration_minutes}, "
                f"stepper={stepper_value}, stepper_adj_time={stepper_adj_time}")

        adjusted_duration = duration_minutes + stepper_value

        result["base_cycle_duration"] = duration_minutes
        result["stepper_value"] = stepper_value
        result["stepper_adjustment_minutes"] = stepper_value
        result["adjusted_cycle_duration"] = adjusted_duration

        # Calculate cycle end time using adjusted duration. H8: aware-UTC
        # datetime objects emitted directly (no .isoformat()).
        cycle_end_time = add_minutes_to_datetime(start_time, adjusted_duration)
        result["cycle_end_time"] = cycle_end_time
        result["estimated_end_time"] = cycle_end_time

        # If the device is idle (not cleaning/returning), always report 0
        # time remaining regardless of webservice cycle start time.
        current_activity = result.get("activity", "idle")
        if current_activity not in ["cleaning", "returning"]:
            result["time_remaining"] = 0
            result["time_remaining_human"] = format_time_human(0, 0, 0)
            return

        # H8: aware-UTC on both sides of the comparison/subtraction.
        now = dt_util.utcnow()

        if start_time and cycle_end_time:
            if now < cycle_end_time:
                time_diff = cycle_end_time - now
                total_seconds = time_diff.total_seconds()

                total_minutes = max(0, int(total_seconds / 60))
                result["time_remaining"] = total_minutes

                # Seconds gated by include_seconds_remaining option — when
                # disabled, the sensor changes only when the minute ticks
                # over, eliminating ~20x of activity-log entries per cycle.
                hours = int(total_seconds // 3600)
                minutes = int((total_seconds % 3600) // 60)
                seconds = (
                    int(total_seconds % 60)
                    if client._include_seconds_remaining
                    else 0
                )
                result["time_remaining_human"] = format_time_human(hours, minutes, seconds)

                _LOGGER.debug("Time remaining (minutes): %d, human readable: %s",
                              total_minutes,
                              result["time_remaining_human"])
            else:
                result["time_remaining"] = 0
                result["time_remaining_human"] = format_time_human(0, 0, 0)
        else:
            result["time_remaining"] = 0
            result["time_remaining_human"] = format_time_human(0, 0, 0)

    except Exception as e:
        if client._debug_mode:
            _LOGGER.warning(f"Error calculating times: {e}")
        result["cycle_end_time"] = None
        result["estimated_end_time"] = None
        result["time_remaining"] = 0
        result["time_remaining_human"] = format_time_human(0, 0, 0)
