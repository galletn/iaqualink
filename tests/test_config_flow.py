"""Smoke tests for the iAqualink Robots config flow.

These are the seed tests for story P1. Stories C2, M17, P4 each add their
own tests that exercise unique_id handling, the api_key removal migration,
and the reauth flow respectively.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
import voluptuous as vol
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.iaqualink_robots.const import DOMAIN
from custom_components.iaqualink_robots.coordinator import AuthFailedError

from tests.const import (
    MOCK_DEVICE_SECOND,
    MOCK_DEVICE_TYPE,
    MOCK_ENTRY_DATA,
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
async def test_second_robot_on_same_account_auto_added(hass: HomeAssistant) -> None:
    """C6 (refined) multi-robot-per-account contract: with one robot from a
    two-robot iAqualink account already configured, a second flow with the
    same credentials discovers both robots, filters out the configured one,
    and creates an entry for the remaining robot via the single-device
    shortcut.

    Pre-refinement (the original C6 pre-discovery probe) this scenario
    aborted with `already_configured`, blocking multi-robot users entirely
    — a regression vs the spec's Risks-clause intent that "the existing
    C2 logic stays" for two-robots-on-one-account. The refined post-review
    design preserves the spec's intent while still preventing spurious
    second entries for a user who simply re-types the same credentials on
    a single-robot account (covered by test_duplicate_entry_aborts).
    """
    # First entry via select_device — user picks robot #1 (MOCK_SERIAL).
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
    assert first["result"].unique_id == MOCK_SERIAL
    await hass.async_block_till_done()

    # Second attempt with the same credentials: discover returns both
    # robots, MOCK_SERIAL is filtered out, MOCK_DEVICE_SECOND remains,
    # single-device shortcut creates its entry.
    second_serial = MOCK_DEVICE_SECOND["serial_number"]
    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    final = await hass.config_entries.flow.async_configure(
        result2["flow_id"], user_input=MOCK_USER_INPUT
    )
    assert final["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert final["result"].unique_id == second_serial
    assert final["data"]["serial_number"] == second_serial


# ---------------------------------------------------------------------------
# C6 (refined): post-discovery dedupe by serial. All-already-configured →
# abort. Case/whitespace-variant credentials still dedupe correctly. Empty
# username rejected by schema.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("mock_discover_single_device", "bypass_setup_fixture")
async def test_all_account_robots_already_configured_aborts(hass: HomeAssistant) -> None:
    """When every discovered robot for an account is already configured
    locally, abort with `already_configured`. Exercises the C6 filter on
    a single-device account: the existing entry's serial matches the only
    discovered serial → filtered list is empty → abort.

    Note: under the refined design, `discover_devices` IS called (we no
    longer pre-abort). The win over pre-C6 is that the user does NOT see
    the select_device step or get the post-create unique_id collision
    message; they see the clean `already_configured` abort instead.
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MOCK_SERIAL,
        data=MOCK_ENTRY_DATA,
    )
    existing.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    final = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_USER_INPUT
    )

    assert final["type"] == data_entry_flow.FlowResultType.ABORT
    assert final["reason"] == "already_configured"


@pytest.mark.usefixtures("mock_discover_single_device", "bypass_setup_fixture")
async def test_case_variant_username_still_dedupes(hass: HomeAssistant) -> None:
    """C6 review P1: the username comparison is case- and whitespace-
    normalized so a user retyping `Test@Example.com ` (mixed case + trailing
    space) is recognized as the same account as `test@example.com` and the
    dedupe filter still fires. Without `.strip().casefold()` on both sides,
    the second flow would create a duplicate entry (Cognito treats email-
    style usernames case-insensitively, so the actual second discover_devices
    call returns the same robot, but the filter would miss it).
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    existing = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MOCK_SERIAL,
        data=MOCK_ENTRY_DATA,
    )
    existing.add_to_hass(hass)

    # Variant credentials: mixed case + trailing space.
    variant_input = {
        **MOCK_USER_INPUT,
        "username": " " + MOCK_USER_INPUT["username"].upper() + " ",
    }

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    final = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=variant_input
    )

    assert final["type"] == data_entry_flow.FlowResultType.ABORT
    assert final["reason"] == "already_configured"


async def test_user_flow_empty_username_rejected_by_schema(hass: HomeAssistant) -> None:
    """C6 review P3: the username form schema enforces `vol.Length(min=1)`
    so an empty submit fails locally rather than probing existing entries
    with `""` and accidentally matching a corrupted entry. Mirrors the H9b
    sign-off P4 pattern applied to the reauth password field.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    bad_input = {**MOCK_USER_INPUT, "username": ""}
    # The schema rejects the input — the flow may surface this as a form
    # re-display or as a voluptuous validation error depending on HA's
    # behaviour; either way, no entry is created and discover_devices is
    # not called.
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[]),
    ) as discover_mock:
        try:
            final = await hass.config_entries.flow.async_configure(
                result["flow_id"], user_input=bad_input
            )
            # If the framework surfaces it as a form re-display, assert
            # the username field is unfilled and discovery was skipped.
            assert final["type"] == data_entry_flow.FlowResultType.FORM
            assert final["step_id"] == "user"
        except vol.Invalid:
            # Alternative path: voluptuous raises directly. Acceptable.
            pass

    discover_mock.assert_not_called()
    assert hass.config_entries.async_entries(DOMAIN) == []


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
# M19 AC#5: AbortFlow re-raise contract in async_step_user.
# ---------------------------------------------------------------------------


