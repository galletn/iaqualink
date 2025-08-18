import logging
import datetime
import asyncio
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.vacuum import ( 
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,  # Note: This requires Home Assistant 2025.1 or later
)

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import entity_registry as er

from .coordinator import AqualinkDataUpdateCoordinator

from .const import (
    NAME,
    VERSION,
    DOMAIN,
    ISSUEURL,
    STARTUP,
    SCAN_INTERVAL
)
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

# Fan speed translation mapping
from homeassistant.helpers import translation

# Fan speed translation mapping (translation keys to internal keys)
FAN_SPEED_KEY_MAP = {
    "floor_only": "floor_only",
    "wall_only": "wall_only", 
    "walls_only": "walls_only",
    "floor_and_walls": "floor_and_walls",
    "smart_floor_and_walls": "smart_floor_and_walls"
}

# Remove manual translation function - Home Assistant handles this automatically

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the vacuum platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]
    
    # Use the device serial number as part of the unique ID
    serial_number = entry.data.get("serial_number", client.robot_id)
    device_name = entry.data.get("name", entry.title)
    
    vacuum = IAquaLinkRobotVacuum(coordinator, client, device_name, serial_number, hass)
    async_add_entities([vacuum], True)

class IAquaLinkRobotVacuum(CoordinatorEntity[AqualinkDataUpdateCoordinator], StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    def __init__(self, coordinator: AqualinkDataUpdateCoordinator, client, device_name: str, serial_number: str, hass):
        """Initialize the vacuum."""
        super().__init__(coordinator)
        self._name = device_name
        self._serial_number = serial_number
        self._attributes: Dict[str, Any] = {}
        self._activity = VacuumActivity.IDLE
        self._supported_features = ROBOT_FEATURES["default"]
        self._client = client
        self._hass = hass
        self._fan_speed_list: List[str] = ["floor_only", "floor_and_walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._status: Optional[str] = None
        
        # Set translation key for fan speed options
        self._attr_translation_key = "fan_speed"

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
                    _LOGGER.debug(f"Vacuum status updated to '{coordinator_status}' from coordinator's resilient status logic")
                # But keep current activity, fan speed, and attributes
                self.async_write_ha_state()
                return
                
            # Log when fan speed changes for debugging
            current_fan_speed = data.get("fan_speed")
            if current_fan_speed != self._attributes.get("fan_speed"):
                _LOGGER.info(f"Vacuum fan speed updated: {self._attributes.get('fan_speed')} -> {current_fan_speed}")
            
            # Get status and activity directly from coordinator
            self._status = data.get("status")
            # Only update attributes if data has changed to avoid unnecessary copying
            if self._attributes != data:
                self._attributes = data  # Reference instead of copy for performance
            
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
                
                # Set fan speed list based on device type
                if device_type == "vr":
                    self._fan_speed_list = ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"]
                elif device_type == "vortrax":  
                    self._fan_speed_list = ["floor_only", "floor_and_walls"]
                else:
                    self._fan_speed_list = ["floor_only", "walls_only", "floor_and_walls"]
            
            # Update current fan speed from coordinator data (store as raw key) - only if not unknown
            coordinator_fan_speed = data.get("fan_speed")
            if coordinator_fan_speed and coordinator_fan_speed != "unknown" and coordinator_fan_speed != self._fan_speed:
                _LOGGER.debug(f"Updating vacuum fan speed from coordinator: {self._fan_speed} -> {coordinator_fan_speed}")
                # Always store the raw key - the fan_speed property will convert to display name
                self._fan_speed = coordinator_fan_speed
                _LOGGER.debug(f"Vacuum _fan_speed now set to: '{self._fan_speed}' (raw key)")
            elif coordinator_fan_speed and coordinator_fan_speed != "unknown":
                _LOGGER.debug(f"Vacuum fan speed unchanged: '{self._fan_speed}' (coordinator also has '{coordinator_fan_speed}')")
            else:
                _LOGGER.debug(f"Coordinator has no valid fan_speed data ({coordinator_fan_speed}), vacuum keeps: '{self._fan_speed}'")
        
        self.async_write_ha_state()
        
        # Debug logging after state update
        if hasattr(self, '_fan_speed') and self._fan_speed:
            # Test the fan_speed property after the update
            current_display = self.fan_speed
            available_speeds = self.fan_speed_list
            _LOGGER.debug(f"After coordinator update - Display fan speed: '{current_display}', Available: {available_speeds}")
            if current_display not in available_speeds:
                _LOGGER.warning(f"Fan speed mismatch! Current '{current_display}' not in available speeds {available_speeds}")
            else:
                _LOGGER.debug(f"Fan speed consistency OK: '{current_display}' is in available speeds")

    @property
    def unique_id(self) -> str:
        """Return a unique ID for this entity."""
        return f"{DOMAIN}_{self._serial_number}"

    @property
    def device_info(self) -> Dict[str, Any]:
        """Return device information about this entity."""
        data = self.coordinator.data or {}
        return {
            "identifiers": {(DOMAIN, self._serial_number)},
            "name": self._name,
            "manufacturer": "iaqualink",
            "model": data.get("model", "Unknown"),
            "sw_version": data.get("sw_version", VERSION),
        }

    @property
    def activity(self) -> VacuumActivity:
        """Return the current activity of the vacuum."""
        return self._activity

    async def _get_translated_fan_speed(self, fan_speed_key: str) -> str:
        """Get translated fan speed name."""
        try:
            # Get translations for the component
            translations = await translation.async_get_translations(
                self._hass, self._hass.config.language, "entity", {DOMAIN}
            )
            
            # Look up the specific fan speed translation
            translation_key = f"component.{DOMAIN}.entity.vacuum.fan_speed.{fan_speed_key}"
            translated = translations.get(translation_key)
            
            if translated:
                return translated
                
        except Exception as e:
            _LOGGER.debug(f"Error getting translation for {fan_speed_key}: {e}")
            
        # Fallback to English translations
        english_fallback = {
            "floor_only": "Floor only",
            "wall_only": "Wall only", 
            "walls_only": "Walls only",
            "floor_and_walls": "Floor and walls",
            "smart_floor_and_walls": "SMART Floor and walls"
        }
        return english_fallback.get(fan_speed_key, fan_speed_key)

    async def _get_translated_fan_speed_list(self) -> List[str]:
        """Get translated fan speed list."""
        translated_speeds = []
        for speed_key in self._fan_speed_list:
            translated = await self._get_translated_fan_speed(speed_key)
            translated_speeds.append(translated)
        return translated_speeds

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
    def name(self) -> str:
        """Return the name of the vacuum."""
        return self._name

    @property
    def should_poll(self) -> bool:
        """Return True if the vacuum should be polled for state updates."""
        return False  # Coordinator handles polling

    @property
    def available(self) -> bool:
        """Keep vacuum available as long as we have data, even during temporary connection issues."""
        # Vacuum should remain available as long as we have coordinator data
        # This prevents the vacuum from going unavailable during temporary connection issues
        return self.coordinator.data is not None

    @property
    def device_state_attributes(self) -> Dict[str, Any]:
        """Return device specific state attributes."""
        # Filter out fan_speed from attributes since it's handled by the fan_speed property
        # This prevents conflicts between raw coordinator data and display names
        filtered_attributes = {k: v for k, v in self._attributes.items() if k != "fan_speed"}
        return filtered_attributes

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return entity specific state attributes."""
        # Filter out fan_speed from attributes since it's handled by the fan_speed property
        # This prevents conflicts between raw coordinator data and display names
        filtered_attributes = {k: v for k, v in self._attributes.items() if k != "fan_speed"}
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
                        _LOGGER.info(f"Websocket confirmed different fan speed than requested: {confirmed_speed} vs {internal_key}")
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
            
            # For extra responsiveness, trigger another refresh after a short delay
            asyncio.create_task(self._delayed_refresh())
            
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
