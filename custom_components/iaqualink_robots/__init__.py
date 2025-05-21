import asyncio
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS, API_KEY

_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config) -> bool:
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}
    from .coordinator import AqualinkClient, AqualinkDataUpdateCoordinator
    from .const import SCAN_INTERVAL

    # Get credentials from entry
    username = entry.data["username"]
    password = entry.data["password"]
    
    # Check if a specific serial number is specified in the entry
    target_serial = entry.data.get("serial_number")
    
    # Create a client for this specific device using the constant API key
    client = AqualinkClient(username, password, API_KEY)
    
    # If a specific serial is provided, use it for device discovery
    if target_serial:
        # Authenticate first
        await client._authenticate()
        # Then discover the specific device
        try:
            await client._discover_device(target_serial=target_serial)
        except RuntimeError as e:
            _LOGGER.error(f"Failed to discover device {target_serial}: {e}")
            return False
    
    # Create coordinator with this client
    coordinator = AqualinkDataUpdateCoordinator(hass, client, SCAN_INTERVAL.total_seconds())
    coordinator._title = entry.title
    
    # Initial refresh to get data
    await coordinator.async_config_entry_first_refresh()
    
    # Store client and coordinator in hass.data
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
