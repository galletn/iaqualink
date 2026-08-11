"""Cyclonext-family protocol — Zodiac CNX / RE / EvoluX (story R25-cyclonext).

Cyclonext is the most minimal of the WebSocket families: lifecycle
commands use ``setCleanerState`` with a ``mode`` field (1=start,
0=stop, 2=pause — no return-to-base support), the cloud nests its
state under ``equipment.robot.1`` (note the ``.1`` — distinct from
every other family's ``robot`` key), and only two fan-speed modes
are exposed (``floor_only`` / ``floor_and_walls``).

The pre-issue-#76 regression — fan-speed translation keys mismatching
between the dispatch map and `vacuum.py`'s `_fan_speed_list` — is
locked into the protocol's `fan_speed_codes()` and tests so a future
display-name-vs-translation-key drift can't silently regress the
"fan speed no longer propagates from HA to AquaLink" symptom that
issue #76 originally reported on RE 4400 iQ.

Activity mapping is the simplest of the family: ``mode == 1`` →
cleaning, anything else → idle. No ``returning`` state since the
cyclonext family has no return-to-base.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.vacuum import VacuumActivity
from homeassistant.util import dt as dt_util

from ..parsing import calculate_times, format_time_human, resolve_cycle_duration
from .base import RobotProtocol

if TYPE_CHECKING:
    from ..client import AqualinkClient

_LOGGER = logging.getLogger(__name__)


#: Cyclonext ``cycle`` int → integration fan-speed key. Only 2 modes;
#: the others were never accepted by the cloud and exposed as
#: "available" via vacuum.py pre-issue-#76 caused the "Walls only"
#: option to silently no-op.
_CYCLONEXT_CYCLE_TO_FAN_SPEED: dict[int, str] = {
    1: "floor_only",
    3: "floor_and_walls",
}

#: Inverse — fan-speed key → ``cycle`` string value (cyclonext expects
#: strings here, like cyclobat's ``mode`` field).
_CYCLONEXT_FAN_SPEED_TO_CYCLE: dict[str, str] = {
    v: str(k) for k, v in _CYCLONEXT_CYCLE_TO_FAN_SPEED.items()
}

#: ``cycle`` → key in the cloud's ``durations`` object.
#:
#: VERIFIED against a live cyclonext (RE 4600 iQ), whose ``durations`` was::
#:
#:     {"waterTim": 45, "quickTim": 90, "smartTim": 0, "deepTim": 150,
#:      "customTim": 75, "firstSmartTim": 150, "scanTim": 30}
#:
#: Note it carries **seven** keys — one more than the VR family, which has
#: no ``scanTim``. The extra key sits past every mapped cycle code, so it
#: never shifted a positional lookup, but it does mean the two families'
#: ``durations`` objects are not interchangeable.
#:
#: cycle 1 → ``quickTim`` = 90 min and cycle 3 → ``deepTim`` = 150 min also
#: match the Floor / Floor+Wall durations an operator independently
#: reported for an Alpha-series cyclonext, so two sources agree.
#:
#: ``resolve_cycle_duration`` still falls back to the legacy positional
#: lookup when a mapped key is absent, which covers firmware that renames
#: or omits one.
_CYCLONEXT_CYCLE_TO_DURATION_KEY: dict[int, str] = {
    1: "quickTim",   # floor_only
    3: "deepTim",    # floor_and_walls
}

#: ``robot.1.mode`` value that means "cleaning". Every other value is
#: idle (the family has no return-to-base, hence no ``returning``).
_CYCLONEXT_MODE_CLEANING = 1

#: Raw ``mode`` values already warned about, so a robot stuck on an
#: uninterpretable value doesn't emit a WARNING on every poll (the
#: coordinator polls roughly every 10s). Mirrors the rate-limiting in
#: ``ws_envelope.py``; keyed by ``repr`` so unhashable values are safe.
_warned_modes: set[str] = set()


def _warn_once_unknown_mode(raw_mode: Any) -> None:
    """WARN once per distinct raw ``mode`` value per process."""
    key = repr(raw_mode)
    if key in _warned_modes:
        return
    _warned_modes.add(key)
    _LOGGER.warning(
        "Cyclonext reported an uninterpretable mode %r (%s); activity is "
        "unknown. Please report this payload (issue #100).",
        raw_mode, type(raw_mode).__name__,
    )


def _coerce_cloud_int(value: Any) -> int | None:
    """Coerce a cloud-reported numeric field to ``int``, or ``None``.

    Issue #100: a CNX 868 iQ reported ``mode`` in a form that did not
    compare equal to the int ``1``, so the parser's ``== 1`` test fell
    through to the idle branch and the robot showed **Idle** in HA while
    physically cleaning. The reporter's trace showed ``None -> Idle``
    rather than ``unknown``, which means ``mode`` was *present* and
    simply wasn't an int.

    That is the same round-tripping the cyclobat parser already guards
    against: this integration sends ``cycle`` (and cyclobat's ``mode``)
    as *strings*, and firmware revisions differ on whether they echo the
    value back as a string or an int. Cyclonext never got the guard.
    Accepting both shapes here is strictly wider than the old ``== 1``.
    """
    if isinstance(value, bool):
        # bool subclasses int; True would otherwise read as "cleaning".
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


class CycloNextProtocol(RobotProtocol):
    """Cyclonext family — Zodiac CNX 30/40/50/4090 iQ, RE 4400/4600 iQ,
    EvoluX EX5000 iQ and rebrands.
    """

    namespace = "cyclonext"

    # -- Envelope builders --------------------------------------------------

    def start_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request(
            "setCleanerState",
            {"mode": 1},
            namespace="cyclonext",
            robot_key="robot.1",
        )

    def stop_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request(
            "setCleanerState",
            {"mode": 0},
            namespace="cyclonext",
            robot_key="robot.1",
        )

    def pause_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request(
            "setCleanerState",
            {"mode": 2},
            namespace="cyclonext",
            robot_key="robot.1",
        )

    # ``return_to_base_payload`` inherits the base ``None`` default —
    # cyclonext has no documented RTB command. Pre-R25 the inline
    # branch list ``["vr", "vortrax", "cyclobat"]`` explicitly excluded
    # cyclonext, so the user-facing return_to_base() call simply
    # no-ops for cyclonext devices. Preserved here.

    # ``clear_desired_payload`` also inherits the base ``None`` — PR #94
    # was VR-only.

    # -- Cloud-encoding helpers --------------------------------------------

    def fan_speed_codes(self) -> dict[str, str]:
        return dict(_CYCLONEXT_FAN_SPEED_TO_CYCLE)

    def duration_key_map(self) -> dict[int, str]:
        """Cyclonext's ``durations`` keys (unverified — see the map)."""
        return dict(_CYCLONEXT_CYCLE_TO_DURATION_KEY)

    def set_fan_speed_payload(
        self, client: AqualinkClient, fan_speed: str
    ) -> dict[str, Any] | None:
        """Override the base default — cyclonext nests under ``robot.1``
        not ``robot`` and uses ``cycle`` (string) instead of ``prCyc`` (int).

        Issue #76 regression note: this is the canonical translation-key
        path. The 2 keys in ``fan_speed_codes()`` must match exactly the
        keys that vacuum.py's ``async_set_fan_speed`` forwards (the
        snake_case translation keys from `_fan_speed_list`).
        """
        codes = self.fan_speed_codes()
        if fan_speed not in codes:
            return None
        return client._build_state_request(
            "setCleaningMode",
            {"cycle": codes[fan_speed]},
            namespace="cyclonext",
            robot_key="robot.1",
        )

    def extract_fan_speed_from_response(
        self,
        payload: dict[str, Any],
        requested_fan_speed: str,
    ) -> str | None:
        """R25-followup P3: coordinator owns the outer try/except."""
        robot_state = (
            payload.get("payload", {})
            .get("robot", {})
            .get("state", {})
            .get("reported", {})
            .get("equipment", {})
            .get("robot.1", {})
        )
        cycle = robot_state.get("cycle")
        if cycle is None:
            return None
        # Issue #100: ``set_fan_speed_payload`` sends ``cycle`` as a string,
        # and firmware differs on whether it echoes back a string or an int.
        # The map is int-keyed, so coerce before lookup.
        coerced = _coerce_cloud_int(cycle)
        if coerced is None:
            return requested_fan_speed
        return _CYCLONEXT_CYCLE_TO_FAN_SPEED.get(coerced, requested_fan_speed)

    # -- Status parser -----------------------------------------------------

    def parse_status(
        self,
        client: AqualinkClient,
        data: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Parse a cyclonext ``StateReported`` envelope into ``result``
        in place. Lifted from pre-R25 ``_update_cyclonext_robot_data``.

        Cyclonext's parser doesn't share its fields with VR/Vortrax —
        reads from ``equipment.robot.1`` (the ``.1``-suffixed key) and
        uses ``mode`` for activity instead of ``state``. Activity has
        only two values (cleaning / idle); cyclonext has no ``returning``
        state since the family has no return-to-base command.
        """
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for cyclonext robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot.1']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot.1 data structure for cyclonext robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            raw_mode = robot_data['mode']
        except (KeyError, TypeError):
            result["activity"] = "unknown"
        else:
            mode = _coerce_cloud_int(raw_mode)
            if mode is None:
                # Present but uninterpretable — report "unknown" rather
                # than silently claiming "idle" (issue #100's symptom).
                _warn_once_unknown_mode(raw_mode)
                result["activity"] = "unknown"
            elif mode == _CYCLONEXT_MODE_CLEANING:
                client._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            else:
                client._activity = VacuumActivity.IDLE
                result["activity"] = "idle"

        # Current cycle → fan-speed key. Pre-fix cyclonext's parser never set
        # `fan_speed`, so the Fan Speed sensor read Unknown forever and the
        # vacuum entity's reported speed never reflected the cloud — even
        # though cyclonext DOES support two cycles and `set_fan_speed_payload`
        # writes them. Mirrors what vr.py does with `prCyc`.
        raw_cycle = _coerce_cloud_int(robot_data.get('cycle'))
        if raw_cycle is not None:
            client._fan_speed = _CYCLONEXT_CYCLE_TO_FAN_SPEED.get(
                raw_cycle, "floor_only"
            )
            result["fan_speed"] = client._fan_speed

        # Stepper (the +/- 15 minute adjustment). The cloud reports these on
        # cyclonext — both captured units carry `stepper` and `stepperAdjTime`
        # — but this parser ignored them and, critically, called
        # `calculate_times` WITHOUT `robot_data`, so the stepper defaulted to 0
        # and a time adjustment made in the app or via the integration's own
        # buttons never moved the remaining-time sensors. Reported as "the
        # +/- 15 minutes keys ... do not work in the integration" (issue #92).
        try:
            result["stepper"] = robot_data['stepper']
            result["stepper_adj_time"] = robot_data.get('stepperAdjTime', 15)
        except (KeyError, TypeError):
            result["stepper"] = 0
            result["stepper_adj_time"] = 15
            _LOGGER.debug("No stepper data for cyclonext robot, using defaults")

        try:
            result["canister"] = robot_data['canister'] * 100
        except (KeyError, TypeError):
            result["canister"] = 0

        try:
            result["error_state"] = robot_data['errors']['code']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        # R25-followup P7: tighten except scope. ``KeyError`` covers older
        # models that don't expose ``totRunTime`` (documented pre-existing
        # behaviour); ``TypeError`` covers ``robot_data`` not being a dict.
        # Other exceptions (e.g., the field is a string that arithmetic
        # downstream would choke on) are no longer silently zeroed —
        # they bubble to the broader parse_status broad-except where
        # they're logged at DEBUG.
        try:
            result["total_hours"] = robot_data['totRunTime']
        except (KeyError, TypeError):
            result["total_hours"] = 0

        # H8: aware-UTC datetime objects emitted directly. H8 review
        # follow-up: emit None on never-cleaned + parse failure.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                # Issue #100: coerce before storing — ``resolve_cycle_duration``
                # rejects a non-int cycle code and returns 0, which would zero
                # the time-remaining sensor for a string-echoing firmware.
                result["cycle"] = _coerce_cloud_int(robot_data['cycle']) or 0

                cycle_duration = resolve_cycle_duration(
                    robot_data['durations'],
                    result["cycle"],
                    self.duration_key_map(),
                )
                result["cycle_duration"] = cycle_duration

                # Pass robot_data so the stepper adjustment is applied —
                # ``calculate_times`` silently defaults the stepper to 0 when
                # it is omitted, which is why cyclonext time adjustments never
                # showed up (issue #92).
                calculate_times(
                    client, cycle_start_time, cycle_duration, result, robot_data
                )
            else:
                # Never-cleaned robot.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle"] = 0
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for cyclonext robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = format_time_human(0, 0, 0)
