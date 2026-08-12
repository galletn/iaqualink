from __future__ import annotations

import logging
import asyncio
from typing import Any, Dict, List, Optional

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,  # Note: This requires Home Assistant 2025.1 or later
)

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import AqualinkClient
from .coordinator import AqualinkDataUpdateCoordinator
from .const import DOMAIN
from .device import build_device_info
from .types import IaqualinkConfigEntry

_LOGGER = logging.getLogger(__name__)

# State comes from the coordinator; commands are individually rate-limited by
# the client, so HA does not need to serialise entity updates for this
# platform.
PARALLEL_UPDATES = 0

ROBOT_FEATURES = {
    "default": (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
    ),
    "vr": (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
        | VacuumEntityFeature.RETURN_HOME
    ),
    "vortrax": (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
        | VacuumEntityFeature.RETURN_HOME
    ),
    "cyclobat": (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
    ),
    "cyclonext": (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
    ),
    "i2d_robot": (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
        | VacuumEntityFeature.RETURN_HOME
    )
}

# Define the platform for our vacuum entity
PLATFORM = "vacuum"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IaqualinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the vacuum platform."""
    coordinator = entry.runtime_data.coordinator
    client = coordinator.client

    # Use the device serial number as part of the unique ID
    serial_number = entry.data.get("serial_number", client.robot_id)

    vacuum = IAquaLinkRobotVacuum(coordinator, client, serial_number, hass)
    async_add_entities([vacuum], True)

    # Story C1a: register the 7 services declared in `services.yaml`. Pre-C1a
    # these were advertised in `services.yaml` but never wired to anything —
    # automations calling `iaqualink_robots.remote_forward` (etc.) raised
    # `ServiceNotFound`. Each call dispatches to the matching ``async_*``
    # method on the vacuum entity (via the public
    # ``platform.async_register_entity_service`` hook). The empty ``{}``
    # schema means no service fields beyond the entity target.
    #
    # **User-facing behaviour change**: any pre-existing automation that
    # called these service names now actually runs the action (instead of
    # failing with ServiceNotFound). Documented in the CHANGELOG.
    from homeassistant.helpers import entity_platform
    platform = entity_platform.async_get_current_platform()
    for service_name, method in (
        ("remote_forward", "async_remote_forward"),
        ("remote_backward", "async_remote_backward"),
        ("remote_rotate_left", "async_remote_rotate_left"),
        ("remote_rotate_right", "async_remote_rotate_right"),
        ("remote_stop", "async_remote_stop"),
        ("add_fifteen_minutes", "async_add_fifteen_minutes"),
        ("reduce_fifteen_minutes", "async_reduce_fifteen_minutes"),
    ):
        platform.async_register_entity_service(service_name, {}, method)


#: Fan-speed translation key → the ``/features`` capability that gates it.
#:
#: Derived from four live captures. Verified both directions: the two
#: cyclonext units advertise only ``quick_clean`` + ``preset_deep_ultra`` and
#: this map reproduces their known-correct 2-entry list, while the two vr
#: units differ from each other only by ``preset_waterline``.
#:
#: ``walls_only`` (i2d's waterline mode) is deliberately absent: i2d speaks
#: the REST hex protocol and its capabilities aren't described by this
#: endpoint, so it must never be narrowed away.
_FAN_SPEED_REQUIRED_FEATURE: Dict[str, str] = {
    "wall_only": "preset_waterline",
    "floor_only": "quick_clean",
    "floor_and_walls": "preset_deep_ultra",
    "smart_floor_and_walls": "smart_cycle",
}


#: Per-device-family fan-speed lists (the HA-display side of the contract).
#:
#: These MUST mirror the per-device-type ``cycle_speed_map`` keys in
#: ``client.py::_set_other_fan_speed`` / ``_set_i2d_fan_speed``, otherwise the
#: user can pick a mode the cloud has no encoding for and the command silently
#: no-ops (issue #76).
#:
#: Glossary — the two similar keys are NOT the same mode:
#:   ``wall_only``  = the cycle a vr/cyclobat robot runs when ONLY scrubbing
#:                    walls. NOT a typo for ``walls_only``.
#:   ``walls_only`` = i2d_robot's "waterline only" mode (0x04). A distinct
#:                    cloud-side encoding; cyclonext does NOT support it.
_FAN_SPEEDS_BY_DEVICE_TYPE: Dict[str, List[str]] = {
    "vr": ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"],
    # cyclobat cloud encoding: 0=floor_only, 1=floor_and_walls,
    # 2=smart_floor_and_walls, 3=wall_only (see client.py).
    "cyclobat": ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"],
    "vortrax": ["floor_only", "floor_and_walls"],
    # cyclonext only supports cycle=1 (floor_only) and cycle=3
    # (floor_and_walls). Before the #76 fix it fell through to the i2d-shaped
    # list and exposed a non-functional "Walls only" option.
    "cyclonext": ["floor_only", "floor_and_walls"],
    "i2d_robot": ["floor_only", "walls_only", "floor_and_walls"],
}

#: Fallback for an unknown future device type: the i2d-shaped 3-entry list,
#: which is what the pre-table `else` branch used.
_FAN_SPEEDS_DEFAULT: List[str] = ["floor_only", "walls_only", "floor_and_walls"]


def _narrow_fan_speeds_to_features(
    speeds: List[str],
    features: Any,
) -> List[str]:
    """Drop fan speeds the device doesn't advertise in ``/features``.

    Fails OPEN in every ambiguous case, because a wrongly-narrowed list
    removes working functionality whereas a wrongly-wide one only reproduces
    today's behaviour:

    * ``features`` missing, empty, or not a list → return ``speeds`` unchanged
      (the features call hasn't succeeded yet, or the family isn't described
      by it).
    * a speed with no entry in the gate map → always kept.
    * if narrowing would empty the list → return ``speeds`` unchanged, since
      an empty ``fan_speed_list`` would strip fan-speed support entirely.
    """
    if not isinstance(features, list) or not features:
        return speeds

    advertised = {str(f) for f in features}
    narrowed = [
        speed for speed in speeds
        if _FAN_SPEED_REQUIRED_FEATURE.get(speed) is None
        or _FAN_SPEED_REQUIRED_FEATURE[speed] in advertised
    ]
    if not narrowed:
        _LOGGER.debug(
            "Feature narrowing would empty the fan-speed list "
            "(features=%s); keeping the family default %s",
            sorted(advertised), speeds,
        )
        return speeds
    if narrowed != speeds:
        dropped = [s for s in speeds if s not in narrowed]
        _LOGGER.debug(
            "Dropped unsupported fan speeds %s — not advertised by the device",
            dropped,
        )
    return narrowed


class IAquaLinkRobotVacuum(CoordinatorEntity[AqualinkDataUpdateCoordinator], StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    # P11: HA 2024+ entity-naming. The vacuum entity is the primary entity for
    # the device — `_attr_name = None` with `_attr_has_entity_name = True` makes
    # HA render the friendly name as just the device-registry name (the user's
    # typed entry title) instead of "<device> <legacy-name-property>". Sensors
    # and buttons compose as "<device> <translated-suffix>". Closes #79.
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: AqualinkDataUpdateCoordinator,
        client: AqualinkClient,
        serial_number: str,
        hass: HomeAssistant,
    ) -> None:
        """Initialize the vacuum."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._attributes: Dict[str, Any] = {}
        self._activity = VacuumActivity.IDLE
        self._supported_features: VacuumEntityFeature = ROBOT_FEATURES["default"]
        self._client = client
        self._hass = hass
        self._fan_speed_list: List[str] = ["floor_only", "floor_and_walls"]
        self._fan_speed: str | None = self._fan_speed_list[0]
        self._status: Optional[str] = None
        self._restored_collision_logged: bool = False
        # Rate-limit state for the fan-speed mismatch warning, which sits in
        # the per-update path. Keyed by (current_display, available_speeds).
        self._warned_fan_speed_mismatches: set[tuple[str | None, tuple[str, ...]]] = set()

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator.

        Runs on every poll (SCAN_INTERVAL is 3s), so the body is kept to
        cheap attribute work and logs only when something actually changed.
        The per-concern helpers below were split out of what used to be a
        single 165-line method.
        """
        super()._handle_coordinator_update()

        data = self.coordinator.data
        if data:
            if self._apply_degraded_state(data):
                # Connection/data error — cached state preserved, nothing
                # else to apply this cycle.
                self.async_write_ha_state()
                return

            self._status = data.get("status")
            # Only update attributes if data has changed (story M14).
            # ``dict(data)`` is a shallow copy — defensive against accidental
            # mutation of ``self._attributes`` (e.g. through
            # ``extra_state_attributes``) leaking back into ``coordinator.data``
            # and corrupting the shared state every other entity reads from.
            # Pre-M14 the assignment was ``self._attributes = data`` (a
            # reference), which the comment marketed as "performance" but was
            # actually a latent aliasing bug. The shallow copy is cheap for
            # the small dicts here (handful of scalar fields); the historic
            # performance concern doesn't measurably exist.
            if self._attributes != data:
                self._attributes = dict(data)

            self._apply_activity(data)
            self._apply_device_capabilities(data)
            self._apply_fan_speed(data)

        self.async_write_ha_state()
        self._warn_on_fan_speed_mismatch()

    # -- _handle_coordinator_update helpers --------------------------------

    def _apply_degraded_state(self, data: Dict[str, Any]) -> bool:
        """Handle the connection/data-error path.

        Returns True when the update was a degraded one and the caller
        should stop: activity, fan speed and attributes are deliberately
        left at their last known good values. The coordinator's resilient
        status logic already preserves the last real status and only marks
        the robot offline after repeated failures, so its ``status`` is
        still worth taking.
        """
        error_state = data.get("error_state")
        if error_state not in ("no_data", "update_failed", "setup_cancelled", "connection_failed"):
            return False

        coordinator_status = data.get("status")
        if coordinator_status:
            self._status = coordinator_status
        _LOGGER.debug(
            "Preserving cached vacuum state during %s error (status=%s)",
            error_state, coordinator_status,
        )
        return True

    def _apply_activity(self, data: Dict[str, Any]) -> None:
        """Map the coordinator's activity string onto the VacuumActivity enum.

        ``unknown`` is deliberately not mapped: it means "the cloud didn't
        say", and downgrading that to IDLE made a cleaning robot flicker to
        idle whenever a single poll came back thin.
        """
        activity = data.get("activity", "idle")
        if activity == "unknown":
            _LOGGER.debug("Preserving activity %s instead of unknown", self._activity)
            return
        self._activity = {
            "cleaning": VacuumActivity.CLEANING,
            "returning": VacuumActivity.RETURNING,
            "error": VacuumActivity.ERROR,
        }.get(activity, VacuumActivity.IDLE)

    def _apply_device_capabilities(self, data: Dict[str, Any]) -> None:
        """Set supported features and the fan-speed list for this device type."""
        device_type = data.get("device_type")
        if not device_type:
            return

        self._supported_features = ROBOT_FEATURES.get(device_type, ROBOT_FEATURES["default"])
        speeds = list(_FAN_SPEEDS_BY_DEVICE_TYPE.get(device_type, _FAN_SPEEDS_DEFAULT))

        # Narrow the per-family list to what THIS device advertises.
        #
        # The per-family lists are a family-wide upper bound, but capability
        # varies within a family: of two live `vr` robots, only one
        # advertised `preset_waterline`. The other was being offered
        # "Wall only", which the cloud silently ignored — the same failure
        # issue #76 reported for cyclonext, still present for vr.
        # Cross-check: deriving the list from features for the two captured
        # cyclonext units independently reproduces the hardcoded list.
        self._fan_speed_list = _narrow_fan_speeds_to_features(
            speeds, data.get("features")
        )

    def _apply_fan_speed(self, data: Dict[str, Any]) -> None:
        """Adopt the coordinator's fan speed, stored as the raw key.

        ``unknown`` and empty values are ignored rather than written
        through — the display property would otherwise render a mode the
        robot isn't in whenever a poll came back without cycle data.
        """
        coordinator_fan_speed = data.get("fan_speed")
        if not coordinator_fan_speed or coordinator_fan_speed == "unknown":
            return
        if coordinator_fan_speed == self._fan_speed:
            return
        _LOGGER.debug(
            "Fan speed updated from coordinator: %s -> %s",
            self._fan_speed, coordinator_fan_speed,
        )
        # Always store the raw key — the fan_speed property converts to the
        # display name.
        self._fan_speed = coordinator_fan_speed

    def _warn_on_fan_speed_mismatch(self) -> None:
        """Warn once per distinct drift between current and available speeds.

        Rate-limited because this runs on EVERY coordinator update: an
        unconditional warning produced thousands of log lines a day for any
        device stuck in a mismatched state.

        Feature-narrowing (see ``_narrow_fan_speeds_to_features``) makes a
        mismatch *more* reachable: a device can report a cloud fan speed we
        deliberately removed from its list because it doesn't advertise the
        capability. That is a legitimate state, not a bug, and must not spam.
        """
        if not getattr(self, "_fan_speed", None):
            return

        current_display = self.fan_speed
        available_speeds = self.fan_speed_list
        if current_display in available_speeds:
            return

        # Lazily created rather than relied upon from __init__: the entity is
        # legitimately constructed via __new__ in places (e.g. tests
        # exercising only _handle_coordinator_update), and a log rate-limiter
        # must never be the reason an update path raises. A class-level
        # default would share the set across instances, so build it
        # per-instance on first use.
        warned = getattr(self, "_warned_fan_speed_mismatches", None)
        if warned is None:
            warned = set()
            self._warned_fan_speed_mismatches = warned

        signature = (current_display, tuple(available_speeds))
        if signature in warned:
            return
        warned.add(signature)
        _LOGGER.warning(
            "Fan speed mismatch: current '%s' is not in the "
            "available speeds %s. The device may be reporting a "
            "cycle it does not advertise support for; this is "
            "logged once per distinct mismatch.",
            current_display, available_speeds,
        )

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return f"{DOMAIN}_{self._serial_number}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information about this entity."""
        return build_device_info(self.coordinator)

    @property
    def activity(self) -> VacuumActivity:
        """Return the current activity of the vacuum."""
        return self._activity

    @property
    def fan_speed(self) -> Optional[str]:
        """Return the current fan speed."""
        if self._fan_speed:
            # For now, use synchronous fallback - translation will be handled in future update
            english_fallback = {
                "floor_only": "Floor only",
                "wall_only": "Wall only",
                "walls_only": "Walls only",
                "floor_and_walls": "Floor and walls",
                "smart_floor_and_walls": "SMART Floor and walls"
            }
            display_name = english_fallback.get(self._fan_speed, self._fan_speed)
            _LOGGER.debug("Vacuum fan_speed property: '%s' -> '%s'", self._fan_speed, display_name)
            return display_name
        _LOGGER.debug("Vacuum fan_speed property: None (no fan speed set)")
        return None

    @property
    def fan_speed_list(self) -> List[str]:
        """Return the list of available fan speeds."""
        # Return English fan speed names for now - translation will be improved in future update
        english_fallback = {
            "floor_only": "Floor only",
            "wall_only": "Wall only",
            "walls_only": "Walls only",
            "floor_and_walls": "Floor and walls",
            "smart_floor_and_walls": "SMART Floor and walls"
        }
        display_list = [english_fallback.get(speed, speed) for speed in self._fan_speed_list]
        _LOGGER.debug("Vacuum fan_speed_list: %s -> %s", self._fan_speed_list, display_list)
        return display_list

    @property
    def should_poll(self) -> bool:
        """Return True if the vacuum should be polled for state updates."""
        return False  # Coordinator handles polling

    @property
    def available(self) -> bool:
        """Vacuum stays available across short cloud blips, unavailable on long outage.

        H7: pre-rewrite this only checked ``coordinator.data is not None``,
        so the vacuum stayed available indefinitely while the coordinator
        served stale ``_last_data`` through the broad-except path. The new
        ``coordinator.is_long_outage`` property flips to True once the
        outage exceeds ``LONG_OUTAGE_THRESHOLD_SECONDS``, so user
        automations bound to ``available`` no longer break on ISP blips
        but are honestly notified of a real multi-minute outage.
        """
        return (
            self.coordinator.data is not None
            and not self.coordinator.is_long_outage
        )

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return entity-specific state attributes plus the H7 `restored` flag.

        ``restored`` (H7 AC #3): ``True`` whenever the vacuum's last update
        came from the coordinator's cached ``_last_data`` rather than a
        fresh poll. Flips back to ``False`` automatically on the next
        successful poll.

        ``fan_speed`` is filtered out of the underlying attributes dict
        because it has a dedicated property — leaving the raw key here
        conflicts with the display-name conversion in
        ``IAquaLinkRobotVacuum.fan_speed``.
        """
        filtered_attributes = {k: v for k, v in self._attributes.items() if k != "fan_speed"}
        # H7 review follow-up: defensive logging if the upstream cloud
        # payload ever starts returning a ``restored`` key of its own. We
        # always overwrite with our H7 flag (the entity-attribute contract
        # is ours, not the cloud's), but a debug breadcrumb makes the
        # collision visible the first time it happens so the next
        # maintainer doesn't chase a phantom value drift. Logs ONCE per
        # IAquaLinkRobotVacuum instance via a class-level set to avoid
        # per-poll log churn.
        if "restored" in filtered_attributes and not getattr(self, "_restored_collision_logged", False):
            _LOGGER.warning(
                "Cloud payload contains a 'restored' attribute (value=%r) "
                "which collides with the H7 'restored' entity flag; "
                "the H7 value will take precedence in extra_state_attributes",
                filtered_attributes["restored"],
            )
            self._restored_collision_logged = True
        filtered_attributes["restored"] = self.coordinator.is_serving_stale_data
        return filtered_attributes

    @property
    def status(self) -> Optional[str]:
        """Return the status of the vacuum."""
        return self._status

    @property
    def supported_features(self) -> VacuumEntityFeature:
        """Return the supported features of the vacuum."""
        return self._supported_features

    def _require_connected(self) -> None:
        """Raise unless the robot is reachable through the cloud.

        These commands used to ``return`` silently when the robot was
        disconnected, so a start/stop/return-home from a dashboard or an
        automation looked like it had worked while nothing was sent. Raising
        makes the failure visible in the UI and in automation traces.
        """
        if self._status != "connected":
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="not_connected",
                translation_placeholders={
                    "name": str(self.coordinator.title or self._serial_number),
                },
            )

    async def async_start(self) -> None:
        """Start the vacuum - delegate to coordinator."""
        self._require_connected()

        await self.coordinator.async_start_cleaning()
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the vacuum - delegate to coordinator."""
        self._require_connected()

        await self.coordinator.async_stop_cleaning()
        await self.coordinator.async_refresh()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        # Convert display name back to internal key if needed
        display_to_key = {
            "Floor only": "floor_only",
            "Wall only": "wall_only",
            "Walls only": "walls_only",
            "Floor and walls": "floor_and_walls",
            "SMART Floor and walls": "smart_floor_and_walls"
        }

        # Check if it's a display name and convert to key
        internal_key = display_to_key.get(fan_speed, fan_speed)

        # Check if the internal key is in our list. ServiceValidationError
        # (not ValueError) so HA renders it as a user mistake against the
        # offending action, with a translated message naming what IS
        # supported — a bare ValueError surfaced as an integration traceback.
        if internal_key not in self._fan_speed_list:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_fan_speed",
                translation_placeholders={
                    "fan_speed": str(fan_speed),
                    "options": ", ".join(self._fan_speed_list),
                },
            )

        self._fan_speed = internal_key

        # Immediately update the entity state to reflect the change
        self.async_write_ha_state()

        try:
            # Send the command and get immediate response via websocket
            command_result = await self._client.set_fan_speed(internal_key, self._fan_speed_list)

            # Log the response for debugging
            if command_result:
                _LOGGER.debug("Fan speed command result: %s", command_result)

                # If command was successful and we got confirmation, update immediately
                if command_result.get("success"):
                    # Use confirmed fan speed from websocket if available, otherwise use requested
                    confirmed_speed = command_result.get("confirmed_fan_speed", internal_key)
                    if confirmed_speed != internal_key:
                        _LOGGER.debug(
                            "Websocket confirmed different fan speed than requested: %s vs %s",
                            confirmed_speed, internal_key)
                        self._fan_speed = confirmed_speed

                    _LOGGER.debug("Fan speed successfully changed to: %s", self._fan_speed)
                    # Push the updated state immediately to Home Assistant
                    self.async_write_ha_state()
                else:
                    _LOGGER.warning("Fan speed command may have failed: %s", command_result)

            # Request immediate refresh to get updated state from device
            # Add a small delay to allow the device to process the command
            await asyncio.sleep(1)
            await self.coordinator.async_request_refresh()

            # For extra responsiveness, trigger another refresh after a short delay.
            # P10: routed through coordinator so cleanup() can cancel it on unload.
            self.coordinator.schedule_task(self._delayed_refresh())

        except HomeAssistantError:
            # Already user-facing and translated (and ConfigEntryAuthFailed
            # arrives through here too, which must reach HA unwrapped so the
            # reauth flow fires).
            await self.coordinator.async_request_refresh()
            raise
        except Exception as e:
            _LOGGER.debug("Failed to set fan speed: %s", e, exc_info=True)
            # Revert local state if the command failed by requesting a refresh,
            # then surface a translated error instead of a raw traceback.
            await self.coordinator.async_request_refresh()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "command": f"set cleaning mode to {internal_key}",
                    "name": str(self.coordinator.title or self._serial_number),
                    "error": str(e),
                },
            ) from e

    async def _delayed_refresh(self) -> None:
        """Trigger a delayed refresh for extra responsiveness."""
        await asyncio.sleep(3)  # Wait 3 seconds then refresh again
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Set the vacuum cleaner to return to the dock - delegate to coordinator."""
        self._require_connected()

        await self.coordinator.async_return_to_base()
        await self.coordinator.async_request_refresh()

    async def async_remote_forward(self, **kwargs: Any) -> None:
        """Send forward command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_forward()

    async def async_remote_backward(self, **kwargs: Any) -> None:
        """Send backward command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_backward()

    async def async_remote_rotate_left(self, **kwargs: Any) -> None:
        """Send rotate left command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_rotate_left()

    async def async_remote_rotate_right(self, **kwargs: Any) -> None:
        """Send rotate right command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_rotate_right()

    async def async_remote_stop(self, **kwargs: Any) -> None:
        """Send stop command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_stop()

    async def async_add_fifteen_minutes(self, **kwargs: Any) -> None:
        """Add 15 minutes to cleaning time - delegate to coordinator."""
        await self.coordinator.async_add_fifteen_minutes()

    async def async_reduce_fifteen_minutes(self, **kwargs: Any) -> None:
        """Reduce 15 minutes from cleaning time - delegate to coordinator."""
        await self.coordinator.async_reduce_fifteen_minutes()
