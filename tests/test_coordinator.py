"""Coordinator regression tests.

Seeded as part of the PR #94 follow-up: locks in the natural-cycle-completion
detector (`cleaning → idle` transition triggers `client.clear_desired_state()`)
so future refactors of `_async_update_data` (C4, H6, R23-R30 are all on the
roadmap to touch it) can't silently break the auto-restart fix.

Suite scope is intentionally narrow: only the transition detector. Broader
coordinator behaviour (websocket listener, polling intervals, retry ladder)
gets tested by upcoming stories — each adds its own slice here.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant


def _build_mock_client(
    *,
    device_type: str = "vr",
    pending_stop_reset: bool = False,
    fetch_status_return: dict | None = None,
) -> MagicMock:
    """Build a MagicMock AqualinkClient with the surface area `_async_update_data` touches.

    Includes only what the coordinator's constructor + _async_update_data path
    actually call. Keeps the mock surface minimal so a failure here points at
    the production code, not the fixture.
    """
    client = MagicMock()
    client.robot_id = "test_robot"
    client.device_type = device_type
    client._pending_stop_reset = pending_stop_reset
    # Constructor wiring
    client.set_hass = MagicMock()
    client.set_coordinator_callback = MagicMock()
    client.set_coordinator_reference = MagicMock()
    # _async_update_data wiring
    client.fetch_status = AsyncMock(return_value=fetch_status_return or {"activity": "idle"})
    client.clear_desired_state = AsyncMock()
    client._reset_websocket_failures = MagicMock()
    return client


@pytest.fixture
def coordinator_factory(hass: HomeAssistant):
    """Factory that builds a real AqualinkDataUpdateCoordinator with a mock client.

    Returns a function so tests can vary the client config per-case without
    rebuilding the fixture each time.
    """

    def _make(
        *,
        device_type: str = "vr",
        pending_stop_reset: bool = False,
        fetch_status_return: dict | None = None,
        last_data: dict | None = None,
    ):
        from custom_components.iaqualink_robots.coordinator import (
            AqualinkDataUpdateCoordinator,
        )

        client = _build_mock_client(
            device_type=device_type,
            pending_stop_reset=pending_stop_reset,
            fetch_status_return=fetch_status_return,
        )
        coord = AqualinkDataUpdateCoordinator(hass, client, interval=3.0)
        coord._last_data = last_data or {}
        coord._setup_complete = True  # skip first-call branch (no websocket listener)
        # Polling interval update touches self.update_interval — harmless but noisy in logs.
        coord._update_polling_interval = MagicMock()
        return coord, client

    return _make


# ---------------------------------------------------------------------------
# Natural-cycle-completion detector — locks in the PR #94 fix.
# ---------------------------------------------------------------------------


async def test_natural_completion_clears_desired_state(coordinator_factory) -> None:
    """When the robot transitions cleaning → idle without a manual stop,
    the coordinator must invoke `client.clear_desired_state()`.
    """
    coord, client = coordinator_factory(
        last_data={"activity": "cleaning"},
        fetch_status_return={"activity": "idle"},
    )

    await coord._async_update_data()

    client.clear_desired_state.assert_awaited_once()


async def test_manual_stop_does_not_clear_desired_state(coordinator_factory) -> None:
    """When `_pending_stop_reset` is set, the transition is from a manual stop.
    `clear_desired_state` must NOT be called — the manual stop already sent
    the equivalent payload.
    """
    coord, client = coordinator_factory(
        pending_stop_reset=True,
        last_data={"activity": "cleaning"},
        fetch_status_return={"activity": "idle"},
    )

    await coord._async_update_data()

    client.clear_desired_state.assert_not_awaited()


async def test_no_transition_does_not_clear_desired_state(coordinator_factory) -> None:
    """Steady-state idle (or cleaning, or returning) must not trigger the fix."""
    coord, client = coordinator_factory(
        last_data={"activity": "idle"},
        fetch_status_return={"activity": "idle"},
    )

    await coord._async_update_data()

    client.clear_desired_state.assert_not_awaited()


async def test_cleaning_to_returning_does_not_trigger(coordinator_factory) -> None:
    """Models that pass through `returning` before `idle` are explicitly
    out-of-scope for the current detector (PR #94 limitation #2). This test
    documents that boundary so a future broadening can be a deliberate change.

    When someone extends the check to `cleaning|returning → idle`, this test
    flips to assert_awaited_once and a new test covers `cleaning → returning`.
    """
    coord, client = coordinator_factory(
        last_data={"activity": "cleaning"},
        fetch_status_return={"activity": "returning"},
    )

    await coord._async_update_data()

    client.clear_desired_state.assert_not_awaited()


async def test_clear_desired_state_failure_does_not_crash(coordinator_factory) -> None:
    """If `clear_desired_state` itself raises, the coordinator must log+swallow
    so polling continues. The PR's try/except is the contract we lock in.
    """
    coord, client = coordinator_factory(
        last_data={"activity": "cleaning"},
        fetch_status_return={"activity": "idle"},
    )
    client.clear_desired_state.side_effect = RuntimeError("simulated cloud failure")

    # Must not raise.
    result = await coord._async_update_data()

    assert result["activity"] == "idle"
    client.clear_desired_state.assert_awaited_once()


# ----------------------------------------------------------------------------
# Story C5 — cycle_duration keyed lookup
# ----------------------------------------------------------------------------
#
# Pre-C5, three sites in coordinator.py (around the VR / vortrax / cyclonext
# `_async_update_data` branches) did
#     cycle_duration = list(robot_data['durations'].values())[result["cycle"]]
# which silently relied on dict-insertion-order matching the cycle codes
# 0..3 (wall_only / floor_only / smart_floor_and_walls / floor_and_walls).
# If `cycle` came back as something unexpected -- e.g., 99 from a cloud
# protocol drift -- the IndexError was masked by the surrounding broad
# `except Exception` block. The integration recovered fine (it set
# `cycle_duration = 0` in the except branch), but the failure was silent
# and the operator had no debug breadcrumb.
#
# C5 extracts the lookup into a named helper `_resolve_cycle_duration` on
# `AqualinkClient` with explicit bounds-checking and a DEBUG-log breadcrumb
# for out-of-range cycle codes. Behavior on the success path is unchanged
# (positional values lookup, because the cloud-side key names for the
# `durations` sub-dict are not documented and the team review explicitly
# downgraded "switch to named keys" out of scope until we have a sample).


def _build_client_for_resolve() -> object:
    """Build an AqualinkClient with just enough state for ``_resolve_cycle_duration``.

    Bypasses ``__init__`` (which reaches into aiohttp for header defaults
    and instantiates an asyncio.Lock) -- the helper under test only reads
    its arguments, so an empty shell is sufficient. Mirrors the pattern in
    ``tests/test_set_fan_speed.py``.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    return AqualinkClient.__new__(AqualinkClient)


def test_resolve_cycle_duration_returns_correct_value_for_each_valid_code() -> None:
    """Cycle codes 0..3 each map to the correct positional entry.

    The cloud's `durations` dict (currently) lists entries in cycle-code
    order: index 0 = wall_only's duration, index 1 = floor_only's, etc.
    This test pins that contract so a refactor that, e.g., re-orders the
    helper to use a sorted iteration or wraps the values in a different
    container, can't silently shift the mapping.
    """
    client = _build_client_for_resolve()
    durations = {
        "wallOnly": 30,
        "floorOnly": 60,
        "smart": 90,
        "floorAndWalls": 120,
    }
    assert client._resolve_cycle_duration(durations, 0) == 30
    assert client._resolve_cycle_duration(durations, 1) == 60
    assert client._resolve_cycle_duration(durations, 2) == 90
    assert client._resolve_cycle_duration(durations, 3) == 120


def test_resolve_cycle_duration_out_of_range_returns_zero() -> None:
    """An unexpected cycle code falls back to 0 (no IndexError, no crash).

    Pre-C5 the broad-except masked the `IndexError`. Post-C5 the helper
    bounds-checks explicitly and returns 0; the surrounding production code
    treats 0 as the "no duration known" signal (sets `cycle_duration = 0`
    in the except branch, which is what the user-visible time-remaining
    sensor renders as "0 Hour(s) 0 Minute(s) 0 Second(s)").
    """
    client = _build_client_for_resolve()
    durations = {"a": 30, "b": 60, "c": 90, "d": 120}
    assert client._resolve_cycle_duration(durations, 99) == 0
    assert client._resolve_cycle_duration(durations, -1) == 0


def test_resolve_cycle_duration_non_int_cycle_returns_zero() -> None:
    """A non-int cycle (string, None) is rejected without crashing.

    Cloud occasionally serialises integer fields as strings; defensive.
    """
    client = _build_client_for_resolve()
    durations = {"a": 30, "b": 60, "c": 90, "d": 120}
    assert client._resolve_cycle_duration(durations, "1") == 0
    assert client._resolve_cycle_duration(durations, None) == 0


def test_resolve_cycle_duration_bool_cycle_rejected_despite_int_subclassing() -> None:
    """``bool`` is a subclass of ``int`` in Python -- but a bool cycle is rejected.

    Without the explicit ``not isinstance(cycle, bool)`` clause, ``True``
    would pass ``isinstance(cycle, int)`` and resolve to
    ``durations.values()[1]`` (since ``True == 1``); ``False`` would
    resolve to index 0. Neither is a valid cycle code; treat as the
    "unexpected" path so the DEBUG breadcrumb fires.
    """
    client = _build_client_for_resolve()
    durations = {"a": 30, "b": 60, "c": 90, "d": 120}
    assert client._resolve_cycle_duration(durations, True) == 0
    assert client._resolve_cycle_duration(durations, False) == 0


def test_resolve_cycle_duration_empty_durations_returns_zero() -> None:
    """An empty or missing durations dict falls back cleanly."""
    client = _build_client_for_resolve()
    assert client._resolve_cycle_duration({}, 0) == 0
    assert client._resolve_cycle_duration({"a": 30}, 3) == 0  # in-range cycle, undersized dict


# ----------------------------------------------------------------------------
# Story H6 — pending-stop-reset state-aware apply + max-age expiry
# ----------------------------------------------------------------------------
#
# Pre-H6 the values dict queued by `stop_cleaning` (`_pending_stop_reset`) was
# applied unconditionally on the next `fetch_status`, regardless of how much
# time had elapsed or what the cloud actually reported. Two failure modes:
#
#   1) A user who pressed stop and then immediately restarted cleaning would
#      see the live `activity == "cleaning"` from the cloud silently
#      overwritten back to `"idle"` — the UI lied about what the robot was
#      doing.
#   2) If a poll never landed (long disconnect, network drop), the next
#      successful poll — possibly minutes later — applied the stale reset
#      and clobbered whatever the robot was doing by then.
#
# H6 introduces `_pending_stop_reset_at` as a paired timestamp set at the
# same site that populates the values dict. The new helper
# `AqualinkClient._apply_pending_stop_reset` consults the pair and either
# applies (cloud=idle, age within window), discards as stale (age >
# PENDING_STOP_RESET_MAX_AGE_SECONDS), or discards as superseded (cloud
# already moved past idle). In every branch both halves of the flag pair
# are cleared together — the reset is single-shot.


_PENDING_STOP_RESET_VALUES = {
    "estimated_end_time": None,
    "time_remaining": 0,
    "time_remaining_human": "0 Hour(s) 0 Minute(s) 0 Second(s)",
    "cycle_start_time": None,
    "activity": "idle",
}


def _build_client_for_pending_stop_reset() -> object:
    """AqualinkClient shell for testing `_apply_pending_stop_reset`.

    Bypasses `__init__` (which reaches into aiohttp + asyncio.Lock) — the
    helper under test only reads `_pending_stop_reset`, `_pending_stop_reset_at`,
    the imported constant `PENDING_STOP_RESET_MAX_AGE_SECONDS`, and
    `dt_util.utcnow()`. Mirrors `_build_client_for_resolve` above.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    return AqualinkClient.__new__(AqualinkClient)


def test_pending_stop_reset_applied_when_cloud_reports_idle_within_window() -> None:
    """AC3 happy path. Cloud already moved to idle within the max-age window;
    the queued reset values overlay onto `data` and both halves of the flag
    pair clear so the next poll is a no-op.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    client = _build_client_for_pending_stop_reset()
    client._pending_stop_reset = dict(_PENDING_STOP_RESET_VALUES)
    client._pending_stop_reset_at = dt_util.utcnow() - dt.timedelta(seconds=2)
    data = {"activity": "idle", "model": "VRX iQ+", "fan_speed": "floor_only"}

    result = client._apply_pending_stop_reset(data)

    # Reset values applied.
    assert result["activity"] == "idle"
    assert result["time_remaining"] == 0
    assert result["estimated_end_time"] is None
    assert result["cycle_start_time"] is None
    # Existing keys not touched.
    assert result["model"] == "VRX iQ+"
    assert result["fan_speed"] == "floor_only"
    # Flag pair cleared (single-shot).
    assert client._pending_stop_reset is None
    assert client._pending_stop_reset_at is None


def test_pending_stop_reset_discarded_when_cloud_reports_cleaning() -> None:
    """AC1 + AC4: user-restarts-between-stop-and-poll path.

    Cloud reports `activity == "cleaning"` (the user already restarted).
    The reset must NOT be applied — overwriting cleaning back to idle would
    lie to the UI. The flag pair clears anyway so a second restart cycle
    doesn't see a stale reset still queued.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    client = _build_client_for_pending_stop_reset()
    client._pending_stop_reset = dict(_PENDING_STOP_RESET_VALUES)
    client._pending_stop_reset_at = dt_util.utcnow() - dt.timedelta(seconds=2)
    data = {"activity": "cleaning", "model": "VRX iQ+", "fan_speed": "floor_only"}

    result = client._apply_pending_stop_reset(data)

    # Live state preserved.
    assert result["activity"] == "cleaning"
    # Reset's nulls did NOT overwrite (no time_remaining key was added).
    assert "time_remaining" not in result
    # Existing keys untouched.
    assert result["model"] == "VRX iQ+"
    # Flag pair cleared (single-shot — superseded resets don't zombie back).
    assert client._pending_stop_reset is None
    assert client._pending_stop_reset_at is None


def test_pending_stop_reset_expires_after_max_age() -> None:
    """AC2: a reset older than `PENDING_STOP_RESET_MAX_AGE_SECONDS` is
    discarded without applying.

    Mocks `dt_util.utcnow` inside the coordinator module to advance the
    clock past the threshold without actually sleeping. Uses
    `max_age + 1 s` to avoid inclusive-vs-exclusive boundary ambiguity.
    """
    import datetime as dt
    from unittest.mock import patch

    from homeassistant.util import dt as dt_util

    from custom_components.iaqualink_robots.const import (
        PENDING_STOP_RESET_MAX_AGE_SECONDS,
    )

    client = _build_client_for_pending_stop_reset()
    set_at = dt_util.utcnow()
    client._pending_stop_reset = dict(_PENDING_STOP_RESET_VALUES)
    client._pending_stop_reset_at = set_at
    data = {"activity": "idle", "model": "VRX iQ+"}

    # Pretend the next poll lands `max_age + 1` seconds after stop.
    advanced = set_at + dt.timedelta(seconds=PENDING_STOP_RESET_MAX_AGE_SECONDS + 1)
    with patch(
        "custom_components.iaqualink_robots.coordinator.dt_util.utcnow",
        return_value=advanced,
    ):
        result = client._apply_pending_stop_reset(data)

    # Reset NOT applied — data untouched (no time_remaining key added).
    assert result == {"activity": "idle", "model": "VRX iQ+"}
    # Flag pair cleared so it doesn't re-trigger on a subsequent poll.
    assert client._pending_stop_reset is None
    assert client._pending_stop_reset_at is None


def test_pending_stop_reset_at_boundary_just_below_max_age_applies() -> None:
    """Boundary check: a reset whose age sits at `max_age - 0.5 s` still
    applies. Pins the inclusive side of the threshold.
    """
    import datetime as dt
    from unittest.mock import patch

    from homeassistant.util import dt as dt_util

    from custom_components.iaqualink_robots.const import (
        PENDING_STOP_RESET_MAX_AGE_SECONDS,
    )

    client = _build_client_for_pending_stop_reset()
    set_at = dt_util.utcnow()
    client._pending_stop_reset = dict(_PENDING_STOP_RESET_VALUES)
    client._pending_stop_reset_at = set_at
    data = {"activity": "idle", "model": "VRX iQ+"}

    advanced = set_at + dt.timedelta(
        seconds=PENDING_STOP_RESET_MAX_AGE_SECONDS - 0.5
    )
    with patch(
        "custom_components.iaqualink_robots.coordinator.dt_util.utcnow",
        return_value=advanced,
    ):
        result = client._apply_pending_stop_reset(data)

    # Just-below threshold: reset DOES apply.
    assert result["time_remaining"] == 0
    assert result["cycle_start_time"] is None
    assert client._pending_stop_reset is None
    assert client._pending_stop_reset_at is None


def test_pending_stop_reset_no_op_when_flag_pair_is_none() -> None:
    """No reset queued → helper is a no-op. Returns `data` unchanged."""
    client = _build_client_for_pending_stop_reset()
    client._pending_stop_reset = None
    client._pending_stop_reset_at = None
    data = {"activity": "cleaning", "model": "VRX iQ+"}

    result = client._apply_pending_stop_reset(data)

    assert result == data
    assert client._pending_stop_reset is None
    assert client._pending_stop_reset_at is None


def test_pending_stop_reset_no_op_when_timestamp_missing() -> None:
    """Defensive: a half-set state (values without timestamp) bails cleanly.

    The invariant is "both halves set or both `None`", maintained by the
    single `stop_cleaning` site (the dispatch into vr/cyclobat/cyclonext/
    vortrax/i2d device-type branches happens *inside* `stop_cleaning`, all
    paths share the same pair-stamp). If a future bug breaks the invariant,
    the helper treats the half-set state as no-op rather than crashing on
    `None - datetime` arithmetic.
    """
    client = _build_client_for_pending_stop_reset()
    client._pending_stop_reset = dict(_PENDING_STOP_RESET_VALUES)
    client._pending_stop_reset_at = None  # invariant violation
    data = {"activity": "cleaning", "model": "VRX iQ+"}

    result = client._apply_pending_stop_reset(data)

    # Reset values NOT applied; data passes through untouched.
    assert result == data


# Story H6 also widens the existing PR #94 / natural-completion tests above
# by guaranteeing `_pending_stop_reset` stays a dict-or-None (the truthiness
# the detector relies on is unchanged). The `_pending_stop_reset_at`
# timestamp is invisible to that detector — it's a private contract between
# `stop_cleaning` and `_apply_pending_stop_reset`.


# ----------------------------------------------------------------------------
# Story H10 — delete _immediate_refresh; use async_request_refresh
# ----------------------------------------------------------------------------
#
# Pre-H10 the websocket realtime-update callback routed through a custom
# `_immediate_refresh` that:
#   - bypassed the coordinator's update lock by calling `_async_update_data()`
#     directly,
#   - manually iterated HA-private `self._listeners` (a reach-through into
#     the DataUpdateCoordinator base class's internal listener registry),
#   - then *also* called `async_update_listeners()` — the public equivalent
#     — two lines below, so the manual loop was redundant.
#
# H10 deletes the custom method in favour of `async_request_refresh()`, HA's
# documented API for the same job. The behavioural change is positive: the
# default ~0.3 s debouncer collapses rapid websocket-event bursts into a
# single refresh, which the old manual loop did not.


def test_immediate_refresh_method_removed() -> None:
    """AC #1: `_immediate_refresh` is deleted from the coordinator class.

    Asserted at the class level rather than via instance to avoid spinning
    up a real ``AqualinkDataUpdateCoordinator``; the method either exists
    on the class (regression) or it doesn't (post-H10 state).
    """
    from custom_components.iaqualink_robots.coordinator import (
        AqualinkDataUpdateCoordinator,
    )

    assert not hasattr(AqualinkDataUpdateCoordinator, "_immediate_refresh"), (
        "`_immediate_refresh` should be removed in H10; "
        "real-time updates now go through `async_request_refresh()` (HA's "
        "public API) which honours the coordinator lock and uses the default "
        "debouncer to collapse rapid event bursts."
    )


def test_no_self_listeners_attribute_access_in_package() -> None:
    """AC #3: no `self._listeners` reach-through anywhere in the package.

    `_listeners` is the HA `DataUpdateCoordinator` base class's internal
    listener registry — private API that may change shape on any HA minor
    release. The public way to notify listeners is `async_update_listeners()`.

    Uses AST traversal so docstrings or block comments that *mention* the
    name (e.g. the historical note in `_handle_realtime_update`) are
    correctly ignored — only actual ``self._listeners`` attribute access
    counts as a reach-through.
    """
    import ast
    from pathlib import Path

    pkg = Path(__file__).parent.parent / "custom_components" / "iaqualink_robots"
    offenders: list[str] = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "_listeners"
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "`self._listeners` reach-through detected. Use the public "
        "`async_update_listeners()` API (called automatically by "
        "`async_request_refresh()`) instead:\n  "
        + "\n  ".join(offenders)
    )


async def test_handle_realtime_update_routes_through_async_request_refresh(
    coordinator_factory,
) -> None:
    """AC #2 + AC #4: the realtime callback now calls
    ``async_request_refresh`` instead of the deleted ``_immediate_refresh``.

    Patches the coordinator's `async_request_refresh` to a tracker mock so
    the test doesn't actually drive the full update loop; the behavioural
    contract under test is "the realtime trigger fires a refresh request",
    not "the refresh succeeds".
    """
    from unittest.mock import AsyncMock

    coord, _client = coordinator_factory(
        last_data={"activity": "cleaning"},
        fetch_status_return={"activity": "idle"},
    )
    coord.async_request_refresh = AsyncMock()

    await coord._handle_realtime_update()

    coord.async_request_refresh.assert_awaited_once()


async def test_handle_realtime_update_swallows_refresh_failure(
    coordinator_factory,
) -> None:
    """The pre-H10 method wrapped its body in try/except so a refresh
    failure didn't propagate up the websocket listener task and cause it
    to die. H10 keeps that contract — `async_request_refresh` failures
    must be logged + swallowed, not raised.
    """
    from unittest.mock import AsyncMock

    coord, _client = coordinator_factory(last_data={"activity": "idle"})
    coord.async_request_refresh = AsyncMock(side_effect=RuntimeError("simulated"))

    # Must not raise.
    await coord._handle_realtime_update()

    coord.async_request_refresh.assert_awaited_once()


# ----------------------------------------------------------------------------
# Story H7 — available-threshold rewrite with `restored` indicator
# ----------------------------------------------------------------------------
#
# Pre-H7 entity `available` properties only checked `coordinator.data is not
# None`. The coordinator served stale `_last_data` through the broad-except
# path until `_consecutive_failures > 30` (the now-removed
# `_max_failures_before_unavailable` attribute), at which point it raised
# `UpdateFailed` and HA marked the entity unavailable. With adaptive polling
# (1.5 s ↔ 10 s), 30 failures spans anywhere from 45 s (active robot) to
# 5 min (idle robot) — short enough that routine ISP blips would disable
# user automations bound to the `available` state.
#
# H7 replaces the count-based gate with a wall-clock threshold:
# `LONG_OUTAGE_THRESHOLD_SECONDS` (currently 30 min). The coordinator stamps
# `_first_failure_at` on the 0→1 transition of `_consecutive_failures` and
# clears it on a successful update. The new properties:
#
#   * `coordinator.is_long_outage`        — True once the outage exceeds the
#                                           threshold; entity `available`
#                                           properties consult it.
#   * `coordinator.is_serving_stale_data` — True any time stale data is being
#                                           returned; surfaced as the
#                                           `restored` attribute on every
#                                           entity.


def test_is_long_outage_false_when_no_outage(coordinator_factory) -> None:
    """Fresh coordinator with no failures → `is_long_outage` is False."""
    coord, _client = coordinator_factory()
    assert coord._first_failure_at is None
    assert coord._consecutive_failures == 0
    assert coord.is_long_outage is False
    assert coord.is_serving_stale_data is False


def test_is_long_outage_false_during_short_blip(coordinator_factory) -> None:
    """A blip <`LONG_OUTAGE_THRESHOLD_SECONDS` (the AC #1 / AC #4 contract)
    keeps `is_long_outage` False so entity `available` stays True.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    coord, _client = coordinator_factory()
    coord._consecutive_failures = 5
    coord._first_failure_at = dt_util.utcnow() - dt.timedelta(seconds=60)
    coord._last_data = {"activity": "cleaning"}  # H7 review follow-up: is_serving_stale_data requires real cached data

    assert coord.is_long_outage is False
    assert coord.is_serving_stale_data is True  # stale-data flag IS True


def test_is_long_outage_true_after_threshold_exceeded(coordinator_factory) -> None:
    """Once the outage exceeds `LONG_OUTAGE_THRESHOLD_SECONDS` (AC #2 / AC #5)
    `is_long_outage` is True — entity `available` then flips to False.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    from custom_components.iaqualink_robots.const import (
        LONG_OUTAGE_THRESHOLD_SECONDS,
    )

    coord, _client = coordinator_factory()
    coord._consecutive_failures = 999
    coord._first_failure_at = dt_util.utcnow() - dt.timedelta(
        seconds=LONG_OUTAGE_THRESHOLD_SECONDS + 5
    )
    coord._last_data = {"activity": "cleaning"}  # H7 review follow-up: is_serving_stale_data requires real cached data

    assert coord.is_long_outage is True
    assert coord.is_serving_stale_data is True


def test_is_long_outage_at_boundary_just_below_threshold_remains_false(
    coordinator_factory,
) -> None:
    """At exactly `threshold − 0.5 s` the outage is NOT long yet.

    Pins the inclusive side of the comparison so an off-by-one refactor that
    flips ``> threshold`` to ``>= threshold`` is caught.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    from custom_components.iaqualink_robots.const import (
        LONG_OUTAGE_THRESHOLD_SECONDS,
    )

    coord, _client = coordinator_factory()
    coord._consecutive_failures = 200
    coord._first_failure_at = dt_util.utcnow() - dt.timedelta(
        seconds=LONG_OUTAGE_THRESHOLD_SECONDS - 0.5
    )

    assert coord.is_long_outage is False


def test_is_serving_stale_data_tracks_consecutive_failures(coordinator_factory) -> None:
    """`is_serving_stale_data` is True iff `_consecutive_failures > 0` AND
    `_last_data` is non-empty (i.e. we have real cached data to serve).
    Flips automatically on recovery — AC #6.

    The `bool(_last_data)` guard is the H7 review follow-up; without it,
    the flag would return True on the first-ever poll failure even though
    the entity is showing synthetic offline-minimal data, not anything
    being "restored".
    """
    coord, _client = coordinator_factory()
    assert coord.is_serving_stale_data is False

    # Failure WITH prior real data → True (the real "restored" case).
    coord._consecutive_failures = 1
    coord._last_data = {"activity": "cleaning"}
    assert coord.is_serving_stale_data is True

    # Failure WITHOUT prior real data → False (synthetic-offline case).
    coord._last_data = {}
    assert coord.is_serving_stale_data is False

    # Recovery → False either way.
    coord._consecutive_failures = 0
    coord._last_data = {"activity": "cleaning"}
    assert coord.is_serving_stale_data is False


async def test_first_failure_at_set_on_zero_to_one_transition(
    coordinator_factory,
) -> None:
    """The broad-except path stamps `_first_failure_at` on the first failure
    of a new streak.
    """
    coord, client = coordinator_factory()
    client.fetch_status.side_effect = RuntimeError("simulated cloud outage")

    # Pre-failure: no streak.
    assert coord._first_failure_at is None

    await coord._async_update_data()

    # First failure: timestamp populated.
    assert coord._first_failure_at is not None
    assert coord._consecutive_failures == 1


async def test_first_failure_at_unchanged_on_subsequent_failures(
    coordinator_factory,
) -> None:
    """Subsequent failures within a streak do NOT re-stamp `_first_failure_at`
    — that would mask the true outage duration.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    coord, client = coordinator_factory()
    client.fetch_status.side_effect = RuntimeError("simulated cloud outage")

    # Seed an existing streak from 30 s ago.
    coord._first_failure_at = dt_util.utcnow() - dt.timedelta(seconds=30)
    coord._consecutive_failures = 10
    original_stamp = coord._first_failure_at

    await coord._async_update_data()

    # Stamp unchanged; counter incremented.
    assert coord._first_failure_at == original_stamp
    assert coord._consecutive_failures == 11


async def test_first_failure_at_cleared_on_successful_recovery(
    coordinator_factory,
) -> None:
    """Recovery from an outage clears `_first_failure_at` AND
    `_consecutive_failures` so `is_serving_stale_data` flips back to False
    on the next poll (AC #6).
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util

    coord, _client = coordinator_factory(
        last_data={"activity": "idle"},
        fetch_status_return={"activity": "idle"},
    )

    # Seed an in-progress outage.
    coord._first_failure_at = dt_util.utcnow() - dt.timedelta(seconds=60)
    coord._consecutive_failures = 5
    assert coord.is_serving_stale_data is True

    # A successful poll recovers.
    await coord._async_update_data()

    assert coord._first_failure_at is None
    assert coord._consecutive_failures == 0
    assert coord.is_serving_stale_data is False
    assert coord.is_long_outage is False


async def test_long_outage_raises_update_failed(coordinator_factory) -> None:
    """When `is_long_outage` is True at the moment a failure lands, the
    coordinator raises `UpdateFailed` (HA marks the entity unavailable)
    rather than returning stale `_last_data`.
    """
    import datetime as dt

    from homeassistant.util import dt as dt_util
    from homeassistant.helpers.update_coordinator import UpdateFailed
    import pytest

    from custom_components.iaqualink_robots.const import (
        LONG_OUTAGE_THRESHOLD_SECONDS,
    )

    coord, client = coordinator_factory(last_data={"activity": "idle"})
    client.fetch_status.side_effect = RuntimeError("simulated long outage")

    # Pre-seed a streak that is *already* past the long-outage threshold,
    # so this failure tips the gate.
    coord._first_failure_at = dt_util.utcnow() - dt.timedelta(
        seconds=LONG_OUTAGE_THRESHOLD_SECONDS + 5
    )
    coord._consecutive_failures = 100

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_short_outage_returns_last_data_not_update_failed(
    coordinator_factory,
) -> None:
    """When `is_long_outage` is False at the moment a failure lands, the
    coordinator returns cached `_last_data` so entities stay available
    through the blip (AC #1 / AC #4).
    """
    coord, client = coordinator_factory(last_data={"activity": "cleaning", "model": "RA"})
    client.fetch_status.side_effect = RuntimeError("simulated transient blip")

    result = await coord._async_update_data()

    # Cached data returned; no UpdateFailed raised.
    assert result["activity"] == "cleaning"
    assert result["model"] == "RA"
    # Streak started.
    assert coord._first_failure_at is not None
    assert coord._consecutive_failures == 1
    # Not yet a long outage.
    assert coord.is_long_outage is False
    assert coord.is_serving_stale_data is True


# ----------------------------------------------------------------------------
# Story C3 — remove phantom hass.localize call
# ----------------------------------------------------------------------------
#
# `hass.localize` is a frontend JavaScript API; it does not exist on the
# Python `HomeAssistant` object. The pre-C3 `_format_time_human` wrapped its
# return value in three `self._hass.localize(...)` calls inside a try/except
# `(KeyError, AttributeError)` block — every call raised `AttributeError`,
# was silently swallowed, and fell through to the English string. Net effect:
# the "localised" branch never executed once. C3 deleted the whole block in
# favour of the English fallback that was already serving every call.
#
# The localised time-unit display ("Hour(s)" → "Heure(s)" / "Uur(en)") is
# intentionally still missing post-C3 — the correct mechanism is HA's
# `translation_key` paired with `translations/*.json`, not a custom localize
# wrapper in the coordinator. See stories M11 and P7 for the proper fix.


def test_no_phantom_localize_calls_in_package() -> None:
    """Any `.localize(` call in the package is dead by definition (the API
    doesn't exist on the Python `hass` object) — C3 deleted the last three
    instances. Lock that in with an AST walk so a future copy-paste of the
    pattern from frontend examples doesn't silently reintroduce dead code.

    Uses AST traversal so the explanatory docstring in `_format_time_human`
    (which mentions `self._hass.localize(...)` for historical context) is
    correctly ignored — only actual call expressions count.
    """
    import ast
    from pathlib import Path

    pkg = Path(__file__).parent.parent / "custom_components" / "iaqualink_robots"
    offenders: list[str] = []
    for path in sorted(pkg.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "localize"
            ):
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        "`.localize(` call detected. `hass.localize` is a frontend JS API; "
        "it does not exist on the Python `HomeAssistant` object. Every "
        "such call raises `AttributeError` and is dead code (story C3). "
        "Use HA's `translation_key` + `translations/*.json` mechanism if "
        "you need a localised entity display:\n  " + "\n  ".join(offenders)
    )
