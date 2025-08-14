import logging
import datetime
from datetime import timedelta
from typing import Any, Dict, List, Mapping, Optional

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.vacuum import ( 
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity,  # Note: This requires Home Assistant 2025.1 or later
)

from homeassistant.helpers.update_coordinator import CoordinatorEntity

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

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up the vacuum platform."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]
    
    # Use the device serial number as part of the unique ID
    serial_number = entry.data.get("serial_number", client.robot_id)
    device_name = entry.data.get("name", entry.title)
    
    vacuum = IAquaLinkRobotVacuum(coordinator, client, device_name, serial_number)
    async_add_entities([vacuum], True)

class IAquaLinkRobotVacuum(CoordinatorEntity[AqualinkDataUpdateCoordinator], StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    def __init__(self, coordinator: AqualinkDataUpdateCoordinator, client, device_name: str, serial_number: str):
        """Initialize the vacuum."""
        super().__init__(coordinator)
        self._name = device_name
        self._serial_number = serial_number
        self._attributes: Dict[str, Any] = {}
        self._activity = VacuumActivity.IDLE
        self._supported_features = ROBOT_FEATURES["default"]
        self._client = client
        self._fan_speed_list: List[str] = ["Floor only", "Floor and walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._status: Optional[str] = None

    def _handle_coordinator_update(self):
        """Handle updated data from the coordinator."""
        super()._handle_coordinator_update()
        
        # Update all attributes from coordinator data - coordinator handles all business logic
        data = self.coordinator.data
        if data:
            # Get status and activity directly from coordinator
            self._status = data.get("status")
            # Only update attributes if data has changed to avoid unnecessary copying
            if self._attributes != data:
                self._attributes = data  # Reference instead of copy for performance
            
            # Map activity from coordinator to VacuumActivity enum
            activity = data.get("activity", "idle")
            if activity == "cleaning":
                self._activity = VacuumActivity.CLEANING
            elif activity == "returning":
                self._activity = VacuumActivity.RETURNING
            elif activity == "error":
                self._activity = VacuumActivity.ERROR
            else:
                self._activity = VacuumActivity.IDLE
                
            # Set device type specific features and fan speed list
            device_type = data.get("device_type")
            if device_type:
                self._supported_features = ROBOT_FEATURES.get(device_type, ROBOT_FEATURES["default"])
                
                # Set fan speed list based on device type
                if device_type in ("vr", "vortrax", "cyclobat"):
                    self._fan_speed_list = ["Wall only", "Floor only", "SMART Floor and walls", "Floor and walls"]
                elif device_type == "cyclonext":
                    self._fan_speed_list = ["Floor only", "Floor and walls"]
                elif device_type == "i2d_robot":
                    self._fan_speed_list = ["Floor only", "Walls only", "Floor and walls"]
        
        self.async_write_ha_state()

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

    @property
    def fan_speed(self) -> Optional[str]:
        """Return the current fan speed."""
        return self._fan_speed

    @property
    def fan_speed_list(self) -> List[str]:
        """Return the list of available fan speeds."""
        return self._fan_speed_list

    @property
    def name(self) -> str:
        """Return the name of the vacuum."""
        return self._name

    @property
    def should_poll(self) -> bool:
        """Return True if the vacuum should be polled for state updates."""
        return False  # Coordinator handles polling

    @property
    def device_state_attributes(self) -> Dict[str, Any]:
        """Return device specific state attributes."""
        return self._attributes

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return entity specific state attributes."""
        return self._attributes

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
        if fan_speed not in self._fan_speed_list:
            raise ValueError('Invalid fan speed')
            
        self._fan_speed = fan_speed
        
        await self._client.set_fan_speed(fan_speed, self._fan_speed_list)
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
