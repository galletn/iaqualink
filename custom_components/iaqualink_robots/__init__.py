import asyncio
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS, API_KEY

_LOGGER = logging.getLogger(__name__)

# Commands previously used to suffix button unique_ids. Kept here (not imported
# from button.py) so the migration is self-contained and doesn't break if
# button.py renames a command later — old user registries will still match.
_M12_BUTTON_COMMANDS = (
    "forward",
    "backward",
    "rotate_left",
    "rotate_right",
    "stop",
    "add_fifteen_minutes",
    "reduce_fifteen_minutes",
)


async def async_setup(hass: HomeAssistant, config) -> bool:
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current schema.

    Version history:
        1 -> 2  (M12) Rewrite button unique_ids from title-derived (mutable,
                broke on rename) to serial-based. See story-M12 for context.
    """
    _LOGGER.debug("Migrating config entry from version %s", entry.version)

    if entry.version == 1:
        serial = entry.data.get("serial_number")
        if not serial:
            _LOGGER.warning(
                "Cannot migrate entry %s to v2: no serial_number in entry.data. "
                "Skipping button unique_id rewrite; the entry will continue working "
                "with legacy unique_ids until re-added.",
                entry.entry_id,
            )
        else:
            registry = er.async_get(hass)
            updates = 0
            for entity_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
                if entity_entry.domain != "button":
                    continue
                # Already migrated (unique_id starts with the serial)? Skip.
                if entity_entry.unique_id.startswith(f"{serial}_"):
                    continue
                # Find the command suffix the legacy unique_id ends with and
                # rebuild as `<serial>_<command>`.
                for cmd in _M12_BUTTON_COMMANDS:
                    if entity_entry.unique_id.endswith(f"_{cmd}"):
                        new_uid = f"{serial}_{cmd}"
                        _LOGGER.info(
                            "M12 migration: %s unique_id %r -> %r",
                            entity_entry.entity_id, entity_entry.unique_id, new_uid,
                        )
                        registry.async_update_entity(
                            entity_entry.entity_id, new_unique_id=new_uid,
                        )
                        updates += 1
                        break
            _LOGGER.debug("M12 migration rewrote %s button unique_ids", updates)

        hass.config_entries.async_update_entry(entry, version=2)

    _LOGGER.debug("Migration to version %s complete", entry.version)
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
    # Debug mode disabled by default to reduce log verbosity
    # To enable debug mode for troubleshooting, change False to True below
    debug_mode = False
    client = AqualinkClient(username, password, API_KEY, debug_mode=debug_mode)

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
    coordinator = AqualinkDataUpdateCoordinator(hass, client, SCAN_INTERVAL.total_seconds(), debug_mode=debug_mode)
    coordinator._title = entry.title  # type: ignore  # Dynamic attribute used for display name

    # Initial refresh to get data
    await coordinator.async_config_entry_first_refresh()

    # Store client and coordinator in hass.data
    hass.data[DOMAIN][entry.entry_id]["client"] = client
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Clean up resources before unloading
    if entry.entry_id in hass.data[DOMAIN]:
        entry_data = hass.data[DOMAIN][entry.entry_id]
        if "coordinator" in entry_data:
            coordinator = entry_data["coordinator"]
            try:
                # Clean up any persistent connections
                await coordinator.cleanup()
            except Exception as e:
                _LOGGER.warning(f"Error during coordinator cleanup: {e}")

    # Unload the platforms
    unload_ok = await asyncio.gather(
        *[
            hass.config_entries.async_forward_entry_unload(entry, platform)
            for platform in PLATFORMS
        ]
    )
    if all(unload_ok):
        hass.data[DOMAIN].pop(entry.entry_id)
    return all(unload_ok)
