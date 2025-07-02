import logging
import datetime
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
)

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    NAME,
    VERSION,
    DOMAIN,
    ISSUEURL,
    STARTUP,
    SCAN_INTERVAL
)

# Define vacuum activity states as constants since VacuumActivity may not be available
VACUUM_ACTIVITY_IDLE = "idle"
VACUUM_ACTIVITY_CLEANING = "cleaning"
VACUUM_ACTIVITY_RETURNING = "returning"
VACUUM_ACTIVITY_ERROR = "error"
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

class IAquaLinkRobotVacuum(CoordinatorEntity, StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    def __init__(self, coordinator, client, device_name, serial_number):
        """Initialize the vacuum."""
        super().__init__(coordinator)
        self._name = device_name
        self._serial_number = serial_number
        self._attributes = {}
        self._activity = VACUUM_ACTIVITY_IDLE
        self._supported_features = ROBOT_FEATURES["default"]
        self._client = client
        self._fan_speed_list = ["Floor only", "Floor and walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._status = None

    def _handle_coordinator_update(self):
        """Handle updated data from the coordinator."""
        super()._handle_coordinator_update()
        
        # Update all attributes from coordinator data - coordinator handles all business logic
        data = self.coordinator.data
        if data:
            # Get status and activity directly from coordinator
            self._status = data.get("status")
            self._attributes = data.copy()  # Use coordinator data as source of truth
            
            # Map activity from coordinator to vacuum activity constants
            activity = data.get("activity", "idle")
            if activity == "cleaning":
                self._activity = VACUUM_ACTIVITY_CLEANING
            elif activity == "returning":
                self._activity = VACUUM_ACTIVITY_RETURNING
            elif activity == "error":
                self._activity = VACUUM_ACTIVITY_ERROR
            else:
                self._activity = VACUUM_ACTIVITY_IDLE
                
            # Set device type specific features and fan speed list
            device_type = data.get("device_type")
            if device_type:
                self._supported_features = ROBOT_FEATURES.get(device_type, ROBOT_FEATURES["default"])
                
                # Set fan speed list based on device type
                if device_type == "vr" or device_type == "vortrax" or device_type == "cyclobat":
                    self._fan_speed_list = ["Wall only", "Floor only", "SMART Floor and walls", "Floor and walls"]
                elif device_type == "cyclonext":
                    self._fan_speed_list = ["Floor only", "Floor and walls"]
                elif device_type == "i2d_robot":
                    self._fan_speed_list = ["Floor only", "Walls only", "Floor and walls"]
        
        self.async_write_ha_state()

    @property
    def unique_id(self):
        """Return a unique ID for this entity."""
        return f"{DOMAIN}_{self._serial_number}"

    @property
    def device_info(self):
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
    def activity(self) -> str:
        """Return the current activity of the vacuum."""
        return self._activity

    @property
    def fan_speed(self):
        """Return the current fan speed."""
        return self._fan_speed

    @property
    def fan_speed_list(self):
        """Return the list of available fan speeds."""
        return self._fan_speed_list

    @property
    def name(self):
        """Return the name of the vacuum."""
        return self._name

    @property
    def should_poll(self):
        """Return True if the vacuum should be polled for state updates."""
        return False  # Coordinator handles polling

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return self._attributes

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attributes

    @property
    def status(self):
        """Return the status of the vacuum."""
        return self._status

    @property
    def supported_features(self):
        """Return the supported features of the vacuum."""
        return self._supported_features

    async def async_start(self):
        """Start the vacuum - delegate to coordinator."""
        if self._status != "connected":
            return
        
        # Delegate to coordinator for business logic
        await self.coordinator.async_start_cleaning()
        await self.coordinator.async_request_refresh()
            
    async def async_stop(self, **kwargs):
        """Stop the vacuum - delegate to coordinator."""
        if self._status != "connected":
            return
        
        # Delegate to coordinator for business logic
        await self.coordinator.async_stop_cleaning()
        await self.coordinator.async_refresh()

    async def async_set_fan_speed(self, fan_speed, **kwargs):
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in self._fan_speed_list:
            raise ValueError('Invalid fan speed')
            
        self._fan_speed = fan_speed
        
        await self._client.set_fan_speed(fan_speed, self._fan_speed_list)
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs):
        """Set the vacuum cleaner to return to the dock - delegate to coordinator."""
        if self._status != "connected":
            return
        
        # Delegate to coordinator for business logic
        await self.coordinator.async_return_to_base()
        await self.coordinator.async_request_refresh()

    async def async_remote_forward(self, **kwargs):
        """Send forward command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_forward()

    async def async_remote_backward(self, **kwargs):
        """Send backward command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_backward()

    async def async_remote_rotate_left(self, **kwargs):
        """Send rotate left command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_rotate_left()

    async def async_remote_rotate_right(self, **kwargs):
        """Send rotate right command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_rotate_right()

    async def async_remote_stop(self, **kwargs):
        """Send stop command to the robot for remote control - delegate to coordinator."""
        await self.coordinator.async_remote_stop()
