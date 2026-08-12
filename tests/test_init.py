"""Lifecycle smoke tests for the iAqualink Robots integration.

These cover async_setup, async_setup_entry and async_unload_entry at the
contract boundary. Deeper coordinator/client tests live in test_coordinator.py
(seeded as part of stories H6/H7/H9/H10) and test_client.py (post-R26 split).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iaqualink_robots.client import AuthFailedError
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
        "Setup forwards to platforms (vacuum/sensor/button) whose own coordinator "
        "wiring is not yet stubbable from this seed. Test body now asserts the post-P3 "
        "runtime_data contract — passes once platform-fixture polish lands."
    ),
    strict=False,
)
async def test_setup_entry_stores_coordinator_on_runtime_data(
    hass: HomeAssistant, mock_coordinator
) -> None:
    """P3: async_setup_entry stashes the coordinator on ``entry.runtime_data``.

    Pre-P3 the coordinator lived in ``hass.data[DOMAIN][entry.entry_id]``; the
    new shape uses the HA 2024+ typed ``entry.runtime_data`` convention.
    """
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

    assert entry.runtime_data.coordinator is mock_coordinator["coord"]


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


async def test_migrate_returns_false_on_v1_failure(hass: HomeAssistant) -> None:
    """M19 AC#10: a raise inside v1->v2 returns False and leaves entry at v1.

    Simulates a transient registry failure mid-migration: the entry must NOT
    advance to v2 (so HA retries the migration on next setup), and the
    function must return False rather than swallow the error.
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
    registry.async_get_or_create(
        domain="button",
        platform=DOMAIN,
        unique_id="bobby_the_robot_forward",
        config_entry=entry,
    )

    # Force the registry update to raise — simulates an unexpected runtime
    # error inside the migration body.
    with patch.object(
        registry,
        "async_update_entity",
        side_effect=RuntimeError("simulated registry failure"),
    ):
        result = await async_migrate_entry(hass, entry)

    assert result is False
    # Entry stays at v1 so HA retries on next setup.
    assert entry.version == 1


