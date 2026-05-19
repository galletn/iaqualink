"""Button entities for iaqualink_robots integration."""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import AqualinkDataUpdateCoordinator
from .device import build_device_info
from .types import IaqualinkConfigEntry

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: IaqualinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button entities."""
    coordinator = config_entry.runtime_data.coordinator
    client = coordinator.client

    # Only create remote control buttons for VR and VortraX robots
    if client.device_type in ["vr", "vortrax"]:
        buttons = [
            AqualinkRemoteButton(coordinator, client, "forward", "remote_forward", "mdi:chevron-up-circle"),
            AqualinkRemoteButton(coordinator, client, "backward", "remote_backward", "mdi:chevron-down-circle"),
            AqualinkRemoteButton(coordinator, client, "rotate_left", "remote_rotate_left", "mdi:rotate-left"),
            AqualinkRemoteButton(coordinator, client, "rotate_right", "remote_rotate_right", "mdi:rotate-right"),
            AqualinkRemoteButton(coordinator, client, "stop", "remote_stop", "mdi:stop-circle-outline"),
            AqualinkRemoteButton(coordinator, client, "add_fifteen_minutes",
                                 "add_fifteen_minutes", "mdi:clock-plus-outline"),
            AqualinkRemoteButton(coordinator, client, "reduce_fifteen_minutes",
                                 "reduce_fifteen_minutes", "mdi:clock-minus-outline"),
        ]
        async_add_entities(buttons)


class AqualinkRemoteButton(CoordinatorEntity, ButtonEntity):
    """Button entity for remote control commands."""

    # P11: HA 2024+ entity-naming. With these two class attributes plus
    # `_attr_translation_key` set in __init__, HA looks up the localized
    # button name at `entity.button.<translation_key>.name` from the
    # translations files and composes the friendly name as
    # `<device-name> <localized-button-name>`. All 9 locales already carry
    # native translations under `entity.button.*` (they were dead before
    # P11 because the translation_key assignment was commented out and a
    # hardcoded English `name` property took precedence). Closes #79 for
    # the button half of the user complaint.
    _attr_has_entity_name = True

    def __init__(self, coordinator: AqualinkDataUpdateCoordinator, client, command: str, translation_key: str, icon: str):
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._command = command
        self._attr_icon = icon

        # Derive unique_id from the robot's serial number. The previous title-based
        # derivation (entry.title lowercased + underscores) broke whenever the user
        # renamed the entry — every rename forked a new entity in the registry.
        # Migration of pre-M12 unique_ids lives in __init__.async_migrate_entry.
        self._attr_unique_id = f"{client.serial}_{command}"
        self._attr_should_poll = False

        # P11: route the friendly name through HA's translation system instead
        # of the legacy hardcoded-English `name` property.
        self._attr_translation_key = translation_key

    @property
    def device_info(self):
        """Defer to the shared device-registry hook so all platforms group together."""
        return build_device_info(self.coordinator)

    @property
    def available(self):
        """Button stays available across short cloud blips, unavailable on long outage.

        H7: pre-rewrite this only checked ``coordinator.data is not None``,
        so buttons stayed available indefinitely while the coordinator
        served stale ``_last_data``. The new ``coordinator.is_long_outage``
        property flips to True once the outage exceeds
        ``LONG_OUTAGE_THRESHOLD_SECONDS``, so a pressed button hitting a
        dead cloud now surfaces as the entity being unavailable rather
        than as a silent no-op command.
        """
        return (
            self.coordinator.data is not None
            and not self.coordinator.is_long_outage
        )

    @property
    def extra_state_attributes(self):
        """Expose the ``restored`` flag (H7, AC #3).

        ``True`` whenever the button's last coordinator update came from
        cached ``_last_data`` rather than a fresh poll. Useful for users
        who want to inspect via ``state_attr`` whether a button press is
        being issued against a robot whose status the integration last
        observed minutes ago.
        """
        return {"restored": self.coordinator.is_serving_stale_data}

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
            _LOGGER.info(f"Button '{self._command}' pressed for robot {self._client.robot_name}")

            if self._command == "forward":
                await self._client.remote_forward()
                _LOGGER.info(f"Remote forward command sent to {self._client.robot_name}")
            elif self._command == "backward":
                await self._client.remote_backward()
                _LOGGER.info(f"Remote backward command sent to {self._client.robot_name}")
            elif self._command == "rotate_left":
                await self._client.remote_rotate_left()
                _LOGGER.info(f"Remote rotate left command sent to {self._client.robot_name}")
            elif self._command == "rotate_right":
                await self._client.remote_rotate_right()
                _LOGGER.info(f"Remote rotate right command sent to {self._client.robot_name}")
            elif self._command == "stop":
                await self._client.remote_stop()
                _LOGGER.info(f"Remote stop command sent to {self._client.robot_name}")
            elif self._command == "add_fifteen_minutes":
                _LOGGER.info(f"About to send add 15 minutes command to {self._client.robot_name}")
                response = await self._client.add_fifteen_minutes()
                _LOGGER.info(f"Add 15 minutes command sent to {self._client.robot_name}, response: {response}")

                # Only request coordinator refresh for timing commands that affect state
                if hasattr(self.coordinator, 'async_request_refresh'):
                    _LOGGER.info("Requesting coordinator refresh after add 15 minutes")
                    await self.coordinator.async_request_refresh()

            elif self._command == "reduce_fifteen_minutes":
                _LOGGER.info(f"About to send reduce 15 minutes command to {self._client.robot_name}")
                response = await self._client.reduce_fifteen_minutes()
                _LOGGER.info(f"Reduce 15 minutes command sent to {self._client.robot_name}, response: {response}")

                # Only request coordinator refresh for timing commands that affect state
                if hasattr(self.coordinator, 'async_request_refresh'):
                    _LOGGER.info("Requesting coordinator refresh after reduce 15 minutes")
                    await self.coordinator.async_request_refresh()

        except Exception as e:
            _LOGGER.error(f"Failed to send {self._command} command to {self._client.robot_name}: {e}", exc_info=True)
