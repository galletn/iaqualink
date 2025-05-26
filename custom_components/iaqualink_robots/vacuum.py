import logging
import datetime
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity
)

from homeassistant.helpers.update_coordinator import CoordinatorEntity

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
)

from .const import (
    NAME,
    VERSION,
    DOMAIN,
    ISSUEURL,
    STARTUP,
    SCAN_INTERVAL
)

# Define the supported features for different robot types
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
    
    async_add_entities([IAquaLinkRobotVacuum(coordinator, client, device_name, serial_number)], True)

class IAquaLinkRobotVacuum(CoordinatorEntity, StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    def __init__(self, coordinator, client, device_name, serial_number):
        """Initialize the vacuum."""
        super().__init__(coordinator)
        self._name = device_name
        self._serial_number = serial_number
        self._attributes = {}
        self._activity = VacuumActivity.IDLE
        self._battery_level = None
        self._supported_features = ROBOT_FEATURES["default"]
        self._client = client
        self._fan_speed_list = ["Floor only", "Floor and walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._status = None
        self._start_time = None
        self._cycle_duration = 120  # Default cycle duration in minutes
        self._time_remaining = None
        self._estimated_end_time = None

    def _handle_coordinator_update(self):
        """Handle updated data from the coordinator and set activity."""
        super()._handle_coordinator_update()
        
        # Update attributes from coordinator data
        data = self.coordinator.data
        if data:
            # Set basic attributes
            self._status = data.get("status")
            self._attributes.update(data)
            
            # Set activity based on data
            activity = data.get("activity")
            if activity == "cleaning":
                self._activity = VacuumActivity.CLEANING
                
                # If we have a cycle duration from the data, use it
                if "cycle_duration" in data:
                    self._cycle_duration = data.get("cycle_duration")
                
                # If we're cleaning and have a start time, update time remaining
                if self._start_time and self._estimated_end_time:
                    self._calculate_time_remaining()
                    self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
                # If we're cleaning but don't have a start time (e.g., after HA restart),
                # set it now based on the cycle_start_time from data if available
                elif "cycle_start_time" in data:
                    try:
                        self._start_time = datetime.datetime.fromisoformat(data["cycle_start_time"])
                        self._estimated_end_time = self._start_time + timedelta(minutes=self._cycle_duration)
                        self._calculate_time_remaining()
                        self._attributes["start_time"] = self._start_time.isoformat()
                        self._attributes["estimated_end_time"] = self._estimated_end_time.isoformat()
                        self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
                    except (ValueError, TypeError):
                        # If we can't parse the cycle_start_time, just use current time
                        self._start_time = datetime.datetime.now()
                        self._estimated_end_time = self._start_time + timedelta(minutes=self._cycle_duration)
                        self._calculate_time_remaining()
                        self._attributes["start_time"] = self._start_time.isoformat()
                        self._attributes["estimated_end_time"] = self._estimated_end_time.isoformat()
                        self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
            elif activity == "returning":
                self._activity = VacuumActivity.RETURNING
            elif activity == "error":
                self._activity = VacuumActivity.ERROR
            else:
                self._activity = VacuumActivity.IDLE
                # Reset time remaining if we're idle and it was previously set
                if self._time_remaining is not None:
                    self._time_remaining = timedelta(seconds=0)
                    self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
                
            # Set device type specific attributes
            device_type = data.get("device_type")
            if device_type:
                self._supported_features = ROBOT_FEATURES.get(device_type, ROBOT_FEATURES["default"])
                
                # Set fan speed list based on device type
                if device_type == "vr" or device_type == "cyclobat":
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
    def activity(self) -> VacuumActivity:
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

    def _format_time_remaining(self, time_delta):
        """Format time remaining as a human-readable string."""
        if not time_delta:
            return "0:00"
        
        total_seconds = time_delta.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        seconds = int(total_seconds % 60)
        
        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes}:{seconds:02d}"
    
    def _calculate_time_remaining(self):
        """Calculate time remaining based on start time and cycle duration."""
        if not self._start_time or not self._estimated_end_time:
            self._time_remaining = None
            return
        
        now = datetime.datetime.now()
        if now >= self._estimated_end_time:
            self._time_remaining = timedelta(seconds=0)
        else:
            self._time_remaining = self._estimated_end_time - now
    
    async def async_start(self):
        """Start the vacuum."""
        if self._status != "connected":
            return

        self._activity = VacuumActivity.CLEANING
        
        # Set start time and calculate estimated end time
        self._start_time = datetime.datetime.now()
        self._estimated_end_time = self._start_time + timedelta(minutes=self._cycle_duration)
        
        # Calculate time remaining
        self._calculate_time_remaining()
        
        # Update attributes
        self._attributes["estimated_end_time"] = self._estimated_end_time.isoformat()
        self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
        
        self.async_write_ha_state()

        await self._client.start_cleaning()
        await self.coordinator.async_request_refresh()
            
    async def async_stop(self, **kwargs):
        """Stop the vacuum."""
        if self._status != "connected":
            return

        self._activity = VacuumActivity.IDLE
        
        # Reset time remaining to zero and set end time to current time
        self._time_remaining = timedelta(seconds=0)
        current_time = datetime.datetime.now()
        self._estimated_end_time = current_time
        
        # Update attributes
        self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
        self._attributes["estimated_end_time"] = self._estimated_end_time.isoformat()
        
        self.async_write_ha_state()

        # Stop the cleaner and get reset values
        await self._client.stop_cleaning()
        # Force an immediate coordinator update to reflect the stopped state
        await self.coordinator.async_refresh()

    async def async_set_fan_speed(self, fan_speed):
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in self._fan_speed_list:
            raise ValueError('Invalid fan speed')
            
        self._fan_speed = fan_speed
        
        await self._client.set_fan_speed(fan_speed, self._fan_speed_list)
        await self.coordinator.async_request_refresh()

    async def async_return_to_base(self, **kwargs):
        """Set the vacuum cleaner to return to the dock."""
        if self._status != "connected":
            return
            
        self._activity = VacuumActivity.RETURNING
        
        # Reset time remaining to zero and set end time to current time
        self._time_remaining = timedelta(seconds=0)
        current_time = datetime.datetime.now()
        self._estimated_end_time = current_time
        
        # Update attributes
        self._attributes["time_remaining"] = self._format_time_remaining(self._time_remaining)
        self._attributes["estimated_end_time"] = self._estimated_end_time.isoformat()
        
        self.async_write_ha_state()

        await self._client.return_to_base()
        await self.coordinator.async_request_refresh()
