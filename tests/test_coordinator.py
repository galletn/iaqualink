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
    client._device_type = device_type
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
