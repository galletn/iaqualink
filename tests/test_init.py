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
# M12 migration — rewrite button unique_ids from title-derived to serial-based.
# ---------------------------------------------------------------------------


async def test_migrate_v1_to_v2_rewrites_button_unique_ids(hass: HomeAssistant) -> None:
    """Legacy `<title>_<command>` button unique_ids get rewritten to `<serial>_<command>`."""
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data=MOCK_ENTRY_DATA,
        title="Bobby the Robot",
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    # Pre-populate legacy entries: title-derived prefix, one per known command.
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
        # Old unique_id is gone.
        assert registry.async_get_entity_id("button", DOMAIN, f"{legacy_prefix}_{cmd}") is None
        # New unique_id exists.
        assert registry.async_get_entity_id("button", DOMAIN, f"{MOCK_SERIAL}_{cmd}") is not None

    assert entry.version == 2


async def test_migrate_idempotent(hass: HomeAssistant) -> None:
    """Running migration on an already-v2 entry is a no-op."""
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data=MOCK_ENTRY_DATA,
        title=MOCK_NAME,
    )
    entry.add_to_hass(hass)

    registry = er.async_get(hass)
    # Pre-populate already-correct entries.
    new_prefix = MOCK_SERIAL
    registry.async_get_or_create(
        domain="button",
        platform=DOMAIN,
        unique_id=f"{new_prefix}_forward",
        config_entry=entry,
    )

    assert await async_migrate_entry(hass, entry)

    # Still there, unchanged.
    assert registry.async_get_entity_id("button", DOMAIN, f"{new_prefix}_forward") is not None
    assert entry.version == 2


async def test_migrate_v1_to_v2_without_serial_is_safe(hass: HomeAssistant) -> None:
    """If a v1 entry somehow lacks `serial_number`, migration logs a warning,
    skips the rewrite, and bumps the version. The integration must remain
    loadable rather than crash on a malformed legacy entry.
    """
    from custom_components.iaqualink_robots import async_migrate_entry

    entry = MockConfigEntry(
        domain=DOMAIN,
        version=1,
        data={k: v for k, v in MOCK_ENTRY_DATA.items() if k != "serial_number"},
        title=MOCK_NAME,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2


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
