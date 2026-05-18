import logging
import asyncio
from typing import Any, Dict, List, Optional

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,  # Note: This requires Home Assistant 2025.1 or later
)

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import AqualinkDataUpdateCoordinator
from .const import DOMAIN
from .device import build_device_info

_LOGGER = logging.getLogger(__name__)
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


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the vacuum platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]

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


class IAquaLinkRobotVacuum(CoordinatorEntity[AqualinkDataUpdateCoordinator], StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    # P11: HA 2024+ entity-naming. The vacuum entity is the primary entity for
    # the device — `_attr_name = None` with `_attr_has_entity_name = True` makes
    # HA render the friendly name as just the device-registry name (the user's
    # typed entry title) instead of "<device> <legacy-name-property>". Sensors
    # and buttons compose as "<device> <translated-suffix>". Closes #79.
    _attr_has_entity_name = True
    _attr_name = None

    def __init__(self, coordinator: AqualinkDataUpdateCoordinator, client, serial_number: str, hass):
        """Initialize the vacuum."""
        super().__init__(coordinator)
        self._serial_number = serial_number
        self._attributes: Dict[str, Any] = {}
        self._activity = VacuumActivity.IDLE
        self._supported_features = ROBOT_FEATURES["default"]
        self._client = client
        self._hass = hass
        self._fan_speed_list: List[str] = ["floor_only", "floor_and_walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._status: Optional[str] = None

    def _handle_coordinator_update(self):
        """Handle updated data from the coordinator."""
        super()._handle_coordinator_update()

        # Update all attributes from coordinator data - coordinator handles all business logic
        data = self.coordinator.data
        if data:
            # Check if this is a no_data or connection error state - preserve cached state in these cases
            error_state = data.get("error_state")
            if error_state in ["no_data", "update_failed", "setup_cancelled", "connection_failed"]:
                # During connection/data errors, preserve current vacuum state
                _LOGGER.debug(f"Vacuum preserving cached state during {error_state} error")
                # Trust the coordinator's resilient status logic - it preserves last known status
                # for temporary failures and only marks offline after multiple consecutive failures
                coordinator_status = data.get("status")
                if coordinator_status:
                    self._status = coordinator_status
                    _LOGGER.debug(
                        f"Vacuum status updated to '{coordinator_status}' from coordinator's resilient status logic")
                # But keep current activity, fan speed, and attributes
                self.async_write_ha_state()
                return

            # Log when fan speed changes for debugging
            current_fan_speed = data.get("fan_speed")
            if current_fan_speed != self._attributes.get("fan_speed"):
                _LOGGER.info(f"Vacuum fan speed updated: {self._attributes.get('fan_speed')} -> {current_fan_speed}")

            # Get status and activity directly from coordinator
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

            # Map activity from coordinator to VacuumActivity enum - only if not "unknown"
            activity = data.get("activity", "idle")
            if activity != "unknown":  # Don't change activity to idle if we get "unknown"
                if activity == "cleaning":
                    self._activity = VacuumActivity.CLEANING
                elif activity == "returning":
                    self._activity = VacuumActivity.RETURNING
                elif activity == "error":
                    self._activity = VacuumActivity.ERROR
                else:
                    self._activity = VacuumActivity.IDLE
            else:
                _LOGGER.debug(f"Vacuum preserving current activity {self._activity} instead of unknown")

            # Set device type specific features and fan speed list
            device_type = data.get("device_type")
            if device_type:
                self._supported_features = ROBOT_FEATURES.get(device_type, ROBOT_FEATURES["default"])

                # Set fan speed list based on device type. The list MUST mirror
                # the per-device-type `cycle_speed_map` keys in
                # `coordinator.py::_set_other_fan_speed` and `_set_i2d_fan_speed`,
                # otherwise the user can pick a mode the cloud has no encoding
                # for and the command silently no-ops (issue #76).
                #
                # Glossary:
                #   `wall_only`  = cycle the robot runs when ONLY scrubbing walls
                #                  (vr/cyclobat capability). NOT a typo for
                #                  `walls_only`.
                #   `walls_only` = i2d_robot's "waterline only" mode (mode 0x04).
                #                  Distinct cloud-side encoding from `wall_only`;
                #                  cyclonext does NOT support this mode.
                if device_type == "vr":
                    self._fan_speed_list = ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"]
                elif device_type == "cyclobat":
                    # cyclobat cloud encoding: 0=floor_only, 1=floor_and_walls,
                    # 2=smart_floor_and_walls, 3=wall_only (see coordinator.py).
                    self._fan_speed_list = ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"]
                elif device_type == "vortrax":
                    self._fan_speed_list = ["floor_only", "floor_and_walls"]
                elif device_type == "cyclonext":
                    # cyclonext cloud encoding only supports cycle=1 (floor_only)
                    # and cycle=3 (floor_and_walls). Pre-fix this branch fell
                    # through to the i2d-shaped 3-entry list and exposed a
                    # non-functional "Walls only" option (issue #76).
                    self._fan_speed_list = ["floor_only", "floor_and_walls"]
                else:
                    # i2d_robot (and any unknown future type) keeps the
                    # 3-entry list which IS valid for i2d (mode 0x04).
                    self._fan_speed_list = ["floor_only", "walls_only", "floor_and_walls"]

            # Update current fan speed from coordinator data (store as raw key) - only if not unknown
            coordinator_fan_speed = data.get("fan_speed")
            if coordinator_fan_speed and coordinator_fan_speed != "unknown" and coordinator_fan_speed != self._fan_speed:
                _LOGGER.debug(
                    f"Updating vacuum fan speed from coordinator: {self._fan_speed} -> {coordinator_fan_speed}")
                # Always store the raw key - the fan_speed property will convert to display name
                self._fan_speed = coordinator_fan_speed
                _LOGGER.debug(f"Vacuum _fan_speed now set to: '{self._fan_speed}' (raw key)")
            elif coordinator_fan_speed and coordinator_fan_speed != "unknown":
                _LOGGER.debug(
                    f"Vacuum fan speed unchanged: '{self._fan_speed}' (coordinator also has '{coordinator_fan_speed}')")
            else:
                _LOGGER.debug(
                    f"Coordinator has no valid fan_speed data ({coordinator_fan_speed}), vacuum keeps: '{self._fan_speed}'")

        self.async_write_ha_state()

        # Debug logging after state update
        if hasattr(self, '_fan_speed') and self._fan_speed:
            # Test the fan_speed property after the update
            current_display = self.fan_speed
            available_speeds = self.fan_speed_list
            _LOGGER.debug(
                f"After coordinator update - Display fan speed: '{current_display}', Available: {available_speeds}")
            if current_display not in available_speeds:
                _LOGGER.warning(
                    f"Fan speed mismatch! Current '{current_display}' not in available speeds {available_speeds}")
            else:
                _LOGGER.debug(f"Fan speed consistency OK: '{current_display}' is in available speeds")

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
            _LOGGER.debug(f"Vacuum fan_speed property: '{self._fan_speed}' -> '{display_name}'")
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
        _LOGGER.debug(f"Vacuum fan_speed_list: {self._fan_speed_list} -> {display_list}")
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
    def supported_features(self) -> int:
        """Return the supported features of the vacuum."""
        return self._supported_features

    async def async_start(self) -> None:
        """Start the vacuum - delegate to coordinator."""
        if self._status != "connected":
            return

        # Delegate to coordinator for business logic
        # Type ignore because mypy doesn't recognize the custom methods
        await self.coordinator.async_start_cleaning()  # type: ignore
        await self.coordinator.async_request_refresh()

    async def async_stop(self, **kwargs) -> None:
        """Stop the vacuum - delegate to coordinator."""
        if self._status != "connected":
            return

        # Delegate to coordinator for business logic
        await self.coordinator.async_stop_cleaning()  # type: ignore
        await self.coordinator.async_refresh()

    async def async_set_fan_speed(self, fan_speed: str, **kwargs) -> None:
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

        # Check if the internal key is in our list
        if internal_key not in self._fan_speed_list:
            raise ValueError('Invalid fan speed')

        self._fan_speed = internal_key

        # Immediately update the entity state to reflect the change
        self.async_write_ha_state()

        try:
            # Send the command and get immediate response via websocket
            command_result = await self._client.set_fan_speed(internal_key, self._fan_speed_list)

            # Log the response for debugging
            if command_result:
                _LOGGER.debug(f"Fan speed command result: {command_result}")

                # If command was successful and we got confirmation, update immediately
                if command_result.get("success"):
                    # Use confirmed fan speed from websocket if available, otherwise use requested
                    confirmed_speed = command_result.get("confirmed_fan_speed", internal_key)
                    if confirmed_speed != internal_key:
                        _LOGGER.info(
                            f"Websocket confirmed different fan speed than requested: {confirmed_speed} vs {internal_key}")
                        self._fan_speed = confirmed_speed

                    _LOGGER.debug(f"Fan speed successfully changed to: {self._fan_speed}")
                    # Push the updated state immediately to Home Assistant
                    self.async_write_ha_state()
                else:
                    _LOGGER.warning(f"Fan speed command may have failed: {command_result}")

            # Request immediate refresh to get updated state from device
            # Add a small delay to allow the device to process the command
            await asyncio.sleep(1)
            await self.coordinator.async_request_refresh()

            # For extra responsiveness, trigger another refresh after a short delay.
            # P10: routed through coordinator so cleanup() can cancel it on unload.
            self.coordinator._schedule_task(self._delayed_refresh())

        except Exception as e:
            _LOGGER.error(f"Failed to set fan speed: {e}")
            # Revert local state if command failed by requesting refresh
            await self.coordinator.async_request_refresh()
            raise

    async def _delayed_refresh(self):
        """Trigger a delayed refresh for extra responsiveness."""
        await asyncio.sleep(3)  # Wait 3 seconds then refresh again
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs) -> None:
        """Set the vacuum cleaner to return to the dock - delegate to coordinator."""
        if self._status != "connected":
            return

        # Delegate to coordinator for business logic
        await self.coordinator.async_return_to_base()  # type: ignore
        await self.coordinator.async_request_refresh()

    async def async_remote_forward(self, **kwargs) -> None:
        """Send forward command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_forward()  # type: ignore

    async def async_remote_backward(self, **kwargs) -> None:
        """Send backward command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_backward()  # type: ignore

    async def async_remote_rotate_left(self, **kwargs) -> None:
        """Send rotate left command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_rotate_left()  # type: ignore

    async def async_remote_rotate_right(self, **kwargs) -> None:
        """Send rotate right command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_rotate_right()  # type: ignore

    async def async_remote_stop(self, **kwargs) -> None:
        """Send stop command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_stop()  # type: ignore

    async def async_add_fifteen_minutes(self, **kwargs) -> None:
        """Add 15 minutes to cleaning time - delegate to coordinator."""
        await self.coordinator.async_add_fifteen_minutes()  # type: ignore

    async def async_reduce_fifteen_minutes(self, **kwargs) -> None:
        """Reduce 15 minutes from cleaning time - delegate to coordinator."""
        await self.coordinator.async_reduce_fifteen_minutes()  # type: ignore
