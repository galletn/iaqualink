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


def test_resolve_cycle_duration_empty_durations_returns_zero() -> None:
    """An empty or missing durations dict falls back cleanly."""
    client = _build_client_for_resolve()
    assert client._resolve_cycle_duration({}, 0) == 0
    assert client._resolve_cycle_duration({"a": 30}, 3) == 0  # in-range cycle, undersized dict
