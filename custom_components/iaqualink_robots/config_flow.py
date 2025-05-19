from homeassistant import config_entries
from homeassistant.core import callback
import voluptuous as vol

from .const import DOMAIN


class IaqualinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            # Optional: Add validation here (e.g., API login test)
            return self.async_create_entry(
                title=user_input["name"],
                data=user_input,
            )

        # Define form schema
        data_schema = vol.Schema({
            vol.Required("name", default="My Pool Robot"): str,
            vol.Required("username"): str,
            vol.Required("password"): str,
            vol.Required("api_key"): str,
        })

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                "info": "Enter your iAquaLink account credentials and API key."
            }
        )