async def test_user_flow_abortflow_re_raised(hass: HomeAssistant) -> None:
    """A non-already_configured AbortFlow from discover_devices must surface.

    Asserts the explicit `except AbortFlow: raise` in async_step_user really
    fires — without it the broad `except Exception` clause below would
    swallow the abort and show `cannot_connect`, hiding the real reason.
    """
    custom_abort = data_entry_flow.AbortFlow("custom_abort_reason")
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(side_effect=custom_abort),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=MOCK_USER_INPUT
        )

    assert result2["type"] == data_entry_flow.FlowResultType.ABORT
    assert result2["reason"] == "custom_abort_reason"


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
# H9b review P2: invalid credentials surface as `invalid_auth` (not `cannot_connect`).
# ---------------------------------------------------------------------------


async def test_user_flow_invalid_auth_shows_invalid_auth(hass: HomeAssistant) -> None:
    """When discover_devices raises `AuthFailedError`, the user form re-shows
    with `invalid_auth` — distinct from the generic `cannot_connect` shown
    for network errors. H9b review P2 fix.
    """
    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(side_effect=AuthFailedError("401 Unauthorized on /get_devices")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=MOCK_USER_INPUT
        )

    assert result2["type"] == data_entry_flow.FlowResultType.FORM
    assert result2["step_id"] == "user"
    assert result2["errors"] == {"base": "invalid_auth"}


# ---------------------------------------------------------------------------
# Story P4 (bundled with H9b per the spec's pair-land Dependencies line):
# async_step_reauth + async_step_reauth_confirm.
# ---------------------------------------------------------------------------


async def _setup_reauth_entry(hass: HomeAssistant) -> config_entries.ConfigEntry:
    """Build a config entry suitable for triggering async_step_reauth.

    The entry is added without `async_setup_entry` running because reauth
    only needs the entry's `data` (username) and `entry_id` (context).
    """
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=MOCK_SERIAL,
        data=MOCK_ENTRY_DATA,
    )
    entry.add_to_hass(hass)
    return entry


async def test_reauth_flow_updates_password(hass: HomeAssistant) -> None:
    """Reauth happy path: new password is accepted, entry data is updated in
    place (unique_id preserved → entity registry intact), and the entry is
    reloaded so the coordinator picks up the new credentials.
    """
    entry = await _setup_reauth_entry(hass)

    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(return_value=[{"serial_number": MOCK_SERIAL, "device_type": "vr", "name": "x"}]),
    ), patch.object(
        hass.config_entries, "async_reload", new=AsyncMock(return_value=True)
    ) as reload_mock:
        # Trigger reauth — HA passes entry_id via context.
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        assert result["type"] == data_entry_flow.FlowResultType.FORM
        assert result["step_id"] == "reauth_confirm"

        final = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"password": "new-password"}
        )

    assert final["type"] == data_entry_flow.FlowResultType.ABORT
    assert final["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-password"
    # unique_id preserved (M13 regression guard semantics).
    assert entry.unique_id == MOCK_SERIAL
    reload_mock.assert_awaited_once_with(entry.entry_id)


async def test_reauth_flow_invalid_password_shows_form(hass: HomeAssistant) -> None:
    """When the new password is rejected by Cognito (AuthFailedError), the
    reauth form re-displays with `invalid_auth` and the entry is NOT updated.
    """
    entry = await _setup_reauth_entry(hass)

    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(side_effect=AuthFailedError("still 401")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        final = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"password": "wrong-password"}
        )

    assert final["type"] == data_entry_flow.FlowResultType.FORM
    assert final["step_id"] == "reauth_confirm"
    assert final["errors"] == {"base": "invalid_auth"}
    # Entry data unchanged.
    assert entry.data["password"] == MOCK_ENTRY_DATA["password"]


async def test_reauth_flow_network_error_shows_cannot_connect(hass: HomeAssistant) -> None:
    """When discovery fails for non-auth reasons (network error), the form
    re-displays with `cannot_connect` — distinct from `invalid_auth`.
    """
    entry = await _setup_reauth_entry(hass)

    with patch(
        "custom_components.iaqualink_robots.config_flow.AqualinkClient.discover_devices",
        new=AsyncMock(side_effect=RuntimeError("simulated network error")),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id},
            data=entry.data,
        )
        final = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"password": "anything"}
        )

    assert final["type"] == data_entry_flow.FlowResultType.FORM
    assert final["step_id"] == "reauth_confirm"
    assert final["errors"] == {"base": "cannot_connect"}
