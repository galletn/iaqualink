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

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed


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
# documented API for the same job. The behavioural change is positive: HA's
# default request-refresh debouncer (cooldown=10s, immediate=True) fires the
# first call synchronously and coalesces subsequent calls within 10s into a
# single trailing refresh — the old manual loop had no such coalescing.
# (C4-cmd review F2 2026-05-18 corrected an earlier "~0.3 s" claim here.)


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
    for path in sorted(pkg.rglob("*.py")):  # rglob walks subpackages too
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
    for path in sorted(pkg.rglob("*.py")):  # rglob walks subpackages too
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


# ----------------------------------------------------------------------------
# Story C4-cmd — replace 3-task callback bursts with one awaited refresh
# ----------------------------------------------------------------------------
#
# Pre-C4-cmd each of the 6 client command methods (start_cleaning,
# stop_cleaning, pause_cleaning, return_to_base, add_fifteen_minutes,
# reduce_fifteen_minutes) fired three fire-and-forget `asyncio.create_task`
# callbacks immediately after sending the cloud request:
#
#     asyncio.create_task(self._coordinator_callback())     # t=0
#     asyncio.create_task(self._delayed_callback(0.5))      # t=0.5s
#     asyncio.create_task(self._delayed_callback(1.0))      # t=1.0s
#
# Combined with the vacuum entity and button entity's own
# `async_request_refresh()` calls, a single press triggered 5+ overlapping
# refreshes hitting the same websocket — the *structural* source of the
# connection flap that the 80-line `_status_history` stabilizer was added to
# mask (story R30 will remove the stabilizer once C4-cmd has soaked).
#
# C4-cmd replaces each triple with a single `await self._coordinator_callback()`.
# The callback routes through the coordinator's `_handle_realtime_update`
# (story H10), which calls the debounced `async_request_refresh` — so any
# near-duplicate refresh from the caller (button.py / vacuum.py) collapses
# into one actual `_async_update_data` call.


def _build_client_for_command() -> object:
    """Build an `AqualinkClient` with the minimal surface area each command
    method touches.

    Bypasses `__init__` (which reaches into aiohttp for header defaults and
    instantiates an asyncio.Lock); the command methods we exercise only read
    the device-type / serial / auth strings and call `set_cleaner_state`,
    so a hand-stitched shell is sufficient. Mirrors the pattern in the
    existing C5/H6 tests above.
    """
    from unittest.mock import AsyncMock, MagicMock

    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient.__new__(AqualinkClient)
    client._device_type = "vr"
    client._serial = "TEST-SERIAL"
    client._id = "user-id"
    client._auth_token = "tok"
    client._app_client_id = "acid"
    client._debug_mode = False
    # Cloud roundtrip stubbed; the command method's wait_for completes immediately.
    client.set_cleaner_state = AsyncMock(return_value={"ok": True})
    # post_command_i2d is the i2d path; not exercised for `device_type="vr"` tests
    # but kept for completeness if a future test switches device_type.
    client.post_command_i2d = AsyncMock(return_value={"ok": True})
    # Adaptive-polling marker — start_cleaning calls _coordinator_ref._mark_recent_activity()
    client._coordinator_ref = MagicMock()
    # add_fifteen_minutes / reduce_fifteen_minutes call _should_use_websocket()
    # and fetch_status() before the cloud roundtrip.
    client._should_use_websocket = MagicMock(return_value=True)
    client.fetch_status = AsyncMock(return_value={"stepper": 0})
    return client


async def test_start_cleaning_awaits_coordinator_callback_exactly_once() -> None:
    """C4-cmd AC #1 + AC #2 (start_cleaning): the post-command refresh is
    a single awaited call to `_coordinator_callback`, not 3 fire-and-forget
    tasks (immediate + 0.5s + 1.0s delayed).
    """
    from unittest.mock import AsyncMock

    client = _build_client_for_command()
    callback = AsyncMock()
    client._coordinator_callback = callback

    await client.start_cleaning()

    callback.assert_awaited_once()


async def test_stop_cleaning_awaits_coordinator_callback_exactly_once() -> None:
    """C4-cmd AC #1 + AC #2 (stop_cleaning)."""
    from unittest.mock import AsyncMock

    client = _build_client_for_command()
    callback = AsyncMock()
    client._coordinator_callback = callback

    await client.stop_cleaning()

    callback.assert_awaited_once()


async def test_pause_cleaning_awaits_coordinator_callback_exactly_once() -> None:
    """C4-cmd AC #1 + AC #2 (pause_cleaning)."""
    from unittest.mock import AsyncMock

    client = _build_client_for_command()
    callback = AsyncMock()
    client._coordinator_callback = callback

    await client.pause_cleaning()

    callback.assert_awaited_once()


