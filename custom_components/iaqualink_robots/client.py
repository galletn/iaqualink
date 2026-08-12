"""HTTP + websocket client for the iaqualink_robots cloud (R26 module split).

Owns:
- authentication (Cognito login + JWT exp decode + safety-margin handling)
- REST session lifecycle (``_cloud_session`` reuses HA's pooled connector
  with a private cookie jar — L19)
- persistent websocket connection + listener + state-shadow merge (C4-ws)
- per-device command surface (start/stop/pause/return/set_fan_speed/
  remote-steering/stepper-adjust) — dispatched through ``protocols/``

The HA-side ``AqualinkDataUpdateCoordinator`` (and its adaptive-polling
logic, realtime-envelope apply, and entity lifecycle plumbing) lives in
``coordinator.py`` and imports the client class from here. Pure data /
time helpers live in ``parsing.py``.
"""
from __future__ import annotations

import base64
import json
import datetime
import math
import aiohttp
import logging
import asyncio
import time

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Awaitable, Callable, cast

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryAuthFailed
# Note: This import requires Home Assistant 2025.1 or later
from homeassistant.components.vacuum import VacuumActivity
from homeassistant.util import dt as dt_util

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from .coordinator import AqualinkDataUpdateCoordinator

from .const import (
    URL_LOGIN,
    URL_GET_DEVICES,
    URL_WS,
    URL_GET_DEVICE_FEATURES,
    MODEL_BY_CLEANER_PN,
    PENDING_STOP_RESET_MAX_AGE_SECONDS,
)
from .parsing import format_time_human
from .protocols import I2DProtocol, RobotProtocol, get_protocol
from .ws_envelope import (
    WSStateEnvelope,
    parse_ws_message,
    reset_drift_warnings,
    validate_state_envelope_fields,
)


def _i2d_protocol() -> I2DProtocol:
    """Return the I2D protocol singleton, typed as ``I2DProtocol``.

    Centralises the cast at one call-site so the coordinator's i2d code
    paths can call ``i2d_url`` (an I2D-only method, absent from the
    ``RobotProtocol`` base) without each site repeating the cast.

    Uses an explicit ``isinstance``-guard + ``raise`` (NOT an ``assert``)
    so the type narrowing survives ``python -O`` — assertions are
    stripped under optimisation and the helper would silently return
    whatever ``get_protocol`` returned, defeating the whole point of
    centralising the cast (review MED).
    """
    protocol = get_protocol("i2d_robot")
    if not isinstance(protocol, I2DProtocol):
        raise RuntimeError(
            f"i2d_robot protocol must be I2DProtocol, got {type(protocol).__name__}"
        )
    return protocol


def _require_protocol(device_type: str) -> RobotProtocol:
    """Return the protocol for ``device_type`` or raise.

    Wraps ``get_protocol`` (which returns ``RobotProtocol | None``) with an
    assertion so the call-site type-narrows to non-None. Every call here
    is gated by ``self._device_type in (...)`` checks that match the keys
    in ``_DEVICE_PROTOCOLS``, so the None branch is unreachable at runtime
    — the explicit raise replaces what would otherwise be a silent
    ``AttributeError`` on a future cloud surfacing an unknown
    ``device_type`` value.
    """
    protocol = get_protocol(device_type)
    if protocol is None:
        raise RuntimeError(f"No protocol registered for device_type={device_type!r}")
    return protocol


_LOGGER = logging.getLogger(__name__)


class AuthFailedError(Exception):
    """Internal marker raised by REST/WS layer on HTTP 401 (or equivalent).

    Converted to ``homeassistant.exceptions.ConfigEntryAuthFailed`` at the
    coordinator boundary so HA stops polling and dispatches its reauth flow.
    Keeping this as a separate internal type lets the REST/WS layers stay
    framework-agnostic — only ``AqualinkDataUpdateCoordinator._async_update_data``
    translates to the HA-specific exception.
    """


# Safety margin subtracted from the JWT `exp` so we refresh slightly before
# the cloud actually invalidates the token. Covers clock skew between AWS
# Cognito and the HA host.
_TOKEN_EXP_SAFETY_MARGIN_S = 60

# Module-level one-shot flag for the JWT-exp fallback WARN. A malformed
# Cognito token shape would otherwise trigger this warning on every hourly
# refresh and flood the operator log. Once we've warned, subsequent fallbacks
# log at DEBUG so the diagnostic info remains available without spam.
_JWT_EXP_FALLBACK_WARNED = False


