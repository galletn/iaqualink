"""Coordinator and client for iaqualinkRobots integration."""
import json
import datetime
import aiohttp
import logging
import asyncio

from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.components.vacuum import VacuumActivity
from .const import (
    URL_LOGIN, 
    URL_GET_DEVICES, 
    URL_WS, 
    URL_FEATURES,
    URL_GET_DEVICE_STATUS,
    URL_GET_DEVICE_FEATURES
)

_LOGGER = logging.getLogger(__name__)

class AqualinkClient:
    """Client to interact with iAqualink API for multiple robot types."""
    def __init__(self, username: str, password: str, api_key: str):
        self._username = username
        self._password = password
        self._api_key = api_key
        self._auth_token: str = ""
        self._id: str = ""
        self._id_token: str = ""
        self._app_client_id: str = ""
        self._serial: str = ""
        self._device_type: str = ""
        self._first_name: str = ""
        self._last_name = None
        self._model = None
        self._activity = VacuumActivity.IDLE
        self._headers = {
            "Content-Type": "application/json; charset=utf-8", 
            "Connection": "keep-alive", 
            "Accept": "*/*"
        }
        self._debug_mode = True

    @property 
    def username(self) -> str:
        return self._username
    
    @property 
    def password(self) -> str:
        return self._password

    @property 
    def api_key(self) -> str:
        return self._api_key

    @property
    def robot_id(self) -> str:
        return self._serial or self._username

    @property
    def robot_name(self) -> str:
        # Use provided title if available
        return getattr(self, "_title", f"{self._device_type}_{self._serial}")

    async def fetch_status(self) -> dict:
        """Authenticate, discover device, subscribe, and parse status."""
        await self._authenticate()
        if not self._serial:
            await self._discover_device()
            
        # Update device status based on device type
        if self._device_type == "i2d_robot":
            data = await self._update_i2d_robot()
        else:
            data = await self._update_other_robots()
            
        # Get model only first time to avoid load
        if not self._model:
            model = await self._get_device_model()
            if model:
                data["model"] = model
        else:
            # Make sure model is included in every update
            data["model"] = self._model
                
        return data

    async def _authenticate(self):
        """Authenticate with iAqualink API."""
        data = {
            "apikey": self._api_key,
            "email": self._username,
            "password": self._password
        }
        data = json.dumps(data)
        auth = await asyncio.wait_for(self.send_login(data, self._headers), timeout=30)
        
        self._first_name = auth["first_name"]
        self._last_name = auth["last_name"]
        self._id = auth["id"]
        self._auth_token = auth["authentication_token"]
        self._id_token = auth["userPoolOAuth"]["IdToken"]
        self._app_client_id = auth["cognitoPool"]["appClientId"]

    async def _discover_device(self, target_serial=None):
        """Get list of devices and pick the pool robot.
        
        If target_serial is provided, select that specific device.
        Otherwise, select the first compatible device found.
        """
        params = {
            "authentication_token": self._auth_token,
            "user_id": self._id,
            "api_key": self._api_key
        }
        devices = await asyncio.wait_for(self.get_devices(params, self._headers), timeout=30)

        # Filter only devices that are compatible with the module
        supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr"]
        compatible_devices = []

        for device in devices:
            device_type = device.get("device_type")
            _LOGGER.debug("🔍 Devices found : %s", device)
            if device_type in supported_device_types:
                serial = device["serial_number"]
                compatible_devices.append(device)
                
                # If we're looking for a specific device and found it, select it
                if target_serial and serial == target_serial:
                    self._serial = serial
                    self._device_type = device_type
                    _LOGGER.debug(f"✅ Target device selected: {device_type} - {serial}")
                    return
            else:
                _LOGGER.debug(f"⏩ Device ignored (unsupported type): {device_type}")
        
        # If we have compatible devices but weren't looking for a specific one,
        # select the first compatible device
        if compatible_devices and not target_serial:
            device = compatible_devices[0]
            self._serial = device["serial_number"]
            self._device_type = device["device_type"]
            _LOGGER.debug(f"✅ Device selected: {self._device_type} - {self._serial}")
            return
            
        # If we were looking for a specific device but didn't find it,
        # or if we found no compatible devices at all
        if target_serial:
            _LOGGER.error(f"❌ Target device {target_serial} not found in the device list.")
            raise RuntimeError(f"Target device {target_serial} not found")
        else:
            _LOGGER.error("❌ No compatible robot found in the device list.")
            raise RuntimeError("No supported robot found")
            
    @classmethod
    async def discover_devices(cls, username, password, api_key):
        """Discover all available devices for an account.
        
        Returns a list of device information dictionaries.
        """
        client = cls(username, password, api_key)
        await client._authenticate()
        
        params = {
            "authentication_token": client._auth_token,
            "user_id": client._id,
            "api_key": client._api_key
        }
        devices = await asyncio.wait_for(client.get_devices(params, client._headers), timeout=30)
        
        # Filter only devices that are compatible with the module
        supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr"]
        compatible_devices = []
        
        for device in devices:
            device_type = device.get("device_type")
            if device_type in supported_device_types:
                compatible_devices.append({
                    "serial_number": device["serial_number"],
                    "device_type": device_type,
                    "name": device.get("name", f"{device_type}_{device['serial_number']}")
                })
                
        return compatible_devices

    async def _ws_subscribe(self):
        """Subscribe via websocket to get live updates."""
        req = {
            "action": "subscribe",
            "namespace": "authorization",
            "payload": {"userId": self._id},
            "service": "Authorization",
            "target": self._serial,
            "version": 1
        }
        return await asyncio.wait_for(self.get_device_status(req), timeout=30)

    async def _get_device_model(self) -> str:
        """Get device model information."""
        url = f"{URL_GET_DEVICE_FEATURES}{self._serial}/features"
        try:
            data = await asyncio.wait_for(self.get_device_features(url), timeout=30)
            self._model = data.get('model', 'Not Supported')
            return self._model
        except Exception:
            # Some models do not return a model number on the features call
            self._model = 'Not Supported'
            return self._model

    async def _update_i2d_robot(self):
        """Update status for i2d_robot type."""
        request = {
            "command": "/command",
            "params": "request=OA11",
            "user_id": self._id
        }
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
        data = await asyncio.wait_for(self.post_command_i2d(url, request), timeout=30)

        result = {
            "serial_number": str(self._serial),
            "device_type": str(self._device_type),
            "status": "offline"
        }

        try:
            if data.get("command", {}).get("request") == "OA11":
                result["status"] = "connected"
                debug = data.get("command", {}).get("response", "")
                result["debug"] = str(debug) if self._debug_mode else ""

                # Clean up response string and convert to bytes
                hex_str = debug.replace(" ", "")
                if len(hex_str) != 36:  # 18 bytes * 2 characters per byte
                    raise ValueError(f"Expected 36 hex characters; got {len(hex_str)} characters.")
                
                try:
                    data_val = bytes.fromhex(hex_str)
                except ValueError as e:
                    raise ValueError(f"Invalid hex string: {e}")

                # Lookup tables for status codes
                state_map = {
                    0x01: "Idle / Docked",
                    0x02: "Cleaning just started",
                    0x03: "Finished",
                    0x04: "Actively cleaning",
                    0x0C: "Paused",
                    0x0D: "Error state D",
                    0x0E: "Error state E"
                }

                error_map = {
                    0x00: "no_error",
                    0x01: "pump_short_circuit",
                    0x02: "right_drive_motor_short_circuit",
                    0x03: "left_drive_motor_short_circuit",
                    0x04: "pump_motor_overconsumption",
                    0x05: "right_drive_motor_overconsumption",
                    0x06: "left_drive_motor_overconsumption",
                    0x07: "floats_on_surface",
                    0x08: "running_out_of_water",
                    0x0A: "communication_error"
                }

                mode_map = {
                    0x03: "Deep clean floor + walls (high power)",
                    0x08: "Quick – floor only (standard)",
                    0x09: "Custom – floor only (high power)",
                    0x0A: "Custom – floor + walls (standard)",
                    0x0B: "Custom – floor + walls (high power)",
                    0x0C: "Waterline only (standard)",
                    0x0D: "Custom – waterline (high power)",
                    0x0E: "Custom – waterline (standard)"
                }

                # Parse fields
                state_code = data_val[2]
                error_code = data_val[3]
                mode_byte = data_val[4]
                # Extract mode code (lower nibble) and canister status (higher nibble)
                mode_code = mode_byte & 0x0F  # Get lower 4 bits for mode
                canister_full = (mode_byte & 0xF0) > 0  # Any non-zero value in upper nibble means canister is full
                time_remaining = data_val[5]
                uptime_min = int.from_bytes(data_val[6:9], byteorder='little')
                total_hours = int.from_bytes(data_val[9:12], byteorder='little')
                hardware_id = data_val[12:15].hex()
                firmware_id = data_val[15:18].hex()

                # Update result dictionary with parsed values (convert all values to strings)
                result["header"] = data_val[:2].hex()
                result["state_code"] = str(state_code)
                result["state"] = state_map.get(state_code, f"Unknown (0x{state_code:02X})")
                result["error_code"] = str(error_code)
                result["error"] = error_map.get(error_code, f"Unknown (0x{error_code:02X})")
                result["mode_code"] = str(mode_code)
                result["cycle"] = mode_map.get(mode_code, f"Unknown (0x{mode_code:02X})")
                result["mode"] = mode_map.get(mode_code, f"Unknown (0x{mode_code:02X})")
                result["canister"] = "100" if canister_full else "0"  # 100% if full, 0% if not full
                result["time_remaining"] = str(time_remaining)
                result["uptime_minutes"] = str(uptime_min)
                result["total_hours"] = str(total_hours)
                result["hardware_id"] = str(hardware_id)
                result["firmware_id"] = str(firmware_id)

                # Update activity state based on state code
                if state_code == 0x04:  # Actively cleaning
                    self._activity = VacuumActivity.CLEANING
                    result["activity"] = "cleaning"
                elif error_code != 0x00:
                    self._activity = VacuumActivity.ERROR
                    result["activity"] = "error"
                    result["error_state"] = error_map.get(error_code, f"unknown_{error_code:02X}")
                else:
                    self._activity = VacuumActivity.IDLE
                    result["activity"] = "idle"
                    result["error_state"] = "no_error"

                if mode_code == 0x03:  # Deep clean mode
                    self._fan_speed = 3

                # Calculate estimated end time if cleaning
                if time_remaining > 0 and self._activity == VacuumActivity.CLEANING:
                    estimated_end_time = self.add_minutes_to_datetime(
                        datetime.datetime.now(), time_remaining)
                    result["estimated_end_time"] = estimated_end_time.isoformat()
                    result["time_remaining_human"] = f"{time_remaining // 60}:{time_remaining % 60:02d}"

        except Exception as e:
            _LOGGER.error(f"Error updating i2d robot status: {e}")
            if isinstance(data, dict) and data.get("status") == "500":
                result["status"] = "offline"
            result["debug"] = str(data) if self._debug_mode else ""
            result["error_state"] = "update_failed"

        return result

    async def _update_other_robots(self):
        """Update status for non-i2d robot types."""
        data = await self._ws_subscribe()

        result = {
            "serial_number": self._serial,
            "device_type": self._device_type
        }

        try:
            result["status"] = data['payload']['robot']['state']['reported']['aws']['status']
        except Exception:
            # Returns empty message sometimes, try second call
            result["debug"] = data if self._debug_mode else None
            
            data = await self._ws_subscribe()
            result["status"] = data['payload']['robot']['state']['reported']['aws']['status']

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['aws']['timestamp'] / 1000
        last_online = datetime.datetime.fromtimestamp(timestamp)
        result["last_online"] = last_online.isoformat()

        # Update based on device type
        if self._device_type == "vr":
            self._update_vr_robot_data(data, result)
        elif self._device_type == "cyclobat":
            self._update_cyclobat_robot_data(data, result)
        elif self._device_type == "cyclonext":
            self._update_cyclonext_robot_data(data, result)

        return result

    def _update_vr_robot_data(self, data, result):
        """Update status for VR type robot."""
        try:
            result["temperature"] = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['val']
        except Exception:
            try: 
                result["temperature"] = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['state']
            except Exception:
                # Zodiac XA 5095 iQ does not support temp
                result["temperature"] = '0'

        robot_state = data['payload']['robot']['state']['reported']['equipment']['robot']['state']
        if robot_state == 1:
            self._activity = VacuumActivity.CLEANING
            result["activity"] = "cleaning"
        elif robot_state == 3:
            self._activity = VacuumActivity.RETURNING
            result["activity"] = "returning"
        else:
            self._activity = VacuumActivity.IDLE
            result["activity"] = "idle"

        # Extract other attributes
        result["canister"] = data['payload']['robot']['state']['reported']['equipment']['robot']['canister']*100
        result["error_state"] = data['payload']['robot']['state']['reported']['equipment']['robot']['errorState']
        result["total_hours"] = data['payload']['robot']['state']['reported']['equipment']['robot']['totalHours']

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['equipment']['robot']['cycleStartTime']
        cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
        result["cycle_start_time"] = cycle_start_time.isoformat()

        result["cycle"] = data['payload']['robot']['state']['reported']['equipment']['robot']['prCyc']

        cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot']['durations']
        cycle_duration = list(cycle_duration_values.values())[result["cycle"]]
        result["cycle_duration"] = cycle_duration

        # Calculate end time and remaining time
        self._calculate_times(cycle_start_time, cycle_duration, result)

    def _update_cyclobat_robot_data(self, data, result):
        """Update status for cyclobat type robot."""
        robot_state = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["main"]["state"]
        if robot_state == 1:
            self._activity = VacuumActivity.CLEANING
            result["activity"] = "cleaning"
        elif robot_state == 3:
            self._activity = VacuumActivity.RETURNING
            result["activity"] = "returning"
        else:
            self._activity = VacuumActivity.IDLE
            result["activity"] = "idle"

        # Extract attributes
        result["total_hours"] = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["stats"]["totRunTime"]
        result["battery_state"] = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["battery"]["state"]
        result["user_charge_percentage"] = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["battery"]["userChargePerc"]
        result["battery_cycles"] = data['payload']['robot']["state"]["reported"]["equipment"]["robot"]["battery"]["cycles"]

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['equipment']['robot']['main']['cycleStartTime']
        cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
        result["cycle_start_time"] = cycle_start_time.isoformat()

        result["cycle"] = data['payload']['robot']['state']['reported']['equipment']['robot']['lastCycle']['endCycleType']

        cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot']['cycles']
        cycle_duration = list(cycle_duration_values.values())[result["cycle"]]
        result["cycle_duration"] = cycle_duration

        # Calculate end time and remaining time
        self._calculate_times(cycle_start_time, cycle_duration, result)

    def _update_cyclonext_robot_data(self, data, result):
        """Update status for cyclonext type robot."""
        if data['payload']['robot']['state']['reported']['equipment']['robot.1']['mode'] == 1:
            self._activity = VacuumActivity.CLEANING
            result["activity"] = "cleaning"
        else:
            self._activity = VacuumActivity.IDLE
            result["activity"] = "idle"

        # Extract attributes
        result["canister"] = data['payload']['robot']['state']['reported']['equipment']['robot.1']['canister']*100
        result["error_state"] = data['payload']['robot']['state']['reported']['equipment']['robot.1']['errors']['code']

        try:
            result["total_hours"] = data['payload']['robot']['state']['reported']['equipment']['robot.1']['totRunTime']
        except Exception:
            # Not supported by some cyclonext models
            result["total_hours"] = 0

        # Convert timestamp to datetime
        timestamp = data['payload']['robot']['state']['reported']['equipment']['robot.1']['cycleStartTime']
        cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
        result["cycle_start_time"] = cycle_start_time.isoformat()

        result["cycle"] = data['payload']['robot']['state']['reported']['equipment']['robot.1']['cycle']

        cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot.1']['durations']
        cycle_duration = list(cycle_duration_values.values())[result["cycle"]]
        result["cycle_duration"] = cycle_duration

        # Calculate end time and remaining time
        self._calculate_times(cycle_start_time, cycle_duration, result)

    def _calculate_times(self, start_time, duration_minutes, result):
        """Calculate end time and remaining time."""
        try:
            # Calculate cycle end time
            cycle_end_time = self.add_minutes_to_datetime(start_time, duration_minutes)
            result["cycle_end_time"] = cycle_end_time.isoformat()
            result["estimated_end_time"] = cycle_end_time.isoformat()
            
            # Calculate remaining time
            now = datetime.datetime.now()
            
            # Only calculate time remaining if we have valid times
            if start_time and cycle_end_time:
                # If current time is before the cycle end time
                if now < cycle_end_time:
                    time_diff = cycle_end_time - now
                    total_seconds = time_diff.total_seconds()
                    
                    # Store time remaining as integer minutes for numeric sensor
                    total_minutes = max(0, int(total_seconds / 60))
                    result["time_remaining"] = total_minutes  # This will be numeric minutes
                    
                    # Format human readable time for display
                    hours = int(total_seconds // 3600)
                    minutes = int((total_seconds % 3600) // 60)
                    seconds = int(total_seconds % 60)
                    result["time_remaining_human"] = f"{hours} Hour(s) {minutes} Minute(s) {seconds} Second(s)"
                    
                    _LOGGER.debug("Time remaining (minutes): %d, human readable: %s", 
                                total_minutes, 
                                result["time_remaining_human"])
                else:
                    # If end time has passed, set to 0 (numeric) for time_remaining
                    result["time_remaining"] = 0
                    result["time_remaining_human"] = "0 Hour(s) 0 Minute(s) 0 Second(s)"
            else:
                # If we don't have valid times, set to None
                result["time_remaining"] = 0
                result["time_remaining_human"] = "0 Hour(s) 0 Minute(s) 0 Second(s)"
                
            _LOGGER.debug(
                "Time calculation: start=%s, end=%s, now=%s, remaining=%s",
                start_time.isoformat() if start_time else None,
                cycle_end_time.isoformat() if cycle_end_time else None,
                now.isoformat(),
                result.get("time_remaining")
            )
            
        except Exception as e:
            _LOGGER.warning(f"Error calculating times: {e}")
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["time_remaining"] = None
            result["time_remaining_human"] = None

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

    async def send_login(self, data, headers):
        """Post a login request to the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.post(URL_LOGIN, data=data) as response:
                    if response.status == 403:
                        _LOGGER.error("Authentication failed: 403 Forbidden. Check your credentials or API key.")
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message="Authentication failed: 403 Forbidden",
                            headers=response.headers
                        )
                    
                    content_type = response.headers.get('Content-Type', '')
                    if 'application/json' not in content_type:
                        text = await response.text()
                        _LOGGER.error(f"Unexpected content type: {content_type}. Response: {text[:200]}...")
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"Unexpected content type: {content_type}",
                            headers=response.headers
                        )
                    
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
                async with session.ws_connect(URL_WS) as websocket:
                    await websocket.send_json(request)
                    message = await websocket.receive(timeout=10)
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
    
    async def set_cleaner_state(self, request):
        """Set cleaner state via the iaqualink_robots API websocket."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.ws_connect(URL_WS) as websocket:
                    await websocket.send_json(request)
            finally:
                await asyncio.wait_for(session.close(), timeout=30)
                # Wait 2 seconds to avoid flooding iaqualink server
                await asyncio.sleep(2)
                
    async def start_cleaning(self):
        """Start the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1240&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            
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
                    "target": self._serial,
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
                    "target": self._serial
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
                    "target": self._serial,
                    "version": 1
                }
            
            if request:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
                
    async def stop_cleaning(self):
        """Stop the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1210&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            
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
                    "target": self._serial,
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
                    "target": self._serial
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
                    "target": self._serial,
                    "version": 1
                }
            
        # Create result with reset time values always, regardless of robot type
        now = datetime.datetime.now()
        result = {
            "estimated_end_time": now.isoformat(),
            "time_remaining": 0,
            "time_remaining_human": "0 Hour(s) 0 Minute(s) 0 Second(s)",
            "cycle_start_time": now.isoformat(),
            "activity": "idle"  # Also set activity to idle to ensure proper state
        }
        
        if request:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
                _LOGGER.debug("Stop cleaning requested, reset time values: %s", result)
                return result  # Return the reset values to be used in the coordinator's update
                
    async def return_to_base(self):
        """Set the vacuum cleaner to return to the dock."""
        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1701&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        elif self._device_type in ["vr", "cyclobat"]:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            request = {
                "action": "setCleanerState",
                "namespace": self._device_type,
                "payload": {
                    "clientToken": clientToken,
                    "state": {"desired": {"equipment": {"robot": {"state": 3}}}}
                },
                "service": "StateController",
                "target": self._serial,
                "version": 1
            }
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            
    async def set_fan_speed(self, fan_speed, fan_speed_list):
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in fan_speed_list:
            raise ValueError('Invalid fan speed')
            
        if self._device_type == "i2d_robot":
            await self._set_i2d_fan_speed(fan_speed)
        else:
            await self._set_other_fan_speed(fan_speed)
            
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
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
        await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        
    async def _set_other_fan_speed(self, fan_speed):
        """Set fan speed for non-i2d robots."""
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        
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
                    "target": self._serial
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
                    "target": self._serial
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
                    "target": self._serial,
                    "version": 1
                }
        
        if request:
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

class AqualinkDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to poll AqualinkClient and update data."""
    def __init__(self, hass, client: AqualinkClient, interval: float):
        super().__init__(
            hass,
            _LOGGER,
            name=client.robot_id,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self._last_data = {}

    async def _async_update_data(self):
        try:
            # Get new status data - this call may include reset time values after stop_cleaning
            status = await self.client.fetch_status()
            
            # Always use fresh status data
            merged_data = status.copy()
            
            # Save data for next update
            self._last_data = merged_data.copy()
            return merged_data
            
        except Exception as err:
            # Get detailed error information
            import traceback
            error_details = traceback.format_exc()
            _LOGGER.error(f"Error updating data: {err}\nDetails:\n{error_details}")
            raise UpdateFailed(f"{err} - See logs for details")
