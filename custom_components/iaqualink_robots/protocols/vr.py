"""VR-family protocol — Polaris VRX, Zodiac RA, similar (story R25-vr).

The VR family uses the ``setCleanerState`` action with the
``equipment.robot.state`` field for lifecycle commands (1=start, 0=stop,
2=pause, 3=return-to-base) and the ``setCleaningMode`` action with
``equipment.robot.prCyc`` for fan-speed changes. The cloud reports the
robot's live state under the same ``equipment.robot`` key.

Pre-R25 these per-family details were duplicated across six if/elif
chains in ``coordinator.py`` plus an inline 120-line
``_update_vr_robot_data`` parser. R25-vr collapses both: the envelope
builders are short methods on ``VRProtocol``, and ``parse_status``
carries the parser body unchanged so behaviour is byte-identical with
pre-R25 (the test suite asserts that fixture-by-fixture).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from ..parsing import calculate_times, format_time_human, resolve_cycle_duration
from .base import RobotProtocol
from .common import apply_common_robot_fields, resolve_smart_duration_key_map

if TYPE_CHECKING:
    from ..client import AqualinkClient

_LOGGER = logging.getLogger(__name__)


#: Cloud's ``prCyc`` value → integration's fan-speed translation key.
#: Used by both ``set_fan_speed_payload`` (we send the int) and
#: ``extract_fan_speed_from_response`` (we read the int from the cloud's
#: state report). The inverse map is computed lazily for the setter.
_VR_PRCYC_TO_FAN_SPEED: dict[int, str] = {
    0: "wall_only",            # VR dedicated wall-scrub (distinct from i2d's walls_only)
    1: "floor_only",
    2: "smart_floor_and_walls",
    3: "floor_and_walls",
}

#: Inverse of ``_VR_PRCYC_TO_FAN_SPEED`` — fan-speed key → ``prCyc`` int.
_VR_FAN_SPEED_TO_PRCYC: dict[str, int] = {v: k for k, v in _VR_PRCYC_TO_FAN_SPEED.items()}

#: ``prCyc`` → key in the cloud's ``durations`` object. Verified against a
#: live VR (RA 6570 iQ) capture, where ``durations`` was::
#:
#:     {"waterTim": 45, "quickTim": 75, "smartTim": 0, "deepTim": 165,
#:      "customTim": 150, "firstSmartTim": 150}
#:
#: The names line up semantically with the fan-speed keys above:
#: wall_only → waterline, floor_only → quick, floor_and_walls → deep.
#: Replaces the legacy positional lookup, which was correct only by
#: accident of the cloud's key insertion order.
_VR_PRCYC_TO_DURATION_KEY: dict[int, str] = {
    0: "waterTim",   # wall_only
    1: "quickTim",   # floor_only
    2: "smartTim",   # smart_floor_and_walls
    3: "deepTim",    # floor_and_walls
}


class VRProtocol(RobotProtocol):
    """VR family — Polaris VRX iQ+, Zodiac RA 65xx iQ, RA 6900 iQ, etc."""

    namespace = "vr"

    # -- Envelope builders --------------------------------------------------

    def start_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request("setCleanerState", {"state": 1})

    def stop_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request("setCleanerState", {"state": 0})

    def pause_payload(self, client: AqualinkClient) -> dict[str, Any]:
        # VR's pause: robot.state = 2.
        return client._build_state_request("setCleanerState", {"state": 2})

    def return_to_base_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request("setCleanerState", {"state": 3})

    def clear_desired_payload(self, client: AqualinkClient) -> dict[str, Any]:
        """PR #94 natural-completion fix: clear ``desired.state`` to 0
        so the cloud doesn't auto-restart the robot after a natural
        cycle end. The same payload as ``stop_payload``; kept distinct
        as a separate method so the call site reads honestly.
        """
        return client._build_state_request("setCleanerState", {"state": 0})

    # -- Cloud-encoding helpers --------------------------------------------

    def fan_speed_codes(self) -> dict[str, int]:
        return dict(_VR_FAN_SPEED_TO_PRCYC)

    def duration_key_map(self) -> dict[int, str]:
        """VR's ``durations`` keys, verified against a live RA 6570 iQ."""
        return dict(_VR_PRCYC_TO_DURATION_KEY)

    def extract_fan_speed_from_response(
        self,
        payload: dict[str, Any],
        requested_fan_speed: str,
    ) -> str | None:
        """R25-followup P3: removed protocol-side ``try/except Exception``.
        Single source-of-truth try/except now lives at the coordinator's
        ``_extract_fan_speed_from_response`` wrapper — double-swallow
        previously masked cloud-shape drift.
        """
        robot_state = (
            payload.get("payload", {})
            .get("robot", {})
            .get("state", {})
            .get("reported", {})
            .get("equipment", {})
            .get("robot", {})
        )
        pr_cyc = robot_state.get("prCyc")
        if pr_cyc is None:
            return None
        return _VR_PRCYC_TO_FAN_SPEED.get(pr_cyc, requested_fan_speed)

    # -- Status parser -----------------------------------------------------

    def parse_status(
        self,
        client: AqualinkClient,
        data: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Parse a VR ``StateReported`` envelope into ``result`` in place.

        Lifted verbatim from pre-R25 ``AqualinkClient._update_vr_robot_data``.
        Mutates ``result`` rather than returning a new dict (preserves the
        pre-R25 contract every caller — polled path + push path — expects).
        Behaviour is byte-identical with pre-R25; ``test_vr.py`` runs the
        full set of fixture payloads through both code paths and diffs.

        The time / duration helpers — ``resolve_cycle_duration``,
        ``calculate_times``, ``format_time_human`` — live in
        ``parsing.py`` (R26 module split) and are imported directly. The
        cached ``_activity`` / ``_fan_speed`` attributes still live on the
        client and the parser writes back to them via ``client.<attr>``.
        """
        # Ensure we have valid data structure before proceeding.
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for VR robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for VR robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # R25-vortrax (R29 fold): temp / activity / canister / errorState /
        # totalHours are shared with the Vortrax family — extracted to
        # ``protocols/common.py::apply_common_robot_fields``.
        apply_common_robot_fields(client, robot_data, result)

        # Stepper information for timing adjustments.
        try:
            result["stepper"] = robot_data['stepper']
            result["stepper_adj_time"] = robot_data.get('stepperAdjTime', 15)
            _LOGGER.debug(
                f"Stepper info: value={robot_data['stepper']}, adj_time={robot_data.get('stepperAdjTime', 15)}"
            )
        except (KeyError, TypeError):
            result["stepper"] = 0
            result["stepper_adj_time"] = 15
            _LOGGER.debug("No stepper data found, using defaults")

        # Current cycle (fan speed) — map cloud prCyc to translation key.
        try:
            current_cycle = robot_data['prCyc']
            result["cycle"] = current_cycle
            client._fan_speed = _VR_PRCYC_TO_FAN_SPEED.get(current_cycle, "floor_only")
            result["fan_speed"] = client._fan_speed
        except Exception as e:
            _LOGGER.debug(f"Error setting fan speed for VR robot: {e}")
            client._fan_speed = "floor_only"
            result["fan_speed"] = client._fan_speed

        # H8: aware-UTC datetime objects stored directly (no .isoformat()).
        # H8 review follow-up: emit None on never-cleaned (cycleStartTime=0)
        # and on parse failure so HA renders "Unknown" instead of "1970-01-01"
        # or "cycle started right now" pinned to the failure moment.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                cycle_duration = resolve_cycle_duration(
                    robot_data['durations'],
                    result["cycle"],
                    resolve_smart_duration_key_map(
                        robot_data, self.duration_key_map()
                    ),
                )
                result["cycle_duration"] = cycle_duration

                calculate_times(client, cycle_start_time, cycle_duration, result, robot_data)
            else:
                # Never-cleaned robot — emit None instead of a 1970 epoch render.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for VR robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = format_time_human(0, 0, 0)