async def test_return_to_base_awaits_coordinator_callback_exactly_once() -> None:
    """C4-cmd AC #1 + AC #2 (return_to_base)."""
    from unittest.mock import AsyncMock

    client = _build_client_for_command()
    callback = AsyncMock()
    client._coordinator_callback = callback

    await client.return_to_base()

    callback.assert_awaited_once()


async def test_add_fifteen_minutes_awaits_coordinator_callback_exactly_once() -> None:
    """C4-cmd AC #1 + AC #2 (add_fifteen_minutes)."""
    from unittest.mock import AsyncMock

    client = _build_client_for_command()
    callback = AsyncMock()
    client._coordinator_callback = callback

    await client.add_fifteen_minutes()

    callback.assert_awaited_once()


async def test_reduce_fifteen_minutes_awaits_coordinator_callback_exactly_once() -> None:
    """C4-cmd AC #1 + AC #2 (reduce_fifteen_minutes)."""
    from unittest.mock import AsyncMock

    client = _build_client_for_command()
    callback = AsyncMock()
    client._coordinator_callback = callback

    await client.reduce_fifteen_minutes()

    callback.assert_awaited_once()


def test_delayed_callback_helper_removed() -> None:
    """C4-cmd: the `_delayed_callback` helper on `AqualinkClient` becomes
    orphaned after the 6 command sites stop spawning the 0.5s/1.0s tasks.

    Asserted at the class level rather than via instance — the method either
    exists on the class (regression: someone re-added the delayed-burst
    pattern) or it doesn't (post-C4-cmd state). The story explicitly calls
    out "Audit the `_delayed_refresh` helper — if it becomes orphaned after
    this change, delete it."
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    assert not hasattr(AqualinkClient, "_delayed_callback"), (
        "`_delayed_callback` should be removed in C4-cmd; the 6 client "
        "command methods now await `_coordinator_callback()` directly "
        "instead of spawning delayed `asyncio.create_task` bursts. If you "
        "need a delayed follow-up refresh in one specific site, await "
        "`asyncio.sleep(...)` + the callback inline; do not reintroduce a "
        "shared fire-and-forget helper."
    )


def test_no_fire_and_forget_callback_create_task_in_command_methods() -> None:
    """C4-cmd regression guard: AST walk of the 6 command methods asserts
    no `asyncio.create_task(self._coordinator_callback(...))` or
    `asyncio.create_task(self._delayed_callback(...))` calls remain.

    AST-based so block comments or docstrings that *mention* the historical
    pattern do not cause a false positive. Scoped to the 6 command methods
    only — the websocket-listener storm at `_websocket_listener` is
    out-of-scope (story C4-ws will address those sites).
    """
    import ast
    from pathlib import Path

    target_methods = {
        "start_cleaning",
        "stop_cleaning",
        "pause_cleaning",
        "return_to_base",
        "add_fifteen_minutes",
        "reduce_fifteen_minutes",
    }
    coordinator_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    )
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "AqualinkClient":
            continue
        for method in class_node.body:
            if not isinstance(method, ast.AsyncFunctionDef):
                continue
            if method.name not in target_methods:
                continue
            for node in ast.walk(method):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                # Match: asyncio.create_task(...)
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create_task"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"
                ):
                    continue
                # Inspect the argument: only flag self._coordinator_callback / self._delayed_callback
                if not node.args:
                    continue
                arg = node.args[0]
                if not isinstance(arg, ast.Call):
                    continue
                inner = arg.func
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "self"
                    and inner.attr in {"_coordinator_callback", "_delayed_callback"}
                ):
                    offenders.append(f"{method.name}:line {node.lineno}")
    assert not offenders, (
        "Fire-and-forget callback `asyncio.create_task` detected in command "
        "methods. C4-cmd replaced these with a single awaited "
        "`self._coordinator_callback()` call so the coordinator's debounced "
        "`async_request_refresh` collapses near-duplicate refreshes into one "
        "`_async_update_data` call. Sites:\n  " + "\n  ".join(offenders)
    )


# ----------------------------------------------------------------------------
# C4-cmd review F1 — `_safe_post_command_refresh` helper propagates auth
# failures while swallowing transient refresh noise.
#
# The original C4-cmd commit inlined `try: await self._coordinator_callback();
# except Exception as e: _LOGGER.debug(...)` at all 6 command sites. The
# Edge Case Hunter review (2026-05-18) caught that this broad-except silently
# demoted `ConfigEntryAuthFailed` raised by `_handle_realtime_update` to DEBUG,
# breaking H10's reauth-on-401 contract for the narrow case of a button press
# landing on a freshly-401-stale token. The 6 inlined blocks are now collapsed
# to one `_safe_post_command_refresh` helper that explicitly re-raises
# `ConfigEntryAuthFailed` and `CancelledError`.
# ----------------------------------------------------------------------------


async def test_safe_post_command_refresh_propagates_config_entry_auth_failed() -> None:
    """Reauth contract: `ConfigEntryAuthFailed` from the coordinator
    callback must propagate to the command method so HA's reauth flow fires.
    Pre-fix, the inlined `except Exception` at each of the 6 command sites
    silently demoted this to DEBUG and the user never saw the reauth prompt.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient(username="u", password="p", api_key="k")

    async def raising_callback():
        raise ConfigEntryAuthFailed("simulated stale token")

    client._coordinator_callback = raising_callback

    with pytest.raises(ConfigEntryAuthFailed):
        await client._safe_post_command_refresh()


