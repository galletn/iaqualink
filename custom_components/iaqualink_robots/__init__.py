from __future__ import annotations

import asyncio
import collections.abc
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    config_validation as cv,
    entity_registry as er,
    issue_registry as ir,
)
from homeassistant.helpers.start import async_at_started

from .const import (
    API_KEY,
    CONF_INCLUDE_SECONDS_REMAINING,
    DEFAULT_INCLUDE_SECONDS_REMAINING,
    DOMAIN,
    PLATFORMS,
)
from .repairs import (
    LEGACY_SERVICE_REPAIR_ISSUE_ID,
    async_is_legacy_service_dismissed,
)
from .types import IaqualinkConfigEntry, IaqualinkData

_LOGGER = logging.getLogger(__name__)

# Hassfest requires integrations that define `async_setup` to declare a
# CONFIG_SCHEMA. This integration is config-entry-only (no YAML), so the
# helper `cv.config_entry_only_config_schema` is the HA-recommended shape.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# P8: services that were declared in services.yaml but never registered
# pre-C1a. Story C1a wired them up so automations actually move the robot
# now; the Repairs notification flags this behaviour change. The set of
# fully-qualified names is what we scan automation / script configs for.
_P8_LEGACY_SERVICES = (
    "remote_forward",
    "remote_backward",
    "remote_rotate_left",
    "remote_rotate_right",
    "remote_stop",
    "add_fifteen_minutes",
    "reduce_fifteen_minutes",
)
_P8_LEGACY_SERVICE_FQNS = frozenset(f"{DOMAIN}.{name}" for name in _P8_LEGACY_SERVICES)

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


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate an old config entry to the current schema.

    Version history:
        1 -> 2  (M12) Rewrite button unique_ids from title-derived (mutable,
                broke on rename) to serial-based. See story-M12.
        2 -> 3  (M17) Drop the dead `api_key` field from entry.data; the
                integration uses the const API_KEY directly. See story-M17.

    P8 AC #4 — future cleanup: the next-major-version migration block
    should call ``ir.async_delete_issue(hass, DOMAIN,
    LEGACY_SERVICE_REPAIR_ISSUE_ID)`` and remove the per-installation
    dismissal Store via ``Store(hass, _LEGACY_SERVICE_STORAGE_VERSION,
    _LEGACY_SERVICE_STORAGE_KEY).async_remove()`` so the one-time "service
    behaviour change" notification is cleared for everyone who upgraded
    through 3.0. The infrastructure (Store key + helper APIs in
    repairs.py, issue registered against the domain) is in place from
    P8; only the cleanup step lands with whatever story bumps the
    schema next.

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


