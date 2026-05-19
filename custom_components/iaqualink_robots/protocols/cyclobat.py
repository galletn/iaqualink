"""Cyclobat-family protocol — Zodiac RF 5600 iQ, P965 iQ, OA 6400 iQ (story R25-cyclobat).

The cyclobat family diverges from VR/Vortrax in three meaningful ways:

* **Envelope shape**: lifecycle commands use the ``setCleaningMode``
  action with the state values nested under ``main.ctrl`` (e.g.
  ``{"main": {"ctrl": 1}}`` to start). Fan-speed changes also use
  ``setCleaningMode`` but nest under ``main.mode`` with string values
  (``"0"`` / ``"1"`` / ``"2"`` / ``"3"``). Both carry
  ``namespace=cyclobat``.
* **Return-to-base anomaly**: cyclobat's RTB is the one cloud call
  that does NOT use the ``main.ctrl`` nesting — it uses the same
  flat ``setCleanerState`` + ``robot.state = 3`` shape as VR/Vortrax.
  Pre-R25 the comment at the inline branch said this is preserved
  as-is "the cloud accepts it today and changing it would be a
  behavior change". This file documents the same.
* **Status payload**: cyclobat exposes a much richer state — battery
  (level, charge state, cycles, warning), per-cycle durations (floor
  / floor+walls / smart / waterline), statistics block (run time,
  temperature, last error), last-cycle history. None of those fields
  overlap with VR/Vortrax's shape, so the R29-fold
  ``apply_common_robot_fields`` doesn't apply here — cyclobat reads
  state/temperature/totalHours from entirely different cloud paths
  (``main.state`` not ``robot.state``; ``stats.tmp`` not
  ``sensors.sns_1.val``; ``stats.totRunTime`` not ``robot.totalHours``).

The fan-speed encoding is also distinct from VR's prCyc: cyclobat
uses string ``mode`` values with a different number assignment
(0=floor_only, 1=floor_and_walls, 2=smart_floor_and_walls,
3=wall_only — VR's prCyc reverses 0 and 3 and uses ints).
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


#: Cyclobat ``main.mode`` value → integration's fan-speed translation key.
#: Used by both ``set_fan_speed_payload`` (we send the string) and
#: ``extract_fan_speed_from_response`` (we read the integer/string from
#: cloud reports — the cloud responds with the parsed int form).
_CYCLOBAT_MODE_TO_FAN_SPEED: dict[int, str] = {
    3: "wall_only",
    0: "floor_only",
    2: "smart_floor_and_walls",
    1: "floor_and_walls",
}

#: Inverse — fan-speed key → ``mode`` string value (cyclobat cloud
#: expects a string here, unlike VR's int ``prCyc``).
_CYCLOBAT_FAN_SPEED_TO_MODE: dict[str, str] = {
    v: str(k) for k, v in _CYCLOBAT_MODE_TO_FAN_SPEED.items()
}


class CycloBatProtocol(RobotProtocol):
    """Cyclobat family — Zodiac RF 5600 iQ, Polaris P965 iQ, etc."""

    namespace = "cyclobat"

    # -- Envelope builders --------------------------------------------------

    def start_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request(
            "setCleaningMode", {"main": {"ctrl": 1}}, namespace="cyclobat"
        )

    def stop_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request(
            "setCleaningMode", {"main": {"ctrl": 0}}, namespace="cyclobat"
        )

    def pause_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request(
            "setCleaningMode", {"main": {"ctrl": 2}}, namespace="cyclobat"
        )

    def return_to_base_payload(self, client: AqualinkClient) -> dict[str, Any]:
        """Anomaly: cyclobat's RTB uses the flat ``setCleanerState`` +
        ``robot.state = 3`` shape (same as VR/Vortrax), NOT the
        ``setCleaningMode`` + ``main.ctrl`` form the rest of cyclobat's
        commands use. Pre-R25 inline branch carried the same comment;
        the cloud accepts it and changing it would be a behaviour change.
        Namespace defaults to ``"cyclobat"`` via ``self._device_type``.
        """
        return client._build_state_request("setCleanerState", {"state": 3})

    # ``clear_desired_payload`` inherits the base ``None`` default — PR #94
    # was VR-only. Override here if a cyclobat operator reports the
    # auto-restart symptom.

    # -- Cloud-encoding helpers --------------------------------------------

    def fan_speed_codes(self) -> dict[str, str]:
        return dict(_CYCLOBAT_FAN_SPEED_TO_MODE)

    def set_fan_speed_payload(
        self, client: AqualinkClient, fan_speed: str
    ) -> dict[str, Any] | None:
        """Override the base default — cyclobat sends ``main.mode`` (a
        string) instead of the base's ``prCyc`` (int).
        """
        codes = self.fan_speed_codes()
        if fan_speed not in codes:
            return None
        return client._build_state_request(
            "setCleaningMode",
            {"main": {"mode": codes[fan_speed]}},
            namespace="cyclobat",
        )

    def extract_fan_speed_from_response(
        self,
        payload: dict[str, Any],
        requested_fan_speed: str,
    ) -> str | None:
        try:
            main = (
                payload.get("payload", {})
                .get("robot", {})
                .get("state", {})
                .get("reported", {})
                .get("equipment", {})
                .get("robot", {})
                .get("main", {})
            )
            mode = main.get("mode")
            if mode is None:
                return None
            return _CYCLOBAT_MODE_TO_FAN_SPEED.get(mode, requested_fan_speed)
        except Exception:  # noqa: BLE001
            return None

    # -- Status parser -----------------------------------------------------

    def parse_status(
        self,
        client: AqualinkClient,
        data: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Parse a cyclobat ``StateReported`` envelope into ``result`` in
        place. Lifted verbatim from pre-R25 ``_update_cyclobat_robot_data``.
        Cyclobat's parser doesn't share its common-fields slice with
        VR/Vortrax (different cloud paths for state/temp/total_hours),
        so the body is self-contained.
        """
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for cyclobat robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
            main_data = robot_data['main']
            battery_data = robot_data['battery']
            stats_data = robot_data['stats']
            last_cycle_data = robot_data['lastCycle']
            cycles_data = robot_data['cycles']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for cyclobat robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Basic status and version info with safe access.
        try:
            raw_status = data['payload']['robot']['state']['reported']['aws']['status']
            result["status"] = client._stabilize_status(raw_status)
        except (KeyError, TypeError):
            pass  # Status already set in calling method.

        result["version"] = robot_data.get('vr', 'Unknown')
        result["serial"] = robot_data.get('sn', '')

        # Main state information with safe access.
        try:
            robot_state = main_data['state']
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

        # Main control and mode information with safe access.
        result["control_state"] = str(main_data.get('ctrl', 'unknown'))
        result["mode"] = str(main_data.get('mode', 'unknown'))
        result["error_code"] = str(main_data.get('error', 'unknown'))

        # Battery information with safe access.
        result["battery_version"] = battery_data.get('vr', 'Unknown')
        result["battery_state"] = str(battery_data.get('state', 'unknown'))
        result["battery_percentage"] = str(battery_data.get('userChargePerc', '0'))
        result["battery_level"] = str(battery_data.get('userChargePerc', '0'))
        result["battery_charge_state"] = str(battery_data.get('userChargeState', 'unknown'))
        result["battery_cycles"] = str(battery_data.get('cycles', '0'))
        result["battery_warning_code"] = str(battery_data.get('warning', {}).get('code', 'unknown'))

        # Statistics with safe access.
        result["total_hours"] = str(stats_data.get('totRunTime', '0'))
        result["diagnostic_code"] = str(stats_data.get('diagnostic', 'unknown'))
        result["temperature"] = str(stats_data.get('tmp', '0'))
        result["last_error_code"] = str(stats_data.get('lastError', {}).get('code', 'unknown'))
        result["last_error_cycle"] = str(stats_data.get('lastError', {}).get('cycleNb', 'unknown'))

        # Last cycle information with safe access.
        result["last_cycle_number"] = str(last_cycle_data.get('cycleNb', 'unknown'))
        result["last_cycle_duration"] = str(last_cycle_data.get('duration', '0'))
        result["last_cycle_mode"] = str(last_cycle_data.get('mode', 'unknown'))
        result["cycle"] = str(last_cycle_data.get('endCycleType', '0'))
        result["last_cycle_error"] = str(last_cycle_data.get('errorCode', 'unknown'))

        # Cycle durations with safe access.
        result["floor_duration"] = str(cycles_data.get('floorTim', {}).get('duration', '0'))
        result["floor_walls_duration"] = str(cycles_data.get('floorWallsTim', {}).get('duration', '0'))
        result["smart_duration"] = str(cycles_data.get('smartTim', {}).get('duration', '0'))
        result["waterline_duration"] = str(cycles_data.get('waterlineTim', {}).get('duration', '0'))
        result["first_smart_done"] = str(cycles_data.get('firstSmartDone', 'false'))
        result["lift_pattern_time"] = str(cycles_data.get('liftPatternTim', '0'))

        # H8: aware-UTC datetime objects emitted directly. H8 review
        # follow-up: emit None on never-cleaned + parse failure.
        try:
            timestamp = main_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                # Cyclobat picks the cycle duration from cycle-type-specific
                # nested fields (no `durations` list like VR/Vortrax).
                cycle_type = last_cycle_data['endCycleType']
                cycle_duration = None
                if cycle_type == 0:  # Floor only
                    cycle_duration = cycles_data['floorTim']['duration']
                elif cycle_type == 1:  # Floor and walls
                    cycle_duration = cycles_data['floorWallsTim']['duration']
                elif cycle_type == 2:  # Smart
                    cycle_duration = cycles_data['smartTim']['duration']
                elif cycle_type == 3:  # Waterline
                    cycle_duration = cycles_data['waterlineTim']['duration']

                if cycle_duration is not None:
                    result["cycle_duration"] = cycle_duration
                    client._calculate_times(cycle_start_time, cycle_duration, result)
                else:
                    result["cycle_duration"] = 0
                    result["time_remaining"] = 0
                    result["time_remaining_human"] = client._format_time_human(0, 0, 0)
            else:
                # Never-cleaned robot.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = client._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for cyclobat robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = client._format_time_human(0, 0, 0)
