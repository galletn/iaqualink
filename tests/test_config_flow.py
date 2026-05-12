"""Smoke tests for the iAqualink Robots config flow.

These are the seed tests for story P1. Stories C2, M17, P4 each add their
own tests that exercise unique_id handling, the api_key removal migration,
and the reauth flow respectively.
"""

from __future__ import annotations

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.iaqualink_robots.const import DOMAIN

from tests.const import (
    MOCK_DEVICE_TYPE,
    MOCK_NAME,
    MOCK_SERIAL,
    MOCK_USER_INPUT,
)


@pytest.mark.usefixtures("mock_discover_single_device")
async def test_user_flow_single_device_creates_entry(hass: HomeAssistant) -> None:
    """A single discovered device skips selection and creates the entry directly."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result2["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result2["title"] == MOCK_NAME
    assert result2["data"]["username"] == MOCK_USER_INPUT["username"]
    assert result2["data"]["serial_number"] == MOCK_SERIAL
    assert result2["data"]["device_type"] == MOCK_DEVICE_TYPE


@pytest.mark.usefixtures("mock_discover_no_devices")
async def test_user_flow_no_devices_shows_error(hass: HomeAssistant) -> None:
    """When discovery returns nothing, the form is shown again with `no_devices`."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "user"
    assert result2["errors"] == {"base": "no_devices"}


@pytest.mark.usefixtures("mock_discover_raises")
async def test_user_flow_discovery_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """When discovery raises, the user sees `cannot_connect`."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "user"
    assert result2["errors"] == {"base": "cannot_connect"}


# ---------------------------------------------------------------------------
# C2: unique_id + serial-non-empty guards (active).
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_discover_single_device")
async def test_duplicate_entry_aborts(hass: HomeAssistant) -> None:
    """Adding the same robot twice should abort with `already_configured`."""
    # First entry — succeeds.
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    first = await hass.config_entries.flow.async_configure(
        result1["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    # Second attempt — must abort.
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2_final = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result2_final["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2_final["reason"] == "already_configured"


@pytest.mark.usefixtures("mock_discover_two_devices")
async def test_duplicate_entry_via_select_device_aborts(hass: HomeAssistant) -> None:
    """Adding the same robot twice via select_device step also aborts."""
    # First entry via select_device.
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    selection = await hass.config_entries.flow.async_configure(
        result1["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert selection["type"] == data_entry_flow.FlowResultType.FORM
    assert selection["step_id"] == "select_device"

    first = await hass.config_entries.flow.async_configure(
        selection["flow_id"],
        user_input={"device": MOCK_SERIAL, "name": "First copy"},
    )
    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    # Second attempt — pick the same device — must abort.
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    selection2 = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=MOCK_USER_INPUT
    )
    result2_final = await hass.config_entries.flow.async_configure(
        selection2["flow_id"],
        user_input={"device": MOCK_SERIAL, "name": "Second copy"},
    )
    assert result2_final["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2_final["reason"] == "already_configured"


@pytest.mark.usefixtures("mock_discover_empty_serial")
async def test_empty_serial_aborts(hass: HomeAssistant) -> None:
    """A discovered device with an empty serial number must abort with `no_serial`.

    Guards against M13's email-frozen-as-unique-id regression: if the device
    list comes back without a usable serial, refuse to create the entry rather
    than silently using the email.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "no_serial"


# ---------------------------------------------------------------------------
# Placeholders for forthcoming stories.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="Story P4: async_step_reauth not yet implemented", strict=True)
async def test_reauth_flow_updates_password(hass: HomeAssistant) -> None:
    """Reauth keeps unique_id, updates password, reloads entry. Implemented in P4."""
    raise NotImplementedError