async def test_migrate_rerun_after_full_chain_is_noop(hass: HomeAssistant) -> None:
    """M19 AC#7: re-running migration on an already-migrated entry is a no-op.

    Mirrors HA's real-world behavior where `async_migrate_entry` can be
    invoked again after a successful migration (e.g., entry reload). The
    second call must not bump the version past CURRENT_VERSION, must not
    re-write any registry entries (snapshot the registry's update count),
    and must not mutate `entry.data` further.
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

    # First migration: full v1 -> v3 chain.
    assert await async_migrate_entry(hass, entry)
    assert entry.version == CURRENT_VERSION
    first_pass_data = dict(entry.data)
    first_pass_uids = {
        cmd: registry.async_get_entity_id("button", DOMAIN, f"{MOCK_SERIAL}_{cmd}")
        for cmd in legacy_commands
    }
    # Sanity: the chain actually moved the entities.
    assert all(uid is not None for uid in first_pass_uids.values())

    # Second migration: must be a true no-op.
    with patch.object(
        registry, "async_update_entity", wraps=registry.async_update_entity
    ) as update_spy:
        assert await async_migrate_entry(hass, entry)

    # No further registry writes triggered by the rerun.
    assert update_spy.call_count == 0
    # Version stays at CURRENT_VERSION (no over-bump).
    assert entry.version == CURRENT_VERSION
    # entry.data unchanged.
    assert dict(entry.data) == first_pass_data
    # Entity ids unchanged (same registry entries point to same unique_ids).
    for cmd in legacy_commands:
        assert (
            registry.async_get_entity_id("button", DOMAIN, f"{MOCK_SERIAL}_{cmd}")
            == first_pass_uids[cmd]
        )


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
# Lifecycle — XFAIL until platform-fixture polish lands.
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason="See test_setup_entry_stores_coordinator_on_runtime_data — same dependency on platform fixtures.",
    strict=False,
)
async def test_unload_entry_calls_cleanup(hass: HomeAssistant, mock_coordinator) -> None:
    """async_unload_entry must call coordinator.cleanup() after platforms detach.

    P3: post-migration the integration no longer reaches into ``hass.data``
    on unload; the cleanup contract is the awaited ``coordinator.cleanup()``
    call alone. HA owns the ``entry.runtime_data`` lifecycle.
    """
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


# ---------------------------------------------------------------------------
# async_setup_entry: cloud-failure -> HA setup-outcome exception mapping.
#
# Pre-fix, `client._authenticate()` was awaited bare (so an `AuthFailedError`
# escaped `async_setup_entry` raw — HA logged a generic setup error and never
# dispatched reauth, leaving delete + re-add as the only visible remedy), and
# `_discover_device` only mapped `RuntimeError` onto `return False` — a
# *permanent* failure verdict for what is usually a transient cloud outage.
#
# The contract now is:
#   AuthFailedError        -> ConfigEntryAuthFailed  (HA starts reauth)
#   ConfigEntryAuthFailed  -> re-raised unchanged    (no double-wrap)
#   asyncio.CancelledError -> re-raised              (shutdown is not a failure)
#   anything else          -> ConfigEntryNotReady    (HA retries with backoff)
#
# These call `async_setup_entry` directly (rather than going through
# `hass.config_entries.async_setup`) so the raised exception is observable;
# HA swallows it into entry.state. The one HA-level test below covers the
# user-visible consequence — the reauth flow actually starting.
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_setup_client():
    """Patch AqualinkClient/coordinator at their real import sites.

    `async_setup_entry` imports both lazily *inside* the function body
    (`from .client import AqualinkClient`), so the patch target must be the
    defining module, not the package namespace.
    """
    with patch(
        "custom_components.iaqualink_robots.client.AqualinkClient",
        autospec=True,
    ) as client_cls, patch(
        "custom_components.iaqualink_robots.coordinator.AqualinkDataUpdateCoordinator",
        autospec=True,
    ) as coord_cls:
        client_instance = MagicMock()
        client_instance.set_hass = MagicMock()
        client_instance._authenticate = AsyncMock()
        client_instance._discover_device = AsyncMock()
        client_cls.return_value = client_instance

        coord_instance = MagicMock()
        coord_instance.async_config_entry_first_refresh = AsyncMock()
        coord_instance.cleanup = AsyncMock()
        coord_instance.async_request_refresh = AsyncMock()
        coord_instance.async_update_listeners = MagicMock()
        coord_cls.return_value = coord_instance

        yield {
            "client_cls": client_cls,
            "client": client_instance,
            "coord_cls": coord_cls,
            "coord": coord_instance,
        }


def _current_entry(hass: HomeAssistant) -> MockConfigEntry:
    """A config entry in the current (v3, no api_key) shape, added to hass."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=CURRENT_VERSION,
        data={k: v for k, v in MOCK_ENTRY_DATA.items() if k != "api_key"},
        title=MOCK_NAME,
        unique_id=MOCK_SERIAL,
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_entry_auth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """A rotated password (AuthFailedError from `_authenticate`) must surface
    as `ConfigEntryAuthFailed` so HA dispatches the reauth flow instead of
    logging an opaque setup error.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    mock_setup_client["client"]._authenticate = AsyncMock(
        side_effect=AuthFailedError("401 Unauthorized from Cognito")
    )
    entry = _current_entry(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)

    # The failure short-circuits before the coordinator is constructed.
    mock_setup_client["coord"].async_config_entry_first_refresh.assert_not_awaited()


async def test_setup_entry_auth_failure_during_discovery_raises_auth_failed(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """The mapping covers `_discover_device` too, not just `_authenticate` —
    the cloud can accept the login and then 401 the device lookup when the
    token is stale.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    mock_setup_client["client"]._discover_device = AsyncMock(
        side_effect=AuthFailedError("401 Unauthorized on /get_device_status")
    )
    entry = _current_entry(hass)

    with pytest.raises(ConfigEntryAuthFailed):
        await async_setup_entry(hass, entry)