async def test_safe_post_command_refresh_propagates_cancelled_error() -> None:
    """Cancellation contract: `CancelledError` (HA shutdown / unload race)
    must propagate so the awaiting command method's `wait_for` / outer
    cancellation handling sees the cancel cleanly. `except Exception`
    doesn't catch `CancelledError` in Python 3.8+ (it's `BaseException`),
    but the helper has an explicit `raise` for clarity — this test pins
    the contract regardless of CancelledError's superclass evolution.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient(username="u", password="p", api_key="k")

    async def cancelling_callback():
        raise asyncio.CancelledError

    client._coordinator_callback = cancelling_callback

    with pytest.raises(asyncio.CancelledError):
        await client._safe_post_command_refresh()


async def test_safe_post_command_refresh_swallows_transient_exception(caplog) -> None:
    """Transient noise (network blip during refresh, parser KeyError, etc.)
    must NOT bubble to the command method — the cloud command itself
    succeeded, and the next polling cycle will recover state. Helper logs
    at DEBUG and returns cleanly.
    """
    import logging

    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient(username="u", password="p", api_key="k")

    async def transient_failure_callback():
        raise RuntimeError("simulated transient refresh failure")

    client._coordinator_callback = transient_failure_callback

    caplog.set_level(logging.DEBUG, logger="custom_components.iaqualink_robots.coordinator")
    # Should NOT raise.
    await client._safe_post_command_refresh()

    debug_records = [
        r for r in caplog.records
        if r.levelno == logging.DEBUG
        and "simulated transient refresh failure" in r.message
    ]
    assert debug_records, (
        "Transient refresh exception must be logged at DEBUG so it's "
        "observable in operator logs without blocking the command method"
    )


async def test_safe_post_command_refresh_no_callback_is_noop() -> None:
    """If `_coordinator_callback` is not set (or set to None), the helper
    must return cleanly without raising AttributeError. Defensive against
    the pre-existing `hasattr` guard at each call site, which the helper
    replaced.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient(username="u", password="p", api_key="k")
    # _coordinator_callback is None by default (see AqualinkClient.__init__).

    # Should NOT raise.
    await client._safe_post_command_refresh()


def test_all_command_sites_use_safe_post_command_refresh() -> None:
    """Structural guard: each of the 6 command methods must route its
    post-command refresh through `_safe_post_command_refresh()`, NOT
    through a direct `await self._coordinator_callback()` call.

    A future PR that inlines the callback again (e.g. for a "tighter
    error message" or to avoid the helper indirection) would silently
    reintroduce the auth-swallow regression this F1 patch fixed.
    """
    import ast
    from pathlib import Path

    coordinator_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    )
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))

    target_methods = {
        "start_cleaning",
        "stop_cleaning",
        "pause_cleaning",
        "return_to_base",
        "add_fifteen_minutes",
        "reduce_fifteen_minutes",
    }

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef) or node.name not in target_methods:
            continue
        for inner in ast.walk(node):
            # Look for direct `await self._coordinator_callback(...)` calls.
            # The single legitimate site for this awaited call now lives
            # inside `_safe_post_command_refresh`, NOT in the 6 commands.
            if (
                isinstance(inner, ast.Await)
                and isinstance(inner.value, ast.Call)
                and isinstance(inner.value.func, ast.Attribute)
                and inner.value.func.attr == "_coordinator_callback"
                and isinstance(inner.value.func.value, ast.Name)
                and inner.value.func.value.id == "self"
            ):
                offenders.append(f"{node.name}:line {inner.lineno}")

    assert not offenders, (
        "Direct `await self._coordinator_callback()` detected in a command "
        "method — must route through `self._safe_post_command_refresh()` "
        "instead so `ConfigEntryAuthFailed` propagates and triggers HA "
        "reauth (C4-cmd review F1). Sites:\n  " + "\n  ".join(offenders)
    )


