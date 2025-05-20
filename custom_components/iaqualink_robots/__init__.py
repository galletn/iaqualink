import asyncio
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS

async def async_setup(hass: HomeAssistant, config) -> bool:
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}
    from .coordinator import AqualinkClient, AqualinkDataUpdateCoordinator
    from .const import SCAN_INTERVAL

    client = AqualinkClient(entry.data["username"], entry.data["password"], entry.data["api_key"])
    coordinator = AqualinkDataUpdateCoordinator(hass, client, SCAN_INTERVAL.total_seconds())
    coordinator.title = entry.title
    await coordinator.async_config_entry_first_refresh()
    hass.data[DOMAIN][entry.entry_id]["client"] = client
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await asyncio.gather(
        *[
            hass.config_entries.async_forward_entry_unload(entry, platform)
            for platform in PLATFORMS
        ]
    )
    if all(unload_ok):
        hass.data[DOMAIN].pop(entry.entry_id)
    return all(unload_ok)