async def async_setup_entry(hass: HomeAssistant, entry: IaqualinkConfigEntry) -> bool:
    from .client import AqualinkClient, AuthFailedError
    from .coordinator import AqualinkDataUpdateCoordinator
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

    # Create a client for this specific device using the constant API key.
    #
    # There is deliberately no `debug_mode` flag any more. It was hardcoded
    # to False with a comment telling users to edit the source, and it gated
    # ~20 `_LOGGER.debug(...)` calls — so those messages never appeared even
    # for a user who had correctly enabled debug logging in
    # `configuration.yaml`, which is exactly what the README tells them to do
    # when reporting a problem. Verbosity is HA's logger config's job.
    #
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
        include_seconds_remaining=include_seconds_remaining,
    )
    # L19: hand the hass reference to the client BEFORE any cloud call so the
    # early-auth path below (when `target_serial` is set) takes the shared
    # `async_get_clientsession` branch in `AqualinkClient._cloud_session`,
    # not the ephemeral fallback. The coordinator __init__ also calls
    # `client.set_hass(hass)` — idempotent reassignment.
    client.set_hass(hass)

    # If a specific serial is provided, use it for device discovery.
    #
    # Both calls are mapped onto HA's setup-outcome exceptions. Pre-fix,
    # `_authenticate()` was awaited bare and `_discover_device` only mapped
    # RuntimeError onto `return False`, which produced two user-visible bugs:
    #
    #   * A password changed while HA was down raised `AuthFailedError`
    #     straight out of `async_setup_entry`. HA showed a generic setup
    #     error and never started the reauth flow, so the only remedy the
    #     user could find was delete + re-add (losing all history).
    #   * A transient cloud outage at boot returned False — a *permanent*
    #     failure — instead of asking HA to retry with backoff.
    #
    # The mapping below mirrors the one the coordinator already applies on
    # every subsequent poll (see `coordinator._async_update_data`).
    if target_serial:
        try:
            await client._authenticate()
            await client._discover_device(target_serial=target_serial)
        except ConfigEntryAuthFailed:
            raise
        except AuthFailedError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except asyncio.CancelledError:
            raise
        except Exception as err:
            raise ConfigEntryNotReady(
                f"Failed to reach iAqualink cloud for device {target_serial}: {err}"
            ) from err

    # Create coordinator with this client
    coordinator = AqualinkDataUpdateCoordinator(hass, client, SCAN_INTERVAL.total_seconds())
    coordinator.title = entry.title

    # Initial refresh to get data
    await coordinator.async_config_entry_first_refresh()

    # P3: per-entry runtime state lives on `entry.runtime_data` (HA 2024+
    # convention). Replaces the prior `hass.data[DOMAIN][entry.entry_id]`
    # dict — type-checkable end-to-end and scoped to the entry's lifecycle.
    entry.runtime_data = IaqualinkData(coordinator=coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Reload the entry when the user toggles an option in the OptionsFlow
    # so the coordinator picks up the new value without an HA restart.
    entry.async_on_unload(entry.add_update_listener(_async_update_options))

    # P8: schedule the legacy-service Repairs check for after HA finishes
    # starting so the automation / script components have already loaded
    # their configs. `async_at_started` runs the callback immediately if
    # HA is already started (e.g. when the user adds the integration at
    # runtime), or queues it for the started event otherwise. The helper
    # awaits coroutine return values, so the lambda hands back the
    # coroutine rather than wrapping it in a task ourselves. The unsub
    # callable is registered with ``entry.async_on_unload`` so the
    # listener is torn down if the entry is unloaded before HA finishes
    # starting (review HIGH #2).
    entry.async_on_unload(
        async_at_started(
            hass,
            lambda _hass: _async_check_legacy_service_repair(hass),
        )
    )

    return True


async def _async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the integration when an OptionsFlow value changes."""
    await hass.config_entries.async_reload(entry.entry_id)


def _contains_legacy_service_ref(node: Any) -> bool:
    """Recursively walk an automation / script config looking for a
    ``service:`` or ``action:`` key whose value is one of our legacy
    fully-qualified service names. Returns True as soon as one is found.

    HA 2024+ accepts both ``service: domain.name`` (classic) and
    ``action: domain.name`` (new keyword) inside action blocks; we match
    either. Lists and nested mappings (``choose``, ``parallel``, ``if`` /
    ``then`` / ``else``, etc.) are walked recursively so the scan works
    on any depth of nesting. Uses ``collections.abc.Mapping`` (not bare
    ``dict``) so ``MappingProxyType`` subtrees — which HA core wraps
    config-entry data and many sub-blocks in — are also walked (review
    MED).
    """
    if isinstance(node, collections.abc.Mapping):
        for key in ("service", "action"):
            value = node.get(key)
            if isinstance(value, str) and value in _P8_LEGACY_SERVICE_FQNS:
                return True
        for v in node.values():
            if _contains_legacy_service_ref(v):
                return True
    elif isinstance(node, list):
        for item in node:
            if _contains_legacy_service_ref(item):
                return True
    return False


def _scan_for_legacy_service_calls(hass: HomeAssistant) -> bool:
    """Best-effort scan of the loaded automation and script entities for
    references to any of the seven legacy ``iaqualink_robots.*`` services.

    Returns True if any reference is found; False otherwise (including
    when the automation / script components are not loaded or their
    entity data cannot be enumerated — we prefer no notification over a
    false positive that would spam every user, regardless of whether they
    use the integration's services). Scan failures are surfaced at
    WARNING level (review MED) so HA-core regressions in the
    automation/script component storage become visible in user logs.
    """
    for component_domain in ("automation", "script"):
        component = hass.data.get(component_domain)
        if component is None:
            continue
        # ``EntityComponent.entities`` is a property; access it inside
        # the try so a non-AttributeError raised by the getter doesn't
        # propagate (review MED).
        try:
            entities_iter = getattr(component, "entities", None)
            if entities_iter is None:
                continue
            for entity in entities_iter:
                raw_config = getattr(entity, "raw_config", None)
                if raw_config and _contains_legacy_service_ref(raw_config):
                    return True
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "P8: scan of %s entities raised; treating as no-match",
                component_domain,
                exc_info=True,
            )
            continue
    return False


async def _async_check_legacy_service_repair(hass: HomeAssistant) -> None:
    """P8 entry point: detect legacy-service references in user configs
    and raise the Repairs issue on first encounter.

    Gated on (a) the persistent dismissal flag in the dedicated Store
    (see ``repairs.async_is_legacy_service_dismissed``) so the
    notification does not re-fire on HA restart after the user dismisses
    it, and (b) the presence of the issue in HA's issue registry so a
    multi-entry user (the callback fires once per entry from
    ``async_setup_entry``) only walks the scan once per HA startup
    (review MED: scan-dedupe).
    """
    if await async_is_legacy_service_dismissed(hass):
        return
    if ir.async_get(hass).async_get_issue(DOMAIN, LEGACY_SERVICE_REPAIR_ISSUE_ID):
        return
    if not _scan_for_legacy_service_calls(hass):
        return
    ir.async_create_issue(
        hass,
        DOMAIN,
        LEGACY_SERVICE_REPAIR_ISSUE_ID,
        is_fixable=True,
        severity=ir.IssueSeverity.WARNING,
        translation_key=LEGACY_SERVICE_REPAIR_ISSUE_ID,
    )


async def async_unload_entry(hass: HomeAssistant, entry: IaqualinkConfigEntry) -> bool:
    # Unload the platforms; clean up the coordinator only after every
    # platform has detached so no remaining listener observes a torn-down
    # coordinator. HA's helper raises if a platform unload fails and we
    # short-circuit the cleanup in that case (the next setup will retry).
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    try:
        await entry.runtime_data.coordinator.cleanup()
    except Exception as e:  # noqa: BLE001 — cleanup is best-effort
        _LOGGER.warning("Error during coordinator cleanup: %s", e)

    return True
