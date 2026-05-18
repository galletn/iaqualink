"""Coordinator lifecycle regression tests (story P10).

Audits ``AqualinkDataUpdateCoordinator.cleanup()`` and the supporting
``schedule_task`` / ``_cancel_pending_tasks`` / ``_stop_websocket_listener``
helpers. The fixture pattern mirrors ``tests/test_coordinator.py`` — a real
coordinator wrapping a MagicMock client.

P10 code review (commit ``c762b52``) fixes encoded here:
- F1: ``schedule_task`` is public (no leading underscore) — cross-module contract.
- F2 / F3 / F4: cleanup phases are independently bounded (5 s each) and idempotent.
- F5 / F7: done-task exceptions are retrieved; pending-task set is cleared on timeout.
- F9: ``test_cleanup_ws_task_timeout_does_not_hang`` narrows the ``wait_for`` patch
  to the listener task only via a filtering side_effect.
- F10: ``test_reload_cycle_no_leak`` asserts per-cycle ``_close_websocket`` was awaited
  exactly once instead of an unreliable ``asyncio.all_tasks()`` count.
- F11: done-callback synchronisation uses a bounded loop of ``asyncio.sleep(0)``
  yields, not a single tick.
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


async def _yield_until(predicate, tries: int = 5) -> None:
    """Yield up to ``tries`` times so deferred callbacks land (F11).

    ``add_done_callback`` is scheduled via ``loop.call_soon``; on a busy loop
    one ``sleep(0)`` is not guaranteed to land it. Five yields is generous
    without making the test slow.
    """
    for _ in range(tries):
        if predicate():
            return
        await asyncio.sleep(0)


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


async def test_cleanup_ws_task_timeout_does_not_hang(coordinator, caplog) -> None:
    """If the listener task swallows cancellation, cleanup must still return.

    F9: patches ``asyncio.wait_for`` with a *narrow* side_effect that raises
    ``TimeoutError`` only when called for the listener task. Other call sites
    (``_cancel_pending_tasks`` batch ``asyncio.wait`` is not affected; the
    ``client._close_websocket`` ``wait_for`` IS still filtered through but
    only when the awaitable is the listener task). Also asserts the WARNING
    log fires.
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

    real_wait_for = asyncio.wait_for

    async def filtering_wait_for(aw, timeout):  # type: ignore[no-untyped-def]
        # Only the listener task gets the synthetic timeout.
        if aw is task:
            raise asyncio.TimeoutError
        return await real_wait_for(aw, timeout)

    with patch(
        "custom_components.iaqualink_robots.coordinator.asyncio.wait_for",
        new=filtering_wait_for,
    ), caplog.at_level("WARNING"):
        await asyncio.wait_for(coordinator.cleanup(), timeout=2.0)

    # cleanup() reached the close_websocket phase despite the WS-listener timeout
    coordinator.client._close_websocket.assert_awaited()
    assert any(
        "Websocket listener did not cancel within 5s" in r.message
        for r in caplog.records
    ), "Expected WARNING about listener cancellation timeout was not logged"

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


async def test_close_websocket_timeout_does_not_hang(coordinator, caplog) -> None:
    """F3: phase 3 of cleanup wraps client._close_websocket in wait_for(5s).

    A stuck TLS shutdown inside ws_session.close() must not strand unload.
    """

    async def slow_close():
        await asyncio.sleep(60)

    coordinator.client._close_websocket = AsyncMock(side_effect=slow_close)

    real_wait_for = asyncio.wait_for

    async def fast_wait_for(aw, timeout):  # type: ignore[no-untyped-def]
        # Force the wait_for ceiling to 0.05 s for the test, regardless of which
        # call site (listener / close_websocket). _cancel_pending_tasks doesn't
        # use wait_for (it uses asyncio.wait), so this is safe.
        return await real_wait_for(aw, 0.05)

    with patch(
        "custom_components.iaqualink_robots.coordinator.asyncio.wait_for",
        new=fast_wait_for,
    ), caplog.at_level("WARNING"):
        await asyncio.wait_for(coordinator.cleanup(), timeout=2.0)

    assert any(
        "_close_websocket() did not return within 5s" in r.message
        for r in caplog.records
    ), "Expected WARNING about close_websocket timeout was not logged"


# ---------------------------------------------------------------------------
# AC #3 — pending refresh tasks are cancelled
# ---------------------------------------------------------------------------


