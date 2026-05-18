"""Legacy domain stub for `iaqualinkRobots` (story C7).

Pre-C7 the integration's manifest declared ``"domain": "iaqualinkRobots"``
(camelCase) while the directory was `iaqualink_robots/` (snake_case). C7
fixed the directory/domain mismatch by renaming the domain to
``iaqualink_robots``, matching Home Assistant's snake_case convention for
every other integration (`media_player`, `binary_sensor`, …) and silencing
hassfest's ``[MANIFEST] Domain does not match dir name`` error.

Existing users' config entries are stored in HA's ``core.config_entries``
keyed on the *old* domain. On upgrade the new-domain integration is never
invoked for those entries — HA looks up the integration by domain and finds
none. The entries would silently orphan: integration disappears from the
device list, automations bound to entity_ids break, history detaches.

This stub directory exists solely to handle that migration. HA loads the
stub when it finds an entry with ``domain == "iaqualinkRobots"``; the stub's
``async_setup_entry`` rewrites the entry's domain plus all registry
references (entity-registry ``platform`` field, device-registry
``identifiers`` tuples) to the new snake_case form, then emits a HA Repair
issue informing the user that a restart is required to complete the
migration.

After one full HA restart the migrated entries load under the new domain
and the stub is invoked no more. The directory can be removed in a future
release once we're confident no users remain on legacy-domain storage.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

_LOGGER = logging.getLogger(__name__)

LEGACY_DOMAIN = "iaqualinkRobots"
NEW_DOMAIN = "iaqualink_robots"

# Stable HA Repair-issue keys. HA dedups by these, so re-running the stub on
# subsequent restarts won't pile up duplicate notifications. The
# ``translation_key`` strings live in:
#     custom_components/iaqualink_robots/strings.json (en)
#     custom_components/iaqualink_robots/translations/*.json (all locales)
# under the ``issues`` section.
_ISSUE_MIGRATION_PENDING_RESTART = "legacy_domain_migrated_restart_required"
_ISSUE_MIGRATION_FAILED = "legacy_domain_migration_failed"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a single legacy-domain config entry to the new domain.

    Idempotent: a re-invocation on an entry that has already been migrated
    (its domain field updated and registries rewritten) is a no-op.

    Always returns True so HA does not mark the entry ``FAILED_SETUP``. The
    actual integration logic runs in the new-domain integration after the
    user restarts HA — the stub itself never starts a coordinator.
    """
    if entry.domain != LEGACY_DOMAIN:
        # Defensive: should never happen — HA only invokes us for our domain.
        _LOGGER.debug(
            "Legacy stub invoked with non-legacy entry %s (domain=%r); no-op",
            entry.entry_id, entry.domain,
        )
        return True

    try:
        await _migrate_entry(hass, entry)
    except Exception:  # noqa: BLE001 — broad catch is the whole point: never
        # let a migration failure crash HA's setup loop. We emit a Repair
        # issue with manual recovery instructions and let the user proceed.
        _LOGGER.exception(
            "C7 migration failed for entry %s; emitting a Repair issue "
            "with manual recovery instructions",
            entry.entry_id,
        )
        ir.async_create_issue(
            hass,
            NEW_DOMAIN,
            _ISSUE_MIGRATION_FAILED,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=_ISSUE_MIGRATION_FAILED,
            translation_placeholders={"entry_title": entry.title or entry.entry_id},
        )
        return True

    ir.async_create_issue(
        hass,
        NEW_DOMAIN,
        _ISSUE_MIGRATION_PENDING_RESTART,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=_ISSUE_MIGRATION_PENDING_RESTART,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """No-op unload. The stub never starts anything to tear down."""
    return True


async def _migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rewrite the entry's domain plus the registry references that key on it.

    Touches three pieces of state in order:

    1. **entity_registry** — each entity tied to this entry has
       ``platform == LEGACY_DOMAIN`` which must become
       ``platform == NEW_DOMAIN``. HA's public ``async_update_entity`` does
       not accept a ``platform=`` kwarg (platform is part of entity identity),
       so we mutate the attribute directly via ``object.__setattr__`` and
       trigger ``async_schedule_save`` to persist.

    2. **device_registry** — each device for this entry carries
       ``identifiers={(LEGACY_DOMAIN, key), ...}`` which must become
       ``identifiers={(NEW_DOMAIN, key), ...}``. The public API
       ``async_update_device(new_identifiers=...)`` handles this cleanly.

    3. **ConfigEntry.domain** — not in the public mutation API
       (``async_update_entry`` rejects unknown kwargs); direct attribute
       mutation followed by ``hass.config_entries._async_schedule_save()``.

    On HA restart, the entry's persisted ``domain`` is ``NEW_DOMAIN`` and
    HA loads the snake_case integration for it.
    """
    ent_registry = er.async_get(hass)
    dev_registry = dr.async_get(hass)

    # 1. Entity registry — list() snapshot so the loop doesn't trip a
    #    "registry mutated during iteration" guard on some HA versions.
    entities_to_update = [
        ent for ent in list(ent_registry.entities.values())
        if ent.config_entry_id == entry.entry_id and ent.platform == LEGACY_DOMAIN
    ]
    for ent in entities_to_update:
        # `RegistryEntry` is a frozen attrs/dataclass; `object.__setattr__`
        # bypasses the freeze. The public API doesn't expose a `platform`
        # parameter so this is the only available mutation path.
        object.__setattr__(ent, "platform", NEW_DOMAIN)
    if entities_to_update:
        ent_registry.async_schedule_save()
        _LOGGER.info(
            "C7 migration: rewrote platform on %d entity(ies) for entry %s",
            len(entities_to_update), entry.entry_id,
        )

    # 2. Device registry — public API handles identifiers cleanly.
    devices_to_update = [
        dev for dev in list(dev_registry.devices.values())
        if any(d[0] == LEGACY_DOMAIN for d in dev.identifiers)
    ]
    for dev in devices_to_update:
        new_ids = {
            (NEW_DOMAIN if d[0] == LEGACY_DOMAIN else d[0], d[1])
            for d in dev.identifiers
        }
        dev_registry.async_update_device(dev.id, new_identifiers=new_ids)
    if devices_to_update:
        _LOGGER.info(
            "C7 migration: rewrote identifiers on %d device(s) for entry %s",
            len(devices_to_update), entry.entry_id,
        )

    # 3. ConfigEntry domain — private attribute mutation. The schedule_save
    #    call persists the change to disk; on next HA restart, the entry
    #    loads under the new domain.
    object.__setattr__(entry, "domain", NEW_DOMAIN)
    hass.config_entries._async_schedule_save()
    _LOGGER.warning(
        "C7 migration: entry %s (%r) migrated from %r to %r. "
        "Please restart Home Assistant to activate the new domain.",
        entry.entry_id, entry.title, LEGACY_DOMAIN, NEW_DOMAIN,
    )
