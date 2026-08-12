"""Button entities for iaqualink_robots integration."""
from __future__ import annotations

import logging

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import AqualinkClient
from .const import DOMAIN
from .coordinator import AqualinkDataUpdateCoordinator
from .device import build_device_info
from .types import IaqualinkConfigEntry

_LOGGER = logging.getLogger(__name__)

# All state comes from the coordinator, and the cloud tolerates the
# remote-control commands being issued back-to-back (they are what the
# iAquaLink app's jog buttons do). No serialisation needed.
PARALLEL_UPDATES = 0


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
        # Icons are no longer passed here: they live in icons.json keyed by
        # translation_key, which is the HA convention (and the `icon-
        # translations` quality-scale rule). A user overriding an icon in the
        # UI now sticks, instead of being re-applied from code on restart.
        buttons = [
            AqualinkRemoteButton(coordinator, client, "forward", "remote_forward"),
            AqualinkRemoteButton(coordinator, client, "backward", "remote_backward"),
            AqualinkRemoteButton(coordinator, client, "rotate_left", "remote_rotate_left"),
            AqualinkRemoteButton(coordinator, client, "rotate_right", "remote_rotate_right"),
            AqualinkRemoteButton(coordinator, client, "stop", "remote_stop"),
            AqualinkRemoteButton(coordinator, client, "add_fifteen_minutes", "add_fifteen_minutes"),
            AqualinkRemoteButton(coordinator, client, "reduce_fifteen_minutes", "reduce_fifteen_minutes"),
        ]
        async_add_entities(buttons)


class AqualinkRemoteButton(CoordinatorEntity[AqualinkDataUpdateCoordinator], ButtonEntity):
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

    def __init__(
        self,
        coordinator: AqualinkDataUpdateCoordinator,
        client: AqualinkClient,
        command: str,
        translation_key: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self._client = client
        self._command = command

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
    def device_info(self) -> DeviceInfo:
        """Defer to the shared device-registry hook so all platforms group together."""
        return build_device_info(self.coordinator)

    @property
    def available(self) -> bool:
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
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the ``restored`` flag (H7, AC #3).

        ``True`` whenever the button's last coordinator update came from
        cached ``_last_data`` rather than a fresh poll. Useful for users
        who want to inspect via ``state_attr`` whether a button press is
        being issued against a robot whose status the integration last
        observed minutes ago.
        """
        return {"restored": self.coordinator.is_serving_stale_data}

    # Commands whose effect on the reported cycle time is immediate; these get
    # one coordinator refresh so the time-remaining sensors don't lag by up to
    # a full poll interval. The movement commands deliberately do NOT refresh —
    # the 3s SCAN_INTERVAL already covers them and an extra refresh per press
    # made rapid jog-button use hammer the cloud.
    _REFRESH_AFTER = frozenset({"add_fifteen_minutes", "reduce_fifteen_minutes"})

    async def async_press(self) -> None:
        """Handle the button press.

        Failures are raised as ``HomeAssistantError`` rather than logged and
        swallowed. Pre-fix a command that never reached the robot still
        rendered as a successful press in the UI, so the user had no signal
        that anything went wrong short of reading the log.
        """
        handlers = {
            "forward": self._client.remote_forward,
            "backward": self._client.remote_backward,
            "rotate_left": self._client.remote_rotate_left,
            "rotate_right": self._client.remote_rotate_right,
            "stop": self._client.remote_stop,
            "add_fifteen_minutes": self._client.add_fifteen_minutes,
            "reduce_fifteen_minutes": self._client.reduce_fifteen_minutes,
        }
        handler = handlers.get(self._command)
        if handler is None:
            # Unreachable via async_setup_entry, which only builds buttons from
            # the literal list above. Guard anyway so a future typo surfaces
            # as a clear error instead of a silent no-op press.
            raise HomeAssistantError(f"Unknown button command {self._command!r}")

        _LOGGER.debug(
            "Button '%s' pressed for robot %s", self._command, self._client.robot_name
        )
        try:
            await handler()
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.debug(
                "Button command %s failed for %s",
                self._command, self._client.robot_name, exc_info=True,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_failed",
                translation_placeholders={
                    "command": self._command,
                    "name": str(self._client.robot_name),
                    "error": str(err),
                },
            ) from err

        if self._command in self._REFRESH_AFTER:
            await self.coordinator.async_request_refresh()