# ----------------------------------------------------------------------------
# Story C4-ws — WS-listener refresh fan-out elimination via push pattern
# ----------------------------------------------------------------------------
#
# Pre-C4-ws the websocket listener fired up to two `asyncio.create_task`
# `_coordinator_callback` invocations per inbound message (one for stepper
# updates, one for other state changes). Both were unawaited and both routed
# into the debounced `async_request_refresh`, which then turned around and
# called `fetch_status` (HTTP roundtrip) to re-fetch state we'd just been
# told via the WS push. A chatty cloud session produced overlapping refresh
# fan-outs and burned API call budget for no information gain.
#
# C4-ws rewrote the listener to:
#
#   * AC #1: filter at intake — heartbeats / ACKs / foreign-service /
#     empty-robot messages are dropped before any callback.
#   * AC #3: dedup via `_state_signature` — repeated StateReported messages
#     with no change in the fields we care about are no-ops.
#   * AC #2 + AC #4: single awaited push via `_realtime_push_callback`,
#     which the coordinator implements as `async_set_updated_data` on the
#     deep-merged baseline + delta envelope. No HTTP roundtrip, no
#     debouncer, target <200 ms cloud → entity latency.
#
# The cached baseline (`_last_ws_envelope`) is seeded on the first successful
# `_ws_subscribe`. Until then, state-bearing messages fall back to the
# refresh callback so a partial envelope can't slip through the parser and
# silently overwrite cached fields with defaults.


