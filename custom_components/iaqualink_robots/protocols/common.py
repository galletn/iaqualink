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
    from ..client import AqualinkClient


_LOGGER = logging.getLogger(__name__)

#: ``prCyc`` code for the smart cycle on the VR/Vortrax families.
_SMART_CYCLE_CODE = 2


def resolve_smart_duration_key_map(
    robot_data: dict[str, Any],
    base_map: dict[int, str],
) -> dict[int, str]:
    """Return ``base_map``, redirecting the smart cycle to ``firstSmartTim``
    when the robot hasn't completed its first smart cycle yet.

    Two live VR captures made this behaviour visible::

        robot A: firstSmrtFlag=1, smartTim=0,   firstSmartTim=150
        robot B: firstSmrtFlag=0, smartTim=130, firstSmartTim=150

    ``smartTim`` is the *learned* smart-cycle duration: the cloud leaves it
    at 0 until the robot has run a smart cycle once, and ``firstSmrtFlag``
    is the "not learned yet" marker. Reading ``smartTim`` unconditionally
    therefore yields a 0-minute duration — and a 0 time-remaining — for
    any robot in that state, which is robot A above.

    The redirect only fires when ``firstSmrtFlag`` is truthy *and*
    ``smartTim`` is missing or 0, so a robot that reports both a set flag
    and a real learned duration keeps the real one.
    """
    if base_map.get(_SMART_CYCLE_CODE) is None:
        return base_map

    durations = robot_data.get("durations")
    if not isinstance(durations, dict):
        return base_map
    if "firstSmartTim" not in durations:
        return base_map

    if not robot_data.get("firstSmrtFlag"):
        return base_map

    learned = durations.get("smartTim")
    if isinstance(learned, (int, float)) and learned > 0:
        return base_map

    _LOGGER.debug(
        "firstSmrtFlag set and smartTim=%r; using firstSmartTim=%r for the "
        "smart cycle duration",
        learned, durations.get("firstSmartTim"),
    )
    redirected = dict(base_map)
    redirected[_SMART_CYCLE_CODE] = "firstSmartTim"
    return redirected


def apply_shared_diagnostic_fields(
    robot_data: dict[str, Any],
    result: dict[str, Any],
) -> None:
    """Apply firmware version, error code and diagnostic code.

    All three appear under the same key names on every WebSocket family and
    were previously read by none of the parsers except cyclobat — which does
    populate ``version`` and ``diagnostic_code``, but had no sensor exposing
    them, so the values were computed and discarded.

    Verified across four live captures (2x vr, 2x cyclonext):

    * ``vr``          — firmware string, e.g. ``"V32E56"`` / ``"V21E27"``.
                        Note the unfortunate collision: the *field* is named
                        ``vr`` on every family, including the family also
                        called ``vr``. It is a firmware revision, not a
                        device type.
    * ``errorCode``   — numeric, distinct from ``errorState``. Present on vr;
                        cyclonext nests its equivalent under ``errors.code``,
                        which its own parser already reads into
                        ``error_state``, so this is a no-op there.
    * ``diagnostic``  — numeric diagnostic/service code.

    Absent fields are left out of ``result`` entirely rather than defaulted,
    so the corresponding sensor reads Unknown instead of a misleading 0 —
    a real distinction on the CNX 50 iQ, which omits several fields the
    RE 4600 iQ reports.
    """
    firmware = robot_data.get('vr')
    if isinstance(firmware, str) and firmware:
        result["version"] = firmware

    for src, dest in (("errorCode", "error_code"),
                      ("diagnostic", "diagnostic_code")):
        value = robot_data.get(src)
        if value is not None and not isinstance(value, (dict, list)):
            result[dest] = value


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

    apply_shared_diagnostic_fields(robot_data, result)

    try:
        result["total_hours"] = robot_data['totalHours']
    except (KeyError, TypeError):
        result["total_hours"] = 0
