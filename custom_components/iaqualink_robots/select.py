"""Cleaning-pattern selects for iaqualink_robots.

The cloud carries a navigation *pattern* per cleaning cycle at
``equipment.robot.cycles.<cycleName>.route`` — "Standard" or "S-Pattern" in
the iAquaLink app. It is a discrete named choice, which is why this is a
``select`` rather than a fan speed or a pair of buttons.

Both directions are verified against live hardware (cyclobat, firmware
V22E3.25.3): reading matches what the app displays, and a write of
``setCleaningMode`` with the ``cycles.<cycleKey>.route`` nesting changed the
value, was echoed back by the cloud over ``StateStreamer``, and was restored
by writing the original value.

Entities are created only for cycles that actually report a ``route``, so a
robot without pattern support gets no select at all rather than a control
that silently does nothing — the failure mode issue #76 was about.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .client import AqualinkClient
from .const import (
    PATTERN_BY_ROUTE,
    PATTERN_CYCLE_NAMES,
    PATTERN_FEATURE,
    ROUTE_BY_PATTERN,
)
from .coordinator import AqualinkDataUpdateCoordinator
from .device import build_device_info
from .types import IaqualinkConfigEntry

_LOGGER = logging.getLogger(__name__)

# State comes from the coordinator; no per-entity polling to serialise.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IaqualinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one pattern select per cycle that supports one.

    Gated on the cloud's ``quick_snake_pattern`` capability rather than on
    device type: no vr or cyclonext payload captured so far contains a
    ``route`` key, so this looks cyclobat-only today, but the feature flag is
    the cloud's own statement and is the safer thing to trust. Both gates must
    agree — the flag AND a real ``route`` value — so a firmware that advertises
    the feature without reporting the field yields no half-working entity.
    """
    coordinator = entry.runtime_data.coordinator
    client = coordinator.client

    features = set(getattr(client, "features", []) or [])
    if PATTERN_FEATURE not in features:
        _LOGGER.debug(
            "Device does not advertise %s; no pattern selects created",
            PATTERN_FEATURE,
        )
        return

    data = coordinator.data or {}
    patterns: dict[str, Any] = data.get("cleaning_patterns") or {}
    if not patterns:
        _LOGGER.debug(
            "Device advertises %s but reported no cycle 'route' values; "
            "no pattern selects created", PATTERN_FEATURE,
        )
        return

    entities = [
        AqualinkPatternSelect(coordinator, client, cycle_key)
        for cycle_key in sorted(patterns)
    ]
    _LOGGER.debug(
        "Creating %d cleaning-pattern select(s): %s",
        len(entities), sorted(patterns),
    )
    async_add_entities(entities)


class AqualinkPatternSelect(
    CoordinatorEntity[AqualinkDataUpdateCoordinator], SelectEntity
):
    """The navigation pattern for one cleaning cycle."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: AqualinkDataUpdateCoordinator,
        client: AqualinkClient,
        cycle_key: str,
    ) -> None:
        super().__init__(coordinator)
        self.coordinator: AqualinkDataUpdateCoordinator = coordinator
        self.client = client
        self._cycle_key = cycle_key

        # Fall back to the raw cloud key for a cycle we haven't named, so a
        # new cycle type degrades to a working-but-untranslated entity rather
        # than being dropped.
        sub_name = PATTERN_CYCLE_NAMES.get(cycle_key, cycle_key.lower())

        self._attr_unique_id = f"{client.serial}_pattern_{sub_name}"
        self._attr_translation_key = f"pattern_{sub_name}"
        self._attr_options = list(ROUTE_BY_PATTERN)
        self._attr_should_poll = False

    @property
    def device_info(self) -> DeviceInfo:
        """Group with every other platform on one device card.

        Takes the coordinator (not the client) — the helper resolves the
        device name from ``coordinator.title``, so passing the client would
        produce a differently-named second card.
        """
        return build_device_info(self.coordinator)

    @property
    def current_option(self) -> str | None:
        """Return the active pattern, or ``None`` if the cloud stops sending it.

        ``None`` (rendered "Unknown") is deliberate for an unrecognised
        ``route``: inventing a third option name, or silently showing
        "Standard", would misreport the robot's actual state.
        """
        patterns = (self.coordinator.data or {}).get("cleaning_patterns") or {}
        route = patterns.get(self._cycle_key)
        if route is None:
            return None
        option = PATTERN_BY_ROUTE.get(route)
        if option is None:
            _LOGGER.warning(
                "Unrecognised cleaning-pattern route %r for cycle %s — "
                "please report it so it can be mapped",
                route, self._cycle_key,
            )
        return option

    @property
    def available(self) -> bool:
        """Unavailable while the coordinator has no pattern for this cycle."""
        if not super().available:
            return False
        patterns = (self.coordinator.data or {}).get("cleaning_patterns") or {}
        return self._cycle_key in patterns

    async def async_select_option(self, option: str) -> None:
        """Write the chosen pattern to the cloud."""
        route = ROUTE_BY_PATTERN.get(option)
        if route is None:
            # HA validates against _attr_options, so this is defensive only.
            _LOGGER.warning("Ignoring unknown cleaning pattern %r", option)
            return

        _LOGGER.debug(
            "Setting %s pattern to %s (route=%s)",
            self._cycle_key, option, route,
        )
        await self.client.set_cleaning_pattern(self._cycle_key, route)
        # The cloud echoes the change back over StateStreamer, which the
        # websocket listener applies — so no optimistic local write here.
        await self.coordinator.async_request_refresh()