def _build_client_for_ws() -> object:
    """Build an `AqualinkClient` with just enough state for the listener tests.

    Bypasses `__init__` (aiohttp + asyncio.Lock side effects) and stamps the
    handful of attributes the new C4-ws code paths read: device_type, the
    cached baseline envelope, the dedup signature, and stepper-cache state.
    The listener helpers (`_parse_ws_state_message`, `_deep_merge_dict`,
    `_state_signature`) are staticmethods on the class — they need no
    instance fields.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    client = AqualinkClient.__new__(AqualinkClient)
    client._serial = "TEST-SERIAL"
    client._device_type = "vr"
    client._last_ws_envelope = None
    client._last_applied_state_signature = None
    client._coordinator_callback = None
    client._realtime_push_callback = None
    return client


class _FakeWS:
    """Async-iterable stand-in for an aiohttp WS connection (story C4-ws tests).

    Python looks up ``__aiter__`` on the type, not the instance, so a bare
    ``MagicMock`` can't satisfy ``async for message in self._ws_connection``.
    This helper implements the iteration protocol directly while leaving
    ``send_json`` / ``closed`` patchable for individual tests.
    """

    def __init__(self, messages):
        self.closed = False
        self._messages = list(messages)
        self.sent: list = []

    async def send_json(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def _make_text_msg(payload_json):
    """Build a fake aiohttp TEXT message whose ``.json()`` returns ``payload_json``."""
    import aiohttp
    from unittest.mock import MagicMock

    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.json = MagicMock(return_value=payload_json)
    return msg


def _make_close_msg():
    """Build a fake aiohttp CLOSED frame that terminates the listener loop."""
    import aiohttp
    from unittest.mock import MagicMock

    close = MagicMock()
    close.type = aiohttp.WSMsgType.CLOSED
    return close


# AC #1 — intake filter -------------------------------------------------------


def test_parse_ws_state_message_returns_none_for_heartbeat() -> None:
    """A heartbeat / keepalive frame has no `service` or `event` field.
    `_parse_ws_state_message` must return None so the listener short-circuits
    before any callback fires (AC #1).
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    assert AqualinkClient._parse_ws_state_message({}) is None
    assert AqualinkClient._parse_ws_state_message({"event": "Heartbeat"}) is None


def test_parse_ws_state_message_returns_none_for_foreign_service() -> None:
    """A message from a different service (e.g. Authorization ACK) is
    irrelevant. Must return None (AC #1).
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    msg = {
        "service": "Authorization",
        "event": "StateReported",
        "payload": {"state": {"reported": {"equipment": {"robot": {"state": 1}}}}},
    }
    assert AqualinkClient._parse_ws_state_message(msg) is None


def test_parse_ws_state_message_returns_none_when_robot_block_empty() -> None:
    """StateReported with no `equipment.robot` content is a status-only
    envelope (we already have it via polling). No-op (AC #1).
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    msg = {
        "service": "StateStreamer",
        "event": "StateReported",
        "payload": {"state": {"reported": {"equipment": {"robot": {}}}}},
    }
    assert AqualinkClient._parse_ws_state_message(msg) is None


def test_parse_ws_state_message_returns_reported_for_real_state_change() -> None:
    """A canonical StateReported with equipment.robot content returns the
    `reported` dict so the listener can merge it onto the cached baseline.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    msg = {
        "service": "StateStreamer",
        "event": "StateReported",
        "payload": {
            "state": {
                "reported": {
                    "aws": {"status": "connected", "timestamp": 1700000000000},
                    "equipment": {"robot": {"state": 1, "errorState": 0}},
                }
            }
        },
    }
    reported = AqualinkClient._parse_ws_state_message(msg)
    assert reported is not None
    assert reported["equipment"]["robot"]["state"] == 1
    assert reported["aws"]["status"] == "connected"


# Deep-merge semantics --------------------------------------------------------


def test_deep_merge_dict_overlays_overlay_onto_base() -> None:
    """Overlay nested keys merge into base; non-overlaid keys preserved.

    Locks the merge semantics the listener's AC #2 path relies on: a delta
    that touches only `state` must NOT wipe `cycleStartTime`, `durations`,
    or other cached fields. Used by `_websocket_listener` to maintain the
    cached baseline envelope across incremental pushes.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    base = {
        "a": {"b": 1, "c": 2, "nested": {"x": 10, "y": 20}},
        "preserved": "yes",
    }
    overlay = {"a": {"b": 99, "nested": {"y": 200}}}
    merged = AqualinkClient._deep_merge_dict(base, overlay)
    assert merged == {
        "a": {"b": 99, "c": 2, "nested": {"x": 10, "y": 200}},
        "preserved": "yes",
    }
    # Base is unmutated (defensive copy, not in-place merge).
    assert base["a"]["b"] == 1
    assert base["a"]["nested"]["y"] == 20


def test_deep_merge_dict_replaces_scalars_and_lists() -> None:
    """List values are replaced wholesale (AWS IoT shadow semantics), not
    concatenated. Scalars in overlay overwrite scalars in base.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    base = {"list": [1, 2, 3], "scalar": "old"}
    overlay = {"list": [9], "scalar": "new"}
    merged = AqualinkClient._deep_merge_dict(base, overlay)
    assert merged == {"list": [9], "scalar": "new"}


# AC #3 — dedup signature -----------------------------------------------------


def test_state_signature_stable_for_unchanged_robot_data() -> None:
    """Identical robot_data → identical signature → listener skips the push.

    Anchors AC #3 ("60 s of unchanged chatter → 0 _async_update_data calls"):
    if the cloud emits redundant StateReported events, the signature catches
    them before we burn a coordinator update on no-op data.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    robot = {"state": 1, "errorState": 0, "prCyc": 2, "cycleStartTime": 1700000000, "stepper": 5}
    assert AqualinkClient._state_signature(robot) == AqualinkClient._state_signature(robot)


def test_state_signature_changes_when_state_changes() -> None:
    """Any of the user-visible fields shifting must change the signature."""
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    a = {"state": 1, "errorState": 0, "prCyc": 2, "cycleStartTime": 0, "stepper": 0}
    b = {**a, "state": 3}
    assert AqualinkClient._state_signature(a) != AqualinkClient._state_signature(b)


def test_state_signature_includes_cyclobat_nested_fields() -> None:
    """Cyclobat carries activity inside `main.state` and battery state inside
    `battery.userChargePerc`. The signature must catch deltas there too,
    otherwise the dedup would silently drop battery/cleaning updates for
    the cyclobat family.
    """
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    a = {"main": {"state": 1, "cycleStartTime": 0}, "battery": {"userChargePerc": "80"}}
    b = {"main": {"state": 1, "cycleStartTime": 0}, "battery": {"userChargePerc": "60"}}
    assert AqualinkClient._state_signature(a) != AqualinkClient._state_signature(b)


# AC #2 + AC #4 — apply_realtime_envelope -------------------------------------


async def test_apply_realtime_envelope_pushes_via_async_set_updated_data(
    coordinator_factory,
) -> None:
    """A merged shadow envelope is parsed via the per-device-type code path
    and pushed via `async_set_updated_data` — no refresh roundtrip (AC #2,
    AC #4). Locks the contract that the push path does NOT call
    `async_request_refresh` or `fetch_status`.
    """
    from unittest.mock import MagicMock

    coord, client = coordinator_factory(
        device_type="vr",
        last_data={"activity": "idle", "model": "RA 6900 iQ"},
    )
    # Real VR parser is needed — un-mock the bound method by giving the
    # client a real one from the class. ``_build_mock_client`` uses
    # MagicMock so the per-device-type parsers are MagicMock by default;
    # rebind to the real function.
    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    # M15: production code reads `client.serial` (public property) — mock must
    # set the public name, not `_serial`, or the parser builds a result dict
    # whose `serial_number` field is a MagicMock auto-attribute. The two tests
    # that don't assert on serial_number tolerated this silently pre-fix.
    client.serial = "TEST"
    client._update_vr_robot_data = AqualinkClient._update_vr_robot_data.__get__(client)
    client._apply_pending_stop_reset = MagicMock(side_effect=lambda d: d)
    client._pending_stop_reset = None
    client._format_time_human = AqualinkClient._format_time_human.__get__(client)
    client._resolve_cycle_duration = AqualinkClient._resolve_cycle_duration.__get__(client)
    client._calculate_times = AqualinkClient._calculate_times.__get__(client)
    client._include_seconds_remaining = True
    client._stepper_adj_time = 15
    client._activity = None
    client._fan_speed = None
    client._hass = None
    coord.async_set_updated_data = MagicMock()

    envelope = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "aws": {"status": "connected", "timestamp": 1700000000000},
                        "equipment": {
                            "robot": {
                                "state": 1,  # cleaning
                                "errorState": 0,
                                "prCyc": 1,  # floor_only
                                "cycleStartTime": 0,
                                "stepper": 0,
                                "stepperAdjTime": 15,
                                "totalHours": 100,
                                "canister": 0.5,
                                "durations": {"a": 30, "b": 60, "c": 90, "d": 120},
                                "sensors": {"sns_1": {"val": 25}},
                            }
                        },
                    }
                }
            }
        }
    }

    await coord._apply_realtime_envelope(envelope)

    coord.async_set_updated_data.assert_called_once()
    pushed = coord.async_set_updated_data.call_args[0][0]
    assert pushed["activity"] == "cleaning"
    assert pushed["fan_speed"] == "floor_only"
    assert pushed["serial_number"] == "TEST"
    assert pushed["model"] == "RA 6900 iQ"  # preserved from _last_data


