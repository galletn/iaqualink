"""Lifecycle smoke tests for the iAqualink Robots integration.

These cover async_setup, async_setup_entry and async_unload_entry at the
contract boundary. Deeper coordinator/client tests live in test_coordinator.py
(seeded as part of stories H6/H7/H9/H10) and test_client.py (post-R26 split).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iaqualink_robots.const import DOMAIN

from tests.const import MOCK_ENTRY_DATA, MOCK_NAME


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
