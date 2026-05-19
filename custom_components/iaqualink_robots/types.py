"""Typed ConfigEntry runtime-data shape for iaqualink_robots (story P3).

Home Assistant's 2024+ convention stashes per-entry runtime state on
``ConfigEntry.runtime_data`` instead of the legacy per-domain dict in
``hass.data``. The typed alias ``IaqualinkConfigEntry = ConfigEntry[IaqualinkData]``
makes ``entry.runtime_data.coordinator`` type-checkable end-to-end —
``mypy --strict`` (story P6) sees the concrete attribute instead of
the ``Any`` ``hass.data`` would surface.

The ``coordinator`` field is the only one stored. The client is
reachable as ``coordinator.client`` (a public property post-M15), so
duplicating it here would be a contract violation. Future per-entry
state can be added as additional dataclass fields without touching the
read sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from .coordinator import AqualinkDataUpdateCoordinator


@dataclass
class IaqualinkData:
    """Per-entry runtime state stored on ``ConfigEntry.runtime_data``."""

    coordinator: AqualinkDataUpdateCoordinator


type IaqualinkConfigEntry = ConfigEntry[IaqualinkData]