async def test_apply_realtime_envelope_does_not_call_async_request_refresh(
    coordinator_factory,
) -> None:
    """The push path must NOT route through `async_request_refresh` — that
    would defeat AC #2 (no roundtrip) and AC #4 (no debouncer delay).
    """
    from unittest.mock import AsyncMock, MagicMock

    coord, client = coordinator_factory(device_type="vr", last_data={"activity": "idle"})

    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    # M15: production code reads `client.serial` (public property) — mock must
    # set the public name, not `_serial`, or the parser builds a result dict
    # whose `serial_number` field is a MagicMock auto-attribute. The two tests
    # that don't assert on serial_number tolerated this silently pre-fix.
    client.serial = "TEST"
    client._update_vr_robot_data = AqualinkClient._update_vr_robot_data.__get__(client)
    client._apply_pending_stop_reset = MagicMock(side_effect=lambda d: d)
    client._pending_stop_reset = None
    client._format_time_human = AqualinkClient._format_time_human.__get__(client)
    client._resolve_cycle_duration = AqualinkClient._resolve_cycle_duration.__get__(client)
    client._calculate_times = AqualinkClient._calculate_times.__get__(client)
    client._include_seconds_remaining = True
    client._stepper_adj_time = 15
    client._activity = None
    client._fan_speed = None
    client._hass = None
    coord.async_request_refresh = AsyncMock()
    coord.async_set_updated_data = MagicMock()
    client.fetch_status.reset_mock()

    envelope = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {
                            "robot": {
                                "state": 1,
                                "errorState": 0,
                                "prCyc": 1,
                                "cycleStartTime": 0,
                                "stepper": 0,
                                "stepperAdjTime": 15,
                                "totalHours": 0,
                                "canister": 0,
                                "durations": {"a": 0, "b": 0, "c": 0, "d": 0},
                                "sensors": {"sns_1": {"val": 0}},
                            }
                        }
                    }
                }
            }
        }
    }

    await coord._apply_realtime_envelope(envelope)

    coord.async_request_refresh.assert_not_awaited()
    client.fetch_status.assert_not_awaited()
    coord.async_set_updated_data.assert_called_once()


