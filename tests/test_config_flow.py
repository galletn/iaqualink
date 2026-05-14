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
    MOCK_DEVICE_SECOND,
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
    # M17: api_key must NOT be written into entry.data for new entries.
    assert "api_key" not in result2["data"]


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


@pytest.mark.usefixtures("mock_discover_single_device", "bypass_setup_fixture")
async def test_duplicate_entry_aborts(hass: HomeAssistant) -> None:
    """Adding the same robot twice should abort with `already_configured`.

    `bypass_setup_fixture` short-circuits async_setup_entry so the test doesn't
    try to authenticate over real network (pytest-socket would block it and
    the entry registration would race the second flow).
    """
    # First entry — succeeds.
    result1 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    first = await hass.config_entries.flow.async_configure(
        result1["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # M13 regression guard: unique_id must be the serial, not the email.
    assert first["result"].unique_id == MOCK_SERIAL
    await hass.async_block_till_done()

    # Second attempt — must abort.
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result2_final = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert result2_final["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2_final["reason"] == "already_configured"


@pytest.mark.usefixtures("mock_discover_two_devices", "bypass_setup_fixture")
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
    # M13 regression guard: unique_id must be the serial, not the email.
    assert first["result"].unique_id == MOCK_SERIAL
    await hass.async_block_till_done()

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


@pytest.mark.usefixtures("mock_discover_two_devices", "bypass_setup_fixture")
async def test_select_device_picks_second_device(hass: HomeAssistant) -> None:
    """Picking the second device in select_device creates an entry keyed to its serial."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    selection = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert selection["step_id"] == "select_device"

    second_serial = MOCK_DEVICE_SECOND["serial_number"]
    final = await hass.config_entries.flow.async_configure(
        selection["flow_id"],
        user_input={"device": second_serial, "name": "Picked second"},
    )
    assert final["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert final["result"].unique_id == second_serial
    assert final["data"]["serial_number"] == second_serial
    # M17: api_key must NOT be written into entry.data via select_device either.
    assert "api_key" not in final["data"]


async def test_user_flow_schema_has_no_api_key_field(hass: HomeAssistant) -> None:
    """M17 regression guard: the user form must NOT prompt for an api_key field.

    Catches a future PR adding `vol.Required("api_key")` (or similar) back to
    the data schema, which would defeat the M17 cleanup at form level rather
    than at the entry-data level.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    schema_keys = {str(key) for key in result["data_schema"].schema}
    assert "api_key" not in schema_keys, (
        f"config-flow data schema must not contain an api_key field; got keys {schema_keys}"
    )


@pytest.mark.usefixtures("mock_discover_empty_serial", "bypass_setup_fixture")
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
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.usefixtures("mock_discover_good_and_empty_serial", "bypass_setup_fixture")
async def test_empty_serial_via_select_device_aborts(hass: HomeAssistant) -> None:
    """Picking a device with an empty serial in the select_device step must abort."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    selection = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert selection["type"] == data_entry_flow.FlowResultType.FORM
    assert selection["step_id"] == "select_device"

    # Pick the bad device — its serial is the empty string, which is also its
    # dropdown key per device_options construction in config_flow.py.
    final = await hass.config_entries.flow.async_configure(
        selection["flow_id"],
        user_input={"device": "", "name": "Bad pick"},
    )
    assert final["type"] == data_entry_flow.FlowResultType.ABORT
    assert final["reason"] == "no_serial"
    assert hass.config_entries.async_entries(DOMAIN) == []


# ---------------------------------------------------------------------------
# M19 AC#4: device_not_found branch in async_step_select_device.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_discover_two_devices", "bypass_setup_fixture")
async def test_select_device_missing_device_shows_device_not_found(
    hass: HomeAssistant,
) -> None:
    """Surface `device_not_found` when the lookup miss occurs in select_device.

    Reaches the select_device form, then clears `_devices` on the live flow
    handler so the next-by-serial lookup returns `None`. The cur_step's
    data_schema (already cached on the flow) still accepts the submitted
    serial, so we exercise the lookup-miss branch rather than schema
    rejection.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    selection = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert selection["type"] == data_entry_flow.FlowResultType.FORM
    assert selection["step_id"] == "select_device"

    # Force a lookup miss: empty out `_devices` on the running flow handler.
    flow = hass.config_entries.flow._progress[selection["flow_id"]]
    flow._devices = []

    final = await hass.config_entries.flow.async_configure(
        selection["flow_id"],
        user_input={"device": MOCK_SERIAL, "name": "Picked ghost"},
    )

    assert final["type"] == data_entry_flow.FlowResultType.FORM
    assert final["step_id"] == "select_device"
    assert final["errors"] == {"base": "device_not_found"}


# ---------------------------------------------------------------------------
# Placeholders for forthcoming stories.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(reason="Story P4: async_step_reauth not yet implemented", strict=True)
async def test_reauth_flow_updates_password(hass: HomeAssistant) -> None:
    """Reauth keeps unique_id, updates password, reloads entry. Implemented in P4."""
    raise NotImplementedError
