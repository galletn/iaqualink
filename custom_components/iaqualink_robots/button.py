"""Button entities for iaqualinkRobots integration."""
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AqualinkDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    client = hass.data[DOMAIN][config_entry.entry_id]["client"]
    
    # Only create remote control buttons for VR and VortraX robots
    if client._device_type in ["vr", "vortrax"]:
        buttons = [
            AqualinkRemoteButton(coordinator, client, "forward", "Remote Forward", "mdi:chevron-up-circle"),
            AqualinkRemoteButton(coordinator, client, "backward", "Remote Backward", "mdi:chevron-down-circle"),
            AqualinkRemoteButton(coordinator, client, "rotate_left", "Remote Rotate Left", "mdi:rotate-left"),
            AqualinkRemoteButton(coordinator, client, "rotate_right", "Remote Rotate Right", "mdi:rotate-right"),
            AqualinkRemoteButton(coordinator, client, "stop", "Remote Stop", "mdi:stop-circle-outline"),
        ]
        async_add_entities(buttons)


class AqualinkRemoteButton(CoordinatorEntity, ButtonEntity):
    """Button entity for remote control commands."""

    def __init__(self, coordinator: AqualinkDataUpdateCoordinator, client, command: str, name: str, icon: str):
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._command = command
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{client.robot_id}_remote_{command}"
        self._attr_should_poll = False
        self._attr_device_info = {
            "identifiers": {(DOMAIN, client.robot_id)},
            "name": client.robot_name,
            "manufacturer": "Zodiac",
            "model": getattr(client, '_model', 'Unknown'),
            "sw_version": "1.0",
        }

    @property
    def available(self):
        """Return if entity is available."""
        # Button is available if coordinator is available and device is connected
        return (
            self.coordinator.last_update_success
            and self.coordinator.data
            and self.coordinator.data.get("status") == "connected"
        )

    async def async_press(self) -> None:
        """Handle the button press."""
        try:
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
        except Exception as e:
            _LOGGER.error(f"Failed to send {self._command} command to {self._client.robot_name}: {e}")