async def test_apply_realtime_envelope_clears_desired_state_on_natural_completion(
    coordinator_factory,
) -> None:
    """The natural-cycle-completion detector (PR #94) must fire on the push
    path too — cleaning → idle without a manual stop calls clear_desired_state.

    The bug this guards against: pre-C4-ws this transition was only checked
    on the polled path. After C4-ws, the WS push beats the next poll to
    seeing the transition, so without the same detector on the push path
    the cloud would silently auto-restart a finished cycle for the ~3 s
    until the polled path notices.
    """
    from unittest.mock import MagicMock

    coord, client = coordinator_factory(
        device_type="vr",
        last_data={"activity": "cleaning", "model": "RA"},
    )

    from custom_components.iaqualink_robots.coordinator import AqualinkClient

    # M15: production code reads `client.serial` (public property) — mock must
    # set the public name, not `_serial`, or the parser builds a result dict
    # whose `serial_number` field is a MagicMock auto-attribute. The two tests
    # that don't assert on serial_number tolerated this silently pre-fix.
    client.serial = "TEST"
    client._update_vr_robot_data = AqualinkClient._update_vr_robot_data.__get__(client)
    client._apply_pending_stop_reset = MagicMock(side_effect=lambda d: d)
    client._pending_stop_reset = None
    client._format_time_human = AqualinkClient._format_time_human.__get__(client)
    client._resolve_cycle_duration = AqualinkClient._resolve_cycle_duration.__get__(client)
    client._calculate_times = AqualinkClient._calculate_times.__get__(client)
    client._include_seconds_remaining = True
    client._stepper_adj_time = 15
    client._activity = None
    client._fan_speed = None
    client._hass = None
    coord.async_set_updated_data = MagicMock()

    # Envelope reports idle (state=0 maps to idle in VR parser).
    envelope = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {
                            "robot": {
                                "state": 0,
                                "errorState": 0,
                                "prCyc": 1,
                                "cycleStartTime": 0,
                                "stepper": 0,
                                "stepperAdjTime": 15,
                                "totalHours": 0,
                                "canister": 0,
                                "durations": {"a": 0, "b": 0, "c": 0, "d": 0},
                                "sensors": {"sns_1": {"val": 0}},
                            }
                        }
                    }
                }
            }
        }
    }

    await coord._apply_realtime_envelope(envelope)

    client.clear_desired_state.assert_awaited_once()


# AC #1 + AC #2 + AC #4 — _websocket_listener integration ---------------------


async def test_websocket_listener_skips_callback_for_heartbeat() -> None:
    """AC #1: a heartbeat message does NOT invoke any callback.

    Stubs the WS connection with a one-shot iterable that yields a
    heartbeat-shaped TEXT message, then closes. Both the push callback and
    the refresh callback must remain un-awaited.
    """
    from unittest.mock import AsyncMock

    client = _build_client_for_ws()
    client._id = "user"
    client._coordinator_callback = AsyncMock()
    client._realtime_push_callback = AsyncMock()
    client._ws_connection = _FakeWS([
        _make_text_msg({"event": "Heartbeat"}),
        _make_close_msg(),
    ])
    client._ensure_websocket_connection = AsyncMock()

    await client._websocket_listener()

    client._coordinator_callback.assert_not_awaited()
    client._realtime_push_callback.assert_not_awaited()


async def test_websocket_listener_pushes_on_state_change_after_baseline_cached() -> None:
    """AC #2 + AC #4: with a baseline envelope cached, a state-bearing message
    fires the push callback exactly once with the merged envelope, and does
    NOT fire the refresh callback. The merged envelope must carry the
    delta's new ``state`` value AND preserve the cached ``prCyc``.
    """
    from unittest.mock import AsyncMock

    client = _build_client_for_ws()
    client._id = "user"
    client._coordinator_callback = AsyncMock()
    client._realtime_push_callback = AsyncMock()
    # Seed a baseline (would have been set by ``_update_other_robots`` post-_ws_subscribe).
    client._last_ws_envelope = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {"robot": {"state": 0, "errorState": 0, "prCyc": 1}}
                    }
                }
            }
        }
    }

    client._ws_connection = _FakeWS([
        _make_text_msg({
            "service": "StateStreamer",
            "event": "StateReported",
            "payload": {"state": {"reported": {"equipment": {"robot": {"state": 1}}}}},
        }),
        _make_close_msg(),
    ])
    client._ensure_websocket_connection = AsyncMock()

    await client._websocket_listener()

    client._realtime_push_callback.assert_awaited_once()
    pushed_envelope = client._realtime_push_callback.await_args[0][0]
    merged_robot = pushed_envelope["payload"]["robot"]["state"]["reported"]["equipment"]["robot"]
    assert merged_robot["state"] == 1  # delta applied
    assert merged_robot["prCyc"] == 1  # preserved from baseline (deep-merge contract)
    client._coordinator_callback.assert_not_awaited()  # No refresh fan-out.


