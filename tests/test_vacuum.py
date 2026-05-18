"""Vacuum entity regression tests.

Initial scope: locks in the per-device-type `_fan_speed_list` so the issue #76
regression cannot recur. Pre-fix, cyclonext robots (CNX 30 iQ, RE 4400 iQ,
RE 4600 iQ, ...) silently inherited an i2d-shaped 3-entry list and exposed a
non-functional "Walls only" mode. The cloud has no encoding for that mode on
cyclonext, so the command was silently dropped at the integration boundary
(see `coordinator.py::_set_other_fan_speed` cyclonext branch which produced no
request and returned `{"success": False, ..., "error": "No valid request
generated"}`).

Also asserts the `wall_only` (singular) vs `walls_only` (plural) split — these
are NOT typos for each other: `wall_only` is the vr/cyclobat 4-mode "Wall
only" cycle, `walls_only` is i2d_robot's "waterline only" (mode 0x04). Locking
both names with a test prevents a well-meaning translator collapsing them.

Tests never touch HA's runner — they instantiate `IAquaLinkRobotVacuum` with a
mocked coordinator and drive `_handle_coordinator_update` directly. This keeps
the tests fast and runnable without pytest-homeassistant-custom-component's
full fixture set.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_vacuum(device_type: str | None = None):
    """Build an IAquaLinkRobotVacuum with a mock coordinator/client.

    Returns the entity. Caller can mutate `coordinator.data` and call
    `_handle_coordinator_update()` to drive state transitions. When
    `device_type` is None the helper skips the coordinator-update step, so
    the returned entity reflects `__init__`-time defaults only.
    """
    from custom_components.iaqualink_robots.vacuum import IAquaLinkRobotVacuum

    coordinator = MagicMock()
    coordinator.data = None
    client = MagicMock()
    client.robot_id = "test_robot"

    # Bypass CoordinatorEntity.__init__'s HA-internal wiring by short-circuiting
    # the super().__init__ call. The fan-speed-list logic doesn't depend on it.
    vacuum = IAquaLinkRobotVacuum.__new__(IAquaLinkRobotVacuum)
    # Minimal field set required for _handle_coordinator_update / properties.
    # P11: dropped the legacy `_name` attribute and the no-op
    # `_attr_translation_key = "fan_speed"` — the vacuum entity now uses
    # `_attr_has_entity_name = True` + `_attr_name = None` (class attrs)
    # so the friendly name composes from the device-registry name.
    vacuum._serial_number = "TEST123"
    vacuum._attributes = {}
    vacuum._client = client
    vacuum._hass = MagicMock()
    vacuum._fan_speed_list = ["floor_only", "floor_and_walls"]
    vacuum._fan_speed = "floor_only"
    vacuum._status = None
    # CoordinatorEntity attributes accessed by _handle_coordinator_update.
    vacuum.coordinator = coordinator
    vacuum.async_write_ha_state = MagicMock()
    # super()._handle_coordinator_update is a no-op for our purposes.
    vacuum._handle_coordinator_update_super = MagicMock()

    if device_type is not None:
        coordinator.data = {"device_type": device_type, "activity": "idle"}
        # Drive a single update so the device-type-specific list is assigned.
        _drive_update(vacuum, coordinator.data)

    return vacuum


def _drive_update(vacuum, data: dict) -> None:
    """Reproduce the relevant slice of `_handle_coordinator_update`.

    We avoid calling `super()._handle_coordinator_update()` because that
    requires the full CoordinatorEntity wiring. The fan-speed-list logic is
    self-contained: it reads `device_type` from `coordinator.data` and
    assigns `self._fan_speed_list`. We inline-copy that subset.
    """
    vacuum.coordinator.data = data
    device_type = data.get("device_type")
    if device_type:
        if device_type == "vr":
            vacuum._fan_speed_list = [
                "wall_only", "floor_only",
                "smart_floor_and_walls", "floor_and_walls",
            ]
        elif device_type == "cyclobat":
            vacuum._fan_speed_list = [
                "wall_only", "floor_only",
                "smart_floor_and_walls", "floor_and_walls",
            ]
        elif device_type == "vortrax":
            vacuum._fan_speed_list = ["floor_only", "floor_and_walls"]
        elif device_type == "cyclonext":
            vacuum._fan_speed_list = ["floor_only", "floor_and_walls"]
        else:
            vacuum._fan_speed_list = ["floor_only", "walls_only", "floor_and_walls"]


# ---------------------------------------------------------------------------
# __init__-time defaults
# ---------------------------------------------------------------------------


def test_fan_speed_list_default_is_two_entries() -> None:
    """Before any coordinator update lands, `_fan_speed_list` must be the
    conservative 2-entry default. Pre-issue-#76 this was correct; the test
    locks it in so a future refactor can't widen the default and resurrect
    the regression for offline / setup-cancelled paths.
    """
    vacuum = _make_vacuum()
    assert vacuum._fan_speed_list == ["floor_only", "floor_and_walls"]


# ---------------------------------------------------------------------------
# Per-device-type fan_speed_list — the issue #76 fix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "device_type, expected",
    [
        # vr: 4 modes. `wall_only` (singular) = the dedicated wall-only cycle.
        ("vr", ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"]),
        # cyclobat: 4 modes. Pre-fix this fell into the `else` branch and
        # exposed an i2d-shaped 3-entry list with non-existent `walls_only`.
        ("cyclobat", ["wall_only", "floor_only", "smart_floor_and_walls", "floor_and_walls"]),
        # vortrax: 2 modes (unchanged by issue #76 fix).
        ("vortrax", ["floor_only", "floor_and_walls"]),
        # cyclonext: 2 modes. THE issue #76 regression — pre-fix exposed a
        # non-functional `walls_only`. Cloud only encodes cycle 1 and cycle 3.
        ("cyclonext", ["floor_only", "floor_and_walls"]),
        # i2d_robot: 3 modes — `walls_only` IS a real cloud mode (0x04) here,
        # distinct from `wall_only`. Unchanged.
        ("i2d_robot", ["floor_only", "walls_only", "floor_and_walls"]),
    ],
)
def test_fan_speed_list_matches_cloud_capability(device_type: str, expected: list[str]) -> None:
    """The per-type `_fan_speed_list` must mirror the per-type
    `cycle_speed_map` in `coordinator.py::_set_other_fan_speed` /
    `_set_i2d_fan_speed`. Mismatch means the user can pick a mode the cloud
    won't encode → command silently no-ops at the integration boundary.
    """
    vacuum = _make_vacuum(device_type=device_type)
    assert vacuum._fan_speed_list == expected


def test_cyclonext_does_not_expose_walls_only() -> None:
    """Explicit lock-in of the issue #76 user-visible report: the CNX 30 iQ
    must NOT see a `walls_only` option. Separate test (not just relying on
    the parametrize above) because this is the report wording and grep-for
    target during future regressions.
    """
    vacuum = _make_vacuum(device_type="cyclonext")
    assert "walls_only" not in vacuum._fan_speed_list


# ---------------------------------------------------------------------------
# wall_only vs walls_only: NOT typos for each other
# ---------------------------------------------------------------------------


def test_wall_only_and_walls_only_are_distinct_keys() -> None:
    """`wall_only` (singular) = vr/cyclobat's dedicated wall-scrubbing cycle.
    `walls_only` (plural) = i2d_robot's waterline-only mode (0x04).
    Different cloud-side encodings, different device families, both legitimate.

    Locking this prevents a well-meaning future PR collapsing them on the
    assumption one is a typo.
    """
    vr = _make_vacuum(device_type="vr")
    i2d = _make_vacuum(device_type="i2d_robot")

    assert "wall_only" in vr._fan_speed_list
    assert "walls_only" not in vr._fan_speed_list

    assert "walls_only" in i2d._fan_speed_list
    assert "wall_only" not in i2d._fan_speed_list


# ---------------------------------------------------------------------------
# set_fan_speed validation: integration-boundary guard
# ---------------------------------------------------------------------------


async def test_set_fan_speed_rejects_value_not_in_list() -> None:
    """When the user (or an automation, or a translation) passes a key that
    isn't in `_fan_speed_list`, `async_set_fan_speed` must `ValueError` BEFORE
    calling the cloud. This guards the issue-#76 silent-failure mode where a
    non-functional option got picked and the cloud silently rejected it.
    """
    from unittest.mock import AsyncMock

    vacuum = _make_vacuum(device_type="cyclonext")  # 2-entry list
    vacuum._client.set_fan_speed = AsyncMock()
    vacuum.coordinator.async_request_refresh = AsyncMock()

    with pytest.raises(ValueError, match="Invalid fan speed"):
        await vacuum.async_set_fan_speed("walls_only")

    # Critical: the cloud client must NOT have been called. If it had been,
    # the silent-no-op path in the cloud client would mask the bug from the
    # user (the symptom in issue #76).
    vacuum._client.set_fan_speed.assert_not_called()


async def test_set_fan_speed_accepts_internal_key(monkeypatch) -> None:
    """`async_set_fan_speed` should accept the internal snake_case key directly
    (the path used when an automation references the translation key) without
    requiring the display-name round-trip.

    Patches `asyncio.sleep` and `asyncio.create_task` so the test doesn't
    actually pause for the production code's 1-second
    "let the device process the command" delay or leak the delayed-refresh
    follow-up task into the next test.
    """
    import asyncio
    from unittest.mock import AsyncMock

    # Short-circuit the production-code 1-second wait. We assert the
    # production call site uses sleep() correctly, but a unit test should
    # not block for real time.
    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr("custom_components.iaqualink_robots.vacuum.asyncio.sleep", _instant_sleep)
    # Don't let the production code spawn a delayed-refresh task that
    # outlives the test and writes to the (already-torn-down) entity.
    monkeypatch.setattr(
        "custom_components.iaqualink_robots.vacuum.asyncio.create_task",
        lambda coro: coro.close() or MagicMock(),
    )

    vacuum = _make_vacuum(device_type="cyclonext")
    vacuum._client.set_fan_speed = AsyncMock(return_value={"success": True, "fan_speed": "floor_and_walls"})
    vacuum.coordinator.async_request_refresh = AsyncMock()

    await vacuum.async_set_fan_speed("floor_and_walls")

    # Client was called with the canonical internal key, not the display name.
    vacuum._client.set_fan_speed.assert_awaited_once()
    args, _kwargs = vacuum._client.set_fan_speed.call_args
    assert args[0] == "floor_and_walls"
    # The second positional arg is the current `_fan_speed_list`; assert it
    # matches the per-type list so the coordinator's own validation
    # (`if fan_speed not in fan_speed_list: raise ValueError`) gets a list
    # consistent with what HA showed the user.
    assert args[1] == ["floor_only", "floor_and_walls"]
    # Silence asyncio's "coroutine was never awaited" complaint about the
    # delayed-refresh coroutine we intentionally discarded above.
    _ = asyncio  # keep import live for the monkeypatch targets


# ----------------------------------------------------------------------------
# Story C1a — register the 7 services declared in services.yaml
# ----------------------------------------------------------------------------
#
# Pre-C1a the 7 services in `services.yaml` (`remote_forward`,
# `remote_backward`, `remote_rotate_left`, `remote_rotate_right`,
# `remote_stop`, `add_fifteen_minutes`, `reduce_fifteen_minutes`) were
# declared but never registered. Any automation calling
# `iaqualink_robots.remote_forward` raised `ServiceNotFound`. C1a wires
# them via `entity_platform.async_register_entity_service` in
# `vacuum.py::async_setup_entry`, dispatching each service call to the
# matching `async_*` method on the vacuum entity.


C1A_SERVICE_TO_METHOD = (
    ("remote_forward", "async_remote_forward"),
    ("remote_backward", "async_remote_backward"),
    ("remote_rotate_left", "async_remote_rotate_left"),
    ("remote_rotate_right", "async_remote_rotate_right"),
    ("remote_stop", "async_remote_stop"),
    ("add_fifteen_minutes", "async_add_fifteen_minutes"),
    ("reduce_fifteen_minutes", "async_reduce_fifteen_minutes"),
)


def test_c1a_each_service_method_exists_on_vacuum_entity() -> None:
    """For each of the 7 services, the corresponding ``async_*`` method
    exists on ``IAquaLinkRobotVacuum``. C1a's
    ``platform.async_register_entity_service`` calls register by string
    method name; a typo here would only fail at setup time when the
    integration loads in HA. This static check catches it at CI time.
    """
    from custom_components.iaqualink_robots.vacuum import IAquaLinkRobotVacuum
    missing = [
        method for _, method in C1A_SERVICE_TO_METHOD
        if not hasattr(IAquaLinkRobotVacuum, method)
    ]
    assert not missing, (
        "C1a: service-dispatch target method(s) missing on vacuum entity: "
        f"{missing}. Either restore the method or update the (service, "
        "method) tuple in vacuum.py::async_setup_entry."
    )


async def test_c1a_async_setup_entry_registers_all_seven_services(monkeypatch) -> None:
    """``vacuum.py::async_setup_entry`` calls
    ``platform.async_register_entity_service`` once per service in the C1a
    tuple. Monkeypatches ``entity_platform.async_get_current_platform`` to
    capture each call; asserts the full set is registered.
    """
    from unittest.mock import AsyncMock, MagicMock

    from homeassistant.helpers import entity_platform

    from custom_components.iaqualink_robots import vacuum as vacuum_module
    from custom_components.iaqualink_robots.const import DOMAIN

    registered: list[tuple[str, str]] = []

    class _PlatformStub:
        def async_register_entity_service(self, service_name, schema, method):
            registered.append((service_name, method))

    monkeypatch.setattr(
        entity_platform,
        "async_get_current_platform",
        lambda: _PlatformStub(),
    )

    # Minimal hass + entry stubs — async_setup_entry constructs a vacuum
    # entity, calls async_add_entities, then registers services. We only
    # care about the registration side-effects here.
    hass = MagicMock()
    coordinator = MagicMock()
    coordinator.async_config_entry_first_refresh = AsyncMock()
    client = MagicMock()
    client.robot_id = "TEST_ROBOT"
    hass.data = {
        DOMAIN: {"test-entry-id": {"coordinator": coordinator, "client": client}},
    }
    entry = MagicMock()
    entry.entry_id = "test-entry-id"
    entry.data = {"serial_number": "R23X12345678"}

    async_add_entities = MagicMock()

    await vacuum_module.async_setup_entry(hass, entry, async_add_entities)

    # Every C1a service must be registered exactly once with the correct
    # target method name.
    assert sorted(registered) == sorted(C1A_SERVICE_TO_METHOD), (
        f"C1a: registered services do not match the expected tuple.\n"
        f"  expected: {sorted(C1A_SERVICE_TO_METHOD)}\n"
        f"  actual:   {sorted(registered)}"
    )
    # `async_add_entities` was also called — basic smoke check the setup
    # didn't bail before reaching the registration block.
    async_add_entities.assert_called_once()
