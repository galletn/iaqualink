import asyncio
import json
import datetime
import aiohttp
import logging
from datetime import timedelta

_LOGGER = logging.getLogger(__name__)

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumEntityFeature,
    VacuumActivity
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
)

from .const import (
    URL_LOGIN,
    URL_GET_DEVICES,
    URL_GET_DEVICE_STATUS,
    URL_GET_DEVICE_FEATURES,
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
    vacuum = IAquaLinkRobotVacuum(entry)
    async_add_entities([vacuum], update_before_add=True)

class IAquaLinkRobotVacuum(StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    def __init__(self, entry):
        """Initialize the vacuum."""
        self._name = entry.data["name"]
        self._attributes = {}
        self._activity = VacuumActivity.IDLE
        self._battery_level = None
        self._supported_features = ROBOT_FEATURES["default"]
        self._username = entry.data["username"]
        self._password = entry.data["password"]
        self._api_key = entry.data["api_key"]
        self._first_name = None
        self._last_name = None
        self._id = None
        self._headers = {
            "Content-Type": "application/json; charset=utf-8", 
            "Connection": "keep-alive", 
            "Accept": "*/*"
        }
        self._temperature = None
        self._authentication_token = None
        self._canister = None
        self._error_state = None
        self._total_hours = None
        self._cycle_start_time = None
        self._app_client_id = None
        self._cycle_duration = None
        self._cycle_end_time = None
        self._time_remaining = None
        self._serial_number = None
        self._model = None
        self._fan_speed_list = ["Floor only", "Floor and walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._debug = None
        self._debug_mode = True
        self._device_type = None
        self._status = None

    @property
    def unique_id(self):
        return self._serial_number or self._name

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._serial_number)},
            "name": self._name,
            "manufacturer": "iaqualink",
            "model": self._model or "Unknown",
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
    def temperature(self):
        """Return the current temperature."""
        return self._temperature

    @property
    def name(self):
        """Return the name of the vacuum."""
        return self._name

    @property
    def username(self):
        """Return the username."""
        return self._username

    @property
    def password(self):
        """Return the password."""
        return self._password

    @property
    def api_key(self):
        """Return the API key."""
        return self._api_key

    @property
    def first_name(self):
        """Return the first name."""
        return self._first_name

    @property
    def last_name(self):
        """Return the last name."""
        return self._last_name

    @property
    def model(self):
        """Return the model."""
        return self._model

    @property
    def serial_number(self):
        """Return the serial number."""
        return self._serial_number

    @property
    def id(self):
        """Return the ID."""
        return self._id

    @property
    def id_token(self):
        """Return the ID token."""
        return self._id_token
        
    @property
    def error_state(self):
        """Return the error state."""
        return self._error_state

    @property
    def total_hours(self):
        """Return the total hours."""
        return self._total_hours 

    @property
    def cycle_start_time(self):
        """Return the cycle start time."""
        return self._cycle_start_time

    @property
    def should_poll(self):
        """Return True if the vacuum should be polled for state updates."""
        return True

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
        """Start the vacuum."""
        if self._status != "connected":
            return

        self._activity = VacuumActivity.CLEANING
        self.async_write_ha_state()

        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1240&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial_number}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        else:
            clientToken = f"{self._id}|{self._authentication_token}|{self._app_client_id}"
            
            request = None
            if self._device_type == "vr":
                request = {
                    "action": "setCleanerState",
                    "namespace": "vr",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot": {"state": 1}}}}
                    },
                    "service": "StateController",
                    "target": self._serial_number,
                    "version": 1
                }
            elif self._device_type == "cyclobat":
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"ctrl": 1}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial_number
                }
            elif self._device_type == "cyclonext":
                request = {
                    "action": "setCleanerState",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"mode": 1}}}}
                    },
                    "service": "StateController",
                    "target": self._serial_number,
                    "version": 1
                }
            
            if request:
                await asyncio.wait_for(self.setCleanerState(request), timeout=30)
            
    async def async_stop(self, **kwargs):
        """Stop the vacuum."""
        if self._status != "connected":
            return

        self._activity = VacuumActivity.IDLE
        self.async_write_ha_state()

        self._time_remaining = 0
        self._attributes["time_remaining"] = 0

        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1210&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial_number}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        else:
            clientToken = f"{self._id}|{self._authentication_token}|{self._app_client_id}"
            
            request = None
            if self._device_type == "vr":
                request = {
                    "action": "setCleanerState",
                    "namespace": "vr",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot": {"state": 0}}}}
                    },
                    "service": "StateController",
                    "target": self._serial_number,
                    "version": 1
                }
            elif self._device_type == "cyclobat":
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"ctrl": 0}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial_number
                }
            elif self._device_type == "cyclonext":
                request = {
                    "action": "setCleanerState",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"mode": 0}}}}
                    },
                    "service": "StateController",
                    "target": self._serial_number,
                    "version": 1
                }
            
            if request:
                await asyncio.wait_for(self.setCleanerState(request), timeout=30)

    async def async_update(self):
        """Get the latest state of the vacuum."""
        await self._authenticate()
        
        # Only get serial number if it's initial to avoid too many calls and load
        if self._serial_number is None:
            await self._initialize_device()

        # Update device status based on device type
        if self._device_type == "i2d_robot":
            await self._update_i2d_robot()
        else:
            await self._update_other_robots()
            
        # Get model only first time to avoid load
        if self._model is None:
            await self._get_device_model()

    async def _authenticate(self):
        """Authenticate with the iAqualink API."""
        data = {
            "apikey": self._api_key,
            "email": self._username,
            "password": self._password
        }
        data = json.dumps(data)
        data = await asyncio.wait_for(self.send_login(data, self._headers), timeout=30)
        
        self._first_name = data["first_name"]
        self._last_name = data["last_name"]
        self._id = data["id"]
        self._authentication_token = data["authentication_token"]
        self._id_token = data["userPoolOAuth"]["IdToken"]
        self._app_client_id = data["cognitoPool"]["appClientId"]

        self._attributes['username'] = self._username
        self._attributes['first_name'] = self._first_name
        self._attributes['last_name'] = self._last_name
        self._attributes['id'] = self._id

    async def _initialize_device(self):
        """Initialize device information."""
        params = {
            "authentication_token": self._authentication_token,
            "user_id": self._id,
            "api_key": self._api_key
        }
        data = await asyncio.wait_for(self.get_devices(params, self._headers), timeout=30)

        # Filter only devices that are compatible with the module
        supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr"]

        index = None
        for i, device in enumerate(data):
            device_type = device.get("device_type")
            _LOGGER.debug("🔍 Devices found : %s", device)
            if device_type in supported_device_types:
                index = i
                _LOGGER.debug("✅ Device selected : %s (index=%d)", device_type, i)
                break
            else:
                _LOGGER.debug("⏩ Device ignored (unsupported type) : %s", device_type)

        if index is None:
            _LOGGER.error("❌ No compatible robot found in the device list.")
            self._status = "offline"
            self._attributes['status'] = self._status
            return

        # Set device information
        self._serial_number = data[index]["serial_number"]
        self._attributes['serial_number'] = self._serial_number

        self._device_type = data[index]["device_type"]
        self._attributes['device_type'] = self._device_type

        # Set features based on device type
        if self._device_type in ROBOT_FEATURES:
            self._supported_features = ROBOT_FEATURES[self._device_type]
        
        # Set fan speed list based on device type
        if self._device_type == "vr" or self._device_type == "cyclobat":
            self._fan_speed_list = ["Wall only", "Floor only", "SMART Floor and walls", "Floor and walls"]
        elif self._device_type == "cyclonext":
            self._fan_speed_list = ["Floor only", "Floor and walls"]
        elif self._device_type == "i2d_robot":
            self._fan_speed_list = ["Floor only", "Walls only", "Floor and walls"]

    async def _update_i2d_robot(self):
        """Update status for i2d_robot type."""
        request = {
            "command": "/command",
            "params": "request=OA11",
            "user_id": self._id
        }
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial_number}/control.json"
        data = await asyncio.wait_for(self.post_command_i2d(url, request), timeout=30)

        try:
            if data["command"]["request"] == "OA11":
                self._status = "connected"
                self._attributes['status'] = self._status
                self._debug = data["command"]["response"]

                response = self._debug

                if isinstance(response, str) and len(response) >= 12:
                    error_code = response[6:8].upper()

                    error_map = {
                        "00": "no_error",
                        "01": "pump_short_circuit",
                        "02": "right_drive_motor_short_circuit",
                        "03": "left_drive_motor_short_circuit",
                        "04": "pump_motor_overconsumption",
                        "05": "right_drive_motor_overconsumption",
                        "06": "left_drive_motor_overconsumption",
                        "07": "floats_on_surface",
                        "08": "running_out_of_water",
                        "0A": "communication_error"
                    }

                    error_text = error_map.get(error_code, f"unknown_{error_code}")

                    self._attributes['error_code'] = error_code
                    self._attributes['error_text'] = error_text

                    if error_code != "00":
                        self._activity = VacuumActivity.ERROR
                else:
                    self._attributes['error_code'] = "??"
                    self._attributes['error_text'] = "unreadable"

                if self._debug_mode:
                    self._attributes['debug'] = self._debug
        except Exception:
            try:
                if data["status"] == "500":
                    self._status = "offline"
                    self._attributes['status'] = self._status
            except Exception:
                self._debug = data
                if self._debug_mode:
                    self._attributes['debug'] = self._debug

        if self._activity == VacuumActivity.CLEANING:
            try:
                response = data["command"]["response"]
                if isinstance(response, str) and len(response) >= 12:
                    minutes_remaining = int(response[10:12], 16)
                    self._estimated_end_time = self.add_minutes_to_datetime(
                        datetime.datetime.now(), minutes_remaining)
                    self._attributes['estimated_end_time'] = self._estimated_end_time
                    self._attributes['time_remaining_human'] = f"{minutes_remaining // 60}:{minutes_remaining % 60:02d}"
                else:
                    self._attributes['estimated_end_time'] = None
                    self._attributes['time_remaining_human'] = None
            except Exception as e:
                _LOGGER.warning(f"Error while parsing remaining minutes: {e}")
                self._attributes['estimated_end_time'] = None
                self._attributes['time_remaining_human'] = None

    async def _update_other_robots(self):
        """Update status for non-i2d robot types."""
        request = {
            "action": "subscribe",
            "namespace": "authorization",
            "payload": {"userId": self.id},
            "service": "Authorization",
            "target": self._serial_number,
            "version": 1
        }

        data = await asyncio.wait_for(self.get_device_status(request), timeout=30)

        try:
            self._status = data['payload']['robot']['state']['reported']['aws']['status']
            self._attributes['status'] = self._status
        except Exception:
            # Returns empty message sometimes, try second call
            self._debug = data
            if self._debug_mode:
                self._attributes['debug'] = self._debug
            
            data = await asyncio.wait_for(self.get_device_status(request), timeout=30)
            self._status = data['payload']['robot']['state']['reported']['aws']['status']
            self._attributes['status'] = self._status

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['aws']['timestamp'] / 1000
        self._last_online = datetime.datetime.fromtimestamp(timestamp)
        self._attributes['last_online'] = self._last_online

        # Update based on device type
        if self._device_type == "vr":
            await self._update_vr_robot(data)
        elif self._device_type == "cyclobat":
            await self._update_cyclobat_robot(data)
        elif self._device_type == "cyclonext":
            await self._update_cyclonext_robot(data)

    async def _update_vr_robot(self, data):
        """Update status for VR type robot."""
        try:
            self._temperature = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['val']
        except Exception:
            try: 
                self._temperature = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['state']
            except Exception:
                # Zodiac XA 5095 iQ does not support temp
                self._temperature = '0'
                
        self._attributes['temperature'] = self._temperature

        robot_state = data['payload']['robot']['state']['reported']['equipment']['robot']['state']
        if robot_state == 1:
            self._activity = VacuumActivity.CLEANING
        elif robot_state == 3:
            self._activity = VacuumActivity.RETURNING
        else:
            self._activity = VacuumActivity.IDLE

        # Extract other attributes
        self._canister = data['payload']['robot']['state']['reported']['equipment']['robot']['canister']
        self._attributes['canister'] = self._canister

        self._error_state = data['payload']['robot']['state']['reported']['equipment']['robot']['errorState']
        self._attributes['error_state'] = self._error_state

        self._total_hours = data['payload']['robot']['state']['reported']['equipment']['robot']['totalHours']
        self._attributes['total_hours'] = self._total_hours

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['equipment']['robot']['cycleStartTime']
        self._cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
        self._attributes['cycle_start_time'] = self._cycle_start_time

        self._cycle = data['payload']['robot']['state']['reported']['equipment']['robot']['prCyc']
        self._attributes['cycle'] = self._cycle

        cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot']['durations']
        self._cycle_duration = list(cycle_duration_values.values())[self._cycle]
        self._attributes['cycle_duration'] = self._cycle_duration

        # Calculate end time and remaining time
        await self._calculate_times(self._cycle_start_time, self._cycle_duration)

    async def _update_cyclobat_robot(self, data):
        """Update status for cyclobat type robot."""
        robot_state = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["main"]["state"]
        if robot_state == 1:
            self._activity = VacuumActivity.CLEANING
        elif robot_state == 3:
            self._activity = VacuumActivity.RETURNING
        else:
            self._activity = VacuumActivity.IDLE

        # Extract attributes
        self._total_hours = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["stats"]["totRunTime"]
        self._attributes['total_hours'] = self._total_hours

        self._battery_state = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["battery"]["state"]
        self._attributes['battery_state'] = self._battery_state

        self._user_charge_percentage = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["battery"]["userChargePerc"]
        self._attributes['user_charge_percentage'] = self._user_charge_percentage

        self._battery_cycles = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["battery"]["cycles"]
        self._attributes['battery_cycles'] = self._battery_cycles

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['equipment']['robot']['main']['cycleStartTime']
        self._cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
        self._attributes['cycle_start_time'] = self._cycle_start_time

        self._cycle = data['payload']['robot']['state']['reported']['equipment']['robot']['lastCycle']['endCycleType']
        self._attributes['cycle'] = self._cycle

        cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot']['cycles']
        self._cycle_duration = list(cycle_duration_values.values())[self._cycle]
        self._attributes['cycle_duration'] = self._cycle_duration

        # Calculate end time and remaining time
        await self._calculate_times(self._cycle_start_time, self._cycle_duration)

    async def _update_cyclonext_robot(self, data):
        """Update status for cyclonext type robot."""
        if data['payload']['robot']['state']['reported']['equipment']['robot.1']['mode'] == 1:
            self._activity = VacuumActivity.CLEANING
        else:
            self._activity = VacuumActivity.IDLE

        # Extract attributes
        self._canister = data['payload']['robot']['state']['reported']['equipment']['robot.1']['canister']
        self._attributes['canister'] = self._canister

        self._error_state = data['payload']['robot']['state']['reported']['equipment']['robot.1']['errors']['code']
        self._attributes['error_state'] = self._error_state

        try:
            self._total_hours = data['payload']['robot']['state']['reported']['equipment']['robot.1']['totRunTime']
            self._attributes['total_hours'] = self._total_hours
        except Exception:
            # Not supported by some cyclonext models
            self._attributes['total_hours'] = 0

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['equipment']['robot.1']['cycleStartTime']
        self._cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
        self._attributes['cycle_start_time'] = self._cycle_start_time

        self._cycle = data['payload']['robot']['state']['reported']['equipment']['robot.1']['cycle']
        self._attributes['cycle'] = self._cycle

        cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot.1']['durations']
        self._cycle_duration = list(cycle_duration_values.values())[self._cycle]
        self._attributes['cycle_duration'] = self._cycle_duration

        # Calculate end time and remaining time
        await self._calculate_times(self._cycle_start_time, self._cycle_duration)

    async def _calculate_times(self, start_time, duration_minutes):
        """Calculate end time and remaining time."""
        try:
            self._cycle_end_time = self.add_minutes_to_datetime(start_time, duration_minutes)
            self._attributes['cycle_end_time'] = self._cycle_end_time
        except Exception:
            self._attributes['cycle_end_time'] = None

        if self._activity == VacuumActivity.CLEANING:
            try:
                self._time_remaining = self.subtract_dates(datetime.datetime.now(), self._cycle_end_time)
                self._attributes['time_remaining'] = self._time_remaining
            except Exception:
                self._attributes['time_remaining'] = None
        else:
            self._attributes['time_remaining'] = None

    async def _get_device_model(self):
        """Get device model information."""
        url = f"{URL_GET_DEVICE_FEATURES}{self._serial_number}/features"
        data = await asyncio.wait_for(self.get_device_features(url), timeout=30)

        try:
            self._model = data['model']
            self._attributes['model'] = self._model
        except Exception:
            # Some models do not return a model number on the features call
            self._model = 'Not Supported'
            self._attributes['model'] = self._model


    async def send_login(self, data, headers):
        """Post a login request to the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.post(URL_LOGIN, data=data) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def get_devices(self, params, headers):
        """Get device list from the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.get(URL_GET_DEVICES, params=params) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def get_device_features(self, url):
        """Get device features from the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.get(url) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def get_device_status(self, request):
        """Get device status from the iaqualink_robots API via websocket."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.ws_connect("wss://prod-socket.zodiac-io.com/devices") as websocket:
                    await websocket.send_json(request)
                    message = await websocket.receive()
                return message.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def post_command_i2d(self, url, request):
        """Send command to i2d robot via the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token, "api_key": self._api_key}) as session:
            try:
                async with session.post(url, json=request) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)
    
    async def setCleanerState(self, request):
        """Set cleaner state via the iaqualink_robots API websocket."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.ws_connect("wss://prod-socket.zodiac-io.com/devices") as websocket:
                    await websocket.send_json(request)
            finally:
                await asyncio.wait_for(session.close(), timeout=30)
                # Wait 2 seconds to avoid flooding iaqualink server
                await asyncio.sleep(2)
                await self.async_update()

    def add_minutes_to_datetime(self, dt, minutes):
        """Add minutes to a datetime object."""
        return dt + datetime.timedelta(minutes=minutes)

    def subtract_dates(self, date1, date2):
        """Subtract two dates and return a formatted time string."""
        try:
            time_diff = date2 - date1
        except Exception:
            return 0

        # If the result is negative, return 0
        if time_diff < timedelta():
            return 0
    
        # Calculate hours, minutes and seconds
        total_seconds = time_diff.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        remaining_seconds = int(total_seconds % 60)

        # Format the time string
        time_str = f"{hours} Hour(s) {minutes} Minute(s) {remaining_seconds} Second(s)"
        return time_str

    async def async_set_fan_speed(self, fan_speed):
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in self._fan_speed_list:
            raise ValueError('Invalid fan speed')
            
        self._fan_speed = fan_speed
        
        if self._device_type == "i2d_robot":
            await self._set_i2d_fan_speed(fan_speed)
        else:
            await self._set_other_fan_speed(fan_speed)
            
        if self._debug_mode:
            self._attributes['debug'] = self._debug
            
    async def _set_i2d_fan_speed(self, fan_speed):
        """Set fan speed for i2d robot."""
        cycle_speed_map = {
            "Walls only": "0A1284",
            "Floor only": "0A1280",
            "Floor and walls": "0A1283"
        }
        
        _cycle_speed = cycle_speed_map.get(fan_speed)
        if not _cycle_speed:
            return
            
        request = {
            "command": "/command",
            "params": f"request={_cycle_speed}&timeout=800",
            "user_id": self._id
        }
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial_number}/control.json"
        await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        
    async def _set_other_fan_speed(self, fan_speed):
        """Set fan speed for non-i2d robots."""
        clientToken = f"{self._id}|{self._authentication_token}|{self._app_client_id}"
        
        request = None
        if self._device_type == "vr":
            cycle_speed_map = {
                "Wall only": "0",
                "Floor only": "1",
                "SMART Floor and walls": "2",
                "Floor and walls": "3"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "vr",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"prCyc": _cycle_speed}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial_number
                }
                
        elif self._device_type == "cyclobat":
            cycle_speed_map = {
                "Wall only": "3",
                "Floor only": "0",
                "SMART Floor and walls": "2",
                "Floor and walls": "1"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"mode": _cycle_speed}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial_number
                }
                
        elif self._device_type == "cyclonext":
            cycle_speed_map = {
                "Floor only": "1",
                "Floor and walls": "3"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                request = {
                    "action": "setCleaningMode",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"cycle": _cycle_speed}}}}
                    },
                    "service": "StateController",
                    "target": self._serial_number,
                    "version": 1
                }
        
        if request:
            await asyncio.wait_for(self.setCleanerState(request), timeout=30)

    async def async_return_to_base(self, **kwargs):
        """Set the vacuum cleaner to return to the dock."""
        if self._status != "connected":
            return
            
        self._activity = VacuumActivity.RETURNING
        self.async_write_ha_state()

        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1701&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial_number}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        elif self._device_type in ["vr", "cyclobat"]:
            clientToken = f"{self._id}|{self._authentication_token}|{self._app_client_id}"
            request = {
                "action": "setCleanerState",
                "namespace": self._device_type,
                "payload": {
                    "clientToken": clientToken,
                    "state": {"desired": {"equipment": {"robot": {"state": 3}}}}
                },
                "service": "StateController",
                "target": self._serial_number,
                "version": 1
            }
            await asyncio.wait_for(self.setCleanerState(request), timeout=30)