async def test_websocket_listener_skips_push_on_duplicate_state_message() -> None:
    """AC #3: identical state messages back-to-back trigger the push exactly
    once. The dedup via `_state_signature` catches the second, so a chatty
    cloud session doesn't fire a refresh per redundant StateReported.
    """
    from unittest.mock import AsyncMock

    client = _build_client_for_ws()
    client._id = "user"
    client._coordinator_callback = AsyncMock()
    client._realtime_push_callback = AsyncMock()
    client._last_ws_envelope = {
        "payload": {
            "robot": {
                "state": {
                    "reported": {
                        "equipment": {"robot": {"state": 0, "errorState": 0, "prCyc": 1}}
                    }
                }
            }
        }
    }

    payload = {
        "service": "StateStreamer",
        "event": "StateReported",
        "payload": {"state": {"reported": {"equipment": {"robot": {"state": 1}}}}},
    }
    client._ws_connection = _FakeWS([
        _make_text_msg(payload),
        _make_text_msg(payload),
        _make_close_msg(),
    ])
    client._ensure_websocket_connection = AsyncMock()

    await client._websocket_listener()

    assert client._realtime_push_callback.await_count == 1


async def test_websocket_listener_falls_back_to_refresh_without_baseline_envelope() -> None:
    """First-push fallback: with `_last_ws_envelope = None` (no baseline yet),
    a state message routes to the refresh callback (so the next ws_subscribe
    seeds the baseline) and NOT to the push callback. Pin-down for the race
    window between listener startup and the first ``_update_other_robots``.
    """
    from unittest.mock import AsyncMock

    client = _build_client_for_ws()
    client._id = "user"
    client._coordinator_callback = AsyncMock()
    client._realtime_push_callback = AsyncMock()
    client._last_ws_envelope = None  # explicit: no baseline yet.

    client._ws_connection = _FakeWS([
        _make_text_msg({
            "service": "StateStreamer",
            "event": "StateReported",
            "payload": {"state": {"reported": {"equipment": {"robot": {"state": 1}}}}},
        }),
        _make_close_msg(),
    ])
    client._ensure_websocket_connection = AsyncMock()

    await client._websocket_listener()

    client._coordinator_callback.assert_awaited_once()
    client._realtime_push_callback.assert_not_awaited()


# AST guard — regression test for the fire-and-forget pattern -----------------


def test_websocket_listener_no_fire_and_forget_create_task() -> None:
    """C4-ws regression guard: AST walk of `_websocket_listener` asserts no
    `asyncio.create_task(self._coordinator_callback(...))` invocations remain.

    Symmetric to the C4-cmd guard at `test_no_fire_and_forget_callback_create_task_in_command_methods`.
    A future refactor that re-introduces the fire-and-forget pattern in the
    listener (the pre-C4-ws "immediate execution without waiting" idiom)
    will fail this test, surfacing the refresh-storm regression before merge.
    """
    import ast
    from pathlib import Path

    coordinator_path = (
        Path(__file__).parent.parent
        / "custom_components"
        / "iaqualink_robots"
        / "coordinator.py"
    )
    tree = ast.parse(coordinator_path.read_text(encoding="utf-8"))

    offenders: list[str] = []
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != "AqualinkClient":
            continue
        for method in class_node.body:
            if not isinstance(method, ast.AsyncFunctionDef):
                continue
            if method.name != "_websocket_listener":
                continue
            for node in ast.walk(method):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "create_task"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "asyncio"
                ):
                    continue
                if not node.args:
                    continue
                arg = node.args[0]
                if not isinstance(arg, ast.Call):
                    continue
                inner = arg.func
                if (
                    isinstance(inner, ast.Attribute)
                    and isinstance(inner.value, ast.Name)
                    and inner.value.id == "self"
                    and inner.attr in {"_coordinator_callback", "_realtime_push_callback"}
                ):
                    offenders.append(f"_websocket_listener:line {node.lineno}")
    assert not offenders, (
        "Fire-and-forget callback `asyncio.create_task` detected in "
        "`_websocket_listener`. C4-ws replaced the two `create_task` "
        "blocks with a single awaited `_realtime_push_callback` so the "
        "coordinator's `async_set_updated_data` push displaces the "
        "refresh fan-out. Sites:\n  " + "\n  ".join(offenders)
    )
