import logging
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import callback
import voluptuous as vol

from .const import (
    API_KEY,
    CONF_INCLUDE_SECONDS_REMAINING,
    DEFAULT_INCLUDE_SECONDS_REMAINING,
    DOMAIN,
)
from .coordinator import AqualinkClient, AuthFailedError

_LOGGER = logging.getLogger(__name__)


class IaqualinkConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    # Version bumps trigger `async_migrate_entry` in __init__.py:
    #   1 -> 2  (M12) rewrite button unique_ids from title-derived to serial-based
    #   2 -> 3  (M17) drop the dead `api_key` field from entry.data
    VERSION = 3
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the OptionsFlow for runtime toggles."""
        return IaqualinkOptionsFlow(config_entry)

    def __init__(self):
        """Initialize the config flow."""
        self._username = None
        self._password = None
        self._devices = []
        self._include_seconds_remaining = DEFAULT_INCLUDE_SECONDS_REMAINING
        # Set by async_step_reauth, consumed by async_step_reauth_confirm. P4.
        self._reauth_entry = None

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            self._username = user_input["username"]
            self._password = user_input["password"]
            self._include_seconds_remaining = user_input.get(
                CONF_INCLUDE_SECONDS_REMAINING, DEFAULT_INCLUDE_SECONDS_REMAINING
            )

            # C6 (refined per post-implementation review): identify entries
            # already configured for this account so we can dedupe by serial
            # after discovery, instead of aborting before it. The earlier
            # pre-discovery abort broke multi-robot-per-account: a user with
            # one robot configured could not add a second robot on the same
            # iAqualink account. The refined design preserves the spec's
            # original "the existing C2 logic stays" intent for multi-robot
            # users — we still skip the spurious entry-create for re-adds of
            # the same robot, just one Cognito round-trip later.
            #
            # Username comparison is case- and whitespace-normalized because
            # Cognito treats email-style usernames case-insensitively and
            # mobile autocomplete commonly trails a space. `include_ignore`
            # is False so users who once "ignored" this integration are not
            # permanently locked out.
            normalized_username = self._username.strip().casefold()
            existing_serials_for_account = {
                entry.data["serial_number"]
                for entry in self._async_current_entries(include_ignore=False)
                if (entry.data.get("username") or "").strip().casefold()
                == normalized_username
                and entry.data.get("serial_number")
            }

            # Try to discover devices with these credentials
            try:
                self._devices = await AqualinkClient.discover_devices(
                    self._username, self._password, API_KEY
                )

                # C6 filter: drop devices already configured for this account.
                # If every discovered device is already configured, abort —
                # this is the re-add-same-account case the original C6 was
                # targeting. If at least one is new, fall through to the
                # standard single-device / select_device path on the filtered
                # set so the user only sees devices they don't already have.
                if existing_serials_for_account:
                    self._devices = [
                        d for d in self._devices
                        if d.get("serial_number") not in existing_serials_for_account
                    ]
                    if not self._devices:
                        return self.async_abort(reason="already_configured")

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
                                "device_type": device["device_type"],
                                CONF_INCLUDE_SECONDS_REMAINING: self._include_seconds_remaining,
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
            except AuthFailedError as e:
                # H9b review P2: distinguish credential failures from network
                # failures. The cloud's 401 response means the username or
                # password is wrong, not that we couldn't reach the server.
                _LOGGER.debug(f"Auth failed during discovery: {e}")
                errors["base"] = "invalid_auth"
            except Exception as e:
                _LOGGER.error(f"Error discovering devices: {e}")
                errors["base"] = "cannot_connect"

        # Define form schema. C6 review P3: enforce min length 1 on username
        # so an empty submit fails the form schema instead of probing
        # existing entries with an empty string and accidentally matching
        # a corrupted entry whose stored username is also empty.
        data_schema = vol.Schema({
            vol.Required("name", default="My Pool Robot"): str,
            vol.Required("username"): vol.All(str, vol.Length(min=1)),
            vol.Required("password"): str,
            vol.Optional(
                CONF_INCLUDE_SECONDS_REMAINING,
                default=DEFAULT_INCLUDE_SECONDS_REMAINING,
            ): bool,
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
                                CONF_INCLUDE_SECONDS_REMAINING: self._include_seconds_remaining,
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

    # ------------------------------------------------------------------
    # Reauthentication flow (story P4, bundled with H9b per the spec's
    # `Dependencies: [P4] lands in same PR pair` line).
    #
    # Triggered when the coordinator raises `ConfigEntryAuthFailed`.
    # Shows the user a password form (email is read-only), validates the
    # new credentials by attempting `discover_devices` once, and on
    # success updates the existing entry in place — preserving the
    # entry's `unique_id` and every entity_id / area / automation
    # referencing it. No history loss, no re-add.
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data):
        """Entry point invoked by HA when ConfigEntryAuthFailed fires."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Prompt the user for a new password and re-validate credentials."""
        errors = {}
        # Sign-off P2: defensive `is None` check instead of `assert`. Under
        # `python -O` assertions are stripped, and an HA dispatch that lost
        # `entry_id` (or pointed at a deleted entry) would have crashed on
        # `self._reauth_entry.data["username"]` with AttributeError. Abort
        # cleanly instead.
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_unavailable")

        if user_input is not None:
            new_password = user_input["password"]
            try:
                # Re-run discovery with the new password — same validation
                # path as initial setup. Cheap (single HTTP call) and proves
                # the credentials reach Cognito.
                await AqualinkClient.discover_devices(
                    self._reauth_entry.data["username"],
                    new_password,
                    API_KEY,
                )
            except AuthFailedError:
                errors["base"] = "invalid_auth"
            except Exception as e:  # noqa: BLE001 — surface as cannot_connect
                _LOGGER.error(f"Error during reauth discovery: {e}")
                errors["base"] = "cannot_connect"
            else:
                # Update password in place. unique_id, entity_id, area,
                # automations, history all preserved.
                self.hass.config_entries.async_update_entry(
                    self._reauth_entry,
                    data={**self._reauth_entry.data, "password": new_password},
                )
                # Sign-off P6: reload is best-effort. If it raises we've
                # already updated the entry data, so HA will retry on the
                # next coordinator cycle. Surface the failure as a WARN so
                # operators can see it without blocking the user-visible
                # success message.
                try:
                    await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
                except Exception as reload_err:  # noqa: BLE001
                    _LOGGER.warning(
                        "Reauth succeeded but reload failed (%s); HA will "
                        "retry on the next update cycle",
                        reload_err,
                    )
                return self.async_abort(reason="reauth_successful")

        return self.async_show_form(
            step_id="reauth_confirm",
            # Sign-off P4: enforce min length 1 locally so an empty submit
            # fails the form schema instead of round-tripping to Cognito.
            data_schema=vol.Schema({
                vol.Required("password"): vol.All(str, vol.Length(min=1)),
            }),
            errors=errors,
            description_placeholders={
                "username": self._reauth_entry.data["username"],
            },
        )


class IaqualinkOptionsFlow(config_entries.OptionsFlow):
    """Runtime-togglable options for an existing entry.

    Currently exposes one knob: whether `time_remaining_human` includes
    seconds (story T1). Toggling reloads the integration so the coordinator
    picks up the new setting.
    """

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_INCLUDE_SECONDS_REMAINING,
            self.config_entry.data.get(
                CONF_INCLUDE_SECONDS_REMAINING, DEFAULT_INCLUDE_SECONDS_REMAINING
            ),
        )
        data_schema = vol.Schema({
            vol.Optional(CONF_INCLUDE_SECONDS_REMAINING, default=current): bool,
        })
        return self.async_show_form(step_id="init", data_schema=data_schema)