async def test_setup_entry_reraises_config_entry_auth_failed_unchanged(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """A `ConfigEntryAuthFailed` raised from deeper in the client must pass
    through untouched — not be re-wrapped (which would lose the original
    message) and not be downgraded to `ConfigEntryNotReady` by the broad
    `except Exception` clause below it.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    original = ConfigEntryAuthFailed("token refresh rejected")
    mock_setup_client["client"]._discover_device = AsyncMock(side_effect=original)
    entry = _current_entry(hass)

    with pytest.raises(ConfigEntryAuthFailed) as excinfo:
        await async_setup_entry(hass, entry)

    assert excinfo.value is original


async def test_setup_entry_discovery_runtime_error_raises_not_ready(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """A transient cloud/network failure during discovery must raise
    `ConfigEntryNotReady` so HA retries with backoff.

    Pre-fix this returned False, which HA treats as a permanent setup
    failure: the entry stayed broken until the user manually reloaded it,
    even though the cloud recovered minutes later.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    mock_setup_client["client"]._discover_device = AsyncMock(
        side_effect=RuntimeError("Device with serial R23X12345678 not found")
    )
    entry = _current_entry(hass)

    with pytest.raises(ConfigEntryNotReady) as excinfo:
        await async_setup_entry(hass, entry)

    # The serial is included so the log line identifies which robot failed.
    assert MOCK_SERIAL in str(excinfo.value)


async def test_setup_entry_network_error_during_auth_raises_not_ready(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """A non-auth error from `_authenticate` (cloud down at boot) is also a
    retry-worthy condition, not an auth problem — `ConfigEntryNotReady`.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    mock_setup_client["client"]._authenticate = AsyncMock(
        side_effect=OSError("Cannot connect to host prod.zodiac-io.com")
    )
    entry = _current_entry(hass)

    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)


async def test_setup_entry_cancelled_error_propagates(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """`asyncio.CancelledError` (HA shutting down mid-setup) must propagate
    as-is. Swallowing it into `ConfigEntryNotReady` would leave HA's task
    cancellation half-applied and can hang shutdown.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    mock_setup_client["client"]._authenticate = AsyncMock(
        side_effect=asyncio.CancelledError()
    )
    entry = _current_entry(hass)

    with pytest.raises(asyncio.CancelledError):
        await async_setup_entry(hass, entry)


async def test_setup_entry_success_path_unaffected(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """The happy path still authenticates, discovers, builds the coordinator
    and stashes it on `entry.runtime_data`. Guards against the new try/except
    changing the ordering or swallowing a successful setup.
    """
    from custom_components.iaqualink_robots import async_setup_entry

    entry = _current_entry(hass)

    with patch.object(
        hass.config_entries, "async_forward_entry_setups", new=AsyncMock()
    ) as forward_mock, patch(
        "custom_components.iaqualink_robots.async_at_started",
        return_value=MagicMock(),
    ):
        assert await async_setup_entry(hass, entry) is True

    mock_setup_client["client"]._authenticate.assert_awaited_once()
    mock_setup_client["client"]._discover_device.assert_awaited_once_with(
        target_serial=MOCK_SERIAL
    )
    mock_setup_client["coord"].async_config_entry_first_refresh.assert_awaited_once()
    assert entry.runtime_data.coordinator is mock_setup_client["coord"]
    forward_mock.assert_awaited_once()


async def test_setup_entry_auth_failure_starts_reauth_flow(
    hass: HomeAssistant, mock_setup_client
) -> None:
    """End-to-end consequence of the mapping: going through HA's real setup
    path with bad credentials must leave the entry in SETUP_ERROR *and*
    register a reauth flow the user can complete from the UI.

    This is the bug the mapping fixes — pre-fix the raw `AuthFailedError`
    never reached HA's `ConfigEntryAuthFailed` handling, so no repair card
    appeared and the only apparent fix was delete + re-add.
    """
    mock_setup_client["client"]._authenticate = AsyncMock(
        side_effect=AuthFailedError("401 Unauthorized from Cognito")
    )
    entry = _current_entry(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is config_entries.ConfigEntryState.SETUP_ERROR

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(
        flow["context"].get("source") == config_entries.SOURCE_REAUTH
        for flow in flows
    ), f"expected a reauth flow to be in progress, got {flows}"
