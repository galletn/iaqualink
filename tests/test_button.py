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
    client.serial = MOCK_SERIAL
    client.device_type = MOCK_DEVICE_TYPE
    client.robot_id = MOCK_SERIAL
    client.robot_name = "Test Pool Robot"

    coordinator = MagicMock()
    coordinator.title = "Bobby the Robot"  # legacy title — must NOT influence unique_id
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
    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    button = AqualinkRemoteButton(coordinator, client, command, translation_key)

    assert button.unique_id == f"{MOCK_SERIAL}_{command}"
    # And specifically NOT derived from the (legacy) title.
    assert "bobby" not in button.unique_id.lower()
    assert coordinator.title.lower().replace(" ", "_") not in button.unique_id


async def test_button_press_raises_home_assistant_error_on_failure(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """A command that fails must surface in the UI, not just in the log.

    Pre-3.0.0-rc.4 async_press caught every exception, logged it at error and
    returned — so a press that never reached the robot rendered as a
    successful press.
    """
    from unittest.mock import AsyncMock

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    client.remote_forward = AsyncMock(side_effect=RuntimeError("cloud said no"))
    button = AqualinkRemoteButton(coordinator, client, "forward", "remote_forward")

    with pytest.raises(HomeAssistantError):
        await button.async_press()

    client.remote_forward.assert_awaited_once()


async def test_button_press_success_does_not_raise(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """The happy path stays quiet and dispatches to the matching client method."""
    from unittest.mock import AsyncMock

    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    client.remote_stop = AsyncMock(return_value=None)
    coordinator.async_request_refresh = AsyncMock()
    button = AqualinkRemoteButton(coordinator, client, "stop", "remote_stop")

    await button.async_press()

    client.remote_stop.assert_awaited_once()
    # Movement commands must NOT force an extra refresh — the 3s poll covers
    # them, and a refresh per press hammered the cloud during rapid jogging.
    coordinator.async_request_refresh.assert_not_awaited()


async def test_timing_button_routes_through_coordinator(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """The +/-15 minute buttons must go through the coordinator, not the client.

    Calling ``client.add_fifteen_minutes()`` directly skipped the 3-second
    debounce (a double-tap issued two cloud writes) and, more importantly,
    skipped the failure check: the client signals failure by *returning*
    ``{"success": False, ...}`` rather than raising, so a rejected adjustment
    was reported to the user as a successful press. The coordinator owns the
    debounce, the raise-on-failure, and the post-command refresh.
    """
    from unittest.mock import AsyncMock

    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    coordinator.async_add_fifteen_minutes = AsyncMock()
    client.add_fifteen_minutes = AsyncMock(return_value={"success": True})
    button = AqualinkRemoteButton(
        coordinator, client, "add_fifteen_minutes", "add_fifteen_minutes"
    )

    await button.async_press()

    coordinator.async_add_fifteen_minutes.assert_awaited_once()
    client.add_fifteen_minutes.assert_not_awaited()


async def test_timing_button_surfaces_coordinator_failure(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """A rejected adjustment must reach the UI as an error, not a silent success."""
    from unittest.mock import AsyncMock

    from homeassistant.exceptions import HomeAssistantError

    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    coordinator.async_reduce_fifteen_minutes = AsyncMock(
        side_effect=HomeAssistantError("cloud rejected the adjustment")
    )
    button = AqualinkRemoteButton(
        coordinator, client, "reduce_fifteen_minutes", "reduce_fifteen_minutes"
    )

    with pytest.raises(HomeAssistantError):
        await button.async_press()


async def test_button_icons_come_from_icons_json(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """Icons moved to icons.json (the `icon-translations` quality-scale rule).

    A hardcoded `_attr_icon` would override a user's icon customisation on
    every restart, so the entity must not set one.
    """
    import json
    from pathlib import Path

    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    button = AqualinkRemoteButton(coordinator, client, "forward", "remote_forward")
    assert getattr(button, "_attr_icon", None) is None

    icons = json.loads(
        (
            Path(__file__).parent.parent
            / "custom_components"
            / "iaqualink_robots"
            / "icons.json"
        ).read_text(encoding="utf-8")
    )
    # Every button translation_key must have an icon defined.
    for translation_key in (
        "remote_forward",
        "remote_backward",
        "remote_rotate_left",
        "remote_rotate_right",
        "remote_stop",
        "add_fifteen_minutes",
        "reduce_fifteen_minutes",
    ):
        assert icons["entity"]["button"][translation_key]["default"].startswith("mdi:")


async def test_button_unique_id_stable_across_title_rename(
    hass: HomeAssistant, mock_client_and_coordinator,
) -> None:
    """Renaming the entry must NOT change the unique_id."""
    from custom_components.iaqualink_robots.button import AqualinkRemoteButton

    coordinator, client = mock_client_and_coordinator
    b1 = AqualinkRemoteButton(coordinator, client, "forward", "remote_forward")
    original_uid = b1.unique_id

    # Simulate a rename — change the title attribute then construct another button.
    coordinator.title = "Pool Robot Renamed"
    b2 = AqualinkRemoteButton(coordinator, client, "forward", "remote_forward")

    assert b2.unique_id == original_uid
