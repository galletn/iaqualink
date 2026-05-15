"""Button entity tests.

Seeded by story M12: locks in serial-based unique_id derivation so a future
refactor can't reintroduce the title-based pattern that broke on entry rename.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from homeassistant.core import HomeAssistant

from tests.const import MOCK_DEVICE_TYPE, MOCK_SERIAL


@pytest.fixture
def mock_client_and_coordinator():
    """Minimal coordinator + client mocks for AqualinkRemoteButton.__init__."""
    client = MagicMock()
    client._serial = MOCK_SERIAL
    client._device_type = MOCK_DEVICE_TYPE
    client._model = "VRX IQ+"
    client.robot_id = MOCK_SERIAL
    client.robot_name = "Test Pool Robot"

    coordinator = MagicMock()
    coordinator._title = "Bobby the Robot"  # legacy title — must NOT influence unique_id
    coordinator.data = {}
    return coordinator, client


@pytest.mark.parametrize(
    "command,translation_key",
    [
        ("forward", "remote_forward"),
        ("backward", "remote_backward"),
        ("rotate_left", "remote_rotate_left"),
        ("rotate_right", "remote_rotate_right"),
        ("stop", "remote_stop"),
        ("add_fifteen_minutes", "add_fifteen_minutes"),
        ("reduce_fifteen_minutes", "reduce_fifteen_minutes"),
    ],
)
async def test_button_unique_id_uses_serial(
    hass: HomeAssistant, mock_client_and_coordinator, command, translation_key,
) -> None:
    """Every button unique_id must be `<serial>_<command>` — never derived from entry.title."""
    from custom_components.iaqualinkrobots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    button = AqualinkRemoteButton(coordinator, client, command, translation_key, "mdi:icon")

    assert button.unique_id == f"{MOCK_SERIAL}_{command}"
    # And specifically NOT derived from the (legacy) title.
    assert "bobby" not in button.unique_id.lower()
    assert coordinator._title.lower().replace(" ", "_") not in button.unique_id


async def test_button_unique_id_stable_across_title_rename(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """Renaming the entry must NOT change the unique_id."""
    from custom_components.iaqualinkrobots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    b1 = AqualinkRemoteButton(coordinator, client, "forward", "remote_forward", "mdi:x")
    original_uid = b1.unique_id

    # Simulate a rename — change the title attribute then construct another button.
    coordinator._title = "Pool Robot Renamed"
    b2 = AqualinkRemoteButton(coordinator, client, "forward", "remote_forward", "mdi:x")

    assert b2.unique_id == original_uid
