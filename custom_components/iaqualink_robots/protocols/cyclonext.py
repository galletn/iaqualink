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

from .base import RobotProtocol

if TYPE_CHECKING:
    from ..coordinator import AqualinkClient

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
        try:
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
            return _CYCLONEXT_CYCLE_TO_FAN_SPEED.get(cycle, requested_fan_speed)
        except Exception:  # noqa: BLE001
            return None

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
            if robot_data['mode'] == 1:
                client._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
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
            result["error_state"] = robot_data['errors']['code']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        try:
            result["total_hours"] = robot_data['totRunTime']
        except Exception:
            # Not supported by some cyclonext models.
            result["total_hours"] = 0

        # H8: aware-UTC datetime objects emitted directly. H8 review
        # follow-up: emit None on never-cleaned + parse failure.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                result["cycle"] = robot_data['cycle']

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
            _LOGGER.debug(f"Error processing cycle times for cyclonext robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = client._format_time_human(0, 0, 0)
