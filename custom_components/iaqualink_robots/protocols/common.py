"""Shared parser helpers across protocol families (stories R25-vr / R25-vortrax).

The VR and Vortrax families share a substantial slice of their status-parse
logic — both read the ``equipment.robot`` block, both surface temperature
via ``sensors.sns_1`` with the same ``.val`` → ``.state`` → ``'0'``
fallback ladder, and both map the cloud's ``robot.state`` integer onto
the integration's ``cleaning`` / ``returning`` / ``idle`` raw keys with
matching ``VacuumActivity`` mirroring. Pre-R25 these were two copies of
roughly 40 lines each; R29 was a placeholder story for "extract the
shared bits", folded into R25-vortrax per the team review.

Each per-family parser is responsible for:

1. Validating the incoming payload (presence checks)
2. Extracting its ``robot_data`` block from the nested payload path
3. Calling ``apply_common_robot_fields(client, robot_data, result)``
4. Applying its own family-specific fields (VR has ``stepper`` /
   ``prCyc`` / stepper-aware time math; Vortrax has ``product_number``
   from ``eboxData`` + stepper-less time math)

Splitting at the ``robot_data``-already-extracted boundary keeps each
parser's payload-shape try/except local to that family while letting
the genuinely-shared fields live once.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.vacuum import VacuumActivity

if TYPE_CHECKING:
    from ..coordinator import AqualinkClient


_LOGGER = logging.getLogger(__name__)


def apply_common_robot_fields(
    client: AqualinkClient,
    robot_data: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Apply temp / activity / canister / errorState / totalHours.

    These five fields appear on both VR and Vortrax cloud responses with
    the same shape, the same default semantics, and the same edge-case
    handling. Lifted verbatim from the pre-R25 ``_update_vr_robot_data``
    + ``_update_vortrax_robot_data`` parsers — behaviour is byte-identical.

    The function mutates ``result`` in place (keeps the pre-R25 contract
    every caller expects) and updates ``client._activity`` on the
    state→activity branch (the vacuum entity reads it for the
    ``VacuumActivity`` enum mapping).
    """
    # Temperature: prefer ``.val`` then ``.state`` then ``'0'``. Zodiac
    # XA 5095 iQ does not expose temp at all, hence the final fallback.
    try:
        result["temperature"] = robot_data['sensors']['sns_1']['val']
    except Exception:
        try:
            result["temperature"] = robot_data['sensors']['sns_1']['state']
        except Exception:
            result["temperature"] = '0'

    # Activity: cloud robot.state → integration raw key (post-M11
    # lowercase keys, not title-cased).
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

    # Canister, error state, total hours — all simple field reads with
    # default fallbacks for missing or wrong-shaped fields.
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
