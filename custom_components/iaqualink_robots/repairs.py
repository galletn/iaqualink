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
sees the user's loaded configs. The notification is gated on a flag in
``entry.data`` (``LEGACY_SERVICE_REPAIR_FLAG``) so it does not re-fire on
HA restart after the user dismisses it.

AC #4 (auto-clear on next major release) is satisfied by the next-major-
version migration code in ``__init__.async_migrate_entry`` calling
``ir.async_delete_issue`` and removing the flag — see the TODO note in
``__init__`` near the migration table.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN

LEGACY_SERVICE_REPAIR_ISSUE_ID = "legacy_service_behavior_change"
LEGACY_SERVICE_REPAIR_FLAG = "p8_legacy_service_notified"


class LegacyServiceBehaviorChangeFixFlow(RepairsFlow):
    """One-step confirmation flow for the legacy-service behaviour-change issue.

    When the user clicks "Submit" on the confirm step the flow marks every
    domain config entry as notified (so the issue does not re-fire on next
    HA restart) and finishes — HA removes the issue from the repair dashboard
    automatically once the flow returns ``async_create_entry``. The issue is
    keyed only on the domain (not on a specific entry), so a user with
    multiple robots dismisses it once for all of them.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            for entry in self.hass.config_entries.async_entries(DOMAIN):
                if not entry.data.get(LEGACY_SERVICE_REPAIR_FLAG):
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, LEGACY_SERVICE_REPAIR_FLAG: True},
                    )
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
