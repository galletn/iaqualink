import asyncio
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .const import (
    API_KEY,
    CONF_INCLUDE_SECONDS_REMAINING,
    DEFAULT_INCLUDE_SECONDS_REMAINING,
    DOMAIN,
    PLATFORMS,
)

_LOGGER = logging.getLogger(__name__)

# Hassfest requires integrations that define `async_setup` to declare a
# CONFIG_SCHEMA. This integration is config-entry-only (no YAML), so the
# helper `cv.config_entry_only_config_schema` is the HA-recommended shape.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

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
                broke on rename) to serial-based. See story-M12.
        2 -> 3  (M17) Drop the dead `api_key` field from entry.data; the
                integration uses the const API_KEY directly. See story-M17.

    M19 AC#10: each version block is wrapped in try/except so a failure
    leaves the entry at its previous version and returns False, signalling
    HA to retry the migration on next setup. The version bump is only
    performed when the migration body completes without raising.
    """
    _LOGGER.debug("Migrating config entry from version %s", entry.version)

    if entry.version == 1:
        try:
            serial = entry.data.get("serial_number")
            if isinstance(serial, str):
                serial = serial.strip()
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
                skipped_collisions = 0
                # Snapshot to list() — async_update_entity inside the loop can
                # invalidate a live registry view on some HA versions.
                for entity_entry in list(
                    er.async_entries_for_config_entry(registry, entry.entry_id)
                ):
                    if entity_entry.domain != "button":
                        continue
                    uid = entity_entry.unique_id or ""
                    # Already migrated (unique_id starts with the serial)? Skip.
                    if uid.startswith(f"{serial}_"):
                        continue
                    # Find the command suffix the legacy unique_id ends with and
                    # rebuild as `<serial>_<command>`.
                    for cmd in _M12_BUTTON_COMMANDS:
                        if uid.endswith(f"_{cmd}"):
                            new_uid = f"{serial}_{cmd}"
                            # A `<serial>_<cmd>` entity may already exist (e.g.
                            # the user downgraded then re-upgraded). Skip with a
                            # warning rather than letting async_update_entity
                            # raise and abort the whole migration.
                            if registry.async_get_entity_id(
                                "button", DOMAIN, new_uid
                            ) is not None:
                                _LOGGER.warning(
                                    "M12 migration: %s target unique_id %r already exists, "
                                    "leaving legacy %r in place. Resolve manually via the "
                                    "entity registry.",
                                    entity_entry.entity_id, new_uid, uid,
                                )
                                skipped_collisions += 1
                                break
                            _LOGGER.debug(
                                "M12 migration: %s unique_id %r -> %r",
                                entity_entry.entity_id, uid, new_uid,
                            )
                            registry.async_update_entity(
                                entity_entry.entity_id, new_unique_id=new_uid,
                            )
                            updates += 1
                            break
                _LOGGER.info(
                    "M12 migration: rewrote %s button unique_id(s); %s collision(s) skipped",
                    updates, skipped_collisions,
                )
        except Exception:  # noqa: BLE001
            # Leave entry at v1 so HA retries on next setup.
            _LOGGER.exception(
                "M12 migration (v1 -> v2) failed for entry %s; entry left at v1",
                entry.entry_id,
            )
            return False
        hass.config_entries.async_update_entry(entry, version=2)

    if entry.version == 2:
        try:
            # M17: drop the dead `api_key` field. The integration has always used
            # the API_KEY constant directly; the stored field was never read.
            if "api_key" in entry.data:
                new_data = {k: v for k, v in entry.data.items() if k != "api_key"}
                hass.config_entries.async_update_entry(entry, data=new_data, version=3)
                _LOGGER.debug("M17 migration: dropped api_key from entry data")
            else:
                hass.config_entries.async_update_entry(entry, version=3)
        except Exception:  # noqa: BLE001
            # Leave entry at v2 so HA retries on next setup.
            _LOGGER.exception(
                "M17 migration (v2 -> v3) failed for entry %s; entry left at v2",
                entry.entry_id,
            )
            return False

    _LOGGER.debug("Migration to version %s complete", entry.version)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {}
    from .coordinator import AqualinkClient, AqualinkDataUpdateCoordinator
    from .const import SCAN_INTERVAL

    # M19 AC#8: stray api_key cleanup. The M17 migration drops the dead
    # `api_key` field from entry.data, but a user who restored a backup
    # from before migration can land here with the field still present
    # and the version already at CURRENT_VERSION (so async_migrate_entry
    # is never invoked). Strip it lazily here as a belt-and-braces.
    if "api_key" in entry.data:
        _LOGGER.debug(
            "async_setup_entry: stray api_key found in entry.data; dropping it"
        )
        new_data = {k: v for k, v in entry.data.items() if k != "api_key"}
        hass.config_entries.async_update_entry(entry, data=new_data)

    # Get credentials from entry
    username = entry.data["username"]
    password = entry.data["password"]

    # Check if a specific serial number is specified in the entry
    target_serial = entry.data.get("serial_number")

    # Create a client for this specific device using the constant API key
    # Debug mode disabled by default to reduce log verbosity
    # To enable debug mode for troubleshooting, change False to True below
    debug_mode = False
    # Resolve the include_seconds_remaining option. entry.options (set by
    # the OptionsFlow at runtime) takes precedence over entry.data (set by
    # the initial ConfigFlow). Falls back to the default for entries that
    # pre-date the option's introduction.
    include_seconds_remaining = entry.options.get(
        CONF_INCLUDE_SECONDS_REMAINING,
        entry.data.get(CONF_INCLUDE_SECONDS_REMAINING, DEFAULT_INCLUDE_SECONDS_REMAINING),
    )
    client = AqualinkClient(
        username,
        password,
        API_KEY,
        debug_mode=debug_mode,
        include_seconds_remaining=include_seconds_remaining,
    )

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
    coordinator.title = entry.title

    # Initial refresh to get data
    await coordinator.async_config_entry_first_refresh()

    # Store client and coordinator in hass.data
    hass.data[DOMAIN][entry.entry_id]["client"] = client
    hass.data[DOMAIN][entry.entry_id]["coordinator"] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when the user toggles an option in the OptionsFlow
    # so the coordinator picks up the new value without an HA restart.
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # Story C7: if the user just completed the legacy-domain migration
    # (i.e. they restarted HA after the legacy stub ran), there will be
    # outstanding Repair issues asking them to restart. The entry has now
    # loaded successfully under the new domain — dismiss both issues so the
    # user doesn't see a "restart required" notification that no longer
    # applies. ``async_delete_issue`` is a no-op when the issue doesn't
    # exist, so this is safe to call unconditionally on every setup.
    from homeassistant.helpers import issue_registry as ir
    ir.async_delete_issue(hass, DOMAIN, "legacy_domain_migrated_restart_required")
    ir.async_delete_issue(hass, DOMAIN, "legacy_domain_migration_failed")

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when an OptionsFlow value changes."""
    await hass.config_entries.async_reload(entry.entry_id)


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
