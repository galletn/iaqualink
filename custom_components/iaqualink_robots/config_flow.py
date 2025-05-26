import logging
from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol

from .const import DOMAIN, API_KEY
from .coordinator import AqualinkClient

_LOGGER = logging.getLogger(__name__)

class IaqualinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL
    
    def __init__(self):
        """Initialize the config flow."""
        self._username = None
        self._password = None
        self._api_key = None
        self._devices = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            self._username = user_input["username"]
            self._password = user_input["password"]
            self._api_key = API_KEY  # Use the constant API key
            
            # Try to discover devices with these credentials
            try:
                self._devices = await AqualinkClient.discover_devices(
                    self._username, self._password, self._api_key
                )
                
                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    # If only one device is found, skip the selection step
                    if len(self._devices) == 1:
                        device = self._devices[0]
                        return self.async_create_entry(
                            title=user_input.get("name", device["name"]),
                            data={
                                "name": user_input.get("name", device["name"]),
                                "username": self._username,
                                "password": self._password,
                                "api_key": self._api_key,
                                "serial_number": device["serial_number"],
                                "device_type": device["device_type"]
                            },
                        )
                    # If multiple devices are found, go to the device selection step
                    return await self.async_step_select_device()
            except Exception as e:
                _LOGGER.error(f"Error discovering devices: {e}")
                errors["base"] = "cannot_connect"

        # Define form schema
        data_schema = vol.Schema({
            vol.Required("name", default="My Pool Robot"): str,
            vol.Required("username"): str,
            vol.Required("password"): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": "Enter your iAquaLink account credentials."
            }
        )
        
    async def async_step_select_device(self, user_input=None):
        """Handle the device selection step."""
        errors = {}
        
        if user_input is not None:
            # Find the selected device
            selected_serial = user_input["device"]
            selected_device = next(
                (device for device in self._devices if device["serial_number"] == selected_serial),
                None
            )
            
            if selected_device:
                return self.async_create_entry(
                    title=user_input.get("name", selected_device["name"]),
                    data={
                        "name": user_input.get("name", selected_device["name"]),
                        "username": self._username,
                        "password": self._password,
                        "api_key": self._api_key,
                        "serial_number": selected_device["serial_number"],
                        "device_type": selected_device["device_type"]
                    },
                )
            else:
                errors["base"] = "device_not_found"
        
        # Create a dictionary of device serial numbers to names for the selector
        device_options = {
            device["serial_number"]: f"{device['name']} ({device['device_type']})"
            for device in self._devices
        }
        
        # Define form schema for device selection
        data_schema = vol.Schema({
            vol.Required("device"): vol.In(device_options),
            vol.Optional("name"): str,
        })
        
        return self.async_show_form(
            step_id="select_device",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": "Select which device to add."
            }
        )
