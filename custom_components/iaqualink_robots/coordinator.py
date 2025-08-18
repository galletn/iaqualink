"""Coordinator and client for iaqualinkRobots integration."""
import json
import datetime
import aiohttp
import logging
import asyncio
import re

from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed  # type: ignore # noqa
# Note: This import requires Home Assistant 2025.1 or later
from homeassistant.components.vacuum import VacuumActivity  # type: ignore # noqa
from .const import (
    URL_LOGIN, 
    URL_GET_DEVICES, 
    URL_WS, 
    URL_GET_DEVICE_FEATURES
)

_LOGGER = logging.getLogger(__name__)

class AqualinkClient:
    """Client to interact with iAqualink API for multiple robot types."""
    def __init__(self, username: str, password: str, api_key: str, debug_mode: bool = False):
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
        self._token_expires_at = None  # Track token expiration
        self._model_fetch_attempts = 0  # Track model fetch attempts
        self._model_last_attempt = None  # Track last model fetch attempt time
        self._pending_stop_reset = None  # Store reset values from stop command
        self._activity = VacuumActivity.IDLE  # Default activity state
        self._hass = None  # Will be set by coordinator
        self._headers = {
            "Content-Type": "application/json; charset=utf-8", 
            "Connection": "keep-alive", 
            "Accept": "*/*"
        }
        self._debug_mode = debug_mode  # Debug mode configurable
        # Note: Persistent websocket connection disabled - using fresh connections for reliability
        # self._ws_connection = None
        # self._ws_session = None
        # self._last_ws_activity = None
        # Websocket failure tracking for resilient status reporting
        self._ws_consecutive_failures = 0
        self._last_known_status = None
        self._max_ws_failures_before_offline = 5  # Allow 5 websocket failures before marking offline
        # Simple instance ID for logging purposes
        self._instance_id = f"ha-{hash(username + str(id(self))) % 10000:04d}"

    def _get_resilient_status(self, error_context: str) -> str:
        """Get status that's resilient to temporary websocket failures.
        
        Args:
            error_context: Context of the error (e.g., 'update_failed', 'setup_cancelled')
            
        Returns:
            Status string - preserves last known status for temporary failures,
            returns 'offline' only after multiple consecutive failures
        """
        if self._ws_consecutive_failures >= self._max_ws_failures_before_offline:
            # Multiple consecutive failures - device is likely offline
            _LOGGER.warning(f"Device marked offline after {self._ws_consecutive_failures} consecutive websocket failures")
            return "offline"
        elif self._last_known_status:
            # Temporary failure - preserve last known status
            _LOGGER.debug(f"Preserving last known status '{self._last_known_status}' during temporary failure ({error_context})")
            return self._last_known_status
        else:
            # No previous status available - default to a neutral status
            _LOGGER.debug(f"No previous status available, using 'unknown' for {error_context}")
            return "unknown"
    
    def _get_close_code_info(self, close_code):
        """Get human-readable information about websocket close codes."""
        close_codes = {
            1000: "Normal Closure - Connection closed normally",
            1001: "Going Away - Server going down or browser navigating away",  
            1002: "Protocol Error - Websocket protocol error",
            1003: "Unsupported Data - Server received unsupported data type",
            1005: "No Status - No status code was provided", 
            1006: "Abnormal Closure - Connection closed abnormally without close frame",
            1007: "Invalid Data - Invalid UTF-8 or inconsistent data received",
            1008: "Policy Violation - Message violates policy",
            1009: "Message Too Big - Message too large to process",
            1011: "Server Error - Unexpected server condition",
            1012: "Service Restart - Service is restarting",
            1013: "Try Again Later - Service is overloaded",
            1014: "Bad Gateway - Server acting as gateway received invalid response",
            1015: "TLS Handshake Failed - TLS handshake failure"
        }
        
        return close_codes.get(close_code, f"Unknown close code: {close_code}")

    def _log_connection_context(self):
        """Log additional context to help troubleshoot websocket issues."""
        import socket
        try:
            hostname = socket.gethostname()
            local_ip = socket.gethostbyname(hostname)
            _LOGGER.debug(
                f"Connection context - Hostname: {hostname}, IP: {local_ip}, Account: {self._username}"
            )
        except Exception as e:
            _LOGGER.debug(f"Could not gather connection context: {e}")

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

    def _is_token_expired(self) -> bool:
        """Check if authentication token is expired."""
        if not self._token_expires_at:
            return True
        return datetime.datetime.now() >= self._token_expires_at

    def set_hass(self, hass):
        """Set Home Assistant instance for translations."""
        self._hass = hass

    def _format_time_human(self, hours: int, minutes: int, seconds: int) -> str:
        """Format time in human readable format with translations."""
        if not self._hass:
            # Fallback to English if no hass instance
            return f"{hours} Hour(s) {minutes} Minute(s) {seconds} Second(s)"
        
        try:
            # Try to get translated time format strings
            hours_text = self._hass.localize("component.iaqualinkRobots.entity.vacuum.time_format.hours") or "Hour(s)"
            minutes_text = self._hass.localize("component.iaqualinkRobots.entity.vacuum.time_format.minutes") or "Minute(s)"
            seconds_text = self._hass.localize("component.iaqualinkRobots.entity.vacuum.time_format.seconds") or "Second(s)"
            
            return f"{hours} {hours_text} {minutes} {minutes_text} {seconds} {seconds_text}"
        except (KeyError, AttributeError):
            # Fallback to English if translation fails
            return f"{hours} Hour(s) {minutes} Minute(s) {seconds} Second(s)"

    async def fetch_status(self, quick_setup=False) -> dict:
        """Authenticate, discover device, and parse status.
        
        Args:
            quick_setup: If True, skip websocket operations for faster initial setup
        """
        # Only authenticate if we don't have a valid token
        if not self._auth_token or self._is_token_expired():
            await self._authenticate()
            
        # Only discover device if we don't have serial and device type
        if not self._serial or not self._device_type:
            await self._discover_device()
            
        # During quick setup, return minimal data without websocket operations
        if quick_setup:
            return {
                "serial_number": self._serial,
                "device_type": self._device_type,
                "status": "ready",
                "model": "Unknown",
                "activity": "unknown",
                "setup_mode": True
            }
            
        # Update device status based on device type
        if self._device_type == "i2d_robot":
            data = await self._update_i2d_robot()
            # For i2d robots, model is not available via API, set to Hidden
            data["model"] = "Hidden"
        else:
            data = await self._update_other_robots()
            
            # Get model for non-i2d robots, only if not already cached
            if not self._model:
                self._model = await self._get_device_model()
            data["model"] = str(self._model)
        
        # Apply any pending reset values from stop command
        if self._pending_stop_reset:
            _LOGGER.debug("Applying pending stop reset values: %s", self._pending_stop_reset)
            data.update(self._pending_stop_reset)
            self._pending_stop_reset = None  # Clear after applying
                
        return data

    async def _authenticate(self):
        """Authenticate with iAqualink API."""
        if self._debug_mode:
            _LOGGER.debug("Authenticating with iAqualink API...")
        try:
            data = {
                "apiKey": self._api_key,
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
            
            # Set token expiration to 1 hour from now (conservative estimate)
            self._token_expires_at = datetime.datetime.now() + datetime.timedelta(hours=1)
            if self._debug_mode:
                _LOGGER.debug("Authentication successful, token expires at: %s", self._token_expires_at)
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Authentication cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    async def _discover_device(self, target_serial=None):
        """Get list of devices and pick the pool robot.
        
        If target_serial is provided, select that specific device.
        Otherwise, select the first compatible device found.
        """
        try:
            params = {
                "authentication_token": self._auth_token,
                "user_id": self._id,
                "api_key": self._api_key
            }
            devices = await asyncio.wait_for(self.get_devices(params, self._headers), timeout=30)

            # Filter only devices that are compatible with the module
            supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr", "vortrax"]
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
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Device discovery cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior
            
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
        supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr", "vortrax"]
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

    # Note: Persistent websocket connection methods disabled for fresh connection approach
    # async def _ensure_websocket_connection(self):
    #     """Ensure we have an active websocket connection, creating one if needed."""
    #     # This method is commented out as we now use fresh connections for each request
    #     pass
    
    # async def _close_websocket(self):
    #     """Close the websocket connection and session if they exist."""
    #     # This method is commented out as we now use fresh connections for each request
    #     pass
    
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
        
        # Always use fresh connection for data requests to ensure we get current data
        # This restores the original working behavior
        return await asyncio.wait_for(self.get_device_status(req), timeout=30)

    # Cache compiled regex patterns for better performance
    _VORTRAX_PATTERNS = [
        re.compile(pattern, re.IGNORECASE | re.DOTALL) for pattern in [
            r'container-[0-9a-f]+[^>]*>[^<]*<div[^>]*>[^<]*<div[^>]*>[^<]*<section[^>]*>[^<]*<div[^>]*>[^<]*<div[^>]*>[^<]*<ul[^>]*>[^<]*<li[^>]*>[^<]*<a[^>]*>[^<]*<h4[^>]*>([^<]+)</h4>',
            r'<h4[^>]*>(?:VortraX|VORTRAX)\s*([\w-]+)[^<]*</h4>',
            r'<div[^>]*class="[^"]*product-title[^"]*"[^>]*>\s*(?:VortraX|VORTRAX)\s*([\w-]+)\s*</div>',
            r'product-name[^>]*>\s*(?:VortraX|VORTRAX)\s*([\w-]+)\s*</',
            r'<title[^>]*>(?:VortraX|VORTRAX)\s*([\w-]+)',
            r'(?:VortraX|VORTRAX)\s*([\w-]+)'
        ]
    ]

    async def _get_vortrax_model_from_web(self, pn: str) -> str:
        """Get vortrax model from the zodiac website using product number."""
        # List of URLs to try
        urls = [
            f"https://www.zodiac-poolcare.com/search?key={pn}",
            f"https://www.zodiac-poolcare.be/content/zodiac/be/nl_be/search.html?key={pn}",
            f"https://www.zodiac-poolcare.fr/search?key={pn}"
        ]

        timeout = aiohttp.ClientTimeout(total=5)  # 5 second timeout
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in urls:
                try:
                    _LOGGER.debug(f"Trying to fetch VortraX model from {url}")
                    async with session.get(url) as response:
                        if response.status != 200:
                            _LOGGER.debug(f"Failed to fetch from {url}, status: {response.status}")
                            continue

                        html = await response.text()
                        _LOGGER.debug(f"Got HTML content length: {len(html)}")
                        
                        # Try each pre-compiled pattern
                        for pattern in self._VORTRAX_PATTERNS:
                            matches = pattern.finditer(html)
                            for match in matches:
                                model = match.group(1).strip()
                                # Clean up the model string
                                model = re.sub(r'^(?:VortraX|VORTRAX)\s*', '', model, flags=re.IGNORECASE)
                                if model:
                                    _LOGGER.debug(f"Found VortraX model {model} for PN {pn}")
                                    # Only accept model numbers that look valid
                                    if re.match(r'^[A-Z0-9-]+$', model):
                                        return f"VortraX {model}"

                except asyncio.TimeoutError:
                    _LOGGER.debug(f"Timeout fetching from {url}")
                    continue
                except Exception as e:
                    _LOGGER.debug(f"Error fetching from {url}: {e}")
                    continue

        # If no model found after trying all URLs and patterns
        _LOGGER.debug(f"No VortraX model found for PN {pn}")
        return f"VortraX (PN: {pn})"

    async def _get_device_model(self) -> str:
        """Get device model information."""
        # Return cached model if available and valid (not "Unknown" or "Not Supported")
        if self._model and self._model not in ["Unknown", "Not Supported"]:
            _LOGGER.debug(f"Using cached model: {self._model}")
            return self._model
            
        # Check if we should retry fetching the model
        should_retry = self._should_retry_model_fetch()
        if not should_retry:
            _LOGGER.debug(f"Skipping model fetch - too many recent attempts. Current model: {self._model or 'None'}")
            return self._model or "Unknown"
            
        _LOGGER.debug(f"Fetching model for device type: {self._device_type}, serial: {self._serial} (attempt {self._model_fetch_attempts + 1})")
        
        # Record this attempt
        self._model_fetch_attempts += 1
        self._model_last_attempt = datetime.datetime.now()
        
        # For vortrax robots, get model from web based on product number
        if self._device_type == "vortrax":
            try:
                # Use the persistent websocket connection
                data = await self._ws_subscribe()
                if data and 'payload' in data and data['payload'].get('robot', {}).get('state', {}).get('reported', {}).get('eboxData', {}).get('completeCleanerPn'):
                    pn = data['payload']['robot']['state']['reported']['eboxData']['completeCleanerPn']
                    _LOGGER.debug(f"Found VortraX product number: {pn}")
                    if pn:
                        model = await self._get_vortrax_model_from_web(pn)
                        if model != "Unknown":
                            self._model = model  # Cache the successful result
                            self._model_fetch_attempts = 0  # Reset attempts on success
                            _LOGGER.debug(f"Successfully cached VortraX model: {self._model}")
                            return model
                else:
                    _LOGGER.debug("No product number data available for VortraX model detection")
            except Exception as e:
                _LOGGER.debug(f"Error getting vortrax product number: {e}")

        # For other robots, try the features API
        url = f"{URL_GET_DEVICE_FEATURES}{self._serial}/features"
        try:
            data = await asyncio.wait_for(self.get_device_features(url), timeout=30)
            
            # Handle empty or malformed responses
            if not data or not isinstance(data, dict):
                _LOGGER.debug(f"Empty or invalid features response for device {self._serial}")
                return self._handle_model_fetch_failure()
            
            # Try to get model, if not found or None, return Unknown
            model = data.get('model')
            if model is not None and model != "":
                self._model = str(model)  # Cache the successful result
                self._model_fetch_attempts = 0  # Reset attempts on success
                _LOGGER.debug(f"Successfully cached model from features API: {self._model}")
                return self._model
                
            # Only mark as "Not Supported" if we get an explicit indication
            if data.get('modelSupported') is False:
                self._model = "Not Supported"
                self._model_fetch_attempts = 0  # Don't retry for "Not Supported"
                _LOGGER.debug(f"Model not supported for device {self._serial}")
                return self._model
                
            return self._handle_model_fetch_failure()
                
        except Exception as e:
            _LOGGER.debug(f"Error getting device model: {e}")
            return self._handle_model_fetch_failure()

    def _should_retry_model_fetch(self) -> bool:
        """Determine if we should retry fetching the model."""
        # Always try if we haven't attempted yet
        if self._model_fetch_attempts == 0:
            return True
            
        # If we have a valid model (not Unknown/Not Supported), don't retry
        if self._model and self._model not in ["Unknown", "Not Supported"]:
            return False
            
        # If we've tried too many times, give up
        max_attempts = 5
        if self._model_fetch_attempts >= max_attempts:
            _LOGGER.debug(f"Max model fetch attempts ({max_attempts}) reached for device {self._serial}")
            return False
            
        # If we haven't tried recently, allow retry
        if not self._model_last_attempt:
            return True
            
        # Implement exponential backoff: wait longer between retries
        # Attempt 1: immediate, Attempt 2: 5 min, Attempt 3: 15 min, Attempt 4: 30 min, Attempt 5: 60 min
        backoff_minutes = [0, 5, 15, 30, 60]
        wait_minutes = backoff_minutes[min(self._model_fetch_attempts - 1, len(backoff_minutes) - 1)]
        
        time_since_last = datetime.datetime.now() - self._model_last_attempt
        if time_since_last.total_seconds() >= (wait_minutes * 60):
            _LOGGER.debug(f"Retrying model fetch after {wait_minutes} minute wait")
            return True
            
        return False

    def _handle_model_fetch_failure(self) -> str:
        """Handle a failed model fetch attempt."""
        # Only cache "Unknown" if we've exhausted all attempts
        if self._model_fetch_attempts >= 5:
            self._model = "Unknown"
            _LOGGER.debug(f"Caching 'Unknown' model after {self._model_fetch_attempts} failed attempts")
        else:
            _LOGGER.debug(f"Model fetch failed (attempt {self._model_fetch_attempts}/5), will retry later")
            
        return self._model or "Unknown"

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
                    0x00: "Quick clean floor only (standard)",
                    0x03: "Deep clean floor + walls (high power)",
                    0x04: "Waterline only (standard)",
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

                # Set fan speed based on mode code, regardless of activity state (using translation keys)
                if mode_code == 0x03:  # Deep clean mode
                    self._fan_speed = 3
                    result["fan_speed"] = "floor_and_walls"
                elif mode_code == 0x00:  # Quick clean floor only mode
                    self._fan_speed = 1
                    result["fan_speed"] = "floor_only"
                elif mode_code == 0x04:  # Waterline only 
                    self._fan_speed = 2
                    result["fan_speed"] = "walls_only"

                # Update activity state based on state code
                if state_code == 0x04 or state_code == 0x02:  # Actively cleaning or Just started cleaning
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

                # Calculate estimated end time if cleaning
                if time_remaining > 0 and self._activity == VacuumActivity.CLEANING:
                    estimated_end_time = self.add_minutes_to_datetime(
                        datetime.datetime.now(), time_remaining)
                    result["estimated_end_time"] = estimated_end_time.isoformat()
                    result["time_remaining_human"] = self._format_time_human(time_remaining // 60, time_remaining % 60, 0)

        except Exception as e:
            if self._debug_mode:
                _LOGGER.error(f"Error updating i2d robot status: {e}")
            if isinstance(data, dict) and data.get("status") == "500":
                result["status"] = "offline"
            result["debug"] = str(data) if self._debug_mode else ""
            result["error_state"] = "update_failed"

        return result

    async def _update_other_robots(self):
        """Update status for non-i2d robot types."""
        try:
            data = await self._ws_subscribe()
            # Check if we got valid data
            if data is None:
                _LOGGER.debug("No data received from websocket subscribe")
                self._ws_consecutive_failures += 1
                return {
                    "serial_number": self._serial,
                    "device_type": self._device_type,
                    "status": self._get_resilient_status("no_data"),
                    "error_state": "no_data"
                }
            # Reset failure count on successful websocket operation
            self._ws_consecutive_failures = 0
        except asyncio.CancelledError:
            # If cancelled during setup, return minimal data to allow integration to continue
            _LOGGER.debug("Robot update cancelled - returning minimal data")
            self._ws_consecutive_failures += 1
            return {
                "serial_number": self._serial,
                "device_type": self._device_type,
                "status": self._get_resilient_status("setup_cancelled"),
                "error_state": "setup_cancelled"
            }
        except Exception as e:
            # If websocket fails, increment failure count
            self._ws_consecutive_failures += 1
            _LOGGER.debug(f"Robot update failed (attempt {self._ws_consecutive_failures}): {e}")
            
            return {
                "serial_number": self._serial,
                "device_type": self._device_type,
                "status": self._get_resilient_status("update_failed"),
                "error_state": "update_failed"
            }

        result = {
            "serial_number": self._serial,
            "device_type": self._device_type
        }

        try:
            if data and 'payload' in data:
                result["status"] = data['payload']['robot']['state']['reported']['aws']['status']
                # Store the last known good status
                self._last_known_status = result["status"]
            else:
                result["status"] = self._get_resilient_status("no_data")
                result["debug"] = f"No data received: {data}" if self._debug_mode else ""
        except Exception:
            # Returns empty message sometimes, try second call
            result["debug"] = str(data) if self._debug_mode else ""  # Always store a string value
            
            try:
                data = await self._ws_subscribe()
                if data and 'payload' in data:
                    result["status"] = data['payload']['robot']['state']['reported']['aws']['status']
                    # Store the last known good status
                    self._last_known_status = result["status"]
                else:
                    result["status"] = self._get_resilient_status("no_data_retry")
            except asyncio.CancelledError:
                # If second call is also cancelled, return what we have
                self._ws_consecutive_failures += 1
                result["status"] = self._get_resilient_status("setup_cancelled")
                result["error_state"] = "setup_cancelled"
                return result
            except Exception:
                self._ws_consecutive_failures += 1
                result["status"] = self._get_resilient_status("update_failed")
                result["error_state"] = "update_failed"
                return result

        # Convert timestamp to datetime (only if we have valid data)
        if data and 'payload' in data:
            timestamp = data['payload']['robot']['state']['reported']['aws']['timestamp'] / 1000
            last_online = datetime.datetime.fromtimestamp(timestamp)
            result["last_online"] = last_online.isoformat()
            
            # Only update device-specific data if we have valid payload data
            # Update based on device type
            if self._device_type == "vr":
                self._update_vr_robot_data(data, result)
            elif self._device_type == "vortrax":
                self._update_vortrax_robot_data(data, result)
            elif self._device_type == "cyclobat":
                self._update_cyclobat_robot_data(data, result)
            elif self._device_type == "cyclonext":
                self._update_cyclonext_robot_data(data, result)
        else:
            result["last_online"] = "unknown"
            # Don't call device-specific update methods when we don't have valid data
            # Set default values for missing data
            result["activity"] = "unknown"
            result["error_state"] = result.get("error_state", "no_data")

        return result

    def _update_vr_robot_data(self, data, result):
        """Update status for VR type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for VR robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return
            
        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for VR robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return
            
        try:
            result["temperature"] = robot_data['sensors']['sns_1']['val']
        except Exception:
            try: 
                result["temperature"] = robot_data['sensors']['sns_1']['state']
            except Exception:
                # Zodiac XA 5095 iQ does not support temp
                result["temperature"] = '0'

        try:
            robot_state = robot_data['state']
            if robot_state == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                self._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Extract other attributes with safe access
        try:
            result["canister"] = robot_data['canister']*100
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

        # Get current cycle (fan speed) and update it
        try:
            current_cycle = robot_data['prCyc']
            result["cycle"] = current_cycle
            # Map cycle to fan speed (using translation keys)
            cycle_map = {
                0: "wall_only",           # Wall only
                1: "floor_only",          # Floor only
                2: "smart_floor_and_walls", # SMART mode
                3: "floor_and_walls"      # Floor and walls
            }
            self._fan_speed = cycle_map.get(current_cycle, "floor_only")
            result["fan_speed"] = self._fan_speed
        except Exception as e:
            _LOGGER.debug(f"Error setting fan speed for VR robot: {e}")
            self._fan_speed = "floor_only"  # Default to floor only if we can't determine
            result["fan_speed"] = self._fan_speed

        # Convert timestamp to datetime
        try:
            timestamp = robot_data['cycleStartTime']
            cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
            result["cycle_start_time"] = cycle_start_time.isoformat()

            cycle_duration_values = robot_data['durations']
            cycle_duration = list(cycle_duration_values.values())[result["cycle"]]
            result["cycle_duration"] = cycle_duration

            # Calculate end time and remaining time
            self._calculate_times(cycle_start_time, cycle_duration, result)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for VR robot: {e}")
            result["cycle_start_time"] = datetime.datetime.now().isoformat()
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _update_cyclobat_robot_data(self, data, result):
        """Update status for cyclobat type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for cyclobat robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return
            
        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
            main_data = robot_data['main']
            battery_data = robot_data['battery']
            stats_data = robot_data['stats']
            last_cycle_data = robot_data['lastCycle']
            cycles_data = robot_data['cycles']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for cyclobat robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Basic status and version info with safe access
        try:
            result["status"] = data['payload']['robot']['state']['reported']['aws']['status']
        except (KeyError, TypeError):
            pass  # Status already set in calling method
            
        result["version"] = robot_data.get('vr', 'Unknown')
        result["serial"] = robot_data.get('sn', '')

        # Main state information with safe access
        try:
            robot_state = main_data['state']
            if robot_state == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                self._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Main control and mode information with safe access
        result["control_state"] = str(main_data.get('ctrl', 'unknown'))
        result["mode"] = str(main_data.get('mode', 'unknown'))
        result["error_code"] = str(main_data.get('error', 'unknown'))
        
        # Battery information with safe access
        result["battery_version"] = battery_data.get('vr', 'Unknown')
        result["battery_state"] = str(battery_data.get('state', 'unknown'))
        result["battery_percentage"] = str(battery_data.get('userChargePerc', '0'))
        result["battery_level"] = str(battery_data.get('userChargePerc', '0'))
        result["battery_charge_state"] = str(battery_data.get('userChargeState', 'unknown'))
        result["battery_cycles"] = str(battery_data.get('cycles', '0'))
        result["battery_warning_code"] = str(battery_data.get('warning', {}).get('code', 'unknown'))

        # Statistics with safe access
        result["total_hours"] = str(stats_data.get('totRunTime', '0'))
        result["diagnostic_code"] = str(stats_data.get('diagnostic', 'unknown'))
        result["temperature"] = str(stats_data.get('tmp', '0'))
        result["last_error_code"] = str(stats_data.get('lastError', {}).get('code', 'unknown'))
        result["last_error_cycle"] = str(stats_data.get('lastError', {}).get('cycleNb', 'unknown'))

        # Last cycle information with safe access
        result["last_cycle_number"] = str(last_cycle_data.get('cycleNb', 'unknown'))
        result["last_cycle_duration"] = str(last_cycle_data.get('duration', '0'))
        result["last_cycle_mode"] = str(last_cycle_data.get('mode', 'unknown'))
        result["cycle"] = str(last_cycle_data.get('endCycleType', '0'))
        result["last_cycle_error"] = str(last_cycle_data.get('errorCode', 'unknown'))

        # Cycle durations with safe access
        result["floor_duration"] = str(cycles_data.get('floorTim', {}).get('duration', '0'))
        result["floor_walls_duration"] = str(cycles_data.get('floorWallsTim', {}).get('duration', '0'))
        result["smart_duration"] = str(cycles_data.get('smartTim', {}).get('duration', '0'))
        result["waterline_duration"] = str(cycles_data.get('waterlineTim', {}).get('duration', '0'))
        result["first_smart_done"] = str(cycles_data.get('firstSmartDone', 'false'))
        result["lift_pattern_time"] = str(cycles_data.get('liftPatternTim', '0'))

        # Convert timestamp to datetime for cycle start time with safe access
        try:
            timestamp = main_data['cycleStartTime']
            cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
            result["cycle_start_time"] = cycle_start_time.isoformat()

            # Get the appropriate cycle duration based on the current cycle type
            cycle_type = last_cycle_data['endCycleType']
            cycle_duration = None
            if cycle_type == 0:  # Floor only
                cycle_duration = cycles_data['floorTim']['duration']
            elif cycle_type == 1:  # Floor and walls
                cycle_duration = cycles_data['floorWallsTim']['duration']
            elif cycle_type == 2:  # Smart
                cycle_duration = cycles_data['smartTim']['duration']
            elif cycle_type == 3:  # Waterline
                cycle_duration = cycles_data['waterlineTim']['duration']

            if cycle_duration is not None:
                result["cycle_duration"] = cycle_duration
                # Calculate end time and remaining time
                self._calculate_times(cycle_start_time, cycle_duration, result)
            else:
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for cyclobat robot: {e}")
            result["cycle_start_time"] = datetime.datetime.now().isoformat()
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _update_vortrax_robot_data(self, data, result):
        """Update status for vortrax type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for vortrax robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return
            
        # Store product number if available
        try:
            result["product_number"] = data['payload']['robot']['state']['reported']['eboxData']['completeCleanerPn']
        except Exception:
            result["product_number"] = None

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for vortrax robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Rest of the vortrax robot data update with safe access
        try:
            result["temperature"] = robot_data['sensors']['sns_1']['val']
        except Exception:
            try: 
                result["temperature"] = robot_data['sensors']['sns_1']['state']
            except Exception:
                result["temperature"] = '0'

        try:
            robot_state = robot_data['state']
            if robot_state == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                self._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Extract other attributes with safe access
        try:
            result["canister"] = robot_data['canister']*100
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

        # Convert timestamp to datetime with safe access
        try:
            timestamp = robot_data['cycleStartTime']
            cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
            result["cycle_start_time"] = cycle_start_time.isoformat()

            result["cycle"] = robot_data['prCyc']

            cycle_duration_values = robot_data['durations']
            cycle_duration = list(cycle_duration_values.values())[result["cycle"]]
            result["cycle_duration"] = cycle_duration

            # Calculate end time and remaining time
            self._calculate_times(cycle_start_time, cycle_duration, result)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for vortrax robot: {e}")
            result["cycle_start_time"] = datetime.datetime.now().isoformat()
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _update_cyclonext_robot_data(self, data, result):
        """Update status for cyclonext type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for cyclonext robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return
            
        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot.1']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot.1 data structure for cyclonext robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            if robot_data['mode'] == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Extract attributes with safe access
        try:
            result["canister"] = robot_data['canister']*100
        except (KeyError, TypeError):
            result["canister"] = 0
            
        try:
            result["error_state"] = robot_data['errors']['code']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        try:
            result["total_hours"] = robot_data['totRunTime']
        except Exception:
            # Not supported by some cyclonext models
            result["total_hours"] = 0

        # Convert timestamp to datetime with safe access
        try:
            timestamp = robot_data['cycleStartTime']
            cycle_start_time = datetime.datetime.fromtimestamp(timestamp)
            result["cycle_start_time"] = cycle_start_time.isoformat()

            result["cycle"] = robot_data['cycle']

            cycle_duration_values = robot_data['durations']
            cycle_duration = list(cycle_duration_values.values())[result["cycle"]]
            result["cycle_duration"] = cycle_duration

            # Calculate end time and remaining time
            self._calculate_times(cycle_start_time, cycle_duration, result)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for cyclonext robot: {e}")
            result["cycle_start_time"] = datetime.datetime.now().isoformat()
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _calculate_times(self, start_time, duration_minutes, result):
        """Calculate end time and remaining time."""
        try:
            # Calculate cycle end time
            cycle_end_time = self.add_minutes_to_datetime(start_time, duration_minutes)
            result["cycle_end_time"] = cycle_end_time.isoformat()
            result["estimated_end_time"] = cycle_end_time.isoformat()
            
            # If the device is idle (not cleaning/returning), always report 0 time remaining
            # regardless of what the webservice reports for cycle start time
            current_activity = result.get("activity", "idle")
            if current_activity not in ["cleaning", "returning"]:
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
                _LOGGER.debug("Device is idle (%s), setting time remaining to 0", current_activity)
                return
            
            # Calculate remaining time only if device is actively cleaning or returning
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
                    result["time_remaining_human"] = self._format_time_human(hours, minutes, seconds)
                    
                    _LOGGER.debug("Time remaining (minutes): %d, human readable: %s", 
                                total_minutes, 
                                result["time_remaining_human"])
                else:
                    # If end time has passed, set to 0 (numeric) for time_remaining
                    result["time_remaining"] = 0
                    result["time_remaining_human"] = self._format_time_human(0, 0, 0)
            else:
                # If we don't have valid times, set to 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
                
            _LOGGER.debug(
                "Time calculation: activity=%s, start=%s, end=%s, now=%s, remaining=%s",
                current_activity,
                start_time.isoformat() if start_time else None,
                cycle_end_time.isoformat() if cycle_end_time else None,
                now.isoformat(),
                result.get("time_remaining")
            )
            
        except Exception as e:
            if self._debug_mode:
                _LOGGER.warning(f"Error calculating times: {e}")
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

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
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(URL_LOGIN, data=data) as response:
                    if response.status == 403:
                        if self._debug_mode:
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
                        if self._debug_mode:
                            _LOGGER.error(f"Unexpected content type: {content_type}. Response: {text[:200]}...")
                        raise aiohttp.ClientResponseError(
                            response.request_info,
                            response.history,
                            status=response.status,
                            message=f"Unexpected content type: {content_type}",
                            headers=response.headers
                        )
                    
                    return await response.json()
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Login request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    async def get_devices(self, params, headers):
        """Get device list from the iaqualink_robots API."""
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(URL_GET_DEVICES, params=params) as response:
                    return await response.json()
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Get devices request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    async def get_device_features(self, url):
        """Get device features from the iaqualink_robots API."""
        try:
            async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
                async with session.get(url) as response:
                    return await response.json()
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Get device features request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    async def get_device_status(self, request):
        """Get device status from the iaqualink_robots API via websocket.
        This creates a fresh websocket connection for each request to ensure current data."""
        try:
            async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
                async with session.ws_connect(URL_WS) as websocket:
                    await websocket.send_json(request)
                    message = await asyncio.wait_for(websocket.receive(), timeout=10)
                    if message.type == aiohttp.WSMsgType.TEXT:
                        return message.json()
                    elif message.type == aiohttp.WSMsgType.CLOSED:
                        _LOGGER.debug("Websocket connection closed during device status request")
                        return None
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        _LOGGER.debug(f"Websocket error during device status request: {message.data}")
                        return None
                    else:
                        _LOGGER.debug(f"Unexpected websocket message type {message.type} ({message.type.name if hasattr(message.type, 'name') else 'unknown'}) in device status request")
                        return None
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Get device status request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior
        except Exception as e:
            _LOGGER.debug(f"Device status websocket connection failed: {e}")
            # Return None instead of raising to let callers handle gracefully
            return None

    async def post_command_i2d(self, url, request):
        """Send command to i2d robot via the iaqualink_robots API."""
        try:
            async with aiohttp.ClientSession(headers={"Authorization": self._id_token, "api_key": self._api_key}) as session:
                async with session.post(url, json=request) as response:
                    return await response.json()
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Post command i2d request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior
    
    async def set_cleaner_state(self, request):
        """Set cleaner state via the iaqualink_robots API websocket."""
        response_data = None
        try:
            # Use fresh websocket connection for each command to ensure reliability
            async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
                async with session.ws_connect(URL_WS) as websocket:
                    await websocket.send_json(request)
                    
                    # Try to get response to confirm command was received
                    try:
                        response = await asyncio.wait_for(websocket.receive(), timeout=10)
                        if response.type == aiohttp.WSMsgType.TEXT:
                            response_data = response.json()
                            _LOGGER.debug(f"Command response received: {response_data}")
                    except asyncio.TimeoutError:
                        _LOGGER.debug("No response received within timeout, command may still succeed")
                    except Exception as e:
                        _LOGGER.debug(f"Error receiving response: {e}")
                        
        except Exception as e:
            if self._debug_mode:
                _LOGGER.debug(f"Error sending websocket command: {e}")
                
        # Wait 2 seconds to avoid flooding iaqualink server
        await asyncio.sleep(2)
        return response_data
        
    # Note: _listen_for_updates method disabled - using fresh connections for reliability
                
    async def start_cleaning(self):
        """Start the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            # Record the start time when starting cleaning for i2d robots
            now = datetime.datetime.now()
            result = {
                "cycle_start_time": now.isoformat(),
                "activity": "cleaning"
            }
            request = {
                "command": "/command",
                "params": "request=0A1240&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
            return result  # Return the time values to be used in the coordinator's update
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            
            request = None
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Use exact format from MITM proxy capture
                request = {
                    "action": "setCleanerState",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "state": 1
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
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
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Use exact format from MITM proxy capture
                request = {
                    "action": "setCleanerState",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "state": 0
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
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
        reset_values = {
            "estimated_end_time": now.isoformat(),
            "time_remaining": 0,
            "time_remaining_human": self._format_time_human(0, 0, 0),
            "cycle_start_time": now.isoformat(),
            "activity": "idle"  # Also set activity to idle to ensure proper state
        }
        
        # Store reset values to be applied in next status fetch
        self._pending_stop_reset = reset_values.copy()
        
        if request:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
                _LOGGER.debug("Stop cleaning requested, reset time values: %s", reset_values)
                return reset_values  # Return the reset values for immediate use by vacuum entity
                
    async def pause_cleaning(self):
        """Pause the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            # i2d robots might not support pause - use stop instead
            return await self.stop_cleaning()
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            
            request = None
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Use exact format from MITM proxy capture - state 2 = pause
                request = {
                    "action": "setCleanerState",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "state": 2
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclobat":
                # Cyclobat pause might be different - using same format for now
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"ctrl": 2}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclonext":
                # Cyclonext pause might be different
                request = {
                    "action": "setCleanerState",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"mode": 2}}}}
                    },
                    "service": "StateController",
                    "target": self._serial,
                    "version": 1
                }
            
            if request:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
                
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
        elif self._device_type in ["vr", "vortrax", "cyclobat"]:
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
            
    def _extract_fan_speed_from_response(self, response_data, requested_fan_speed):
        """Extract current fan speed from websocket response if available."""
        if not response_data:
            return None
            
        try:
            payload = response_data.get("payload", {})
            
            # For different robot types, the fan speed might be in different locations
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Look for prCyc (program cycle) in the response
                robot_state = payload.get("robot", {}).get("state", {}).get("reported", {}).get("equipment", {}).get("robot", {})
                pr_cyc = robot_state.get("prCyc")
                if pr_cyc is not None:
                    cycle_map = {0: "wall_only", 1: "floor_only", 2: "smart_floor_and_walls", 3: "floor_and_walls"}
                    return cycle_map.get(pr_cyc, requested_fan_speed)
                    
            elif self._device_type == "cyclobat":
                # Look for mode in cyclobat response
                robot_state = payload.get("robot", {}).get("state", {}).get("reported", {}).get("equipment", {}).get("robot", {}).get("main", {})
                mode = robot_state.get("mode")
                if mode is not None:
                    cycle_map = {3: "wall_only", 0: "floor_only", 2: "smart_floor_and_walls", 1: "floor_and_walls"}
                    return cycle_map.get(mode, requested_fan_speed)
                    
            elif self._device_type == "cyclonext":
                # Look for cycle in cyclonext response
                robot_state = payload.get("robot", {}).get("state", {}).get("reported", {}).get("equipment", {}).get("robot.1", {})
                cycle = robot_state.get("cycle")
                if cycle is not None:
                    cycle_map = {1: "floor_only", 3: "floor_and_walls"}
                    return cycle_map.get(cycle, requested_fan_speed)
                    
        except Exception as e:
            _LOGGER.debug(f"Error extracting fan speed from response: {e}")
            
        return None

    async def set_fan_speed(self, fan_speed, fan_speed_list):
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in fan_speed_list:
            raise ValueError('Invalid fan speed')
            
        if self._device_type == "i2d_robot":
            return await self._set_i2d_fan_speed(fan_speed)
        else:
            return await self._set_other_fan_speed(fan_speed)
            
    async def _set_i2d_fan_speed(self, fan_speed):
        """Set fan speed for i2d robot."""
        cycle_speed_map = {
            "walls_only": "0A1284",
            "floor_only": "0A1280",
            "floor_and_walls": "0A1283"
        }
        
        _cycle_speed = cycle_speed_map.get(fan_speed)
        if not _cycle_speed:
            return None
            
        request = {
            "command": "/command",
            "params": f"request={_cycle_speed}&timeout=800",
            "user_id": self._id
        }
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
        response = await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        
        # For i2d robots, extract success/failure from response
        if response and response.get("command", {}).get("response"):
            _LOGGER.debug(f"i2d fan speed command response: {response}")
            return {"success": True, "fan_speed": fan_speed, "response": response}
        
        return {"success": False, "fan_speed": fan_speed}
        
    async def _set_other_fan_speed(self, fan_speed):
        """Set fan speed for non-i2d robots."""
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        
        request = None
        if self._device_type == "vr" or self._device_type == "vortrax":
            cycle_speed_map = {
                "wall_only": "0",
                "floor_only": "1",
                "smart_floor_and_walls": "2",
                "floor_and_walls": "3"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                # Use exact format from MITM proxy capture matching setCleaningMode action
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "prCyc": int(_cycle_speed)
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
                
        elif self._device_type == "cyclobat":
            cycle_speed_map = {
                "wall_only": "3",
                "floor_only": "0",
                "smart_floor_and_walls": "2",
                "floor_and_walls": "1"
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
            response_data = await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            
            # Extract relevant information from the websocket response
            result = {"success": True, "fan_speed": fan_speed}
            if response_data:
                _LOGGER.debug(f"Fan speed command response: {response_data}")
                result["response"] = response_data
                
                # Try to extract the actual fan speed from the response
                confirmed_fan_speed = self._extract_fan_speed_from_response(response_data, fan_speed)
                if confirmed_fan_speed:
                    result["confirmed_fan_speed"] = confirmed_fan_speed
                    _LOGGER.debug(f"Websocket confirmed fan speed: {confirmed_fan_speed}")
                
                # For some robot types, we might get immediate state confirmation
                payload = response_data.get("payload", {})
                if payload:
                    result["payload"] = payload
                    
            return result
        
        return {"success": False, "fan_speed": fan_speed, "error": "No valid request generated"}

    async def _enter_remote_control_mode(self):
        """Enter remote control mode by setting robot to pause state (state: 2)."""
        _LOGGER.debug("Entering remote control mode (state: 2)")
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        request = {
            "action": "setCleanerState",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "state": 2  # Pause mode enables remote control
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
        
    async def _exit_remote_control_mode(self):
        """Exit remote control mode by setting robot to stopped state (state: 0)."""
        _LOGGER.debug("Exiting remote control mode (state: 0)")
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        request = {
            "action": "setCleanerState",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "state": 0  # Stopped state exits remote control
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def _send_remote_command(self, rmt_ctrl_value, command_name="remote"):
        """Send a remote control command after ensuring robot is in remote control mode."""
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        request = {
            "action": "setRemoteSteeringControl",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "rmt_ctrl": rmt_ctrl_value
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }
        _LOGGER.debug(f"Sending {command_name} command (rmt_ctrl: {rmt_ctrl_value})")
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def remote_forward(self):
        """Send forward command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote forward not supported for device type: {self._device_type}")
            return
        
        # Ensure robot is in remote control mode, send forward command, then stop
        await self._enter_remote_control_mode()
        await asyncio.sleep(0.5)  # Brief pause for mode transition
        await self._send_remote_command(1, "forward")

    async def remote_backward(self):
        """Send backward command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote backward not supported for device type: {self._device_type}")
            return
        
        # Ensure robot is in remote control mode, send backward command
        await self._enter_remote_control_mode()
        await asyncio.sleep(0.5)  # Brief pause for mode transition
        await self._send_remote_command(2, "backward")

    async def remote_rotate_left(self):
        """Send rotate left command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote rotate left not supported for device type: {self._device_type}")
            return
        
        # Ensure robot is in remote control mode, send rotate left command
        await self._enter_remote_control_mode()
        await asyncio.sleep(0.5)  # Brief pause for mode transition
        await self._send_remote_command(4, "rotate left")
        request = {
            "action": "setRemoteSteeringControl",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "rmt_ctrl": 4
                            }
                        }
                    }
                },
                "clientToken": f"{self._id}|{self._auth_token}|{self._app_client_id}"
            },
            "service": "StateController",
            "target": self._serial
        }
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def remote_rotate_right(self):
        """Send rotate right command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote rotate right not supported for device type: {self._device_type}")
            return
        
        # Ensure robot is in remote control mode, send rotate right command
        await self._enter_remote_control_mode()
        await asyncio.sleep(0.5)  # Brief pause for mode transition
        await self._send_remote_command(4, "rotate right")

    async def remote_stop(self):
        """Send stop command and exit remote control mode for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote stop not supported for device type: {self._device_type}")
            return
        
        # Send stop command, then exit remote control mode
        await self._send_remote_command(0, "stop")
        await asyncio.sleep(0.5)  # Brief pause before exiting
        await self._exit_remote_control_mode()

    async def add_fifteen_minutes(self):
        """Add 15 minutes to cleaning time for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Add 15 minutes not supported for device type: {self._device_type}")
            return
            
        try:
            # Get current stepper value to calculate new value
            current_status = await self.fetch_status()
            current_stepper = current_status.get('equipment', {}).get('robot', {}).get('stepper', 0)
            new_stepper = current_stepper + 15
            
            if self._debug_mode:
                _LOGGER.debug(f"Adding 15 minutes: current stepper={current_stepper}, new stepper={new_stepper}")
            
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            
            # Send the stepper command based on captured websocket traffic from official app
            request = {
                "action": "setCleaningMode",
                "version": 1,
                "namespace": "vr",
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": new_stepper
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial
            }
            
            _LOGGER.info(f"Sending add 15 minutes command with stepper={new_stepper}: {request}")
            result = await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            _LOGGER.info(f"Add 15 minutes response: {result}")
            
            # Also send a null command to clear the desired state (as seen in websocket traffic)
            clear_request = {
                "action": "setCleaningMode",
                "version": 1, 
                "namespace": "vr",
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": None
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial
            }
            
            await asyncio.wait_for(self.set_cleaner_state(clear_request), timeout=30)
            
            if self._debug_mode:
                _LOGGER.debug(f"✅ Add 15 minutes command completed successfully")
            return result
            
        except Exception as e:
            _LOGGER.error(f"❌ Add 15 minutes command failed: {e}")
            return None

    async def reduce_fifteen_minutes(self):
        """Reduce 15 minutes from cleaning time for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Reduce 15 minutes not supported for device type: {self._device_type}")
            return
            
        try:
            # Get current stepper value to calculate new value
            current_status = await self.fetch_status()
            current_stepper = current_status.get('equipment', {}).get('robot', {}).get('stepper', 0)
            new_stepper = max(0, current_stepper - 15)  # Don't go below 0
            
            if self._debug_mode:
                _LOGGER.debug(f"Reducing 15 minutes: current stepper={current_stepper}, new stepper={new_stepper}")
            
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            
            # Send the stepper command based on captured websocket traffic from official app
            request = {
                "action": "setCleaningMode",
                "version": 1,
                "namespace": "vr",
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": new_stepper
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial
            }
            
            _LOGGER.info(f"Sending reduce 15 minutes command with stepper={new_stepper}: {request}")
            result = await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            _LOGGER.info(f"Reduce 15 minutes response: {result}")
            
            # Also send a null command to clear the desired state (as seen in websocket traffic)
            clear_request = {
                "action": "setCleaningMode",
                "version": 1,
                "namespace": "vr",
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": None
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial
            }
            
            await asyncio.wait_for(self.set_cleaner_state(clear_request), timeout=30)
            
            if self._debug_mode:
                _LOGGER.debug(f"✅ Reduce 15 minutes command completed successfully")
            return result
            
        except Exception as e:
            _LOGGER.error(f"❌ Reduce 15 minutes command failed: {e}")
            return None

class AqualinkDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to poll AqualinkClient and update data."""
    def __init__(self, hass, client: AqualinkClient, interval: float, debug_mode: bool = False):
        super().__init__(
            hass,
            _LOGGER,
            name=client.robot_id,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.client.set_hass(hass)  # Set hass reference for translations
        self._debug_mode = debug_mode
        self._last_data = {}
        self._consecutive_failures = 0
        self._title = None  # Add this property to store the device title/name
        # Increasing from 3 to 30 - this is 15 minutes with default 30 second scan interval
        self._max_failures_before_unavailable = 30  # Allow 30 failures before marking unavailable
        self._setup_complete = False  # Flag to track if initial setup is complete
        
        # Note: Background websocket listener disabled - using fresh connections for reliability
        # self._websocket_listener_task = None
        # self._should_stop_listener = False
        # self._live_updates_enabled = True

    # Note: Background websocket listener methods disabled - using fresh connections for reliability
    # All websocket listener functionality has been replaced with fresh connection approach

    async def _async_update_data(self):
        try:
            # Check if this is the first call (initial setup)
            is_first_call = not hasattr(self, '_setup_complete') or not self._setup_complete
            
            if is_first_call:
                # Use quick setup for faster initial loading
                _LOGGER.debug("Performing quick setup for faster initial load")
                status = await self.client.fetch_status(quick_setup=True)
                self._setup_complete = True
                
                # Note: Background websocket listener disabled - using fresh connections
                _LOGGER.debug("Quick setup complete, using fresh connections for data requests")
            else:
                # Normal operation with full status fetch
                status = await self.client.fetch_status()
            
            # Always use fresh status data
            merged_data = status.copy()
            
            # For i2d robot, preserve cycle start time while cleaning
            if (self.client._device_type == "i2d_robot" and 
                merged_data.get("activity") == "cleaning" and 
                self._last_data.get("cycle_start_time")):
                merged_data["cycle_start_time"] = self._last_data["cycle_start_time"]
            
            # Reset failure count on successful update
            self._consecutive_failures = 0
            
            # Save data for next update
            self._last_data = merged_data.copy()
            
            # The coordinator automatically notifies entities when data changes
            # No need for explicit signaling as CoordinatorEntity handles this
            
            return merged_data
            
        except Exception as err:
            self._consecutive_failures += 1
            
            # Get detailed error information
            import traceback
            error_details = traceback.format_exc()
            
            # Only log as error after multiple failures to reduce log spam
            if self._consecutive_failures <= self._max_failures_before_unavailable:
                if self._debug_mode:
                    _LOGGER.warning(f"Update failed (attempt {self._consecutive_failures}/{self._max_failures_before_unavailable}): {err}")
                
                # Return last known good data if available to keep entity available
                if self._last_data:
                    _LOGGER.debug("Returning last known good data to keep entity available")
                    return self._last_data.copy()
                else:
                    # No previous data available, return minimal data to prevent unavailable state
                    _LOGGER.debug("No previous data available, returning minimal data")
                    return {
                        "serial_number": getattr(self.client, '_serial', 'unknown'),
                        "device_type": getattr(self.client, '_device_type', 'unknown'),
                        "status": "offline",
                        "activity": "unknown",
                        "error_state": "connection_failed"
                    }
            else:
                _LOGGER.error(f"Update failed after {self._consecutive_failures} attempts: {err}\nDetails:\n{error_details}")
                # Only raise UpdateFailed after max failures to mark entity unavailable
                raise UpdateFailed(f"Failed after {self._consecutive_failures} attempts: {err}")
    
    async def async_start_cleaning(self):
        """Start cleaning - centralized business logic."""
        await self.client.start_cleaning()
        
    async def async_stop_cleaning(self):
        """Stop cleaning - centralized business logic."""
        await self.client.stop_cleaning()
        
    async def async_return_to_base(self):
        """Return to base - centralized business logic."""
        await self.client.return_to_base()
        
    async def _execute_remote_command(self, command_method):
        """Execute a remote command with device type checking."""
        if self.client._device_type not in {"vr", "vortrax"}:
            return
        await command_method()
        # Only refresh once at the end to avoid multiple API calls
        await self.async_request_refresh()

    async def async_remote_forward(self):
        """Send forward command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_forward)

    async def async_remote_backward(self):
        """Send backward command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_backward)

    async def async_remote_rotate_left(self):
        """Send rotate left command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_rotate_left)

    async def async_remote_rotate_right(self):  
        """Send rotate right command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_rotate_right)

    async def async_remote_stop(self):
        """Send stop command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_stop)

    async def async_add_fifteen_minutes(self):
        """Add 15 minutes to cleaning time - centralized business logic."""
        if self.client._device_type not in {"vr", "vortrax"}:
            return
        await self.client.add_fifteen_minutes()
        # Request immediate refresh to get updated timing information
        await self.async_request_refresh()

    async def async_reduce_fifteen_minutes(self):
        """Reduce 15 minutes from cleaning time - centralized business logic."""
        if self.client._device_type not in {"vr", "vortrax"}:
            return
        await self.client.reduce_fifteen_minutes()
        # Request immediate refresh to get updated timing information
        await self.async_request_refresh()
        
    # Note: Live update methods disabled - using fresh connections for reliability
    
    async def cleanup(self):
        """Clean up resources when coordinator is being unloaded."""
        _LOGGER.debug("Starting coordinator cleanup")
        
        # Note: Websocket listener cleanup disabled - using fresh connections
        _LOGGER.debug("Coordinator cleanup complete - no persistent connections to close")
