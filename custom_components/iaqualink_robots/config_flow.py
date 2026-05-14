import logging
from homeassistant import config_entries, data_entry_flow
import voluptuous as vol

from .const import DOMAIN, API_KEY
from .coordinator import AqualinkClient

_LOGGER = logging.getLogger(__name__)


class IaqualinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    # Version bumps trigger `async_migrate_entry` in __init__.py:
    #   1 -> 2  (M12) rewrite button unique_ids from title-derived to serial-based
    #   2 -> 3  (M17) drop the dead `api_key` field from entry.data
    VERSION = 3
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        """Initialize the config flow."""
        self._username = None
        self._password = None
        self._devices = []

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            self._username = user_input["username"]
            self._password = user_input["password"]

            # Try to discover devices with these credentials
            try:
                self._devices = await AqualinkClient.discover_devices(
                    self._username, self._password, API_KEY
                )

                if not self._devices:
                    errors["base"] = "no_devices"
                else:
                    # If only one device is found, skip the selection step
                    if len(self._devices) == 1:
                        device = self._devices[0]
                        serial = device.get("serial_number")
                        # M19 AC#1: reject non-string serial types (int/list/dict)
                        # before they can reach async_set_unique_id, which would
                        # otherwise raise an opaque TypeError deep inside HA.
                        if serial is not None and not isinstance(serial, str):
                            return self.async_abort(reason="no_serial")
                        if isinstance(serial, str):
                            serial = serial.strip()
                        if not serial:
                            return self.async_abort(reason="no_serial")
                        await self.async_set_unique_id(serial)
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=user_input.get("name", device["name"]),
                            data={
                                "name": user_input.get("name", device["name"]),
                                "username": self._username,
                                "password": self._password,
                                "serial_number": serial,
                                "device_type": device["device_type"]
                            },
                        )
                    # If multiple devices are found, go to the device selection step
                    return await self.async_step_select_device()
            except data_entry_flow.AbortFlow:
                # Re-raise abort signals (e.g., already_configured) so HA can
                # surface them properly. Without this, the broad Exception
                # handler below would swallow them and show a misleading
                # "cannot_connect" error.
                raise
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
                serial = selected_device.get("serial_number")
                # M19 AC#1: reject non-string serial types (int/list/dict)
                # before they can reach async_set_unique_id.
                if serial is not None and not isinstance(serial, str):
                    return self.async_abort(reason="no_serial")
                if isinstance(serial, str):
                    serial = serial.strip()
                if not serial:
                    return self.async_abort(reason="no_serial")
                # M19 AC#2: use .get() with the device_not_found fallback so a
                # missing `name`/`device_type` key surfaces the same error the
                # `else` branch shows rather than raising KeyError.
                device_name = selected_device.get("name")
                device_type = selected_device.get("device_type")
                if not device_name or not device_type:
                    errors["base"] = "device_not_found"
                else:
                    # M19 AC#3: explicit AbortFlow re-raise for defensive
                    # symmetry with async_step_user. No broad Exception
                    # handler exists here today, so AbortFlow already
                    # propagates naturally — this guards against a future
                    # PR adding a catch-all that would otherwise swallow
                    # `already_configured` and similar abort reasons.
                    try:
                        await self.async_set_unique_id(serial)
                        self._abort_if_unique_id_configured()
                        return self.async_create_entry(
                            title=user_input.get("name", device_name),
                            data={
                                "name": user_input.get("name", device_name),
                                "username": self._username,
                                "password": self._password,
                                "serial_number": serial,
                                "device_type": device_type,
                            },
                        )
                    except data_entry_flow.AbortFlow:
                        raise
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
        )