async def test_schedule_task_tracks_and_deregisters(coordinator) -> None:
    """schedule_task adds the task to the tracking set and removes it on done."""

    async def quick() -> None:
        return None

    task = coordinator.schedule_task(quick())
    assert task in coordinator._pending_tasks

    await task
    # F11: bounded yield loop instead of a single sleep(0) — add_done_callback
    # is loop.call_soon-scheduled and may need multiple ticks on a busy loop.
    await _yield_until(lambda: task not in coordinator._pending_tasks)
    assert task not in coordinator._pending_tasks


async def test_cleanup_cancels_pending_scheduled_tasks(coordinator) -> None:
    """A long-running task scheduled via schedule_task is cancelled by cleanup()."""

    async def long_running() -> None:
        await asyncio.sleep(60)

    task = coordinator.schedule_task(long_running())
    assert task in coordinator._pending_tasks

    await coordinator.cleanup()

    assert task.done()
    assert task.cancelled() or isinstance(task.exception(), asyncio.CancelledError)
    # F7: post-state — set is empty regardless of cancel-success.
    assert not coordinator._pending_tasks


async def test_cancel_pending_tasks_retrieves_done_exceptions(coordinator) -> None:
    """F5: a task that completed with an unhandled exception has its exception
    retrieved by _cancel_pending_tasks so Python doesn't log a
    'Task exception was never retrieved' warning on GC.
    """

    async def boom() -> None:
        raise RuntimeError("intentional")

    task = coordinator.schedule_task(boom())
    # Let the task run to completion (it will record the exception).
    try:
        await task
    except RuntimeError:
        pass

    # Place it back into the tracking set (the done-callback removed it).
    coordinator._pending_tasks.add(task)
    assert task.done() and not task.cancelled()

    await coordinator._cancel_pending_tasks()

    # task.exception() being callable without warning means it was retrieved.
    assert isinstance(task.exception(), RuntimeError)


async def test_cancel_pending_tasks_batches_with_single_timeout(coordinator) -> None:
    """F2 (HIGH): N pending tasks must complete cancellation under a SINGLE
    5 s ceiling, not N × 5 s.
    """

    async def long_running() -> None:
        await asyncio.sleep(60)

    tasks = [coordinator.schedule_task(long_running()) for _ in range(8)]

    # Even with 8 tasks, cancel should complete well under 8 × 5 s.
    await asyncio.wait_for(coordinator._cancel_pending_tasks(), timeout=2.0)

    for task in tasks:
        assert task.done()
    assert not coordinator._pending_tasks


# ---------------------------------------------------------------------------
# AC #4 / AC #5 — idle cleanup is safe, reload cycle doesn't leak
# ---------------------------------------------------------------------------


async def test_cleanup_safe_when_idle(coordinator) -> None:
    """cleanup() with no listener task and no pending tasks must not raise."""
    assert coordinator._websocket_listener_task is None
    assert not coordinator._pending_tasks

    await coordinator.cleanup()

    coordinator.client._close_websocket.assert_awaited_once()


async def test_cleanup_idempotent_on_double_call(coordinator) -> None:
    """F4: cleanup() guards against re-entry; the second call short-circuits."""
    await coordinator.cleanup()
    coordinator.client._close_websocket.assert_awaited_once()

    await coordinator.cleanup()  # second call

    # Still only one invocation — phase 3 did not re-run.
    coordinator.client._close_websocket.assert_awaited_once()
    assert coordinator._cleanup_done is True


async def test_reload_cycle_no_leak(hass) -> None:
    """20× create/schedule/cleanup. F10: assert each cycle awaited
    _close_websocket exactly once and left _pending_tasks empty, instead of
    the previous brittle asyncio.all_tasks() count comparison.
    """
    from custom_components.iaqualink_robots.coordinator import (
        AqualinkDataUpdateCoordinator,
    )

    async def quick_sleep() -> None:
        await asyncio.sleep(60)  # long enough that cleanup must cancel it

    for i in range(20):
        client = _build_mock_client()
        coord = AqualinkDataUpdateCoordinator(hass, client, interval=3.0)
        coord.schedule_task(quick_sleep())
        await coord.cleanup()

        assert not coord._pending_tasks, (
            f"Cycle {i}: pending tasks should drain to empty"
        )
        # F10: pin the actual no-leak contract — each cycle's client got
        # _close_websocket invoked exactly once.
        client._close_websocket.assert_awaited_once()
