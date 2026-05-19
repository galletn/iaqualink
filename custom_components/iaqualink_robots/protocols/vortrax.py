"""Vortrax-family protocol — Zodiac VortraX TRX 8500 iQ etc. (story R25-vortrax).

The Vortrax family uses the same ``setCleanerState`` action shape as VR
for lifecycle commands (1=start, 0=stop, 2=pause, 3=return-to-base) and
the same ``setCleaningMode`` + ``prCyc`` for fan-speed changes. What
differs is the parser:

* Vortrax cloud responses carry a ``product_number`` under the
  ``eboxData.completeCleanerPn`` path that VR doesn't surface.
* Vortrax has no stepper UI (no ±15-minute buttons), so the parser
  doesn't read ``stepper`` / ``stepperAdjTime`` and the time math is
  the simpler stepper-less path on ``_calculate_times``.
* Vortrax's ``cycle`` field is read at cycle-duration time (only when
  the robot has actually started a cycle), whereas VR reads it
  unconditionally to drive the live ``fan_speed`` mapping.

The Vortrax UI exposes only ``floor_only`` / ``floor_and_walls`` (see
``vacuum.py::_fan_speed_list``), but the cloud accepts the full 4-mode
VR encoding. ``fan_speed_codes()`` exposes the full map so a future
firmware bump that enables all 4 modes on Vortrax doesn't need a
protocol change — only a ``vacuum.py`` list update. The 2-vs-4
restriction is enforced at the UI layer today.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.util import dt as dt_util

from .base import RobotProtocol
from .common import apply_common_robot_fields

if TYPE_CHECKING:
    from ..coordinator import AqualinkClient

_LOGGER = logging.getLogger(__name__)


#: Cloud's ``prCyc`` value → integration's fan-speed translation key.
#: Vortrax cloud encoding matches VR (verified by inspecting
#: ``coordinator._set_other_fan_speed`` pre-R25-vortrax — the
#: ``vr or vortrax`` branch used a single shared cycle_speed_map).
_VORTRAX_PRCYC_TO_FAN_SPEED: dict[int, str] = {
    0: "wall_only",
    1: "floor_only",
    2: "smart_floor_and_walls",
    3: "floor_and_walls",
}

_VORTRAX_FAN_SPEED_TO_PRCYC: dict[str, int] = {
    v: k for k, v in _VORTRAX_PRCYC_TO_FAN_SPEED.items()
}


class VortraxProtocol(RobotProtocol):
    """Vortrax family — Zodiac VortraX TRX 8500 iQ and rebrands."""

    namespace = "vortrax"

    # -- Envelope builders --------------------------------------------------

    def start_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request("setCleanerState", {"state": 1})

    def stop_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request("setCleanerState", {"state": 0})

    def pause_payload(self, client: AqualinkClient) -> dict[str, Any]:
        # Vortrax pause: same robot.state = 2 mapping as VR.
        return client._build_state_request("setCleanerState", {"state": 2})

    def return_to_base_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return client._build_state_request("setCleanerState", {"state": 3})

    # ``clear_desired_payload`` deliberately inherits the base ``None``
    # default — pre-R25 ``clear_desired_state`` was VR-only (PR #94 fix
    # for VRX IQ+'s auto-restart bug). Vortrax has no documented
    # equivalent today; if a Vortrax operator reports the same auto-
    # restart symptom, override here.

    # -- Cloud-encoding helpers --------------------------------------------

    def fan_speed_codes(self) -> dict[str, int]:
        return dict(_VORTRAX_FAN_SPEED_TO_PRCYC)

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
            return _VORTRAX_PRCYC_TO_FAN_SPEED.get(pr_cyc, requested_fan_speed)
        except Exception:  # noqa: BLE001
            return None

    # -- Status parser -----------------------------------------------------

    def parse_status(
        self,
        client: AqualinkClient,
        data: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        """Parse a Vortrax ``StateReported`` envelope into ``result`` in
        place. Lifted from pre-R25 ``_update_vortrax_robot_data`` with
        the shared ``apply_common_robot_fields`` call replacing the 5
        fields that are common with VR.
        """
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for vortrax robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Vortrax-specific: product_number from eboxData. Read BEFORE the
        # robot_data extraction because eboxData and equipment.robot are
        # siblings under ``state.reported``; if equipment.robot is missing
        # we still want product_number to surface for diagnostics.
        try:
            result["product_number"] = (
                data['payload']['robot']['state']['reported']
                ['eboxData']['completeCleanerPn']
            )
        except Exception:
            result["product_number"] = None

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for vortrax robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # R29 fold: temp / activity / canister / errorState / totalHours
        # come from the shared helper (also used by VRProtocol).
        apply_common_robot_fields(client, robot_data, result)

        # H8: aware-UTC datetime objects emitted directly (no .isoformat()).
        # H8 review follow-up: emit None on never-cleaned + parse failure.
        # Vortrax differs from VR here: no stepper math, ``cycle`` is read
        # at cycle-duration time rather than at fan-speed time.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                result["cycle"] = robot_data['prCyc']

                cycle_duration = client._resolve_cycle_duration(
                    robot_data['durations'], result["cycle"]
                )
                result["cycle_duration"] = cycle_duration

                client._calculate_times(cycle_start_time, cycle_duration, result)
            else:
                # Never-cleaned robot.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle"] = 0
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = client._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for vortrax robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = client._format_time_human(0, 0, 0)
