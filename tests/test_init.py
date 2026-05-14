"""Lifecycle smoke tests for the iAqualink Robots integration.

These cover async_setup, async_setup_entry and async_unload_entry at the
contract boundary. Deeper coordinator/client tests live in test_coordinator.py
(seeded as part of stories H6/H7/H9/H10) and test_client.py (post-R26 split).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iaqualink_robots.const import DOMAIN

from tests.const import MOCK_ENTRY_DATA, MOCK_NAME, MOCK_SERIAL


async def test_async_setup_returns_true(hass: HomeAssistant) -> None:
    """async_setup is a no-op that must return True."""
    assert await async_setup_component(hass, DOMAIN, {})


@pytest.fixture
def mock_coordinator():
    """Patch the coordinator + client so async_setup_entry doesn't hit the cloud."""
    with patch(
        "custom_components.iaqualink_robots.AqualinkClient",
        autospec=True,
    ) as client_cls, patch(
        "custom_components.iaqualink_robots.AqualinkDataUpdateCoordinator",
        autospec=True,
    ) as coord_cls:
        client_instance = MagicMock()
        client_instance._authenticate = AsyncMock()
        client_instance._discover_device = AsyncMock()
        client_cls.return_value = client_instance

        coord_instance = MagicMock()
        coord_instance.async_config_entry_first_refresh = AsyncMock()
        coord_instance.cleanup = AsyncMock()
        # async_request_refresh and update_listeners may be invoked downstream
        coord_instance.async_request_refresh = AsyncMock()
        coord_instance.async_update_listeners = MagicMock()
        coord_cls.return_value = coord_instance

        yield {
            "client_cls": client_cls,
            "client": client_instance,
            "coord_cls": coord_cls,
            "coord": coord_instance,
        }


