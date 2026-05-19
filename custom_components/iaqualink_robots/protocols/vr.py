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

from homeassistant.components.vacuum import VacuumActivity
from homeassistant.util import dt as dt_util

from .base import RobotProtocol

if TYPE_CHECKING:
    from ..coordinator import AqualinkClient

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

    def extract_fan_speed_from_response(
        self,
        payload: dict[str, Any],
        requested_fan_speed: str,
    ) -> str | None:
        try:
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
        except Exception:  # noqa: BLE001 — cloud response shape may drift
            return None

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

        The helper methods this calls — ``_resolve_cycle_duration``,
        ``_calculate_times``, ``_format_time_human``, plus the cached
        ``_activity`` / ``_fan_speed`` attributes — live on the client.
        Routing through ``client.<helper>`` instead of moving them keeps
        the R25-vr diff localised to envelope + parse extraction.
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

        try:
            result["temperature"] = robot_data['sensors']['sns_1']['val']
        except Exception:
            try:
                result["temperature"] = robot_data['sensors']['sns_1']['state']
            except Exception:
                # Zodiac XA 5095 iQ does not support temp.
                result["temperature"] = '0'

        try:
            robot_state = robot_data['state']
            if robot_state == 1:
                client._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                client._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                client._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        try:
            result["canister"] = robot_data['canister'] * 100
        except (KeyError, TypeError):
            result["canister"] = 0

        try:
            result["error_state"] = robot_data['errorState']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        try:
            result["total_hours"] = robot_data['totalHours']
        except (KeyError, TypeError):
            result["total_hours"] = 0

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

                cycle_duration = client._resolve_cycle_duration(
                    robot_data['durations'], result["cycle"]
                )
                result["cycle_duration"] = cycle_duration

                client._calculate_times(cycle_start_time, cycle_duration, result, robot_data)
            else:
                # Never-cleaned robot — emit None instead of a 1970 epoch render.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = client._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for VR robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = client._format_time_human(0, 0, 0)