def _decode_jwt_exp(token: str | None) -> datetime.datetime | None:
    """Decode the `exp` claim from a JWT and return an aware-UTC datetime.

    The Cognito `IdToken` is a standard JWT (`<header>.<payload>.<signature>`).
    The middle segment is base64url-encoded JSON containing an `exp` field —
    seconds-since-epoch. We trust transport security (TLS) and the iAqualink
    auth flow rather than verifying the signature, which would require
    fetching Cognito's public JWKs.

    Returns the parsed expiry (minus a safety margin) on success, or None on
    any parse failure so the caller can fall back to a conservative default.
    H8 swapped this from naive-local to aware-UTC via `dt_util.utc_from_timestamp`,
    matching the rest of the integration's datetime hygiene.
    """
    if token is None:
        return None
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload_b64 = parts[1]
        # base64url decoding needs the trailing '=' padding (multiple of 4).
        # M19 AC#9: split the negative-modulo trick into two named steps for
        # readability. Behavior unchanged — `test_unpadded_base64_handled`
        # still covers it.
        missing = (4 - len(payload_b64) % 4) % 4
        padded = payload_b64 + "=" * missing
        payload = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(payload, dict):
            return None
        exp = payload.get("exp")
        # Tight type guard: exclude bool (Python: isinstance(True, int) is True),
        # require finite and positive epoch. Anything else hits the fallback.
        if not isinstance(exp, (int, float)) or isinstance(exp, bool):
            return None
        if not math.isfinite(exp) or exp <= 0:
            return None
        return dt_util.utc_from_timestamp(int(exp) - _TOKEN_EXP_SAFETY_MARGIN_S)
    except (AttributeError, OSError, OverflowError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _resolve_token_expiry(token: str | None) -> datetime.datetime:
    """Return the JWT-derived expiry, or `now + 1h - safety_margin` on failure.

    Wraps `_decode_jwt_exp` with the fallback-and-warn behavior the auth flow
    needs. The fallback path applies the SAME safety margin as the happy path
    so the two branches have consistent effective lifetimes.

    The fallback WARN log is rate-limited via the module-level
    ``_JWT_EXP_FALLBACK_WARNED`` flag: first malformed token warns, subsequent
    ones log at DEBUG. Prevents log flooding on a persistently-bad Cognito
    response without losing the diagnostic signal entirely.
    """
    global _JWT_EXP_FALLBACK_WARNED
    decoded = _decode_jwt_exp(token)
    if decoded is not None:
        return decoded
    msg = "JWT exp claim missing or unparseable; falling back to 1h hardcoded expiry"
    if not _JWT_EXP_FALLBACK_WARNED:
        _LOGGER.warning(msg)
        _JWT_EXP_FALLBACK_WARNED = True
    else:
        _LOGGER.debug(msg)
    return (
        dt_util.utcnow()
        + datetime.timedelta(hours=1)
        - datetime.timedelta(seconds=_TOKEN_EXP_SAFETY_MARGIN_S)
    )


class AqualinkClient:
    """Client to interact with iAqualink API for multiple robot types."""

    def __init__(
        self,
        username: str,
        password: str,
        api_key: str,
        include_seconds_remaining: bool = True,
    ) -> None:
        self._username: str = username
        self._password: str = password
        self._api_key: str = api_key
        # Whether to include seconds in `time_remaining_human`. Setting this
        # to False reduces HA activity-log entries from ~one-per-poll to
        # one-per-minute without changing SCAN_INTERVAL. Plumbed in from the
        # config-flow / options-flow option `include_seconds_remaining`.
        self._include_seconds_remaining: bool = include_seconds_remaining
        self._auth_token: str = ""
        self._id: str = ""
        self._id_token: str = ""
        self._app_client_id: str = ""
        self._serial: str = ""
        self._device_type: str = ""
        self._first_name: str = ""
        self._last_name: str | None = None
        self._model: str | None = None
        # Cloud-advertised capability list from ``/devices/v2/{serial}/features``.
        # Verified to vary BETWEEN devices of the same family: of two live vr
        # robots, one advertised `preset_waterline` / `custom_mode` /
        # `spot_clean` / `water_temp` and the other did not. Empty until the
        # features call succeeds; consumers must treat empty as "unknown, use
        # the per-family default" rather than "device supports nothing".
        self._features: list[str] = []
        # Equipment identity from ``state.reported.eboxData`` — component part
        # numbers and serials. Cached on each shadow read so ``diagnostics.py``
        # can include it without issuing its own request. Part numbers identify
        # a model (not an account) and feed ``MODEL_BY_CLEANER_PN``.
        self._ebox_data: dict[str, Any] = {}
        # Last raw ``equipment.robot`` block, for the diagnostics download.
        # Lets a reporter hand over exactly what the cloud sent without
        # running any script or sharing credentials (see issue #100).
        self._last_robot_block: dict[str, Any] = {}
        self._token_expires_at: datetime.datetime | None = None
        self._model_fetch_attempts: int = 0
        self._model_last_attempt: datetime.datetime | None = None
        self._pending_stop_reset: dict[str, Any] | None = None
        # H6: paired with `_pending_stop_reset`. Cleared whenever
        # ``_pending_stop_reset`` is cleared; the two are always both-set or
        # both-``None``.
        self._pending_stop_reset_at: datetime.datetime | None = None
        self._remote_control_active: bool = False
        self._activity: VacuumActivity = VacuumActivity.IDLE
        # Set by ``set_hass`` once the coordinator wires the integration in.
        self._hass: HomeAssistant | None = None
        self._headers: dict[str, str] = {
            "Content-Type": "application/json; charset=utf-8",
            "Connection": "keep-alive",
            "Accept": "*/*",
        }
        # Persistent websocket connection for commands and status updates
        self._ws_connection: aiohttp.ClientWebSocketResponse | None = None
        self._ws_session: aiohttp.ClientSession | None = None
        self._last_ws_activity: float | None = None
        # Callback the coordinator wires in to be notified of real-time updates.
        self._coordinator_callback: Callable[[], Awaitable[None]] | None = None
        # C4-ws: push-callback wired by the coordinator. The WS listener uses
        # this to apply a merged shadow envelope directly via
        # ``coordinator.async_set_updated_data`` — no HTTP refresh roundtrip.
        self._realtime_push_callback: (
            Callable[[dict[str, Any]], Awaitable[None]] | None
        ) = None
        # C4-ws: cached deep copy of the last full ``_ws_subscribe`` response.
        # The WS listener deep-merges inbound shadow deltas into this baseline
        # so the existing per-device-type parsers (which expect a complete
        # envelope) keep working unchanged on the push path. ``None`` until the
        # first successful ``_ws_subscribe`` — the listener falls back to
        # ``_coordinator_callback`` (refresh) for that first race window.
        self._last_ws_envelope: dict[str, Any] | None = None
        # C4-ws: tuple of (state, errorState, prCyc, cycleStartTime, stepper,
        # cyclobat-main-state, cyclobat-battery-charge) extracted from the
        # last-applied envelope. Used to dedup repeated StateReported pushes
        # that don't change anything we care about.
        self._last_applied_state_signature: tuple[Any, ...] | None = None
        # Websocket connection failure tracking (not device offline status)
        # Device being 'offline' via working websocket is normal, not a failure
        self._ws_consecutive_failures: int = 0
        self._last_known_status: str | None = None
        # Allow 5 websocket connection failures before marking offline.
        self._max_ws_failures_before_offline: int = 5
        # Simple instance ID for logging purposes
        self._instance_id: str = f"ha-{hash(username + str(id(self))) % 10000:04d}"

        # Serialises `_authenticate()` so two coordinator cycles overlapping
        # near token expiry can't both fire parallel Cognito refresh requests
        # (race on `self._id_token` / `self._token_expires_at`). H9b absorbed
        # this from the H9a review. Double-checked locking pattern used inside
        # `_authenticate` makes the second caller a no-op once the first
        # refresh completes.
        self._auth_lock: asyncio.Lock = asyncio.Lock()

        # Status stabilization to prevent rapid connection status changes
        self._status_history: list[dict[str, Any]] = []
        # 30 second window to stabilize status.
        self._status_stabilization_window: int = 30
        # Minimum time (seconds) a status must persist before changing.
        self._minimum_status_duration: int = 15
        self._last_status_change_time: float = 0
        # The stabilized status we report.
        self._stable_status: str | None = None

        # Set by per-family ``parse_status`` to the most recently observed
        # translation key (e.g. ``"floor_only"``); read by callers that need
        # to surface the last-known fan_speed when the cloud doesn't echo
        # one back on a command response. Initialised lazily so an unknown
        # family doesn't crash on first read. Typed as ``Any`` because the
        # VR / vortrax / cyclobat / cyclonext families write a translation
        # key string here while the i2d family writes the cloud-side int
        # code — the inconsistency predates P6 and tightening it is out
        # of scope.
        self._fan_speed: Any = None

        # Reference to the owning coordinator, set via
        # ``set_coordinator_reference`` once the coordinator constructs
        # itself. Used by paths that need to schedule debounced refreshes
        # without going through the public coordinator API.
        self._coordinator_ref: AqualinkDataUpdateCoordinator | None = None

        # Wall-clock stamp of the last successful stepper-adjust command;
        # used by ``_adjust_stepper`` to rate-limit timing changes to one
        # per 3 s. ``None`` until the first successful adjust.
        self._last_timing_command_time: float | None = None

    def _get_resilient_status(self, error_context: str) -> str:
        """Get status that's resilient to temporary websocket connection failures.

        Note: This handles websocket connection issues, not device offline status.
        A device reporting 'offline' through a working websocket is normal operation.

        Args:
            error_context: Context of the error (e.g., 'update_failed', 'setup_cancelled')

        Returns:
            Status string - preserves last known status for temporary connection failures,
            returns 'offline' only after multiple consecutive connection failures
        """
        if self._ws_consecutive_failures >= self._max_ws_failures_before_offline:
            # Multiple consecutive connection failures - communication is likely offline
            _LOGGER.warning(
                "Device communication marked offline after %s consecutive websocket connection failures",
                self._ws_consecutive_failures)
            return "offline"
        elif self._last_known_status:
            # Temporary connection failure - preserve last known status
            _LOGGER.debug(
                "Preserving last known status '%s' during temporary connection failure (%s)",
                self._last_known_status, error_context)
            return self._last_known_status
        else:
            # No previous status available - default to a neutral status
            _LOGGER.debug("No previous status available, using 'unknown' for %s", error_context)
            return "unknown"

    def _should_use_websocket(self) -> bool:
        """Check if websocket operations should be attempted based on recent connection failure history.

        Note: Device being 'offline' is not considered a websocket failure - only actual
        connection/communication problems count as websocket failures.

        Returns:
            bool: True if websocket operations should be attempted, False to use fallback behavior
        """
        # During high connection failure periods, consider temporary fallback to HTTP-only operations
        if self._ws_consecutive_failures >= 3:
            _LOGGER.debug(
                "Websocket operations temporarily suspended due to %s consecutive connection failures",
                self._ws_consecutive_failures)
            return False
        return True

    def _reset_websocket_failures(self) -> None:
        """Reset websocket failure counters after successful operation."""
        if self._ws_consecutive_failures > 0:
            _LOGGER.debug("Resetting websocket failure counter (was %s)", self._ws_consecutive_failures)
            self._ws_consecutive_failures = 0

    def _stabilize_status(self, raw_status: str) -> str:
        """Stabilize status changes to prevent rapid flapping between connected/disconnected.

        This prevents the sensor from rapidly changing state due to temporary network issues
        or websocket reconnections by requiring a status to persist for a minimum duration.

        Only applies to websocket-based robots (vr, vortrax, cyclobat, cyclonext).
        i2d robots use HTTP API and don't need status stabilization.

        Args:
            raw_status: The immediate status from the API/websocket

        Returns:
            The stabilized status that should be reported to Home Assistant
        """
        # Only apply stabilization to websocket-based robots
        if self._device_type == "i2d_robot":
            return raw_status

        current_time = time.time()

        # Track status in history with timestamps
        self._status_history.append({
            'status': raw_status,
            'timestamp': current_time
        })

        # Clean up old history entries outside the stabilization window
        cutoff_time = current_time - self._status_stabilization_window
        self._status_history = [
            entry for entry in self._status_history
            if entry['timestamp'] > cutoff_time
        ]

        # If this is the first status reading, set it immediately
        if self._stable_status is None:
            self._stable_status = raw_status
            self._last_status_change_time = current_time
            _LOGGER.debug("Status stabilizer: Initial status set to '%s'", raw_status)
            return self._stable_status

        # If the current raw status matches our stable status, just return it
        if raw_status == self._stable_status:
            return self._stable_status

        # Check if enough time has passed since the last status change
        time_since_last_change = current_time - self._last_status_change_time

        if time_since_last_change < self._minimum_status_duration:
            # Not enough time has passed - keep the stable status
            _LOGGER.debug(
                "Status stabilizer: Ignoring rapid change from '%s' to '%s' (only %.1fs elapsed, need %ss)",
                self._stable_status, raw_status, time_since_last_change, self._minimum_status_duration)
            return self._stable_status

        # Enough time has passed - analyze the recent history to see if the new status is consistent
        recent_entries = [
            entry for entry in self._status_history
            if entry['timestamp'] > (current_time - self._minimum_status_duration)
        ]

        # Count how many recent entries match the new raw status
        matching_count = sum(1 for entry in recent_entries if entry['status'] == raw_status)
        total_recent_count = len(recent_entries)

        # If most recent readings are the new status, accept the change
        if total_recent_count > 0 and (matching_count / total_recent_count) >= 0.7:  # 70% threshold
            _LOGGER.debug(
                "Status stabilizer: Status change accepted from '%s' to '%s' (%s/%s recent readings match)",
                self._stable_status, raw_status, matching_count, total_recent_count)
            self._stable_status = raw_status
            self._last_status_change_time = current_time
            return self._stable_status
        else:
            # Not enough consistency - keep the stable status
            _LOGGER.debug(
                "Status stabilizer: Status change rejected from '%s' to '%s' (%s/%s recent readings match, need ≥70%%)",
                self._stable_status, raw_status, matching_count, total_recent_count)
            return self._stable_status

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def robot_id(self) -> str:
        return self._serial or self._username

    @property
    def serial(self) -> str:
        return self._serial

    @property
    def device_type(self) -> str:
        return self._device_type

    @property
    def features(self) -> list[str]:
        """Capability list the cloud advertises for this device.

        From ``/devices/v2/{serial}/features``. Verified to vary between
        devices of the same family, so entity layers should gate on this
        rather than on ``device_type``. Empty until the features call
        succeeds — treat empty as "unknown", not "supports nothing".
        """
        return list(self._features)

    @property
    def robot_name(self) -> str:
        """Human-readable robot name for log messages.

        Prefers the user-typed config-entry title (set on the coordinator
        and exposed via ``coordinator.title``) so log lines render
        "Add 15 minutes command sent to Pool Robot" instead of the
        synthetic "vr_R23X12345678" fallback. Pre-review this looked at
        ``getattr(self, "_title", ...)`` — but ``_title`` lives on the
        coordinator, NOT the client, so the getattr always fell through
        to the synthetic.
        """
        coordinator = self._coordinator_ref
        if coordinator is not None and coordinator.title:
            return coordinator.title
        return f"{self._device_type}_{self._serial}"

    def _is_token_expired(self) -> bool:
        """Check if authentication token is expired.

        Both sides of the comparison are aware-UTC after H8: `_token_expires_at`
        is set by `_resolve_token_expiry` (aware-UTC) and `dt_util.utcnow()`
        returns aware-UTC. Pre-H8 both were naive-local; the comparison still
        worked but produced subtly-wrong values around DST and was a latent
        timezone-correctness bug.
        """
        if not self._token_expires_at:
            return True
        return dt_util.utcnow() >= self._token_expires_at

    def set_hass(self, hass: HomeAssistant) -> None:
        """Set Home Assistant instance for translations."""
        self._hass = hass

    @asynccontextmanager
    async def _cloud_session(self) -> AsyncIterator[aiohttp.ClientSession]:
        """Yield an aiohttp session for iAqualink cloud REST calls (story L19).

        When ``self._hass`` is set, constructs a per-call ``aiohttp.ClientSession``
        that **reuses HA's pooled connector** (so the integration keeps the
        connection-pooling win — no fresh TLS handshake per request) but with
        its own private ``aiohttp.DummyCookieJar``. ``connector_owner=False``
        means closing the wrapper does NOT close the connector — HA still
        owns its lifecycle. This is cheap: ``aiohttp.ClientSession``
        construction is pure Python (no I/O); the TCP/TLS work is in the
        connector, which we share.

        **Why the private cookie jar (L19 review F1).** HA's
        ``async_get_clientsession(hass)`` returns a process-wide session
        with a shared default cookie jar. iAqualink's Cognito- and ALB-
        fronted endpoints set session-affinity cookies (``AWSALB``,
        ``AWSALBCORS``) on responses. Without isolation, those persist
        for the HA-process lifetime and auto-attach to any other
        integration's requests within a matching ``Domain``/``Path``
        scope — the symmetric risk to the header leak the story's
        original AC#1 defended via headers-on-request. A
        ``DummyCookieJar`` silently drops every cookie set on every
        cloud response, defending the leak without affecting auth (we
        don't rely on cookies — every request carries its own
        Authorization header).

        When ``self._hass`` is None (config-flow discovery via the
        ``discover_devices`` classmethod, or unit tests that build a bare
        ``AqualinkClient``), falls back to a one-shot ``aiohttp.ClientSession``
        that closes on exit. Pooling has no value for a single-shot
        discovery probe.

        Headers are NOT passed to the session here — callers MUST attach
        them per-request (``session.get(url, headers=...)``). The shared
        HA connector is shared across many integrations, so setting default
        headers on the wrapper would still leak auth via the request path
        if the wrapper were ever reused — keep headers per-request for
        the same defence-in-depth reasoning the original L19 patch used.

        Note: the websocket session at ``_ensure_websocket_connection`` is
        intentionally owned by this client (not shared) — it holds the live
        WS connection and is closed via ``_close_websocket``.

        There is no longer any third-party HTTP call to isolate: the Vortrax
        web scraper this note used to warn about has been removed in favour
        of the offline ``MODEL_BY_CLEANER_PN`` table, so the integration now
        talks only to the iAqualink cloud.
        """
        if self._hass is not None:
            shared = async_get_clientsession(self._hass)
            async with aiohttp.ClientSession(
                connector=shared.connector,
                connector_owner=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            ) as session:
                yield session
        else:
            async with aiohttp.ClientSession() as session:
                yield session

    async def fetch_status(self, quick_setup: bool = False) -> dict[str, Any]:
        """Authenticate, discover device, and parse status.

        Args:
            quick_setup: If True, skip websocket operations for faster initial setup
        """
        # Only authenticate if we don't have a valid token
        if not self._auth_token or self._is_token_expired():
            await self._authenticate()

        # Only discover device if we don't have serial and device type
        if not self._serial or not self._device_type:
            await self._discover_device()

        # During quick setup, return minimal data without websocket operations
        if quick_setup:
            return {
                "serial_number": self._serial,
                "device_type": self._device_type,
                "status": "ready",
                "model": "Unknown",
                "activity": "unknown",
                "setup_mode": True
            }

        # Update device status based on device type
        if self._device_type == "i2d_robot":
            data = await self._update_i2d_robot()
            # For i2d robots, model is not available via API, set to Hidden
            data["model"] = "Hidden"
        else:
            data = await self._update_other_robots()

            # Get model for non-i2d robots, only if not already cached
            if not self._model:
                self._model = await self._get_device_model()
            data["model"] = str(self._model)
            # Surface the cloud's capability list so entity layers can gate on
            # what the device actually supports instead of on device_type alone.
            data["features"] = list(self._features)

        # Apply any pending reset values from a manual stop command (H6).
        self._apply_pending_stop_reset(data)

        return data

    def _apply_pending_stop_reset(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply / discard a pending stop-reset based on age and live cloud state.

        Returns ``data`` (possibly mutated). Always clears
        ``_pending_stop_reset`` and ``_pending_stop_reset_at`` together; the
        reset is single-shot in every branch.

        Pre-H6 the reset was applied unconditionally on the next poll,
        regardless of how much time had passed or what the cloud reported.
        Two failure modes that H6 closes:

          1) **User restarts cleaning between stop and the next poll.**
             The cloud already reports ``activity == "cleaning"``; pre-H6
             we still applied the reset and overwrote it back to ``"idle"``,
             silently lying to the UI about what the robot was doing.

          2) **Stale zombie reset.** If a poll never lands (long
             disconnect, network drop), the next successful poll — possibly
             minutes later — applied the reset and clobbered whatever the
             robot was doing by then.

        H6 guards (1) with a state-awareness check against
        ``data["activity"]`` and (2) with a timestamp paired against
        ``PENDING_STOP_RESET_MAX_AGE_SECONDS``.

        Extracted into a method so the logic is directly unit-testable
        without spinning up the full ``fetch_status`` cloud round-trip
        (see ``tests/test_coordinator.py`` — apply-pending-stop-reset block).
        """
        if not self._pending_stop_reset or not self._pending_stop_reset_at:
            return data

        # H6 review follow-up: clamp to 0.0 so a backward wall-clock jump
        # (NTP correction, manual clock change) can't make a real outage
        # look fresh and make the stale-age branch unreachable. Mirrors
        # the same fix applied to `_outage_age_seconds` (story H7 review).
        age_seconds = max(
            (dt_util.utcnow() - self._pending_stop_reset_at).total_seconds(),
            0.0,
        )
        live_activity = data.get("activity")

        if age_seconds > PENDING_STOP_RESET_MAX_AGE_SECONDS:
            _LOGGER.debug(
                "Discarding pending stop reset (age %.1fs > %ds; never observed an idle poll)",
                age_seconds,
                PENDING_STOP_RESET_MAX_AGE_SECONDS,
            )
        elif live_activity != "idle":
            _LOGGER.debug(
                "Discarding pending stop reset: cloud reports activity=%r (user restarted before reset applied)",
                live_activity,
            )
        else:
            _LOGGER.debug("Applying pending stop reset values: %s", self._pending_stop_reset)
            data.update(self._pending_stop_reset)

        self._pending_stop_reset = None
        self._pending_stop_reset_at = None
        return data

    async def _authenticate(self, force: bool = False) -> None:
        """Authenticate with iAqualink API.

        Serialised under ``self._auth_lock`` with a double-check guard so two
        coordinator cycles overlapping near token expiry collapse into a
        single Cognito refresh. The second caller acquires the lock after the
        first completed, sees a valid token, and returns without re-auth.

        ``force=True`` bypasses the recheck — used by the 401-retry paths so
        the spec's "refresh-then-retry" mitigation does an actual Cognito
        round-trip even when the local token still looks valid (a cloud-side
        revocation looks locally-valid until the response says otherwise).
        """
        async with self._auth_lock:
            # Double-check inside the lock: if a peer just refreshed, skip.
            # Cheap when uncontested (single attribute reads); collapses the
            # parallel-refresh race documented in the H9a review.
            #
            # H9b review P4: gate on `_id_token` (matching `fetch_status` and
            # `_ensure_websocket_connection`) rather than `_auth_token`. The
            # two are set together inside this method, but keying on the same
            # attribute the caller gates on removes the asymmetry that could
            # let a partial-failure state slip past the recheck.
            if not force and self._id_token and not self._is_token_expired():
                _LOGGER.debug("Skipping authenticate — token still valid")
                return
            _LOGGER.debug("Authenticating with iAqualink API...")
            try:
                payload = {
                    "apiKey": self._api_key,
                    "email": self._username,
                    "password": self._password,
                }
                body = json.dumps(payload)
                auth = await asyncio.wait_for(
                    self.send_login(body, self._headers), timeout=30
                )

                # H9b sign-off: a 200-shaped Cognito body that is missing the
                # expected envelope means the response is an error payload
                # (e.g. ``{"__type": "NotAuthorizedException", ...}``).
                # Translate the resulting KeyError to AuthFailedError so the
                # coordinator boundary triggers reauth instead of letting the
                # broad-except ladder absorb it as transient noise.
                try:
                    self._first_name = auth["first_name"]
                    self._last_name = auth["last_name"]
                    self._id = auth["id"]
                    self._auth_token = auth["authentication_token"]
                    self._id_token = auth["userPoolOAuth"]["IdToken"]
                    self._app_client_id = auth["cognitoPool"]["appClientId"]
                except (KeyError, TypeError) as err:
                    raise AuthFailedError(
                        f"send_login returned an unexpected body shape "
                        f"(missing or wrong-typed key {err}); treating as "
                        "credential rejection"
                    ) from err

                # Prefer the real `exp` claim from the JWT; fall back to a
                # conservative 1h (minus the safety margin, consistent with the
                # decoded happy path) if the token is missing/malformed.
                self._token_expires_at = _resolve_token_expiry(self._id_token)
                _LOGGER.debug("Authentication successful, token expires at: %s", self._token_expires_at)
            except asyncio.CancelledError:
                _LOGGER.debug("Authentication cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior

    async def _discover_device(self, target_serial: str | None = None) -> None:
        """Get list of devices and pick the pool robot.

        If target_serial is provided, select that specific device.
        Otherwise, select the first compatible device found.
        """
        try:
            params = {
                "authentication_token": self._auth_token,
                "user_id": self._id,
                "api_key": self._api_key
            }
            devices = await asyncio.wait_for(self.get_devices(params, self._headers), timeout=30)

            # Filter only devices that are compatible with the module
            supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr", "vortrax"]
            compatible_devices = []

            for device in devices:
                device_type = device.get("device_type")
                _LOGGER.debug("Devices found : %s", device)
                if device_type in supported_device_types:
                    serial = device["serial_number"]
                    compatible_devices.append(device)

                    # If we're looking for a specific device and found it, select it
                    if target_serial and serial == target_serial:
                        self._serial = serial
                        self._device_type = device_type
                        _LOGGER.debug("Target device selected: %s - %s", device_type, serial)
                        return
                else:
                    _LOGGER.debug("Device ignored (unsupported type): %s", device_type)

            # If we have compatible devices but weren't looking for a specific one,
            # select the first compatible device
            if compatible_devices and not target_serial:
                device = compatible_devices[0]
                self._serial = device["serial_number"]
                self._device_type = device["device_type"]
                _LOGGER.debug("Device selected: %s - %s", self._device_type, self._serial)
                return

            # If we were looking for a specific device but didn't find it,
            # or if we found no compatible devices at all
            if target_serial:
                _LOGGER.error("Target device %s not found in the device list.", target_serial)
                raise RuntimeError(f"Target device {target_serial} not found")
            else:
                _LOGGER.error("No compatible robot found in the device list.")
                raise RuntimeError("No supported robot found")
        except asyncio.CancelledError:
            _LOGGER.debug("Device discovery cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    @classmethod
    async def discover_devices(
        cls, username: str, password: str, api_key: str
    ) -> list[dict[str, Any]]:
        """Discover all available devices for an account.

        Returns a list of device information dictionaries.
        """
        client = cls(username, password, api_key)
        await client._authenticate()

        params = {
            "authentication_token": client._auth_token,
            "user_id": client._id,
            "api_key": client._api_key
        }
        devices = await asyncio.wait_for(client.get_devices(params, client._headers), timeout=30)

        # Filter only devices that are compatible with the module
        supported_device_types = ["i2d_robot", "cyclonext", "cyclobat", "vr", "vortrax"]
        compatible_devices = []

        for device in devices:
            device_type = device.get("device_type")
            if device_type in supported_device_types:
                compatible_devices.append({
                    "serial_number": device["serial_number"],
                    "device_type": device_type,
                    "name": device.get("name", f"{device_type}_{device['serial_number']}")
                })

        return compatible_devices

    async def _ensure_websocket_connection(self) -> None:
        """Ensure we have an active websocket connection, creating one if needed.
        Implements retry logic to handle transient connection issues."""
        # Check if connection exists and is still valid
        if (self._ws_connection and not self._ws_connection.closed and
                self._ws_session and not self._ws_session.closed):
            # Connection appears healthy, update activity timestamp
            self._last_ws_activity = time.time()
            return

        # Ensure we have valid authentication before attempting websocket connection
        if not self._id_token or self._is_token_expired():
            _LOGGER.debug("Refreshing authentication before websocket connection")
            await self._authenticate()

        # Reset failure counter on successful reconnection attempts
        max_retries = 3
        retry_delay: float = 2  # seconds

        for attempt in range(max_retries):
            try:
                # Close any existing connection before creating new one
                await self._close_websocket()

                # Create new aiohttp session for websocket
                timeout = aiohttp.ClientTimeout(total=30)
                self._ws_session = aiohttp.ClientSession(timeout=timeout)

                # Create websocket headers with authentication
                ws_headers = {
                    "Authorization": self._id_token,
                    "Accept": "*/*"
                }

                # Connect to websocket
                self._ws_connection = await self._ws_session.ws_connect(
                    URL_WS,
                    headers=ws_headers
                )

                self._last_ws_activity = time.time()
                self._ws_consecutive_failures = 0  # Reset failure counter on success
                _LOGGER.debug("Established persistent websocket connection for robot %s", self._serial)
                return

            except aiohttp.WSServerHandshakeError as e:
                # Typed status check replaces the pre-H9b fragile substring
                # match on the exception message (AC#3). 401 is auth-failed →
                # surface to coordinator as AuthFailedError so HA reauth
                # fires. 403 stays an error log.
                self._ws_consecutive_failures += 1
                await self._close_websocket()
                if e.status == 401:
                    _LOGGER.warning(
                        "Websocket authentication failed (attempt %s): %s", attempt + 1, e
                    )
                    if attempt < max_retries - 1:
                        # Try a token refresh and retry once. If re-auth itself
                        # raises, surface AuthFailedError immediately so the
                        # coordinator's translation fires HA reauth.
                        _LOGGER.debug("Refreshing authentication token due to websocket 401 error")
                        try:
                            await self._authenticate(force=True)
                        except asyncio.CancelledError:
                            # H9b review P7: never swallow cancellation as auth
                            # failure — propagate the shutdown signal.
                            raise
                        except Exception as auth_err:
                            _LOGGER.debug("Re-auth after WS 401 failed: %s", auth_err)
                            # H9b review P8: include both the WS handshake error
                            # and the re-auth error in the message so the traceback
                            # surfaces full context (was only `auth_err` before).
                            raise AuthFailedError(
                                f"WS handshake 401 ({e}) and re-auth failed: {auth_err}"
                            ) from e
                        _LOGGER.debug("Retrying websocket connection in %ss...", retry_delay)
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    # H9b review P6: drop the misleading "fall through to next
                    # iteration" comment — we `raise` immediately here.
                    raise AuthFailedError(f"WS handshake 401 after {max_retries} attempts") from e
                if e.status == 403:
                    _LOGGER.error("Websocket access forbidden (attempt %s): %s", attempt + 1, e)
                else:
                    _LOGGER.warning("Websocket handshake failed (attempt %s): %s", attempt + 1, e)
                if attempt < max_retries - 1:
                    _LOGGER.debug("Retrying websocket connection in %ss...", retry_delay)
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                else:
                    _LOGGER.error(
                        "Failed to establish websocket connection after %s attempts: %s", max_retries, e
                    )
                    raise
            except Exception as e:
                self._ws_consecutive_failures += 1
                await self._close_websocket()
                _LOGGER.warning("Websocket connection failed (attempt %s): %s", attempt + 1, e)
                if attempt < max_retries - 1:
                    _LOGGER.debug("Retrying websocket connection in %ss...", retry_delay)
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5  # Exponential backoff
                else:
                    _LOGGER.error("Failed to establish websocket connection after %s attempts: %s", max_retries, e)
                    raise

    async def _close_websocket(self) -> None:
        """Close the websocket connection and session if they exist."""
        if self._ws_connection:
            try:
                if not self._ws_connection.closed:
                    await self._ws_connection.close()
            except Exception as e:
                _LOGGER.debug("Error closing websocket connection: %s", e)
            finally:
                self._ws_connection = None

        if self._ws_session:
            try:
                if not self._ws_session.closed:
                    await self._ws_session.close()
            except Exception as e:
                _LOGGER.debug("Error closing websocket session: %s", e)
            finally:
                self._ws_session = None

        self._last_ws_activity = None

        # C4-ws review F3: invalidate the cached shadow baseline and the
        # last-applied dedup signature on close. Without this, a
        # reconnect would deep-merge new inbound deltas onto the
        # pre-disconnect baseline (silently propagating stale fields the
        # delta didn't touch) and could even dedup-drop the first real
        # post-reconnect push if the cloud's first message happens to
        # match the pre-disconnect signature. The next ``_ws_subscribe``
        # after reconnect re-seeds both via the normal flow.
        self._last_ws_envelope = None
        self._last_applied_state_signature = None

    async def _websocket_listener(self) -> None:
        """Listen for real-time websocket updates and apply them via the coordinator push hook.

        Story C4-ws rewrote this method:

        - **AC #1 (intake filter):** ``_parse_ws_state_message`` rejects
          heartbeats, shadow ACKs, foreign-service messages, and StateReported
          envelopes with empty ``equipment.robot``. Those messages no longer
          trigger any coordinator work at all.
        - **AC #3 (dedup):** the merged envelope's "significant fields" tuple
          (``_state_signature``) is compared to the last-applied signature;
          repeated StateReported messages that don't change anything we care
          about are dropped before the push.
        - **AC #2 + AC #4 (direct push):** on a real state change, the inbound
          ``reported`` delta is deep-merged into the cached baseline envelope
          and dispatched once via ``await self._realtime_push_callback(...)``
          — which the coordinator implements as ``async_set_updated_data``
          (no fetch_status, no debouncer, no HTTP roundtrip).

        The pre-C4-ws code fired up to two ``asyncio.create_task`` callbacks
        per message (one for stepper, one for state) — both unawaited, both
        funnelling into ``async_request_refresh``. A chatty cloud session
        therefore produced multiple overlapping refresh fan-outs per inbound
        StateReported plus a duplicated dispatch when a single message
        touched both stepper and state simultaneously. That entire pattern
        is replaced by a single awaited push per *delta*.

        Fallback: until ``_last_ws_envelope`` is seeded (first listener cycle
        before the initial poll), state-bearing messages route to the
        refresh callback so the next ``_ws_subscribe`` can seed the baseline.
        """
        message_count = 0
        applied_count = 0

        try:
            # Ensure websocket connection is established
            await self._ensure_websocket_connection()

            if not self._ws_connection or self._ws_connection.closed:
                _LOGGER.warning("Cannot start websocket listener - no connection available")
                return

            # Send initial subscription request to start receiving updates
            subscribe_req = {
                "action": "subscribe",
                "namespace": "authorization",
                "payload": {"userId": self._id},
                "service": "Authorization",
                "target": self._serial,
                "version": 1
            }

            await self._ws_connection.send_json(subscribe_req)
            _LOGGER.debug("Started websocket listener for robot %s", self._serial)

            # Listen for incoming messages
            async for message in self._ws_connection:
                if message.type == aiohttp.WSMsgType.TEXT:
                    message_count += 1
                    inbound_reported = self._extract_ws_state_delta(message)
                    if inbound_reported is None:
                        continue

                    try:
                        if await self._apply_ws_state_delta(inbound_reported):
                            applied_count += 1
                    except asyncio.CancelledError:
                        raise
                    except ConfigEntryAuthFailed:
                        # Surface auth failures to HA so reauth fires; do not
                        # swallow them in the broad-except below. Mirror of the
                        # H10 review fix.
                        raise
                    except Exception as apply_err:  # noqa: BLE001 - per-message isolation
                        _LOGGER.debug(
                            "Error applying realtime websocket message: %s", apply_err
                        )

                elif message.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning("Websocket error in listener")
                    break
                elif message.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.debug("Websocket connection closed in listener")
                    break

        except asyncio.CancelledError:
            _LOGGER.debug("Websocket listener cancelled")
            raise
        except ConfigEntryAuthFailed:
            # C4-ws review F1: auth failures from the per-message handler
            # MUST propagate up to HA's coordinator so reauth dispatches.
            # Pre-fix, the broad-except below caught the propagated value
            # (ConfigEntryAuthFailed inherits from Exception) and demoted
            # it to WARNING — the listener task died silently and the
            # user never saw the reauth prompt until the next polled
            # cycle's 401 hit. Same H10 precedent as the C4-cmd command
            # sites: never swallow auth failures.
            _LOGGER.debug("Websocket listener exiting on ConfigEntryAuthFailed")
            raise
        except Exception as e:
            _LOGGER.warning("Websocket listener error: %s", e)
        finally:
            _LOGGER.debug(
                "Websocket listener stopped after %s messages (%s applied)",
                message_count, applied_count
            )

    # -- _websocket_listener helpers ---------------------------------------
    #
    # Split out of what used to be a single 200-line method with two
    # independent concerns (classify the inbound message / apply the delta)
    # tangled inside one loop body. Behaviour is unchanged: the intake filter
    # returns None for everything that used to `continue`, and the apply step
    # returns True exactly when a push happened (the old `applied_count += 1`).

    def _extract_ws_state_delta(
        self, message: aiohttp.WSMessage
    ) -> dict[str, Any] | None:
        """Return the ``reported`` delta of a state message, else ``None``.

        Rejects, in order: undecodable JSON, message shapes the typed
        classifier doesn't recognise, non-StateStreamer services (ACK /
        error / heartbeat), envelopes failing top-level field validation,
        and StateReported envelopes carrying no ``equipment.robot`` content
        (status-only pings).
        """
        try:
            message_json = message.json()
        except Exception as parse_err:
            _LOGGER.debug("Could not JSON-decode websocket message: %s", parse_err)
            return None

        # P12 AC #1 + AC #2: typed classifier. ``parse_ws_message`` narrows
        # the raw dict into ``WSStateEnvelope`` / ``WSAck`` / ``WSError`` /
        # ``WSHeartbeat``, or returns None (logging DEBUG on truly unknown
        # shapes, rate-limited so a chatty cloud session doesn't spam).
        typed_payload = parse_ws_message(message_json)
        if typed_payload is None:
            return None

        # Only StateStreamer envelopes carry the reported delta we apply.
        # ACK / error / heartbeat messages are recognised + classified above
        # so they don't trip the "unknown shape" DEBUG log, but they have no
        # payload for us to act on. The runtime ``service`` check narrows the
        # value to a state envelope; mypy can't prove TypedDict-union
        # narrowing through a ``.get()`` call, so the explicit ``cast``
        # documents the invariant without a runtime cost (TypedDict is just a
        # dict at runtime, ``cast`` is a no-op).
        if typed_payload.get("service") != "StateStreamer":
            return None
        state_envelope = cast(WSStateEnvelope, typed_payload)

        # P12 AC #3: top-level field validation — warns once per missing
        # field per process so cloud-shape drift surfaces with an actionable
        # log line.
        if not validate_state_envelope_fields(state_envelope):
            return None

        # AC #1 (original C4-ws intake filter).
        return self._parse_ws_state_message(state_envelope)

    async def _apply_ws_state_delta(self, inbound_reported: dict[str, Any]) -> bool:
        """Merge one inbound delta into the baseline and push it.

        Returns ``True`` when a realtime push actually happened, ``False``
        when the delta was deduped away or routed to the refresh fallback.
        """
        # First-push fallback: no cached baseline yet → route to the refresh
        # callback so the next ``_ws_subscribe`` seeds ``_last_ws_envelope``.
        # Avoids parsing a partial envelope that would silently overwrite
        # cached fields with parser defaults.
        if self._last_ws_envelope is None:
            _LOGGER.debug(
                "WS state arrived before baseline envelope cached; falling back to refresh"
            )
            if self._coordinator_callback is not None:
                await self._coordinator_callback()
            return False

        # Deep-merge the inbound delta onto the cached baseline.
        # ``_deep_merge_dict`` is pure and returns a fresh dict so the
        # in-memory baseline isn't mutated under us if the callback yields
        # mid-merge.
        cached_reported = (
            self._last_ws_envelope
            .get("payload", {})
            .get("robot", {})
            .get("state", {})
            .get("reported", {})
        )
        merged_reported = self._deep_merge_dict(cached_reported, inbound_reported)

        # AC #3: dedup. Build a signature of the user-visible state and bail
        # if it's identical to what we last pushed. Defends against the cloud
        # emitting repeated StateReported events with no functional change.
        merged_robot = merged_reported.get("equipment", {}).get("robot", {})
        new_signature = self._state_signature(merged_robot)
        if new_signature == self._last_applied_state_signature:
            return False

        # Refresh the baseline so the next inbound delta sees this push as
        # part of the merge target.
        self._last_ws_envelope["payload"]["robot"]["state"]["reported"] = merged_reported
        self._last_applied_state_signature = new_signature

        # AC #2 + AC #4: single awaited push. The coordinator implements this
        # as ``async_set_updated_data`` — no HTTP roundtrip, no debouncer.
        applied = False
        if self._realtime_push_callback is not None:
            await self._realtime_push_callback(self._last_ws_envelope)
            applied = True
        elif self._coordinator_callback is not None:
            # Defensive fallback if push wasn't wired (shouldn't happen
            # post-C4-ws, but keep behaviour intact).
            await self._coordinator_callback()

        # P12: clear the drift-warning sets on each successful push so a
        # transient partial-payload phase doesn't permanently silence the
        # WARNING / DEBUG diagnostics.
        reset_drift_warnings()
        return applied

    def set_coordinator_callback(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Set the callback function to notify coordinator of real-time updates."""
        self._coordinator_callback = callback

    def set_realtime_push_callback(
        self, callback: Callable[[dict[str, Any]], Awaitable[None]]
    ) -> None:
        """Set the push callback used by the WS listener (story C4-ws).

        The push callback is invoked with a deep-merged shadow envelope
        (the same shape ``_ws_subscribe`` returns). The coordinator implements
        the merge target — running the per-device-type parser on the envelope
        and calling ``async_set_updated_data`` — so the WS listener side stays
        a pure dispatcher.

        Distinct from ``set_coordinator_callback`` (the legacy callback that
        post-C4-cmd is only invoked by the 6 command methods) so the two
        callsites can evolve independently.
        """
        self._realtime_push_callback = callback

    def set_coordinator_reference(
        self, coordinator: AqualinkDataUpdateCoordinator
    ) -> None:
        """Set reference to coordinator for adaptive polling control."""
        self._coordinator_ref = coordinator

    @staticmethod
    def _deep_merge_dict(
        base: dict[str, Any], overlay: dict[str, Any]
    ) -> dict[str, Any]:
        """Return a new dict with ``overlay`` overlaid onto ``base`` (story C4-ws).

        Recursive on nested dicts. Scalars and lists are replaced wholesale
        (no list concatenation) — matches the AWS IoT shadow document
        semantics where a delta's array overwrites the previous array.

        Used by the WS listener to apply incremental ``StateReported`` deltas
        on top of the cached baseline envelope so the existing per-device-type
        parsers receive a complete, well-shaped dict regardless of whether
        the cloud pushed a full state or just the changed sub-tree.

        Does NOT mutate either input — returns a fresh dict so the cached
        baseline isn't side-channelled into divergent states by callers
        retaining references to it.
        """
        if not isinstance(base, dict) or not isinstance(overlay, dict):
            return overlay if isinstance(overlay, dict) or overlay is not None else base
        result = dict(base)
        for key, overlay_value in overlay.items():
            base_value = result.get(key)
            if isinstance(base_value, dict) and isinstance(overlay_value, dict):
                result[key] = AqualinkClient._deep_merge_dict(base_value, overlay_value)
            else:
                result[key] = overlay_value
        return result

    @staticmethod
    def _parse_ws_state_message(
        message_json: WSStateEnvelope | dict[str, Any],
    ) -> dict[str, Any] | None:
        """Extract the ``reported`` shadow document from an inbound WS message.

        Returns the ``payload.state.reported`` dict for messages we care about
        (StateStreamer service + StateReported event + non-empty
        ``equipment.robot``) and ``None`` for everything else: heartbeats,
        shadow ACKs, foreign-service messages, malformed payloads.

        Pure function: no I/O, no instance state. Lifted out of the listener
        body so the AC #1 filter is independently testable.

        P12: accepts ``WSStateEnvelope`` (the classified-typed shape from
        ``parse_ws_message``) in addition to the raw ``dict[str, Any]`` —
        the runtime ``isinstance`` + ``service`` / ``event`` guards stay
        because callers in test fixtures may pass raw dicts directly.
        """
        if not isinstance(message_json, dict):
            return None
        if message_json.get("service") != "StateStreamer":
            return None
        if message_json.get("event") != "StateReported":
            return None
        payload = message_json.get("payload") or {}
        state = payload.get("state") or {}
        reported = state.get("reported")
        if not isinstance(reported, dict):
            return None
        equipment = reported.get("equipment") or {}
        # C4-ws review F2: cyclonext writes its state under ``robot.1``,
        # not ``robot``. Pre-fix this filter rejected every cyclonext
        # push at the empty-robot check and the entire device family
        # silently fell back to polled-only updates (~3 s latency vs the
        # ~<200 ms push path everyone else got). Fallback to ``robot.1``
        # lets the parser see cyclonext deltas — the per-device-type
        # apply path at ``_apply_realtime_envelope`` already dispatches
        # to ``_update_cyclonext_robot_data`` which reads from the same
        # key.
        robot = equipment.get("robot") or equipment.get("robot.1") or {}
        if not isinstance(robot, dict) or not robot:
            # No equipment.robot content → nothing to apply. Heartbeats and
            # status-only messages land here.
            return None
        # ``reported`` is a TypedDict in the WSStateEnvelope path; widen to
        # the legacy ``dict[str, Any]`` return shape downstream consumers
        # (``_deep_merge_dict``, ``_apply_realtime_envelope``) expect.
        # Runtime ``isinstance`` guard (review MED) before the cast so a
        # drifted ``reported`` value (string, scalar, list — possible if
        # the cloud emits a partial-render shape) surfaces as ``None`` —
        # the listener's per-message ``continue`` handles None gracefully.
        # Without this guard the cast would type-promise a dict and the
        # next call (``_deep_merge_dict``) would hit AttributeError mid-
        # merge, defeating P12's "surface drift, don't swallow it" goal.
        if not isinstance(reported, dict):
            return None
        return cast(dict[str, Any], reported)

    @staticmethod
    def _state_signature(robot_data: dict[str, Any]) -> tuple[Any, ...]:
        """Build a comparable signature of the ``equipment.robot`` fields that drive entity state (story C4-ws).

        The dedup check at AC #3 ("60 s of unchanged WS chatter → 0
        ``_async_update_data`` invocations") relies on this signature being
        stable when the user-visible state hasn't moved. Includes top-level
        VR/vortrax/cyclonext keys *and* cyclobat's nested ``main`` /
        ``battery`` keys so all 4 push-capable device families dedup cleanly.

        Pure: no instance state. ``None`` keys are placed in the tuple so a
        field's appearance/disappearance still counts as a change.
        """
        if not isinstance(robot_data, dict):
            return ()

        def _as_dict(value: Any) -> dict[str, Any]:
            return value if isinstance(value, dict) else {}

        main = _as_dict(robot_data.get("main"))
        battery = _as_dict(robot_data.get("battery"))
        errors = _as_dict(robot_data.get("errors"))
        return (
            robot_data.get("state"),
            robot_data.get("errorState"),
            robot_data.get("prCyc"),
            robot_data.get("cycleStartTime"),
            robot_data.get("stepper"),
            main.get("state"),
            main.get("cycleStartTime"),
            battery.get("userChargePerc"),
            battery.get("userChargeState"),
            # C4-ws review F2: cyclonext-specific fields. Cyclonext writes
            # its state under different keys (``mode`` instead of
            # ``state``, ``cycle`` instead of ``prCyc``, ``errors.code``
            # instead of ``errorState``). Without these the signature
            # tuple was all-None for cyclonext, every consecutive push
            # deduped against the previous all-None tuple, and only the
            # FIRST push after baseline-cache ever fired — real
            # subsequent state changes were silently dropped.
            # ``_update_cyclonext_robot_data`` at L1985 confirms these
            # are the fields the parser actually reads.
            robot_data.get("mode"),
            robot_data.get("cycle"),
            errors.get("code"),
        )

    async def _ws_subscribe(self) -> dict[str, Any] | None:
        """Subscribe via websocket to get live updates."""
        req = {
            "action": "subscribe",
            "namespace": "authorization",
            "payload": {"userId": self._id},
            "service": "Authorization",
            "target": self._serial,
            "version": 1
        }

        # Always use fresh connection for data requests to ensure we get current data
        # This restores the original working behavior
        return await asyncio.wait_for(self.get_device_status(req), timeout=30)

    async def _get_device_model(self) -> str:
        """Get device model information."""
        # Return cached model if available and valid (not "Unknown" or "Not Supported")
        if self._model and self._model not in ["Unknown", "Not Supported"]:
            _LOGGER.debug("Using cached model: %s", self._model)
            return self._model

        # Check if we should retry fetching the model
        should_retry = self._should_retry_model_fetch()
        if not should_retry:
            _LOGGER.debug(
                "Skipping model fetch - too many recent attempts. Current model: %s",
                self._model or 'None')
            return self._model or "Unknown"

        _LOGGER.debug(
            "Fetching model for device type: %s, serial: %s (attempt %s)",
            self._device_type, self._serial, self._model_fetch_attempts + 1)

        # Record this attempt
        self._model_fetch_attempts += 1
        self._model_last_attempt = dt_util.utcnow()

        # Pre-fix there was a vortrax-only branch here that resolved the model
        # by scraping zodiac-poolcare.com for the cleaner part number. That
        # page stopped rendering results server-side, so the scrape matched
        # nothing and always fell through. The part-number path now lives in
        # ``_model_from_cleaner_pn`` (table-based, no network, all families)
        # and runs AFTER the features API, which is authoritative when it
        # answers — the cloud derives ``model`` from the same part number.

        # Try the features API first.
        url = f"{URL_GET_DEVICE_FEATURES}{self._serial}/features"
        try:
            data = await asyncio.wait_for(self.get_device_features(url), timeout=30)

            # Handle empty or malformed responses
            if not data or not isinstance(data, dict):
                _LOGGER.debug("Empty or invalid features response for device %s", self._serial)
                return self._handle_model_fetch_failure()

            # Capture the capability list regardless of whether a model comes
            # back — the two are independent, and a device with no `model` still
            # advertises its features (verified on a live vr robot that returned
            # 19 features and no model at all).
            advertised = data.get('features')
            if isinstance(advertised, list):
                self._features = [str(f) for f in advertised]
                _LOGGER.debug(
                    "Device advertises %d features: %s",
                    len(self._features), self._features,
                )

            # Try to get model, if not found or None, return Unknown
            features_model = data.get('model')
            if features_model is not None and features_model != "":
                self._model = str(features_model)  # Cache the successful result
                self._model_fetch_attempts = 0  # Reset attempts on success
                _LOGGER.debug("Successfully cached model from features API: %s", self._model)
                return self._model

            # Only mark as "Not Supported" if we get an explicit indication
            if data.get('modelSupported') is False:
                self._model = "Not Supported"
                self._model_fetch_attempts = 0  # Don't retry for "Not Supported"
                _LOGGER.debug("Model not supported for device %s", self._serial)
                return self._model

            # The features call gave no model. Fall back to the part number.
            resolved = await self._model_from_cleaner_pn()
            if resolved is not None:
                self._model = resolved
                self._model_fetch_attempts = 0
                return self._model

            return self._handle_model_fetch_failure()

        except Exception as e:
            _LOGGER.debug("Error getting device model: %s", e)
            return self._handle_model_fetch_failure()

    def _apply_last_cloud_contact(
        self, data: dict[str, Any], result: dict[str, Any]
    ) -> None:
        """Set ``last_cloud_contact`` from ``aws.timestamp``; cache eboxData.

        The cloud stamps every shadow with the moment the *robot* last talked
        to it, alongside the ``aws.status`` string this integration already
        surfaces. Status alone can't distinguish "connected and reporting" from
        "connected but silent for two hours", which is the ambiguity behind
        every "is my robot actually updating?" report — the reporter on issue
        #100 had a Connected status and stale state simultaneously.

        The value is epoch **milliseconds** (verified: 1786201126595 — 13
        digits), unlike ``cycleStartTime`` which is seconds. Emitted as an
        aware-UTC datetime so HA renders it as a relative timestamp, matching
        the H8 convention used for the other datetime fields.
        """
        try:
            reported = data['payload']['robot']['state']['reported']
        except (KeyError, TypeError):
            return

        # Cache equipment identity for diagnostics (see ``_ebox_data``).
        ebox = reported.get('eboxData')
        if isinstance(ebox, dict) and ebox:
            self._ebox_data = dict(ebox)

        # Cache the RAW robot block for diagnostics.
        #
        # Until now nothing captured this: diagnostics dumped the *parsed*
        # result, so a report could show `activity: idle` without revealing
        # the cloud value the parser derived it from. That is precisely the
        # dead end on issue #100 — a robot reporting Idle while physically
        # cleaning, with no way to see what the cloud actually sent. The
        # alternative was asking reporters to run a script with their
        # password in it, which is not a reasonable thing to request.
        #
        # Only the robot block is kept, not the whole envelope: the envelope
        # carries `clientToken`, which embeds the auth token. Note that
        # `async_redact_data` matches dict KEYS, so it would not scrub a
        # credential nested inside a stringified blob — hence keeping a real
        # dict and a narrow slice of it rather than `str(data)`.
        equipment = reported.get('equipment')
        if isinstance(equipment, dict):
            block = equipment.get('robot')
            if isinstance(block, list):
                # cyclonext over REST: robot is [null, {...}] — index 1.
                block = next(
                    (item for item in block if isinstance(item, dict)), None
                )
            if not isinstance(block, dict):
                block = equipment.get('robot.1')
            if isinstance(block, dict) and block:
                self._last_robot_block = dict(block)

        try:
            stamp = reported['aws']['timestamp']
        except (KeyError, TypeError):
            return
        if not isinstance(stamp, (int, float)) or isinstance(stamp, bool):
            return
        if stamp <= 0:
            return
        try:
            result["last_cloud_contact"] = dt_util.utc_from_timestamp(
                stamp / 1000
            )
        except (OverflowError, OSError, ValueError) as err:
            _LOGGER.debug("Unusable aws.timestamp %r: %s", stamp, err)

    async def _model_from_cleaner_pn(self) -> str | None:
        """Resolve the model from ``eboxData.completeCleanerPn``.

        Returns a model name, or ``"Unknown (<pn>)"`` when the part number is
        present but not in ``MODEL_BY_CLEANER_PN``, or ``None`` when there is
        no usable part number at all (caller then falls through to its normal
        failure handling).

        Replaces ``_get_vortrax_model_from_web``, which scraped
        ``zodiac-poolcare.com`` for vortrax only. That page is now a
        client-rendered shell with no product names in the HTML, so the scrape
        returned "Unknown" for every input — verified against the three known
        part numbers. This path needs no network call beyond the shadow the
        client already fetches, and it works for every family rather than one.

        Surfacing the raw part number on a miss is deliberate: it tells the
        user something concrete, and it is exactly the datum needed to extend
        the table (see ``tools/capture_payload.py``).
        """
        try:
            data = await self._ws_subscribe()
            pn = (
                ((data or {}).get('payload') or {})
                .get('robot', {})
                .get('state', {})
                .get('reported', {})
                .get('eboxData', {})
                .get('completeCleanerPn')
            )
        except Exception as err:
            _LOGGER.debug("Could not read completeCleanerPn: %s", err)
            return None

        if not pn or not isinstance(pn, str):
            # An empty string is the *expected* value for a device whose cloud
            # record was never fully provisioned — which is also exactly why
            # its features call carried no model. Nothing to resolve.
            _LOGGER.debug(
                "No completeCleanerPn for device (got %r); model unresolvable",
                pn,
            )
            return None

        pn = pn.strip()
        known = MODEL_BY_CLEANER_PN.get(pn)
        if known:
            _LOGGER.debug("Resolved model %s from part number %s", known, pn)
            return known

        _LOGGER.info(
            "Unrecognised cleaner part number %s — please report it so the "
            "model can be mapped (see MODEL_BY_CLEANER_PN in const.py)", pn,
        )
        return f"Unknown ({pn})"

    def _should_retry_model_fetch(self) -> bool:
        """Determine if we should retry fetching the model."""
        # Always try if we haven't attempted yet
        if self._model_fetch_attempts == 0:
            return True

        # If we have a valid model (not Unknown/Not Supported), don't retry
        if self._model and self._model not in ["Unknown", "Not Supported"]:
            return False

        # If we've tried too many times, give up
        max_attempts = 5
        if self._model_fetch_attempts >= max_attempts:
            _LOGGER.debug("Max model fetch attempts (%s) reached for device %s", max_attempts, self._serial)
            return False

        # If we haven't tried recently, allow retry
        if not self._model_last_attempt:
            return True

        # Implement exponential backoff: wait longer between retries
        # Attempt 1: immediate, Attempt 2: 5 min, Attempt 3: 15 min, Attempt 4: 30 min, Attempt 5: 60 min
        backoff_minutes = [0, 5, 15, 30, 60]
        wait_minutes = backoff_minutes[min(self._model_fetch_attempts - 1, len(backoff_minutes) - 1)]

        time_since_last = dt_util.utcnow() - self._model_last_attempt
        if time_since_last.total_seconds() >= (wait_minutes * 60):
            _LOGGER.debug("Retrying model fetch after %s minute wait", wait_minutes)
            return True

        return False

    def _handle_model_fetch_failure(self) -> str:
        """Handle a failed model fetch attempt."""
        # Only cache "Unknown" if we've exhausted all attempts
        if self._model_fetch_attempts >= 5:
            self._model = "Unknown"
            _LOGGER.debug("Caching 'Unknown' model after %s failed attempts", self._model_fetch_attempts)
        else:
            _LOGGER.debug("Model fetch failed (attempt %s/5), will retry later", self._model_fetch_attempts)

        return self._model or "Unknown"

    async def _update_i2d_robot(self) -> dict[str, Any]:
        """Update status for i2d_robot type — HTTP fetch + protocol parse.

        R25-i2d (R27 fold): the 138-line god-function body now lives as
        ``I2DProtocol.parse_status`` + the pure ``parse_i2d_payload``
        helper. This method handles the HTTP transport (which can't move
        to the protocol because the URL helper depends on the client's
        serial); the protocol owns the request shape, the response
        decode, and the per-family side-effects.
        """
        i2d = _i2d_protocol()
        status_request = {
            "command": "/command",
            "params": "request=OA11",
            "user_id": self._id,
        }
        data = await asyncio.wait_for(
            self.post_command_i2d(i2d.i2d_url(self), status_request),
            timeout=30,
        )

        result: dict[str, Any] = {}
        i2d.parse_status(self, data, result)
        return result

    def _degraded_result(self, error_state: str) -> dict[str, Any]:
        """Minimal but coherent payload for a shadow read that produced nothing.

        Callers return this instead of raising so the entity keeps rendering
        a sane state (and the coordinator's outage ladder decides when to go
        unavailable). ``_get_resilient_status`` is deliberately NOT wrapped in
        ``_stabilize_status`` here — matching the pre-refactor behaviour of
        all four early-return paths this replaces.
        """
        return {
            "serial_number": self._serial,
            "device_type": self._device_type,
            "status": self._get_resilient_status(error_state),
            "error_state": error_state,
        }

    def _cache_ws_envelope(self, data: Any) -> None:
        """C4-ws: snapshot the full envelope as the WS listener's merge baseline.

        Deep-copied via a json round-trip to insulate the cache from later
        in-place mutation of ``data`` (the per-device-type parsers read it but
        shouldn't mutate; this is belt-and-braces). The ``isinstance`` guard
        covers a defensive caller returning a non-dict on edge paths.
        """
        if not (isinstance(data, dict) and "payload" in data):
            return
        try:
            self._last_ws_envelope = json.loads(json.dumps(data))
        except (TypeError, ValueError) as cache_err:  # non-serialisable contents
            _LOGGER.debug(
                "Could not snapshot _last_ws_envelope (non-JSON-serialisable contents): %s",
                cache_err,
            )

    def _read_reported_status(
        self, data: Any, result: dict[str, Any], fallback_state: str
    ) -> None:
        """Read ``aws.status`` out of a shadow envelope into ``result``.

        Raises on an unexpected envelope shape — the caller retries the
        subscribe once before giving up, which is the historical behaviour
        (the cloud intermittently answers a subscribe with an empty message).
        """
        if data and 'payload' in data:
            raw_status = data['payload']['robot']['state']['reported']['aws']['status']
            result["status"] = self._stabilize_status(raw_status)
            # Store the last known good status
            self._last_known_status = raw_status
            self._apply_last_cloud_contact(data, result)
        else:
            result["status"] = self._stabilize_status(
                self._get_resilient_status(fallback_state)
            )
            _LOGGER.debug("No data received from websocket: %s", data)

    async def _update_other_robots(self) -> dict[str, Any]:
        """Update status for non-i2d robot types (the shadow-stream families)."""
        try:
            data = await self._ws_subscribe()
            if data is None:
                _LOGGER.debug("No data received from websocket subscribe")
                # Failure counting for genuine websocket problems happens in
                # get_device_status; a legitimately offline device must not
                # inflate the counter here.
                return self._degraded_result("no_data")
            # Reset failure count on successful websocket operation
            self._ws_consecutive_failures = 0
            self._cache_ws_envelope(data)
        except asyncio.CancelledError:
            # If cancelled during setup, return minimal data to allow integration to continue
            _LOGGER.debug("Robot update cancelled - returning minimal data")
            self._ws_consecutive_failures += 1
            return self._degraded_result("setup_cancelled")
        except Exception as e:
            # If websocket fails, increment failure count
            self._ws_consecutive_failures += 1
            _LOGGER.debug("Robot update failed (attempt %s): %s", self._ws_consecutive_failures, e)
            return self._degraded_result("update_failed")

        result: dict[str, Any] = {
            "serial_number": self._serial,
            "device_type": self._device_type,
        }

        try:
            self._read_reported_status(data, result, "no_data")
        except Exception:
            # The cloud returns an empty message sometimes — try a second call.
            #
            # The former `result["debug"]` field was dropped here: it was
            # gated on the hardcoded-False debug_mode (so always ""), was
            # never read by any entity, and duplicated what
            # `tools/capture_payload.py` does properly.
            _LOGGER.debug("Websocket status read failed; retrying. Payload: %s", data)

            try:
                data = await self._ws_subscribe()
                self._read_reported_status(data, result, "no_data_retry")
            except asyncio.CancelledError:
                # If second call is also cancelled, return what we have
                self._ws_consecutive_failures += 1
                raw_status = self._get_resilient_status("setup_cancelled")
                result["status"] = self._stabilize_status(raw_status)
                result["error_state"] = "setup_cancelled"
                return result
            except Exception:
                self._ws_consecutive_failures += 1
                raw_status = self._get_resilient_status("update_failed")
                result["status"] = self._stabilize_status(raw_status)
                result["error_state"] = "update_failed"
                return result

        # Convert timestamp to datetime (only if we have valid data).
        # H8: aware-UTC datetime object stored directly (no .isoformat()) so
        # the sensor entity declaring device_class=TIMESTAMP renders correctly.
        if data and 'payload' in data:
            timestamp = data['payload']['robot']['state']['reported']['aws']['timestamp'] / 1000
            result["last_online"] = dt_util.utc_from_timestamp(timestamp)

            # Only update device-specific data if we have valid payload data
            self.apply_robot_data(data, result)
        else:
            # H8 review follow-up: emit None instead of "unknown" string.
            # `last_online` sensor declares device_class=TIMESTAMP and HA's
            # frontend expects a datetime or None; a string state breaks the
            # entity (shows "unavailable" or logs a SensorStateClass error).
            result["last_online"] = None
            # Don't call device-specific update methods when we don't have valid data
            # Set default values for missing data
            result["activity"] = "unknown"
            result["error_state"] = result.get("error_state", "no_data")

        return result

    # ------------------------------------------------------------------
    # Public surface for the coordinator.
    #
    # The coordinator used to reach through to five private client members
    # (`_update_<family>_robot_data`, `_apply_pending_stop_reset`,
    # `_pending_stop_reset`, `_reset_websocket_failures`, `_close_websocket`).
    # `tests/test_static.py` already forbids that pattern for `_serial` /
    # `_device_type` / `_title`; these are the same class of leak. The
    # implementations stay private — these are the supported names.
    # ------------------------------------------------------------------

    #: Families whose state arrives over the AWS IoT shadow stream. i2d speaks
    #: the REST hex protocol only, so a realtime envelope can never be for it
    #: and the push path must not try to parse one.
    _SHADOW_STREAM_DEVICE_TYPES = frozenset(
        {"vr", "cyclonext_vr", "vortrax", "cyclobat", "cyclonext"}
    )

    @property
    def supports_realtime_shadow(self) -> bool:
        """Whether this robot's state arrives over the websocket shadow stream."""
        return self.device_type in self._SHADOW_STREAM_DEVICE_TYPES

    @property
    def has_pending_stop_reset(self) -> bool:
        """Whether a manual-stop reset is queued but not yet applied (H6)."""
        return self._pending_stop_reset is not None

    def apply_pending_stop_reset(self, data: dict[str, Any]) -> dict[str, Any]:
        """Apply and clear a queued manual-stop reset. See H6."""
        return self._apply_pending_stop_reset(data)

    def reset_websocket_failures(self) -> None:
        """Clear the consecutive-websocket-failure counter after a good update."""
        self._reset_websocket_failures()

    async def close_websocket(self) -> None:
        """Close the persistent websocket connection and its session."""
        await self._close_websocket()

    async def run_websocket_listener(self) -> None:
        """Run the realtime websocket listener until cancelled or closed."""
        await self._websocket_listener()

    def apply_robot_data(self, data: dict[str, Any], result: dict[str, Any]) -> None:
        """Parse a cloud payload for THIS robot's family into ``result``.

        The single dispatch point for both the polled path
        (``_update_other_robots``) and the push path
        (``coordinator._apply_realtime_envelope``). It replaces four
        near-identical ``_update_<family>_robot_data`` seams and the two
        copies of the if/elif chain that selected between them — one of which
        lived in the coordinator and reached through to the client's private
        methods to do it.

        Unknown ``device_type`` values are a no-op rather than an error: the
        polled path already fills in a coherent offline-shaped ``result``,
        and a cloud that starts reporting a family we don't parse yet should
        degrade to "no device-specific fields", not crash the update.
        """
        # Dispatches on the public `device_type` property, not the private
        # attribute: this is the client's public surface, and the coordinator
        # (whose if/elif chain this replaced) also keyed off the public name.
        protocol = get_protocol(self.device_type)
        if protocol is None:
            _LOGGER.debug(
                "No protocol registered for device_type=%r; skipping "
                "device-specific parsing",
                self.device_type,
            )
            return
        protocol.parse_status(self, data, result)

    async def send_login(
        self, data: str | bytes, headers: dict[str, str]
    ) -> dict[str, Any]:
        """Post a login request to the iaqualink_robots API.

        Surfaces Cognito-side credential rejection as ``AuthFailedError`` so
        the coordinator boundary translates it to ``ConfigEntryAuthFailed``
        and HA's reauth flow fires. Three rejection shapes are handled:
        explicit 401, explicit 403, and a 2xx response whose body is not the
        expected JSON envelope (Cognito occasionally returns a 200 with an
        error body or a non-JSON content type). Without this, a 200-shaped
        rejection slipped through and only blew up on the ``auth["...":]``
        dict accesses in ``_authenticate`` as ``KeyError`` — which the
        coordinator broad-except handled as transient noise and the user
        never saw the reauth prompt.
        """
        try:
            # L19: shared HA pool via _cloud_session (falls back to ephemeral
            # session when self._hass is None — config-flow discovery + tests).
            # Headers go on the request, not the session, to avoid leaking
            # auth into the shared pool.
            async with self._cloud_session() as session:
                async with session.post(URL_LOGIN, data=data, headers=headers) as response:
                    if response.status == 401:
                        _LOGGER.debug("Authentication failed: 401 Unauthorized.")
                        raise AuthFailedError("401 Unauthorized on send_login")

                    if response.status == 403:
                        _LOGGER.error("Authentication failed: 403 Forbidden. Check your credentials or API key.")
                        raise AuthFailedError("403 Forbidden on send_login")

                    content_type = response.headers.get('Content-Type', '')
                    if 'application/json' not in content_type:
                        text = await response.text()
                        _LOGGER.error("Unexpected content type: %s. Response: %s...", content_type, text[:200])
                        # Cognito returns non-JSON only when the request is
                        # rejected (e.g. an HTML error page on auth failure).
                        raise AuthFailedError(
                            f"send_login returned non-JSON content type: {content_type}"
                        )

                    body: dict[str, Any] = await response.json()
                    return body
        except asyncio.CancelledError:
            _LOGGER.debug("Login request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior
        raise RuntimeError("send_login exited without returning")

    async def get_devices(
        self, params: dict[str, Any], headers: dict[str, str]
    ) -> list[dict[str, Any]]:
        """Get device list from the iaqualink_robots API.

        Implements the spec's Risks-clause mitigation: on HTTP 401, refresh
        the token once and retry; only the second 401 raises ``AuthFailedError``
        so a transient cloud 401 doesn't trip HA reauth unnecessarily.
        """
        for attempt in range(2):
            try:
                # L19: shared HA pool via _cloud_session.
                async with self._cloud_session() as session:
                    async with session.get(URL_GET_DEVICES, params=params, headers=headers) as response:
                        if response.status == 401:
                            if attempt == 0:
                                _LOGGER.debug("401 on get_devices — refreshing token and retrying once")
                                await self._authenticate(force=True)
                                # `params` carries `authentication_token` from the caller; refresh it
                                # in-place so the retry uses the new token.
                                if "authentication_token" in params:
                                    params["authentication_token"] = self._auth_token
                                continue
                            raise AuthFailedError(
                                f"401 Unauthorized on {URL_GET_DEVICES} after re-auth retry"
                            )
                        body: list[dict[str, Any]] = await response.json()
                        return body
            except asyncio.CancelledError:
                _LOGGER.debug("Get devices request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior
        raise RuntimeError("get_devices exited without returning")

    async def get_device_features(self, url: str) -> dict[str, Any]:
        """Get device features from the iaqualink_robots API.

        Implements the spec's one-retry mitigation (see ``get_devices``).
        """
        for attempt in range(2):
            try:
                # L19: shared HA pool. Auth header constructed per-request so
                # the retry path picks up the rotated `self._id_token` cleanly
                # (it was already happening before because the loop re-entered
                # the `with ClientSession(...)` block — this preserves that).
                async with self._cloud_session() as session:
                    async with session.get(url, headers={"Authorization": self._id_token}) as response:
                        if response.status == 401:
                            if attempt == 0:
                                _LOGGER.debug("401 on get_device_features — refreshing token and retrying once")
                                await self._authenticate(force=True)
                                continue  # next iteration re-reads self._id_token into a fresh request header
                            raise AuthFailedError(
                                f"401 Unauthorized on {url} after re-auth retry"
                            )
                        body: dict[str, Any] = await response.json()
                        return body
            except asyncio.CancelledError:
                _LOGGER.debug("Get device features request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior
        raise RuntimeError("get_device_features exited without returning")

    async def get_device_status(
        self, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Get device status from the iaqualink_robots API via websocket.
        Uses persistent websocket connection for improved performance and reliability."""
        max_retries = 2

        for attempt in range(max_retries):
            try:
                # Use persistent websocket connection
                await self._ensure_websocket_connection()

                if not self._ws_connection or self._ws_connection.closed:
                    raise Exception("Websocket connection not available")

                await self._ws_connection.send_json(request)
                message = await asyncio.wait_for(self._ws_connection.receive(), timeout=10)

                if message.type == aiohttp.WSMsgType.TEXT:
                    # Reset consecutive failures on successful status update
                    self._ws_consecutive_failures = 0
                    body: dict[str, Any] = message.json()
                    return body
                elif message.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.debug("Websocket connection closed during device status request")
                    await self._close_websocket()  # Clean up closed connection
                    if attempt < max_retries - 1:
                        continue  # Try again
                    return None
                elif message.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug("Websocket error during device status request: %s", message.data)
                    await self._close_websocket()  # Clean up error connection
                    if attempt < max_retries - 1:
                        continue  # Try again
                    return None
                else:
                    _LOGGER.debug(
                        "Unexpected websocket message type %s (%s) in device status request",
                        message.type,
                        message.type.name if hasattr(message.type, 'name') else 'unknown')
                    return None

            except asyncio.CancelledError:
                _LOGGER.debug("Get device status request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior
            except AuthFailedError:
                # H9b review P1: auth failures must propagate to the coordinator
                # so HA reauth fires — never swallow them into the resilient
                # "return None" path used for transient WS noise.
                # Sign-off P7: close is best-effort during auth-failure
                # propagation — a teardown error here must NOT mask the
                # AuthFailedError, or the broad-except below absorbs it and
                # reauth never fires.
                try:
                    await self._close_websocket()
                except Exception:  # noqa: BLE001 — close is best-effort
                    pass
                raise
            except Exception as e:
                if attempt < max_retries - 1:
                    _LOGGER.debug("Device status attempt %s failed: %s. Retrying...", attempt + 1, e)
                    await self._close_websocket()
                    await asyncio.sleep(0.5)  # Brief pause before retry
                else:
                    # Only increment failure counter after all retries exhausted
                    self._ws_consecutive_failures += 1
                    _LOGGER.debug("Device status websocket connection failed after retries: %s", e)
                    await self._close_websocket()
                    # Return None instead of raising to let callers handle gracefully
                    return None
        return None

    async def post_command_i2d(
        self, url: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Send command to i2d robot via the iaqualink_robots API.

        Implements the spec's one-retry mitigation (see ``get_devices``).
        """
        for attempt in range(2):
            try:
                # L19: shared HA pool. Auth header constructed per-request so
                # the retry picks up the rotated `self._id_token` cleanly.
                async with self._cloud_session() as session:
                    async with session.post(
                        url,
                        json=request,
                        headers={"Authorization": self._id_token, "api_key": self._api_key},
                    ) as response:
                        if response.status == 401:
                            if attempt == 0:
                                _LOGGER.debug("401 on post_command_i2d — refreshing token and retrying once")
                                await self._authenticate(force=True)
                                continue  # next iteration re-reads self._id_token
                            raise AuthFailedError(
                                f"401 Unauthorized on {url} after re-auth retry"
                            )
                        body: dict[str, Any] = await response.json()
                        return body
            except asyncio.CancelledError:
                _LOGGER.debug("Post command i2d request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior
        raise RuntimeError("post_command_i2d exited without returning")

    async def set_cleaner_state(
        self, request: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Set cleaner state via the iaqualink_robots API websocket.
        Implements resilient error handling to avoid device status disruption."""
        response_data: dict[str, Any] | None = None
        max_retries = 2

        for attempt in range(max_retries):
            try:
                # Use persistent websocket connection for improved reliability and performance
                await self._ensure_websocket_connection()

                if not self._ws_connection or self._ws_connection.closed:
                    raise Exception("Websocket connection not available")

                await self._ws_connection.send_json(request)

                # Try to get response to confirm command was received
                try:
                    response = await asyncio.wait_for(self._ws_connection.receive(), timeout=10)
                    if response.type == aiohttp.WSMsgType.TEXT:
                        response_data = response.json()
                        _LOGGER.debug("Command response received: %s", response_data)
                        return response_data  # Success - return immediately
                    elif response.type == aiohttp.WSMsgType.CLOSED:
                        _LOGGER.debug("Websocket closed during command, will retry")
                        await self._close_websocket()
                        continue  # Retry
                except asyncio.TimeoutError:
                    _LOGGER.debug("No response received within timeout, command may still succeed")
                    return response_data  # Command sent successfully, just no response
                except Exception as e:
                    _LOGGER.debug("Error receiving response: %s", e)
                    # Don't fail the command just because we can't get a response
                    return response_data

            except AuthFailedError:
                # H9b review P1: command-time WS 401 must propagate so HA
                # reauth fires; the broad-except below is for transient WS
                # noise only.
                # Sign-off P7: best-effort close — see get_device_status.
                try:
                    await self._close_websocket()
                except Exception:  # noqa: BLE001 — close is best-effort
                    pass
                raise
            except Exception as e:
                if attempt < max_retries - 1:
                    _LOGGER.debug("Websocket command attempt %s failed: %s. Retrying...", attempt + 1, e)
                    await self._close_websocket()
                    await asyncio.sleep(1)  # Brief pause before retry
                else:
                    # Only log as warning, not error, to avoid device unavailability
                    _LOGGER.warning("Websocket command failed after %s attempts: %s", max_retries, e)
                    await self._close_websocket()

        return response_data

    async def _safe_post_command_refresh(self) -> None:
        """Trigger the coordinator refresh callback after a cloud command.

        Each of the 6 command methods (``start_cleaning`` /
        ``stop_cleaning`` / ``pause_cleaning`` / ``return_to_base`` /
        ``add_fifteen_minutes`` / ``reduce_fifteen_minutes``) calls this
        helper after sending its cloud request to trigger a single
        coordinator refresh.

        C4-cmd review F1 (2026-05-18): the original per-site
        ``except Exception`` blocks swallowed ``ConfigEntryAuthFailed``
        raised by ``_handle_realtime_update`` — the H10 review's
        precedent explicitly says auth failures must propagate so HA's
        reauth flow fires. This helper preserves the broad-except for
        transient noise (network blips, parser errors during refresh)
        while letting ``ConfigEntryAuthFailed`` and ``CancelledError``
        through. Single helper instead of 6 inlined try/except blocks
        also closes M2 (drift hazard if future patches forget one site).
        """
        callback = self._coordinator_callback
        if callback is None:
            return
        try:
            await callback()
        except asyncio.CancelledError:
            raise
        except ConfigEntryAuthFailed:
            # H10 precedent: auth failures must surface so HA dispatches
            # reauth — never swallow them into the transient-noise branch.
            raise
        except Exception as e:  # noqa: BLE001 — transient refresh noise
            _LOGGER.debug("Error triggering coordinator callback: %s", e)

    def _build_state_request(
        self,
        action: str,
        desired: dict[str, Any],
        *,
        namespace: str | None = None,
        robot_key: str = "robot",
    ) -> dict[str, Any]:
        """Build a StateController websocket request envelope (story R23).

        Collapses 21 near-identical envelope literals across the 11 command
        methods (start / stop / pause / return_to_base / clear_desired_state
        / set_fan_speed / enter+exit remote control / remote steering /
        stepper add+reduce) into one canonical shape.

        AC#4 ("zero behavior change") is interpreted as **dict-content
        equal** — recursive value-equality, ignoring JSON key order.
        Confirmed with the user before implementation: the iAqualink
        cloud is AWS IoT-shaped and parses JSON into dicts, so transmit
        key order is unobservable. Every field present in the pre-R23
        envelopes is still present with the same value; nothing was
        dropped. Per-device-type drift is preserved by the caller passing
        the appropriate ``desired`` / ``namespace`` / ``robot_key`` —
        cyclonext uses ``robot.1`` instead of ``robot`` and cyclobat
        nests under ``main.ctrl`` / ``main.mode``.

        Args:
            action: Cloud action verb — ``"setCleanerState"``,
                ``"setCleaningMode"``, or ``"setRemoteSteeringControl"``.
            desired: The dict to nest under
                ``payload.state.desired.equipment.<robot_key>``. Embedded
                verbatim — the helper does not transform it.
            namespace: Override the top-level ``namespace`` field.
                Defaults to ``self._device_type``.
            robot_key: Equipment dict key. ``"robot"`` (default) for
                vr / vortrax / cyclobat; ``"robot.1"`` for cyclonext.
        """
        ns = namespace if namespace is not None else self._device_type
        client_token = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        return {
            "action": action,
            "version": 1,
            "namespace": ns,
            "payload": {
                "state": {"desired": {"equipment": {robot_key: desired}}},
                "clientToken": client_token,
            },
            "service": "StateController",
            "target": self._serial,
        }

    async def start_cleaning(self) -> dict[str, Any] | None:
        """Start the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            # R25-i2d: i2d uses HTTP POST not WS. The protocol owns the
            # HTTP body shape; the URL helper lives on I2DProtocol.
            # Record the start time when starting cleaning for i2d robots.
            # H8: aware-UTC datetime stored directly (no .isoformat()).
            now = dt_util.utcnow()
            result = {
                "cycle_start_time": now,
                "activity": "cleaning"
            }
            i2d = _i2d_protocol()
            await asyncio.wait_for(
                self.post_command_i2d(i2d.i2d_url(self), i2d.start_payload(self)),
                timeout=800,
            )
            return result  # Return the time values to be used in the coordinator's update
        else:
            # R23: envelope literals collapsed to `_build_state_request` calls.
            # R25 series (all 5 families): every branch dispatches through
            # ``get_protocol(<family>)``; the if/elif here is the remaining
            # device-type dispatch that R26 (module split) will collapse to
            # a single ``protocol.start_payload(self)`` lookup.
            request = None
            if self._device_type == "vr":
                request = _require_protocol("vr").start_payload(self)
            elif self._device_type == "vortrax":
                request = _require_protocol("vortrax").start_payload(self)
            elif self._device_type == "cyclobat":
                request = _require_protocol("cyclobat").start_payload(self)
            elif self._device_type == "cyclonext":
                request = _require_protocol("cyclonext").start_payload(self)

            if request:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

                # Mark activity for adaptive polling
                if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                    # Get coordinator reference and mark activity
                    try:
                        # Access coordinator through the client's stored reference
                        if hasattr(self, '_coordinator_ref') and self._coordinator_ref:
                            self._coordinator_ref._mark_recent_activity()
                    except Exception:
                        pass

                # C4-cmd: one awaited refresh per command — replaces the
                # historical 3-task burst (immediate + 0.5s + 1.0s delayed).
                # The coordinator's debounced `async_request_refresh` (called
                # inside `_handle_realtime_update`) collapses near-duplicate
                # refreshes from button.py / vacuum.py into a single
                # `_async_update_data` call.
                # C4-cmd review F1: helper re-raises ConfigEntryAuthFailed +
                # CancelledError; old inlined except-Exception swallowed reauth.
                await self._safe_post_command_refresh()
        return None

    async def clear_desired_state(self) -> None:
        """Clear the desired state to prevent auto-restart after natural cycle completion.

        When the robot finishes cleaning naturally, the cloud may still have
        desired state = 1 (cleaning). This can cause the cloud to restart the robot.
        This method sets desired state = 0 to prevent that.
        """
        # Only implemented for vr - tested on Polaris VRX IQ+
        # Other device types (vortrax, cyclobat, cyclonext) may need similar
        # treatment if they exhibit the same auto-restart bug
        if self._device_type != "vr":
            return

        # R25-vr: VRProtocol.clear_desired_payload owns the per-family envelope.
        request = _require_protocol("vr").clear_desired_payload(self)
        if request is None:
            return

        try:
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            _LOGGER.debug("Cleared desired state to prevent auto-restart")
        except Exception as e:
            _LOGGER.warning("Failed to clear desired state: %s", e)

    async def stop_cleaning(self) -> dict[str, Any]:
        """Stop the vacuum cleaning."""
        request = None
        if self._device_type == "i2d_robot":
            # R25-i2d: stop_payload + i2d_url helper on the protocol.
            i2d = _i2d_protocol()
            await asyncio.wait_for(
                self.post_command_i2d(i2d.i2d_url(self), i2d.stop_payload(self)),
                timeout=800,
            )
        else:
            # R23: envelope literals collapsed to `_build_state_request` calls.
            # R25 series (all 5 families): every branch dispatches through
            # ``get_protocol(<family>).stop_payload``.
            if self._device_type == "vr":
                request = _require_protocol("vr").stop_payload(self)
            elif self._device_type == "vortrax":
                request = _require_protocol("vortrax").stop_payload(self)
            elif self._device_type == "cyclobat":
                request = _require_protocol("cyclobat").stop_payload(self)
            elif self._device_type == "cyclonext":
                request = _require_protocol("cyclonext").stop_payload(self)

        # Create result with reset time values always, regardless of robot type.
        # H8 review follow-up: emit None for the two TIMESTAMP-classed sensors
        # so they render "Unknown" on stop, rather than the previous H8 shape
        # which set them to `utcnow()` and produced a misleading "cycle started
        # right now" display when the cycle had just ended.
        reset_values = {
            "estimated_end_time": None,
            "time_remaining": 0,
            "time_remaining_human": format_time_human(0, 0, 0),
            "cycle_start_time": None,
            "activity": "idle"  # Also set activity to idle to ensure proper state
        }

        # Store reset values to be applied in next status fetch.
        # H6: also stamp ``_pending_stop_reset_at`` so ``fetch_status`` can
        # expire stale resets (>``PENDING_STOP_RESET_MAX_AGE_SECONDS`` old)
        # and discard the reset entirely if the cloud has already moved
        # past idle by the time the next poll lands.
        self._pending_stop_reset = reset_values.copy()
        self._pending_stop_reset_at = dt_util.utcnow()

        if request:
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            _LOGGER.debug("Stop cleaning requested, reset time values: %s", reset_values)

            # C4-cmd: one awaited refresh (see start_cleaning for rationale).
            # C4-cmd review F1: helper re-raises ConfigEntryAuthFailed +
            # CancelledError; old inlined except-Exception swallowed reauth.
            await self._safe_post_command_refresh()

        return reset_values

    async def pause_cleaning(self) -> dict[str, Any] | None:
        """Pause the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            # i2d robots might not support pause - use stop instead
            return await self.stop_cleaning()
        else:
            # R23: envelope literals collapsed to `_build_state_request` calls.
            # R25 series (all 4 WS families): every branch dispatches through
            # ``get_protocol(<family>).pause_payload``. (i2d's pause shares
            # the stop body and is handled above by delegating to
            # ``stop_cleaning``.)
            request = None
            if self._device_type == "vr":
                request = _require_protocol("vr").pause_payload(self)
            elif self._device_type == "vortrax":
                request = _require_protocol("vortrax").pause_payload(self)
            elif self._device_type == "cyclobat":
                request = _require_protocol("cyclobat").pause_payload(self)
            elif self._device_type == "cyclonext":
                request = _require_protocol("cyclonext").pause_payload(self)

            if request is not None:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

                # C4-cmd: one awaited refresh (see start_cleaning for rationale).
                # C4-cmd review F1: helper re-raises ConfigEntryAuthFailed +
                # CancelledError; old inlined except-Exception swallowed reauth.
                await self._safe_post_command_refresh()
        return None

    async def return_to_base(self) -> None:
        """Set the vacuum cleaner to return to the dock."""
        if self._device_type == "i2d_robot":
            # R25-i2d: return_to_base_payload + i2d_url helper.
            i2d = _i2d_protocol()
            i2d_request = i2d.return_to_base_payload(self)
            if i2d_request is None:
                return
            await asyncio.wait_for(
                self.post_command_i2d(i2d.i2d_url(self), i2d_request),
                timeout=800,
            )
        elif self._device_type in ("vr", "vortrax", "cyclobat"):
            # R25 series: all 3 families route through their protocol. The
            # cyclobat anomaly (flat ``setCleanerState`` + ``state:3``
            # instead of the ``setCleaningMode`` + ``main.ctrl`` shape its
            # other commands use) lives inside ``CycloBatProtocol`` now.
            if self._device_type == "vr":
                request = _require_protocol("vr").return_to_base_payload(self)
            elif self._device_type == "vortrax":
                request = _require_protocol("vortrax").return_to_base_payload(self)
            else:  # cyclobat
                request = _require_protocol("cyclobat").return_to_base_payload(self)
            if request is None:
                return
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

            # C4-cmd: one awaited refresh (see start_cleaning for rationale).
            # C4-cmd review F1: helper re-raises ConfigEntryAuthFailed +
            # CancelledError; old inlined except-Exception swallowed reauth.
            await self._safe_post_command_refresh()

    def _extract_fan_speed_from_response(
        self,
        response_data: dict[str, Any] | None,
        requested_fan_speed: str,
    ) -> str | None:
        """Extract current fan speed from a WS response, via the device's
        protocol.

        R25 series: all 4 WS families (vr / vortrax / cyclobat / cyclonext)
        own their decode in ``<Family>Protocol.extract_fan_speed_from_response``.
        i2d's HTTP responses don't carry a confirmed-mode field; its
        protocol returns ``None`` for this method.
        """
        if not response_data:
            return None

        try:
            protocol = get_protocol(self._device_type)
            if protocol is None:
                return None
            return protocol.extract_fan_speed_from_response(
                response_data, requested_fan_speed
            )
        except Exception as e:
            _LOGGER.debug("Error extracting fan speed from response: %s", e)
            return None

    async def set_fan_speed(
        self, fan_speed: str, fan_speed_list: list[str]
    ) -> dict[str, Any] | None:
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in fan_speed_list:
            raise ValueError('Invalid fan speed')

        if self._device_type == "i2d_robot":
            return await self._set_i2d_fan_speed(fan_speed)
        else:
            return await self._set_other_fan_speed(fan_speed)

    async def _set_i2d_fan_speed(self, fan_speed: str) -> dict[str, Any] | None:
        """Set fan speed for i2d robot — delegates to I2DProtocol (R25-i2d)."""
        i2d = _i2d_protocol()
        request = i2d.set_fan_speed_payload(self, fan_speed)
        if request is None:
            return None

        response = await asyncio.wait_for(
            self.post_command_i2d(i2d.i2d_url(self), request), timeout=800
        )

        # For i2d robots, extract success/failure from response
        if response and response.get("command", {}).get("response"):
            _LOGGER.debug("i2d fan speed command response: %s", response)
            return {"success": True, "fan_speed": fan_speed, "response": response}

        return {"success": False, "fan_speed": fan_speed}

    async def _set_other_fan_speed(self, fan_speed: str) -> dict[str, Any]:
        """Set fan speed for non-i2d robots."""
        # R23: envelope literals collapsed to `_build_state_request` calls.
        # R25 series (all 4 WS families): every branch dispatches through
        # ``get_protocol(<family>).set_fan_speed_payload``. Per-family fields
        # remain distinct inside each protocol: vr/vortrax use ``prCyc`` (int),
        # cyclobat uses ``main.mode`` (str), cyclonext uses ``cycle`` under
        # ``robot.1`` (str).
        request = None
        if self._device_type == "vr":
            request = _require_protocol("vr").set_fan_speed_payload(self, fan_speed)

        elif self._device_type == "vortrax":
            request = _require_protocol("vortrax").set_fan_speed_payload(self, fan_speed)

        elif self._device_type == "cyclobat":
            request = _require_protocol("cyclobat").set_fan_speed_payload(self, fan_speed)

        elif self._device_type == "cyclonext":
            # R25-cyclonext: ``CycloNextProtocol.set_fan_speed_payload`` owns
            # the cyclonext-specific encoding (``cycle`` field, ``robot.1`` key,
            # string values, only ``floor_only`` / ``floor_and_walls`` accepted).
            # The translation-key contract issue #76 originally tripped on
            # (display-name keys vs snake_case translation keys) is locked at
            # the protocol layer + by ``tests/protocols/test_cyclonext.py``.
            request = _require_protocol("cyclonext").set_fan_speed_payload(self, fan_speed)

        if request:
            response_data = await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

            # Extract relevant information from the websocket response
            result = {"success": True, "fan_speed": fan_speed}
            if response_data:
                _LOGGER.debug("Fan speed command response: %s", response_data)
                result["response"] = response_data

                # Try to extract the actual fan speed from the response
                confirmed_fan_speed = self._extract_fan_speed_from_response(response_data, fan_speed)
                if confirmed_fan_speed:
                    result["confirmed_fan_speed"] = confirmed_fan_speed
                    _LOGGER.debug("Websocket confirmed fan speed: %s", confirmed_fan_speed)

                # For some robot types, we might get immediate state confirmation
                payload = response_data.get("payload", {})
                if payload:
                    result["payload"] = payload

            return result

        return {"success": False, "fan_speed": fan_speed, "error": "No valid request generated"}

    async def set_cleaning_pattern(
        self, cycle_key: str, route: int
    ) -> dict[str, Any]:
        """Set the navigation pattern for one cleaning cycle.

        ``route`` is 0 (Standard) or 1 (S-Pattern), nested per-cycle under
        ``cycles.<cycleKey>``, so writing one cycle's pattern leaves the others
        untouched.

        Verified end-to-end on a live cyclobat (firmware V22E3.25.3): the write
        changed the value, the cloud echoed it back over ``StateStreamer``, and
        writing the original value restored it.

        Currently cyclobat-only — no vr or cyclonext payload captured so far
        carries a ``route`` key. Rather than silently no-op for other families
        (the issue #76 failure mode), an unsupported device_type returns an
        explicit error, and the select entity is not created there anyway.
        """
        protocol = get_protocol(self._device_type)
        builder = getattr(protocol, "set_pattern_payload", None)
        if protocol is None or builder is None:
            _LOGGER.warning(
                "Cleaning pattern not supported for device type %s",
                self._device_type,
            )
            return {
                "success": False,
                "error": f"pattern unsupported for {self._device_type}",
            }

        request = builder(self, cycle_key, route)
        response_data = await asyncio.wait_for(
            self.set_cleaner_state(request), timeout=30
        )
        _LOGGER.debug(
            "Cleaning pattern command sent (%s -> route=%s), response: %s",
            cycle_key, route, response_data,
        )
        return {
            "success": True,
            "cycle": cycle_key,
            "route": route,
            "response": response_data,
        }

    async def _enter_remote_control_mode(self) -> None:
        """Enter remote control mode by setting robot to pause state (state: 2)."""
        _LOGGER.debug("Entering remote control mode (state: 2)")
        # R23: envelope literal collapsed. state=2 enables remote control on vr/vortrax.
        request = self._build_state_request("setCleanerState", {"state": 2})
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def _exit_remote_control_mode(self) -> None:
        """Exit remote control mode by setting robot to stopped state (state: 0)."""
        _LOGGER.debug("Exiting remote control mode (state: 0)")
        # R23: envelope literal collapsed. state=0 exits remote control.
        request = self._build_state_request("setCleanerState", {"state": 0})
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def _send_remote_command(
        self, rmt_ctrl_value: int, command_name: str = "remote"
    ) -> None:
        """Send a remote control command with optimized timeout."""
        # R23: envelope literal collapsed. Note the action is
        # `setRemoteSteeringControl` (not `setCleanerState`) — the cloud
        # routes steering commands through a different state-controller verb.
        request = self._build_state_request(
            "setRemoteSteeringControl",
            {"rmt_ctrl": rmt_ctrl_value},
        )
        _LOGGER.debug("Sending %s command (rmt_ctrl: %s)", command_name, rmt_ctrl_value)
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=15)  # Shorter timeout, no artificial delays

    async def remote_forward(self) -> None:
        """Send forward command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning("Remote forward not supported for device type: %s", self._device_type)
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(1, "forward")

    async def remote_backward(self) -> None:
        """Send backward command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning("Remote backward not supported for device type: %s", self._device_type)
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(2, "backward")

    async def remote_rotate_left(self) -> None:
        """Send rotate left command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning("Remote rotate left not supported for device type: %s", self._device_type)
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(4, "rotate left")

    async def remote_rotate_right(self) -> None:
        """Send rotate right command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning("Remote rotate right not supported for device type: %s", self._device_type)
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(3, "rotate right")

    async def remote_stop(self) -> None:
        """Send stop command for VR and VortraX robots and exit remote control mode."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning("Remote stop not supported for device type: %s", self._device_type)
            return

        # Send stop command first
        await self._send_remote_command(0, "stop")

        # Exit remote control mode if currently active
        if self._remote_control_active:
            await self._exit_remote_control_mode()
            self._remote_control_active = False

    async def _adjust_stepper(self, delta_minutes: int) -> dict[str, Any] | None:
        """Adjust the stepper by ``delta_minutes`` (R24).

        Shared body for :meth:`add_fifteen_minutes` (+15) and
        :meth:`reduce_fifteen_minutes` (-15). Replaces two near-identical
        ~95-line methods that pre-R24 differed only in the sign of the
        delta and the per-direction strings — bug fixes had to be applied
        twice and any drift between them was a silent hazard.

        Applies ``max(0, current + delta_minutes)`` to clamp the new
        stepper non-negative. Pre-R24 a reduce when the stepper was
        already at zero sent ``{"stepper": -15}`` to the cloud; cloud
        behavior on a negative stepper is undefined. R24 clamps for the
        safer semantic.

        Routes the cloud roundtrip through R23's ``_build_state_request``
        helper (set + clear pair, matching the WS-traffic pattern the
        cloud expects). The post-command refresh goes through C4-cmd's
        ``_safe_post_command_refresh`` so ``ConfigEntryAuthFailed`` /
        ``CancelledError`` propagate to HA's reauth + shutdown paths.
        """
        if self._device_type not in ("vr", "vortrax"):
            verb = "Add" if delta_minutes > 0 else "Reduce"
            abs_delta = abs(delta_minutes)
            _LOGGER.warning(
                "%s %s minutes not supported for device type: %s",
                verb, abs_delta, self._device_type
            )
            return None

        if not self._should_use_websocket():
            _LOGGER.warning(
                "Websocket operations temporarily suspended due to connection "
                "issues. Please try again later."
            )
            return {
                "success": False,
                "error": "websocket_unavailable",
                "message": "Connection issues detected. Please try again in a moment.",
            }

        try:
            if self._last_timing_command_time is not None:
                time_since_last = time.time() - self._last_timing_command_time
                if time_since_last < 3:
                    remaining_cooldown = 3 - time_since_last
                    _LOGGER.warning(
                        "Rate limiting timing commands. Please wait "
                        "%.1f more seconds.", remaining_cooldown
                    )
                    return {
                        "success": False,
                        "error": "rate_limited",
                        "cooldown_remaining": remaining_cooldown,
                        "message": (
                            f"Please wait {remaining_cooldown:.1f} seconds before "
                            "sending another timing command."
                        ),
                    }

            self._last_timing_command_time = time.time()

            # fetch_status() returns a flat dict with 'stepper' at the top level.
            current_status = await self.fetch_status()
            current_stepper = current_status.get("stepper", 0)

            # R24: clamp non-negative — see docstring.
            new_stepper = max(0, current_stepper + delta_minutes)

            abs_delta = abs(delta_minutes)
            action_label = f"{'Add' if delta_minutes > 0 else 'Reduce'} {abs_delta} minutes"

            _LOGGER.debug(
                "%s: Setting stepper "
                "%s → %s",
                action_label, current_stepper, new_stepper
            )
            _LOGGER.debug(
                "%s: current stepper=%s, "
                "new stepper=%s",
                action_label, current_stepper, new_stepper
            )

            # R23: set + clear envelope pair.
            request = self._build_state_request(
                "setCleaningMode", {"stepper": new_stepper}
            )
            result = await asyncio.wait_for(self.set_cleaner_state(request), timeout=15)
            _LOGGER.debug("%s response: %s", action_label, result)

            clear_request = self._build_state_request(
                "setCleaningMode", {"stepper": None}
            )
            await asyncio.wait_for(self.set_cleaner_state(clear_request), timeout=15)

            _LOGGER.debug("%s command completed successfully", action_label)

            # C4-cmd: one awaited refresh; helper re-raises ConfigEntryAuthFailed.
            await self._safe_post_command_refresh()

            past_verb = "Added" if delta_minutes > 0 else "Reduced"
            preposition = "to" if delta_minutes > 0 else "from"
            verb_lower = "add" if delta_minutes > 0 else "reduce"
            return {
                "success": True,
                "action": f"{verb_lower}_{abs_delta}_minutes",
                "previous_stepper": current_stepper,
                "new_stepper": new_stepper,
                "message": (
                    f"{past_verb} {abs_delta} minutes {preposition} cleaning time. "
                    f"Stepper: {current_stepper} → {new_stepper}"
                ),
            }

        except Exception as e:
            verb = "add" if delta_minutes > 0 else "reduce"
            abs_delta = abs(delta_minutes)
            _LOGGER.error(
                "%s %s minutes command failed: %s",
                verb.capitalize(), abs_delta, e
            )
            return {
                "success": False,
                "error": str(e),
                "message": f"Failed to {verb} {abs_delta} minutes: {str(e)}",
            }

    async def add_fifteen_minutes(self) -> dict[str, Any] | None:
        """Add 15 minutes to cleaning time for VR and VortraX robots."""
        return await self._adjust_stepper(15)

    async def reduce_fifteen_minutes(self) -> dict[str, Any] | None:
        """Reduce 15 minutes from cleaning time for VR and VortraX robots."""
        return await self._adjust_stepper(-15)