@pytest.mark.xfail(
    reason=(
        "Story P3: setup currently forwards to platforms (vacuum/sensor/button) which "
        "have their own coordinator wiring not yet stubbable from this seed. Will pass "
        "once runtime_data migration + platform-fixture polish lands."
    ),
    strict=False,
)
async def test_setup_entry_stores_client_and_coordinator(
    hass: HomeAssistant, mock_coordinator
) -> None:
    """async_setup_entry creates a client + coordinator and stashes them in hass.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_ENTRY_DATA,
        title=MOCK_NAME,
        unique_id=MOCK_ENTRY_DATA["serial_number"],
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    mock_coordinator["client_cls"].assert_called_once()
    mock_coordinator["coord"].async_config_entry_first_refresh.assert_awaited_once()

    entry_store = hass.data[DOMAIN][entry.entry_id]
    assert entry_store["client"] is mock_coordinator["client"]
    assert entry_store["coordinator"] is mock_coordinator["coord"]


# ---------------------------------------------------------------------------
# Config entry migrations.
#   v1 -> v2  M12: rewrite button unique_ids from title-derived to serial-based
#   v2 -> v3  M17: drop the dead `api_key` field from entry.data
# CURRENT_VERSION below tracks the latest target; bump per future migration.
# ---------------------------------------------------------------------------

CURRENT_VERSION = 3


async def test_migrate_v1_to_current_rewrites_button_unique_ids(hass: HomeAssistant) -> None:
    """Legacy v1 entries get the full migration chain: button uids fixed AND api_key dropped."""
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=MOCK_ENTRY_DATA,  # MOCK_ENTRY_DATA still includes api_key (legacy shape)
        title="Bobby the Robot",
    )
    entry.add_to_hass(hass)
    assert "api_key" in entry.data  # precondition: legacy shape must include the field

    registry = er.async_get(hass)
    legacy_prefix = "bobby_the_robot"
    legacy_commands = [
        "forward", "backward", "rotate_left", "rotate_right",
        "stop", "add_fifteen_minutes", "reduce_fifteen_minutes",
    ]
    for cmd in legacy_commands:
        registry.async_get_or_create(
            domain="button",
            platform=DOMAIN,
            unique_id=f"{legacy_prefix}_{cmd}",
            config_entry=entry,
        )

    assert await async_migrate_entry(hass, entry)

    for cmd in legacy_commands:
        assert registry.async_get_entity_id("button", DOMAIN, f"{legacy_prefix}_{cmd}") is None
        assert registry.async_get_entity_id("button", DOMAIN, f"{MOCK_SERIAL}_{cmd}") is not None

    assert entry.version == CURRENT_VERSION
    # M17 portion of the chain: api_key must be gone.
    assert "api_key" not in entry.data


async def test_migrate_v2_to_v3_drops_api_key(hass: HomeAssistant) -> None:
    """A v2 entry (post-M12, pre-M17) just has api_key stripped."""
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        title=MOCK_NAME,
    )
    entry.add_to_hass(hass)
    assert "api_key" in entry.data  # precondition

    assert await async_migrate_entry(hass, entry)

    assert "api_key" not in entry.data
    assert entry.version == CURRENT_VERSION
    # Other fields preserved.
    assert entry.data["username"] == MOCK_ENTRY_DATA["username"]
    assert entry.data["serial_number"] == MOCK_SERIAL


async def test_migrate_idempotent(hass: HomeAssistant) -> None:
    """Running migration on an already-current entry is a no-op."""
    from custom_components.iaqualink_robots import async_migrate_entry

    current_data = {k: v for k, v in MOCK_ENTRY_DATA.items() if k != "api_key"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CURRENT_VERSION,
        data=current_data,
        title=MOCK_NAME,
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        domain="button",
        platform=DOMAIN,
        unique_id=f"{MOCK_SERIAL}_forward",
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    assert registry.async_get_entity_id("button", DOMAIN, f"{MOCK_SERIAL}_forward") is not None
    assert entry.version == CURRENT_VERSION
    assert "api_key" not in entry.data


async def test_migrate_v1_skips_existing_v2_shape_collision(hass: HomeAssistant) -> None:
    """A pre-existing `<serial>_<cmd>` entity (downgrade/re-upgrade scenario)
    must NOT cause the migration to crash. The legacy entry is left in place
    with a warning so the user can resolve manually.
    """
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=MOCK_ENTRY_DATA,
        title="Bobby the Robot",
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    # Pre-existing v2-shape entity from an earlier partial-migration / re-add.
    registry.async_get_or_create(
        domain="button",
        platform=DOMAIN,
        unique_id=f"{MOCK_SERIAL}_forward",
        config_entry=entry,
    )
    # Legacy v1-shape entity that would *want* to migrate into the colliding uid.
    legacy_uid = "bobby_the_robot_forward"
    registry.async_get_or_create(
        domain="button",
        platform=DOMAIN,
        unique_id=legacy_uid,
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    # Migration completed (entry advanced), v2-shape entity preserved, legacy
    # entry left in place untouched.
    assert entry.version == CURRENT_VERSION
    assert registry.async_get_entity_id("button", DOMAIN, f"{MOCK_SERIAL}_forward") is not None
    assert registry.async_get_entity_id("button", DOMAIN, legacy_uid) is not None


async def test_migrate_v1_leaves_non_button_entities_untouched(hass: HomeAssistant) -> None:
    """Sensor / vacuum / other domain entities must NOT be rewritten by the M12
    button migration, even if their unique_id happens to end with a command suffix.
    """
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=MOCK_ENTRY_DATA,
        title="Bobby the Robot",
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    # Sensor with a unique_id that incidentally ends with `_stop` (a command suffix).
    sensor_uid = "bobby_the_robot_stop"
    registry.async_get_or_create(
        domain="sensor",
        platform=DOMAIN,
        unique_id=sensor_uid,
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    # The sensor's unique_id is unchanged.
    assert registry.async_get_entity_id("sensor", DOMAIN, sensor_uid) is not None
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{MOCK_SERIAL}_stop") is None


async def test_migrate_v1_to_current_without_serial_is_safe(hass: HomeAssistant) -> None:
    """If a v1 entry lacks `serial_number`, migration logs a warning, skips
    the M12 rewrite, drops api_key (M17), bumps to current version, and the
    integration remains loadable.
    """
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={k: v for k, v in MOCK_ENTRY_DATA.items() if k != "serial_number"},
        title=MOCK_NAME,
    )
    entry.add_to_hass(hass)
    assert "api_key" in entry.data  # precondition: legacy shape must include the field

    assert await async_migrate_entry(hass, entry)
    assert entry.version == CURRENT_VERSION
    # M17 step still ran:
    assert "api_key" not in entry.data


async def test_migrate_v1_without_serial_preserves_registry(hass: HomeAssistant) -> None:
    """M19 AC#6: v1-no-serial path must leave the registry untouched.

    Guards against a regression where the missing-serial branch would
    rebuild unique_ids as `_<cmd>` (empty-serial prefix), corrupting the
    user's existing button entries. The migration should detect no serial,
    log a warning, skip the rewrite entirely, and the legacy entities must
    keep their original unique_ids.
    """
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={k: v for k, v in MOCK_ENTRY_DATA.items() if k != "serial_number"},
        title="Bobby the Robot",
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    legacy_prefix = "bobby_the_robot"
    legacy_commands = [
        "forward", "backward", "rotate_left", "rotate_right",
        "stop", "add_fifteen_minutes", "reduce_fifteen_minutes",
    ]
    for cmd in legacy_commands:
        registry.async_get_or_create(
            domain="button",
            platform=DOMAIN,
            unique_id=f"{legacy_prefix}_{cmd}",
            config_entry=entry,
        )

    assert await async_migrate_entry(hass, entry)
    assert entry.version == CURRENT_VERSION

    # Legacy unique_ids must remain intact; no `_<cmd>` (empty-prefix) entries
    # may have been created.
    for cmd in legacy_commands:
        assert registry.async_get_entity_id("button", DOMAIN, f"{legacy_prefix}_{cmd}") is not None
        assert registry.async_get_entity_id("button", DOMAIN, f"_{cmd}") is None


# ---------------------------------------------------------------------------
# Lifecycle — XFAIL until P3 (runtime_data) and platform-fixture polish lands.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="See test_setup_entry_stores_client_and_coordinator — same dependency on platform fixtures.",
    strict=False,
)
async def test_unload_entry_calls_cleanup(hass: HomeAssistant, mock_coordinator) -> None:
    """async_unload_entry must call coordinator.cleanup() and remove the entry from hass.data."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_ENTRY_DATA,
        title=MOCK_NAME,
        unique_id=MOCK_ENTRY_DATA["serial_number"],
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    mock_coordinator["coord"].cleanup.assert_awaited_once()
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
