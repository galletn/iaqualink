"""Coordinator lifecycle regression tests (story P10).

Audits ``AqualinkDataUpdateCoordinator.cleanup()`` and the supporting
``_schedule_task`` / ``_cancel_pending_tasks`` / ``_stop_websocket_listener``
helpers. The fixture pattern mirrors ``tests/test_coordinator.py`` — a real
coordinator wrapping a MagicMock client.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


def _build_mock_client() -> MagicMock:
    """Mock client surface used by cleanup paths."""
    client = MagicMock()
    client.robot_id = "test_robot"
    client.device_type = "vr"
    client.set_hass = MagicMock()
    client.set_coordinator_callback = MagicMock()
    client.set_realtime_push_callback = MagicMock()
    client.set_coordinator_reference = MagicMock()
    client._close_websocket = AsyncMock()
    return client


@pytest.fixture
def coordinator(hass):
    """Build a real coordinator wired to a mock client."""
    from custom_components.iaqualink_robots.coordinator import (
        AqualinkDataUpdateCoordinator,
    )

    client = _build_mock_client()
    coord = AqualinkDataUpdateCoordinator(hass, client, interval=3.0)
    return coord


# ---------------------------------------------------------------------------
# AC #1 — listener task is cancelled and awaited
# ---------------------------------------------------------------------------


async def test_cleanup_cancels_ws_listener_task(coordinator) -> None:
    """A running websocket listener task is cancelled by cleanup()."""

    async def long_running() -> None:
        await asyncio.sleep(60)

    task = asyncio.create_task(long_running())
    coordinator._websocket_listener_task = task

    await coordinator.cleanup()

    assert task.done()
    assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError)


async def test_cleanup_ws_task_timeout_does_not_hang(coordinator) -> None:
    """If the listener task swallows cancellation, cleanup must still return.

    Patches asyncio.wait_for inside coordinator.py to raise TimeoutError so the
    timeout branch executes without a real 5 s wall-clock wait. The contract
    we lock in: cleanup tolerates the TimeoutError and proceeds to the next
    cleanup phase (close_websocket).
    """

    async def stubborn() -> None:
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # Swallow once and keep going to model a stuck listener.
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                pass

    task = asyncio.create_task(stubborn())
    coordinator._websocket_listener_task = task

    with patch(
        "custom_components.iaqualink_robots.coordinator.asyncio.wait_for",
        new=AsyncMock(side_effect=asyncio.TimeoutError),
    ):
        await asyncio.wait_for(coordinator.cleanup(), timeout=2.0)

    # cleanup() reached the close_websocket phase despite the WS-listener timeout
    coordinator.client._close_websocket.assert_awaited()

    # Drain the stubborn task so pytest doesn't warn about pending tasks.
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=1.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass


# ---------------------------------------------------------------------------
# AC #2 — owned aiohttp session is closed
# ---------------------------------------------------------------------------


async def test_cleanup_closes_owned_session_via_client(coordinator) -> None:
    """cleanup() must delegate to client._close_websocket(), which closes the
    owned ws_session + ws_connection. This is the contract that wraps AC #2 —
    the only persistent aiohttp ClientSession in the integration lives on
    ``client._ws_session`` and is released inside ``client._close_websocket``.
    """
    await coordinator.cleanup()

    coordinator.client._close_websocket.assert_awaited_once()


# ---------------------------------------------------------------------------
# AC #3 — pending refresh tasks are cancelled
# ---------------------------------------------------------------------------


async def test_schedule_task_tracks_and_deregisters(coordinator) -> None:
    """_schedule_task adds the task to the tracking set and removes it on done."""

    async def quick() -> None:
        return None

    task = coordinator._schedule_task(quick())
    assert task in coordinator._pending_tasks

    await task
    # done callback runs synchronously; yield once so it lands
    await asyncio.sleep(0)
    assert task not in coordinator._pending_tasks


async def test_cleanup_cancels_pending_scheduled_tasks(coordinator) -> None:
    """A long-running task scheduled via _schedule_task is cancelled by cleanup()."""

    async def long_running() -> None:
        await asyncio.sleep(60)

    task = coordinator._schedule_task(long_running())
    assert task in coordinator._pending_tasks

    await coordinator.cleanup()

    assert task.done()
    assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError)


# ---------------------------------------------------------------------------
# AC #4 / AC #5 — idle cleanup is safe, reload cycle doesn't leak
# ---------------------------------------------------------------------------


async def test_cleanup_safe_when_idle(coordinator) -> None:
    """cleanup() with no listener task and no pending tasks must not raise."""
    assert coordinator._websocket_listener_task is None
    assert not coordinator._pending_tasks

    await coordinator.cleanup()

    coordinator.client._close_websocket.assert_awaited_once()


async def test_reload_cycle_no_leak(hass) -> None:
    """20× create/schedule/cleanup must not leak asyncio.Task objects.

    Compares ``asyncio.all_tasks()`` before and after — the count should
    return to baseline (the test's own task, plus any HA-runner overhead).
    """
    from custom_components.iaqualink_robots.coordinator import (
        AqualinkDataUpdateCoordinator,
    )

    # Let the loop settle so transient HA-bookkeeping tasks aren't counted.
    await asyncio.sleep(0)
    baseline = len(asyncio.all_tasks())

    async def quick_sleep() -> None:
        await asyncio.sleep(60)  # long enough that cleanup must cancel it

    for _ in range(20):
        client = _build_mock_client()
        coord = AqualinkDataUpdateCoordinator(hass, client, interval=3.0)
        coord._schedule_task(quick_sleep())
        await coord.cleanup()
        # Yield so done-callbacks run and the set drains.
        await asyncio.sleep(0)
        assert not coord._pending_tasks

    await asyncio.sleep(0)
    final = len(asyncio.all_tasks())

    # ±1 tolerance — HA's test loop has its own background bookkeeping that can
    # fluctuate by one between samples. The point is no monotonic growth.
    assert final <= baseline + 1, (
        f"Task leak: baseline={baseline}, final={final}"
    )
