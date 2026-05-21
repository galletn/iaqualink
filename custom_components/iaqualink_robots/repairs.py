"""P8: Repairs flow for the legacy-service behaviour-change notification.

Pre-3.0 versions of this integration advertised seven services in
``services.yaml`` (``remote_forward`` / ``remote_backward`` /
``remote_rotate_left`` / ``remote_rotate_right`` / ``remote_stop`` /
``add_fifteen_minutes`` / ``reduce_fifteen_minutes``) but never called
``async_register_entity_service`` to wire them up — automations calling
them raised ``ServiceNotFound``. Story C1a fixed the registration. The
behaviour change (silently failing -> actually moving the robot) warrants
a one-shot Repairs notification, which is what this module implements.

The issue is raised from ``__init__.async_setup_entry`` after the
``automation`` / ``script`` components have started (via
``homeassistant.helpers.start.async_at_started``) so the detection scan
sees the user's loaded configs. The dismissal flag lives in a dedicated
``homeassistant.helpers.storage.Store`` (NOT in ``entry.data``) so the
fix flow does NOT trigger ``entry.add_update_listener`` — pre-review-fix
the dismissal mutated ``entry.data`` which fired the ``_async_update_options``
listener and reloaded every iaqualink config entry, causing a cloud
re-auth + WebSocket reconnect cascade. The Store is keyed per-installation
(one key for all entries) because the issue itself is keyed on domain +
issue_id, not per-entry.

AC #4 (auto-clear on next major release) is satisfied by future-version
migration code in ``__init__.async_migrate_entry`` calling
``ir.async_delete_issue`` and removing the Store via ``Store.async_remove`` —
see the AC #4 note in ``async_migrate_entry``'s docstring.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.storage import Store

from .const import DOMAIN

LEGACY_SERVICE_REPAIR_ISSUE_ID = "legacy_service_behavior_change"

# P8: dismissal state lives in its own Store rather than ``entry.data`` so
# the fix flow does not trigger the per-entry ``add_update_listener``
# cascade. Bump ``_LEGACY_SERVICE_STORAGE_VERSION`` if the schema ever
# grows beyond ``{"dismissed": bool}``.
_LEGACY_SERVICE_STORAGE_VERSION = 1
_LEGACY_SERVICE_STORAGE_KEY = f"{DOMAIN}_p8_dismissal"


def _p8_store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    """Return the per-installation Store for the P8 dismissal flag.

    A fresh ``Store`` instance per call is the HA-canonical pattern —
    the underlying file is read/written on demand and HA caches the
    decoded content internally.
    """
    return Store(hass, _LEGACY_SERVICE_STORAGE_VERSION, _LEGACY_SERVICE_STORAGE_KEY)


async def async_is_legacy_service_dismissed(hass: HomeAssistant) -> bool:
    """Return True if the user has dismissed the P8 Repairs notification.

    A missing Store file (first-ever startup, or post-migration cleanup)
    returns False — the dismissal is treated as not-yet-set, so the
    detection scan runs and the issue can be raised.
    """
    data = await _p8_store(hass).async_load()
    return bool(data and data.get("dismissed"))


async def async_mark_legacy_service_dismissed(hass: HomeAssistant) -> None:
    """Record dismissal of the P8 Repairs notification.

    Subsequent ``async_is_legacy_service_dismissed(hass)`` calls return
    True until the Store is removed (next-major-version migration —
    see AC #4 forward contract in ``__init__.async_migrate_entry``).
    """
    await _p8_store(hass).async_save({"dismissed": True})


class LegacyServiceBehaviorChangeFixFlow(RepairsFlow):
    """One-step confirmation flow for the legacy-service behaviour-change issue.

    When the user clicks "Submit" on the confirm step the flow records the
    dismissal in the dedicated Store (NOT in ``entry.data`` — see module
    docstring) and finishes. HA removes the issue from the repair dashboard
    automatically once the flow returns ``async_create_entry``. The Store
    is per-installation, so a multi-robot user dismisses the shared issue
    once for all entries.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        # Forward ``user_input`` so HA-runner paths that pass it through
        # ``async_step_init`` (e.g. a future progress + auto-confirm UX)
        # don't get re-shown the form.
        return await self.async_step_confirm(user_input)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            await async_mark_legacy_service_dismissed(self.hass)
            return self.async_create_entry(title="", data={})
        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """HA-required factory for the integration's repairs flows.

    HA discovers this module by loading ``repairs.py`` from the integration
    package and calling ``async_create_fix_flow`` whenever the user opens a
    fixable issue belonging to this domain.
    """
    if issue_id == LEGACY_SERVICE_REPAIR_ISSUE_ID:
        return LegacyServiceBehaviorChangeFixFlow()
    raise NotImplementedError(f"No repairs flow for issue_id {issue_id!r}")
