"""Home Assistant data update coordinator for iaqualink_robots (R26 module split).

This module is the HA-side glue between the platform entities and the cloud-
facing :class:`~.client.AqualinkClient`. It owns:

- ``DataUpdateCoordinator`` subclass with adaptive polling (1.5 s active /
  10 s idle / 3 s base)
- ``_async_update_data`` polled path with H7 outage-aware availability and
  H9b ``AuthFailedError`` → ``ConfigEntryAuthFailed`` translation
- ``_apply_realtime_envelope`` push path (C4-ws) — merges the WS-listener's
  deep-merged shadow envelope and pushes via ``async_set_updated_data``
  without a refresh roundtrip
- P10 lifecycle: fire-and-forget task tracking + bounded cleanup
- HA-bound stepper-adjust business logic (debounce, translated
  ServiceValidationError / HomeAssistantError reporting, refresh wiring)

The cloud client (auth, REST, websocket, command surface) lives in
``client.py``. Pure date/time helpers live in ``parsing.py``.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import time

from collections.abc import Coroutine
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .client import AqualinkClient, AuthFailedError
from .const import DOMAIN, LONG_OUTAGE_THRESHOLD_SECONDS

_LOGGER = logging.getLogger(__name__)


class AqualinkDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to poll AqualinkClient and update data."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: AqualinkClient,
        interval: float,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=client.robot_id,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.client.set_hass(hass)  # Set hass reference for translations
        self._last_data: dict[str, Any] = {}
        self._consecutive_failures: int = 0
        # Wall-clock timestamp of the first failure in the current outage
        # streak (story H7). Set on the 0→1 transition of
        # ``_consecutive_failures`` and cleared back to ``None`` whenever
        # the counter returns to 0 on a successful update. Consulted by the
        # ``is_long_outage`` property to gate when entities flip to
        # ``available=False`` — replaces the prior count-based gate which
        # could trigger after as little as 45 s of failures at the fast
        # adaptive polling rate.
        self._first_failure_at: datetime.datetime | None = None
        # Latch so the long-outage traceback is logged once per outage rather
        # than on every poll for its whole duration. Cleared alongside
        # ``_first_failure_at`` on the next successful update.
        self._long_outage_logged: bool = False
        # Backs the public ``title`` property; set by ``__init__.py`` from
        # ``entry.title`` so entities and tests can read a stable display
        # name via the public API (story M15). Kept underscored so the
        # public name stays free for HA's ``DataUpdateCoordinator`` base
        # class — if a future HA version adds its own ``title`` attribute,
        # the subclass property here cleanly overrides it without an
        # attribute-shadowing surprise.
        self._title: str | None = None
        self._setup_complete: bool = False
        self._last_timing_command: float = 0

        # Adaptive polling for efficiency
        self._base_interval: float = interval  # Store original interval (3 seconds)
        self._fast_interval: float = 1.5  # Fast interval when robot is active
        self._slow_interval: float = 10  # Slow interval when robot is idle
        self._last_activity_time: float = 0

        # Real-time websocket listener for instant updates
        self._websocket_listener_task: asyncio.Task[None] | None = None

        # P10: track every fire-and-forget task scheduled by entities so
        # ``cleanup()`` can cancel them deterministically on unload. Tasks
        # self-deregister via ``add_done_callback`` in ``schedule_task``.
        self._pending_tasks: set[asyncio.Task[Any]] = set()

        # P10 review F4: re-entry guard. ``cleanup()`` may be entered twice
        # under rapid OptionsFlow reload + entry removal; flip this on first
        # entry so the second call short-circuits before re-cancelling.
        self._cleanup_done: bool = False

        # Set up callback for real-time updates
        self.client.set_coordinator_callback(self._handle_realtime_update)
        # C4-ws: push callback used by the WS listener. Bypasses the refresh
        # roundtrip and applies merged shadow envelopes via
        # ``async_set_updated_data`` for <200 ms-class latency on real state
        # changes (story AC #4).
        self.client.set_realtime_push_callback(self._apply_realtime_envelope)
        self.client.set_coordinator_reference(self)

        # Persistent websocket connection provides improved reliability and performance
        # Real-time websocket listener provides instant updates when robot state changes
        # Commands use persistent connection with automatic reconnection and retry logic

    @property
    def title(self) -> str | None:
        return self._title

    @title.setter
    def title(self, value: str | None) -> None:
        self._title = value

    # -- H7: outage-aware availability -----------------------------------------
    #
    # Two properties surface the coordinator's outage state to entity platforms:
    #
    #   * ``is_long_outage``        — True when the current outage has lasted
    #                                 longer than ``LONG_OUTAGE_THRESHOLD_SECONDS``;
    #                                 entity ``available`` properties consult
    #                                 this to flip to False after a sustained
    #                                 outage while ignoring short blips.
    #   * ``is_serving_stale_data`` — True any time the integration is returning
    #                                 last-known-good data because the latest
    #                                 poll failed; surfaced as the ``restored``
    #                                 attribute so power users can detect a
    #                                 transient outage in templates / automations.
    #
    # Both are computed; no state beyond ``_first_failure_at`` and
    # ``_consecutive_failures`` (both maintained in ``_async_update_data``).

    def _outage_age_seconds(self) -> float:
        """Seconds since the current outage streak began; ``0.0`` if no outage.

        Helper used by both ``is_long_outage`` and the broad-except log
        messages. Returning ``0.0`` rather than ``None`` keeps the
        log-formatter happy without a special case at every call site.

        Clamps to ``0.0`` if the wall-clock has jumped backward since the
        stamp was taken (NTP correction, manual user clock change, DST
        transition in the host timezone — ``dt_util.utcnow()`` is aware-UTC
        post-H8 so DST doesn't normally affect it, but the host clock can
        still skew). Without the clamp, a backward jump would make a real
        outage look fresh and never trip ``is_long_outage``.
        """
        if self._first_failure_at is None:
            return 0.0
        elapsed = (dt_util.utcnow() - self._first_failure_at).total_seconds()
        return max(elapsed, 0.0)

    @property
    def is_long_outage(self) -> bool:
        """``True`` when the current outage exceeds ``LONG_OUTAGE_THRESHOLD_SECONDS``.

        Pre-H7 entity ``available`` flipped to False after 30 consecutive
        failures at adaptive polling — anywhere from 45 s (active robot,
        1.5 s interval) to 5 min (idle robot, 10 s interval). H7 replaces
        the count with a wall-clock threshold so user automations that
        depend on the ``available`` state are not killed by short ISP
        blips. See AC #1 / AC #2.
        """
        if self._first_failure_at is None:
            return False
        return self._outage_age_seconds() > LONG_OUTAGE_THRESHOLD_SECONDS

    @property
    def is_serving_stale_data(self) -> bool:
        """``True`` while the integration is returning ``_last_data`` because
        the latest poll failed (story H7, AC #3 / AC #6).

        Surfaced verbatim as the ``restored`` attribute on every entity so
        advanced users can branch automations on it
        (``{{ state_attr('vacuum.x', 'restored') }}``). Flips back to
        ``False`` automatically on the next successful poll (the success
        path clears ``_first_failure_at`` and ``_consecutive_failures``).

        The ``bool(self._last_data)`` guard is the H7 review follow-up:
        on the first-ever poll failure (no prior real data), the broad-
        except path returns synthetic offline-minimal data (``status:
        offline``, ``error_state: connection_failed``). Without the
        guard, ``restored`` would be ``True`` even though nothing is being
        "restored" — the user has just never had real data yet. The
        guard makes the flag mean what it says: cached prior-real data
        is being returned, not synthetic placeholder data.
        """
        return self._consecutive_failures > 0 and bool(self._last_data)

    # Persistent websocket connection provides resilient command execution
    # Automatic retry logic and circuit breaker patterns prevent device unavailability

    async def _handle_realtime_update(self) -> None:
        """Handle real-time updates from the websocket listener (story H10).

        Pre-H10 this routed through a custom ``_immediate_refresh`` that:
          - bypassed the coordinator's update lock by calling
            ``_async_update_data()`` directly,
          - manually iterated HA-private ``self._listeners`` to push entity
            updates — a private-API reach-through that risked breakage on
            HA minor releases that change the internal listener data
            structure,
          - then **also** called ``async_update_listeners()`` (the public
            equivalent) two lines below, so the manual loop was redundant
            with the public call.

        H10 deletes that block in favour of ``async_request_refresh()`` —
        HA's documented API for "fetch fresh data right now". It goes
        through the coordinator's lock, fires the listener notification
        via the public ``async_update_listeners`` path, and honours HA's
        default request-refresh debouncer (``cooldown=10s``,
        ``immediate=True`` — the first call fires the refresh
        synchronously; subsequent calls within 10 s coalesce into one
        trailing refresh) which collapses rapid websocket-event bursts
        into a single update — a desirable property the old loop didn't
        have. (C4-cmd review F2 2026-05-18 corrected this docstring from
        an earlier "~0.3 s" claim that misread HA's defaults.)
        """
        try:
            await self.async_request_refresh()
        except ConfigEntryAuthFailed:
            # H10 review follow-up: do NOT swallow auth failures. The
            # broad ``except Exception`` below would otherwise demote a
            # legitimate credential rejection to a debug log, preventing
            # HA from dispatching its reauth flow (delivered by H9b/P4).
            # Re-raise so the websocket listener task fails the right way
            # and HA's coordinator-level auth escalation kicks in.
            raise
        except Exception as e:
            _LOGGER.debug("Error handling real-time update: %s", e)

    async def _apply_realtime_envelope(self, envelope: dict[str, Any]) -> None:
        """Apply a merged shadow envelope to coordinator state without polling (story C4-ws).

        Invoked by the client's websocket listener with the deep-merge of the
        cached baseline (last successful ``_ws_subscribe`` response) and the
        inbound StateReported delta. The envelope therefore has the same shape
        the per-device-type parsers already expect, so the push path re-uses
        ``client.apply_robot_data`` unchanged — no ``fetch_status`` HTTP
        roundtrip, no debouncer delay, target ~<200 ms latency from cloud →
        entity (AC #4).

        Re-uses the post-fetch orchestration from ``_async_update_data`` (natural
        cleaning→idle clearing, ``apply_pending_stop_reset``, failure-counter
        reset, adaptive polling update, ``_last_data`` snapshot) so the two
        update paths (polled & pushed) keep `self.data` semantically identical.

        Propagates ``ConfigEntryAuthFailed`` (HA reauth) and ``CancelledError``
        (HA shutdown). All other exceptions are logged + swallowed so a single
        malformed inbound message doesn't kill the WS listener task.
        """
        try:
            client = self.client
            result: dict[str, Any] = {
                "serial_number": client.serial,
                "device_type": client.device_type,
            }
            # Preserve model & last_online from prior data — model is fetched on
            # its own cadence (not in every shadow update); last_online is only
            # updated if the envelope carries an aws.timestamp.
            if self._last_data:
                if "model" in self._last_data:
                    result["model"] = self._last_data["model"]
                if "last_online" in self._last_data:
                    result["last_online"] = self._last_data["last_online"]

            # Bump last_online from the envelope's aws.timestamp when present.
            try:
                ts_ms = envelope["payload"]["robot"]["state"]["reported"]["aws"]["timestamp"]
                result["last_online"] = dt_util.utc_from_timestamp(ts_ms / 1000)
            except (KeyError, TypeError):
                pass  # Keep the preserved value from _last_data.

            # Dispatch to the per-device-type parser. The envelope was built
            # from the cached baseline merged with the inbound delta, so the
            # parsers see a full canonical shape and behave identically to the
            # polled path.
            if not client.supports_realtime_shadow:
                # i2d has no shadow stream; if we somehow got here, bail without
                # mutating state so the polled HTTP path stays canonical.
                _LOGGER.debug(
                    "Skipping realtime envelope apply for device_type=%r (no shadow stream)",
                    client.device_type,
                )
                return
            client.apply_robot_data(envelope, result)

            # Honour a queued manual-stop reset (H6) on the push path too.
            client.apply_pending_stop_reset(result)

            # Natural-completion detector (PR #94 fix): if the user-visible
            # activity transitioned cleaning → idle without a manual stop in
            # flight, clear desired state so the cloud doesn't auto-restart.
            # Mirror of the polled path in ``_async_update_data``.
            if (
                self._last_data.get("activity") == "cleaning"
                and result.get("activity") == "idle"
                and not client.has_pending_stop_reset
            ):
                _LOGGER.debug(
                    "Robot finished cleaning naturally (realtime push) - clearing desired state to prevent auto-restart"
                )
                try:
                    await client.clear_desired_state()
                except Exception as e:
                    _LOGGER.warning(
                        "Failed to clear desired state after natural completion (push path): %s", e
                    )

            # Successful push counts as a successful update — reset outage state
            # so ``is_long_outage`` / ``is_serving_stale_data`` flip back to
            # healthy without waiting for the next polled cycle (story H7).
            self._consecutive_failures = 0
            self._first_failure_at = None
            self._long_outage_logged = False
            client.reset_websocket_failures()

            # Adaptive polling: a state-bearing push is fresh evidence the
            # robot is active or recently commanded, so feed the same signal
            # the polled path feeds.
            self._update_polling_interval(result)

            # Snapshot for the next push (the cleaning→idle detector reads it
            # on the *next* invocation).
            self._last_data = result.copy()

            # Push to entities without going through the refresh path.
            self.async_set_updated_data(result)
        except (asyncio.CancelledError, ConfigEntryAuthFailed):
            # CancelledError: HA shutdown — let the listener task unwind.
            # ConfigEntryAuthFailed: surface to HA so reauth fires (H10 precedent).
            raise
        except Exception as e:  # noqa: BLE001 - listener task isolation
            _LOGGER.debug("Error applying realtime envelope: %s", e)

    def schedule_task(
        self, coro: "Coroutine[Any, Any, Any]"
    ) -> asyncio.Task[Any]:
        """Schedule a fire-and-forget coroutine and track it for cleanup.

        P10: callers that previously used ``asyncio.create_task(coro)`` should
        go through this helper so ``cleanup()`` can cancel any in-flight task
        on entry unload. Tasks self-deregister via ``add_done_callback`` once
        they complete, so the set never accumulates finished tasks.

        Public (no leading underscore) because it's part of the coordinator-entity
        contract — entity classes (``vacuum.py``) consume it across module
        boundaries. Promoted from the private form by the P10 review.
        """
        task: asyncio.Task[Any] = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def _cancel_pending_tasks(self) -> None:
        """Cancel and await every task scheduled via ``schedule_task``.

        P10 review F2 (HIGH): cancel every task first, then await the whole
        set with a SINGLE 5 s ``asyncio.wait`` — total wall-clock is bounded
        at 5 s regardless of N. The pre-review per-task sequential await would
        have blown HA's 60 s integration-unload budget at N≥12 stuck tasks.

        P10 review F5: tasks already done before this point may have completed
        with an unretrieved exception; calling ``task.exception()`` marks them
        retrieved (suppresses Python's ``Task exception was never retrieved``
        GC warning).

        P10 review F6: if ``cleanup()`` itself is being cancelled (e.g. HA
        shutdown), ``asyncio.wait`` will raise ``CancelledError`` — re-raise
        rather than swallow so the outer cancel propagates correctly.

        P10 review F7: clear the tracking set unconditionally at the end. The
        ``add_done_callback`` ``discard`` is a no-op on missing keys, so any
        still-pending task that times out can also safely drop from the set
        — the alternative is a misleading ``len(_pending_tasks) > 0`` post-state.
        """
        # Snapshot — done-callbacks fire ``self._pending_tasks.discard`` during
        # the loop and would otherwise mutate the iteration.
        tasks = list(self._pending_tasks)

        # Mark already-done task exceptions as retrieved.
        live_tasks = []
        for task in tasks:
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                if exc is not None:
                    _LOGGER.debug("Pending task ended with exception: %s", exc)
                continue
            task.cancel()
            live_tasks.append(task)

        if live_tasks:
            try:
                _, pending = await asyncio.wait(live_tasks, timeout=5.0)
            except asyncio.CancelledError:
                # Outer cleanup is being cancelled — propagate.
                self._pending_tasks.clear()
                raise
            if pending:
                _LOGGER.warning(
                    "%d pending task(s) did not cancel within 5s; abandoning further wait",
                    len(pending),
                )

        # Drop any stragglers so the post-state matches the log.
        self._pending_tasks.clear()

    async def _start_websocket_listener(self) -> None:
        """Start the websocket listener task for real-time updates."""
        if self._websocket_listener_task and not self._websocket_listener_task.done():
            return  # Already running

        try:
            self._websocket_listener_task = asyncio.create_task(
                self.client.run_websocket_listener()
            )
            _LOGGER.debug("Started websocket listener for real-time updates")
        except Exception as e:
            _LOGGER.warning("Failed to start websocket listener: %s", e)

    async def _stop_websocket_listener(self) -> None:
        """Stop the websocket listener task.

        P10: bounded by a 5 s wait_for so a listener task that has not yet
        unwound from a long-running ``async for message in ws_connection``
        loop can't deadlock ``async_unload_entry``. A timeout is logged at
        WARNING because it means we've already issued ``task.cancel()`` AND
        ``wait_for`` issued its own follow-up cancel on timeout — the task
        will unwind on its own; we just stop blocking on it.
        """
        if self._websocket_listener_task and not self._websocket_listener_task.done():
            self._websocket_listener_task.cancel()
            try:
                await asyncio.wait_for(self._websocket_listener_task, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "Websocket listener did not cancel within 5s; "
                    "cancellation re-issued by wait_for, abandoning further wait"
                )
            _LOGGER.debug("Stopped websocket listener")

    def _update_polling_interval(self, data: dict[str, Any]) -> None:
        """Adaptive polling interval based on robot activity to optimize CPU usage."""
        try:
            current_activity = data.get("activity", "idle")
            current_time = time.time()

            # Determine if robot is active
            is_active = current_activity in ["cleaning", "returning"] or data.get("status") == "online"

            # Check for recent commands (within last 30 seconds)
            recent_command = (current_time - self._last_activity_time) < 30

            if is_active or recent_command:
                # Robot is active or recently commanded - use fast polling
                new_interval = self._fast_interval
                self._last_activity_time = current_time
            else:
                # Robot is idle - use slower polling to save CPU
                new_interval = self._slow_interval

            # Only update interval if it has changed to avoid unnecessary updates
            current_interval = self.update_interval.total_seconds() if self.update_interval else self._base_interval
            if abs(current_interval - new_interval) > 0.5:
                self.update_interval = timedelta(seconds=new_interval)
                _LOGGER.debug("Adaptive polling: %s -> %ss interval", current_activity, new_interval)

        except Exception as e:
            _LOGGER.debug("Error updating polling interval: %s", e)

    def _mark_recent_activity(self) -> None:
        """Mark recent activity to trigger faster polling temporarily."""
        self._last_activity_time = time.time()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            # Check if this is the first call (initial setup)
            is_first_call = not self._setup_complete

            if is_first_call:
                # Use quick setup for faster initial loading
                _LOGGER.debug("Performing quick setup for faster initial load")
                status = await self.client.fetch_status(quick_setup=True)
                self._setup_complete = True

                # Use coordinator polling for consistent status updates with persistent websocket
                _LOGGER.debug("Quick setup complete, using persistent websocket connections with retry logic")
            else:
                # Normal operation with full status fetch
                status = await self.client.fetch_status()

            # Always use fresh status data
            merged_data = status.copy()

            # For i2d robot, preserve cycle start time while cleaning
            if (self.client.device_type == "i2d_robot" and
                merged_data.get("activity") == "cleaning" and
                    self._last_data.get("cycle_start_time")):
                merged_data["cycle_start_time"] = self._last_data["cycle_start_time"]

            # Detect natural cleaning → idle transition and clear desired state
            # This prevents the cloud from auto-restarting the robot
            if (self._last_data.get("activity") == "cleaning" and
                merged_data.get("activity") == "idle" and
                    not self.client.has_pending_stop_reset):  # Not a manual stop
                _LOGGER.debug("Robot finished cleaning naturally - clearing desired state to prevent auto-restart")
                try:
                    await self.client.clear_desired_state()
                except Exception as e:
                    _LOGGER.warning("Failed to clear desired state after natural completion: %s", e)

            # Reset failure count + outage timestamp on successful update.
            # H7: clearing ``_first_failure_at`` here flips
            # ``is_serving_stale_data`` back to ``False`` so the
            # ``restored`` entity attribute reverts to ``False`` once a
            # poll succeeds (AC #6).
            self._consecutive_failures = 0
            self._first_failure_at = None
            self._long_outage_logged = False

            # Reset websocket failure count on successful status update
            self.client.reset_websocket_failures()

            # Adaptive polling based on robot activity for CPU efficiency
            self._update_polling_interval(merged_data)

            # Start websocket listener for real-time updates after first successful update
            if is_first_call:
                await self._start_websocket_listener()
                _LOGGER.debug("Started real-time websocket listener for instant updates")

            # Save data for next update
            self._last_data = merged_data.copy()

            # The coordinator automatically notifies entities when data changes
            # No need for explicit signaling as CoordinatorEntity handles this

            return merged_data

        except AuthFailedError as err:
            # H9b AC#1/AC#2: surface auth failures to HA so reauth flow fires
            # (delivered by P4). Caught *before* the broad Exception handler
            # so the consecutive-failures ladder doesn't swallow them — a 401
            # is not "transient cloud noise", it needs explicit user action.
            _LOGGER.warning("Authentication failed; requesting HA reauth: %s", err)
            raise ConfigEntryAuthFailed(str(err)) from err

        except Exception as err:
            self._consecutive_failures += 1
            # Stamp the wall-clock start of this outage streak on the 0→1
            # transition (story H7). The ``is_long_outage`` property
            # consults the elapsed time against
            # ``LONG_OUTAGE_THRESHOLD_SECONDS`` to decide when to flip
            # entities to ``available=False``.
            if self._first_failure_at is None:
                self._first_failure_at = dt_util.utcnow()

            if not self.is_long_outage:
                # DEBUG, not WARNING: this fires on EVERY failed poll, and at
                # SCAN_INTERVAL=3s a 30-minute outage would write ~600 warning
                # lines before the long-outage branch below escalates once.
                #
                # It was previously gated behind the always-False debug_mode
                # flag, so it never appeared at all; un-gating it at WARNING
                # (as removing that flag first did) would have replaced one
                # bug with a louder one, in the release whose point is less
                # log noise. A short blip is not warning-worthy — the
                # long-outage ERROR is the operator-visible signal.
                _LOGGER.debug(
                    "Update failed (attempt %s, "
                    "outage age %.0fs "
                    "< %ss threshold): %s",
                    self._consecutive_failures,
                    self._outage_age_seconds(),
                    LONG_OUTAGE_THRESHOLD_SECONDS, err)

                # Return last known good data if available to keep entity available.
                # Entity `available` properties also consult `is_long_outage`,
                # so the entity stays usable for short blips and gracefully
                # flips to unavailable once the outage exceeds the threshold.
                if self._last_data:
                    _LOGGER.debug("Returning last known good data to keep entity available")
                    return self._last_data.copy()
                else:
                    # No previous data — return minimal offline data so
                    # platforms can render a coherent "offline" state until
                    # is_long_outage flips and HA marks the entity unavailable.
                    _LOGGER.debug("No previous data available, returning minimal data")
                    return {
                        "serial_number": self.client.serial,
                        "device_type": self.client.device_type,
                        "status": "offline",
                        "activity": "unknown",
                        "error_state": "connection_failed"
                    }
            else:
                # Logged ONCE per outage, on the not-long -> long transition.
                #
                # `is_long_outage` stays true for the rest of the outage, so an
                # unlatched log here writes a full traceback every polling
                # cycle: a multi-hour cloud outage produced thousands of them.
                # The traceback is still worth having — it is the one place the
                # cause of a sustained outage is recorded — but once is enough,
                # and DataUpdateCoordinator already logs the UpdateFailed
                # raised below. The latch resets with `_first_failure_at` on
                # the next successful update.
                if not self._long_outage_logged:
                    self._long_outage_logged = True
                    _LOGGER.exception(
                        "Update failed after %s attempts (%.0fs outage, > %ss threshold): %s",
                        self._consecutive_failures,
                        self._outage_age_seconds(),
                        LONG_OUTAGE_THRESHOLD_SECONDS,
                        err,
                    )
                # Long-outage threshold exceeded — raise UpdateFailed so HA
                # marks the entity unavailable. The `is_long_outage` gate on
                # entity `available` properties also short-circuits to False
                # before this point if the platform queries it independently.
                raise UpdateFailed(
                    f"Failed after {self._consecutive_failures} attempts "
                    f"over {self._outage_age_seconds():.0f}s: {err}"
                )

    async def async_start_cleaning(self) -> None:
        """Start cleaning - centralized business logic."""
        await self.client.start_cleaning()

    async def async_stop_cleaning(self) -> None:
        """Stop cleaning - centralized business logic."""
        await self.client.stop_cleaning()

    async def async_return_to_base(self) -> None:
        """Return to base - centralized business logic."""
        await self.client.return_to_base()

    #: Families whose cloud API exposes the remote-steering and cleaning-time
    #: commands. The buttons are only created for these, but the seven
    #: *services* are registered on the vacuum entity for every device type,
    #: so the guard has to live here.
    _REMOTE_COMMAND_DEVICE_TYPES = frozenset({"vr", "vortrax"})

    def _require_remote_command_support(self, command: str) -> None:
        """Raise unless this robot's family supports the remote commands.

        Previously an unsupported family made these methods ``return``
        silently, so ``iaqualink_robots.remote_forward`` called on a cyclonext
        reported success and did nothing at all — the same silent-no-op
        failure mode as issue #76, reached through the service API instead of
        the fan-speed list.
        """
        if self.client.device_type in self._REMOTE_COMMAND_DEVICE_TYPES:
            return
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="command_not_supported",
            translation_placeholders={
                "command": command,
                "device_type": str(self.client.device_type),
            },
        )

    async def _execute_remote_command(
        self, command_method: Callable[[], Awaitable[None]]
    ) -> None:
        """Execute a remote command with device type checking but no automatic refresh."""
        self._require_remote_command_support(getattr(command_method, "__name__", "command"))
        await command_method()
        # Remove automatic refresh to improve responsiveness - let normal polling handle updates

    async def async_remote_forward(self) -> None:
        """Send forward command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_forward)

    async def async_remote_backward(self) -> None:
        """Send backward command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_backward)

    async def async_remote_rotate_left(self) -> None:
        """Send rotate left command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_rotate_left)

    async def async_remote_rotate_right(self) -> None:
        """Send rotate right command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_rotate_right)

    async def async_remote_stop(self) -> None:
        """Send stop command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_stop)

    async def _async_adjust_stepper(
        self,
        delta_minutes: int,
        client_method: Callable[[], Awaitable[dict[str, Any] | None]],
        success_message_default: str,
        failure_message_default: str,
    ) -> None:
        """Shared body for :meth:`async_add_fifteen_minutes` and
        :meth:`async_reduce_fifteen_minutes` (R24).

        Owns the HA-side concerns the client helper doesn't: the
        3-second debounce on ``_last_timing_command`` and the
        post-command ``async_request_refresh``.

        ``client_method`` is the unbound-but-pre-bound coroutine to
        invoke after debounce (``self.client.add_fifteen_minutes`` or
        ``self.client.reduce_fifteen_minutes``). The default messages
        are the user-visible fallbacks when the client helper's
        ``result.message`` is missing.

        Outcome reporting (changed in 3.0.0-rc.4)
        -----------------------------------------
        This method used to call ``browser_mod.notification`` and fall
        back to ``persistent_notification.create`` for all three outcomes
        (debounced / succeeded / failed). Two problems: an integration
        must not depend on a custom frontend component being installed,
        and creating a notification for an ordinary successful command is
        exactly the log/UI spam this release is removing.

        Now: a debounced press raises ``ServiceValidationError`` (HA shows
        it inline on the offending action, and it is classed as a user
        error, not an integration bug), a failed command raises
        ``HomeAssistantError``, and success is silent — the time-remaining
        entities already reflect it after the refresh below.
        """
        self._require_remote_command_support(
            "add_fifteen_minutes" if delta_minutes > 0 else "reduce_fifteen_minutes"
        )

        current_time = time.time()
        time_since_last = current_time - self._last_timing_command
        debounce_seconds = 3

        if time_since_last < debounce_seconds:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="timing_command_rate_limited",
                translation_placeholders={
                    "seconds": f"{debounce_seconds - time_since_last:.1f}",
                },
            )

        self._last_timing_command = current_time

        result = await client_method()

        if isinstance(result, dict) and not result.get("success"):
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="timing_command_failed",
                translation_placeholders={
                    "error": str(result.get("message", failure_message_default)),
                },
            )

        _LOGGER.debug(
            "Cleaning time adjusted by %s minutes: %s",
            delta_minutes,
            result.get("message", success_message_default)
            if isinstance(result, dict) else success_message_default,
        )

        await self.async_request_refresh()

    async def async_add_fifteen_minutes(self) -> None:
        """Add 15 minutes to cleaning time - centralized business logic."""
        await self._async_adjust_stepper(
            15,
            self.client.add_fifteen_minutes,
            "Added 15 minutes to cleaning time.",
            "Failed to add 15 minutes.",
        )

    async def async_reduce_fifteen_minutes(self) -> None:
        """Reduce 15 minutes from cleaning time - centralized business logic."""
        await self._async_adjust_stepper(
            -15,
            self.client.reduce_fifteen_minutes,
            "Reduced cleaning time by 15 minutes.",
            "Failed to reduce 15 minutes.",
        )

    async def cleanup(self) -> None:
        """Clean up resources when coordinator is being unloaded.

        P10: cancels the WS listener task (5 s ceiling), cancels every
        coordinator-scheduled fire-and-forget task (single 5 s ceiling for
        the batch), then closes the client's persistent WS connection + owned
        aiohttp session (also 5 s ceiling). Each phase is bounded so no
        single hung resource can blow HA's 60 s integration-unload budget.

        P10 review F4: idempotent. A re-entry guard short-circuits the second
        call if HA races two unloads (rare but possible under rapid
        OptionsFlow toggle + entry removal).
        """
        if self._cleanup_done:
            _LOGGER.debug("Coordinator cleanup already ran; skipping re-entry")
            return
        self._cleanup_done = True

        pending_count = len(self._pending_tasks)
        _LOGGER.debug(
            "Starting coordinator cleanup (%d pending task(s) tracked)",
            pending_count,
        )

        # Stop websocket listener task (bounded by 5 s wait_for inside).
        await self._stop_websocket_listener()

        # Cancel any pending entity-scheduled tasks (e.g. delayed refreshes).
        # Bounded by a single 5 s asyncio.wait for the batch.
        await self._cancel_pending_tasks()

        # Close persistent websocket connection + owned session in client.
        # P10 review F3: wrap in wait_for so a stuck TLS shutdown can't
        # strand the unload — symmetric with the other two phases.
        try:
            await asyncio.wait_for(self.client.close_websocket(), timeout=5.0)
        except asyncio.TimeoutError:
            _LOGGER.warning(
                "client.close_websocket() did not return within 5s; abandoning wait"
            )

        _LOGGER.debug(
            "Coordinator cleanup complete - listener stopped, %d pending task(s) handled, session closed",
            pending_count,
        )
