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
            AqualinkRemoteButton(coordinator, client, "forward", "remote_forward", "mdi:chevron-up-circle"),
            AqualinkRemoteButton(coordinator, client, "backward", "remote_backward", "mdi:chevron-down-circle"),
            AqualinkRemoteButton(coordinator, client, "rotate_left", "remote_rotate_left", "mdi:rotate-left"),
            AqualinkRemoteButton(coordinator, client, "rotate_right", "remote_rotate_right", "mdi:rotate-right"),
            AqualinkRemoteButton(coordinator, client, "stop", "remote_stop", "mdi:stop-circle-outline"),
            AqualinkRemoteButton(coordinator, client, "add_fifteen_minutes", "add_fifteen_minutes", "mdi:clock-plus-outline"),
            AqualinkRemoteButton(coordinator, client, "reduce_fifteen_minutes", "reduce_fifteen_minutes", "mdi:clock-minus-outline"),
        ]
        async_add_entities(buttons)


class AqualinkRemoteButton(CoordinatorEntity, ButtonEntity):
    """Button entity for remote control commands."""

    def __init__(self, coordinator: AqualinkDataUpdateCoordinator, client, command: str, translation_key: str, icon: str):
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._command = command
        self._attr_icon = icon
        
        # Use coordinator title (entry.title) for entity ID, same as vacuum device_name
        # This should give us "bobby" if that's the entry title
        title = getattr(coordinator, "_title", None)
        if title:
            # Clean the title for use as entity ID (lowercase, replace spaces with underscores)
            device_name = title.lower().replace(" ", "_")
        else:
            # Fallback to robot_id if no title
            device_name = client.robot_id
            
        self._attr_unique_id = f"{device_name}_{command}"
        self._attr_should_poll = False
        
        # Set proper button names - store the name to prevent override
        self._button_name = self._get_button_name(translation_key)
        
        # Don't set translation_key if we want custom names to persist
        # self._attr_translation_key = translation_key
        
        self._attr_device_info = {
            "identifiers": {(DOMAIN, client.robot_id)},
            "name": client.robot_name,
            "manufacturer": "Zodiac",
            "model": getattr(client, '_model', 'Unknown'),
            "sw_version": "1.0",
        }

    @property
    def name(self) -> str:
        """Return the name of the button."""
        return self._button_name

    @property  
    def has_entity_name(self) -> bool:
        """Return True if entity has a name."""
        return True

    def _get_button_name(self, translation_key: str) -> str:
        """Get the proper button name based on translation key."""
        name_map = {
            "remote_forward": "Remote Forward",
            "remote_backward": "Remote Backward", 
            "remote_rotate_left": "Remote Rotate Left",
            "remote_rotate_right": "Remote Rotate Right",
            "remote_stop": "Remote Stop",
            "add_fifteen_minutes": "Add 15 Minutes",
            "reduce_fifteen_minutes": "Reduce 15 Minutes"
        }
        return name_map.get(translation_key, translation_key.replace("_", " ").title())

    @property
    def available(self):
        """Return if entity is available."""
        # Keep buttons available as long as we have coordinator data, same as sensors
        # This prevents buttons from going unavailable during temporary connection issues
        return self.coordinator.data is not None

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
