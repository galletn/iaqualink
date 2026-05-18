"""Coordinator and client for iaqualink_robots integration."""
import base64
import json
import datetime
import math
import aiohttp
import logging
import asyncio
import re
import time

from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed  # type: ignore # noqa
from homeassistant.exceptions import ConfigEntryAuthFailed  # type: ignore # noqa
# Note: This import requires Home Assistant 2025.1 or later
from homeassistant.components.vacuum import VacuumActivity  # type: ignore # noqa
from homeassistant.util import dt as dt_util  # type: ignore # noqa
from .const import (
    URL_LOGIN,
    URL_GET_DEVICES,
    URL_WS,
    URL_GET_DEVICE_FEATURES,
    PENDING_STOP_RESET_MAX_AGE_SECONDS,
    LONG_OUTAGE_THRESHOLD_SECONDS,
)

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
        if type(exp) not in (int, float) or not math.isfinite(exp) or exp <= 0:
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
        debug_mode: bool = False,
        include_seconds_remaining: bool = True,
    ):
        self._username = username
        self._password = password
        self._api_key = api_key
        # Whether to include seconds in `time_remaining_human`. Setting this
        # to False reduces HA activity-log entries from ~one-per-poll to
        # one-per-minute without changing SCAN_INTERVAL. Plumbed in from the
        # config-flow / options-flow option `include_seconds_remaining`.
        self._include_seconds_remaining = include_seconds_remaining
        self._auth_token: str = ""
        self._id: str = ""
        self._id_token: str = ""
        self._app_client_id: str = ""
        self._serial: str = ""
        self._device_type: str = ""
        self._first_name: str = ""
        self._last_name = None
        self._model = None
        self._token_expires_at = None  # Track token expiration
        self._model_fetch_attempts = 0  # Track model fetch attempts
        self._model_last_attempt = None  # Track last model fetch attempt time
        self._pending_stop_reset = None  # Store reset values from stop command
        # H6: paired with `_pending_stop_reset`. Set to ``dt_util.utcnow()`` at
        # the same site that populates the reset dict; consulted in
        # ``fetch_status`` to expire stale resets (>10 s old) and to discard
        # the reset if the cloud has already moved past idle (the
        # "user restarts between stop and poll" race). Cleared whenever
        # ``_pending_stop_reset`` is cleared; the two are always set / both
        # ``None`` together.
        self._pending_stop_reset_at = None
        self._remote_control_active = False  # Track if we're in remote control mode
        self._activity = VacuumActivity.IDLE  # Default activity state
        self._hass = None  # Will be set by coordinator
        self._headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Connection": "keep-alive",
            "Accept": "*/*"
        }
        self._debug_mode = debug_mode  # Debug mode configurable
        # Persistent websocket connection for commands and status updates
        self._ws_connection = None
        self._ws_session = None
        self._last_ws_activity = None
        self._coordinator_callback = None  # Callback to notify coordinator of real-time updates
        # Websocket connection failure tracking (not device offline status)
        # Device being 'offline' via working websocket is normal, not a failure
        self._ws_consecutive_failures = 0
        self._last_known_status = None
        self._max_ws_failures_before_offline = 5  # Allow 5 websocket connection failures before marking offline
        # Simple instance ID for logging purposes
        self._instance_id = f"ha-{hash(username + str(id(self))) % 10000:04d}"

        # Serialises `_authenticate()` so two coordinator cycles overlapping
        # near token expiry can't both fire parallel Cognito refresh requests
        # (race on `self._id_token` / `self._token_expires_at`). H9b absorbed
        # this from the H9a review. Double-checked locking pattern used inside
        # `_authenticate` makes the second caller a no-op once the first
        # refresh completes.
        self._auth_lock = asyncio.Lock()

        # Status stabilization to prevent rapid connection status changes
        self._status_history = []  # Track recent status values
        self._status_stabilization_window = 30  # 30 second window to stabilize status
        self._minimum_status_duration = 15  # Minimum time (seconds) a status must persist before changing
        self._last_status_change_time = 0
        self._stable_status = None  # The stabilized status we report

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
                f"Device communication marked offline after {self._ws_consecutive_failures} consecutive websocket connection failures")
            return "offline"
        elif self._last_known_status:
            # Temporary connection failure - preserve last known status
            _LOGGER.debug(
                f"Preserving last known status '{self._last_known_status}' during temporary connection failure ({error_context})")
            return self._last_known_status
        else:
            # No previous status available - default to a neutral status
            _LOGGER.debug(f"No previous status available, using 'unknown' for {error_context}")
            return "unknown"

    def _get_close_code_info(self, close_code):
        """Get human-readable information about websocket close codes."""
        close_codes = {
            1000: "Normal Closure - Connection closed normally",
            1001: "Going Away - Server going down or browser navigating away",
            1002: "Protocol Error - Websocket protocol error",
            1003: "Unsupported Data - Server received unsupported data type",
            1005: "No Status - No status code was provided",
            1006: "Abnormal Closure - Connection closed abnormally without close frame",
            1007: "Invalid Data - Invalid UTF-8 or inconsistent data received",
            1008: "Policy Violation - Message violates policy",
            1009: "Message Too Big - Message too large to process",
            1011: "Server Error - Unexpected server condition",
            1012: "Service Restart - Service is restarting",
            1013: "Try Again Later - Service is overloaded",
            1014: "Bad Gateway - Server acting as gateway received invalid response",
            1015: "TLS Handshake Failed - TLS handshake failure"
        }

        return close_codes.get(close_code, f"Unknown close code: {close_code}")

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
                f"Websocket operations temporarily suspended due to {self._ws_consecutive_failures} consecutive connection failures")
            return False
        return True

    def _reset_websocket_failures(self):
        """Reset websocket failure counters after successful operation."""
        if self._ws_consecutive_failures > 0:
            _LOGGER.debug(f"Resetting websocket failure counter (was {self._ws_consecutive_failures})")
            self._ws_consecutive_failures = 0

    def _stabilize_status(self, raw_status):
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
            if self._debug_mode:
                _LOGGER.debug(f"Status stabilizer: Initial status set to '{raw_status}'")
            return self._stable_status

        # If the current raw status matches our stable status, just return it
        if raw_status == self._stable_status:
            return self._stable_status

        # Check if enough time has passed since the last status change
        time_since_last_change = current_time - self._last_status_change_time

        if time_since_last_change < self._minimum_status_duration:
            # Not enough time has passed - keep the stable status
            if self._debug_mode:
                _LOGGER.debug(
                    f"Status stabilizer: Ignoring rapid change from '{self._stable_status}' to '{raw_status}' (only {time_since_last_change:.1f}s elapsed, need {self._minimum_status_duration}s)")
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
            if self._debug_mode:
                _LOGGER.debug(
                    f"Status stabilizer: Status change accepted from '{self._stable_status}' to '{raw_status}' ({matching_count}/{total_recent_count} recent readings match)")
            self._stable_status = raw_status
            self._last_status_change_time = current_time
            return self._stable_status
        else:
            # Not enough consistency - keep the stable status
            if self._debug_mode:
                _LOGGER.debug(
                    f"Status stabilizer: Status change rejected from '{self._stable_status}' to '{raw_status}' ({matching_count}/{total_recent_count} recent readings match, need ≥70%)")
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
    def robot_name(self) -> str:
        # Use provided title if available
        return getattr(self, "_title", f"{self._device_type}_{self._serial}")

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

    def set_hass(self, hass):
        """Set Home Assistant instance for translations."""
        self._hass = hass

    @staticmethod
    def _resolve_cycle_duration(durations, cycle) -> int:
        """Look up the cycle duration for cycle code ``cycle`` (story C5).

        Pre-C5, the three callers each did
        ``list(durations.values())[result["cycle"]]`` -- a positional lookup
        that silently raised ``IndexError`` (masked by the surrounding
        broad-except) when ``cycle`` was unexpected. C5 extracts the
        lookup into this helper so all three call sites share one
        implementation and out-of-range / non-int cycle codes fall back to
        0 with an explicit DEBUG breadcrumb instead of an exception the
        operator never sees.

        Behavior on the success path is unchanged (positional values
        lookup over insertion-ordered dict). The cloud-side key names for
        the ``durations`` sub-dict are not currently documented and the
        team review explicitly downgraded "switch to named keys" out of
        scope until we have a verified API sample.

        Marked ``@staticmethod`` because the lookup is pure-function-on-
        arguments and reads no instance state -- documents the invariant
        explicitly and protects against a future regression that adds a
        ``self.`` access only the production path would exercise.
        ``bool`` is filtered out via the ``not isinstance(cycle, bool)``
        clause: in Python, ``bool`` subclasses ``int``, so ``True`` would
        otherwise pass the ``isinstance(cycle, int)`` guard and silently
        return ``durations.values()[1]``.
        """
        if (
            not isinstance(cycle, int)
            or isinstance(cycle, bool)
            or not (0 <= cycle < len(durations))
        ):
            _LOGGER.debug(
                "Unexpected cycle code %r for durations=%r; falling back to 0",
                cycle, durations,
            )
            return 0
        return list(durations.values())[cycle]

    def _format_time_human(self, hours: int, minutes: int, seconds: int) -> str:
        """Format an ``HH/MM/SS`` triple as a human-readable string.

        Story C3: the previous body wrapped the format string in a
        ``self._hass.localize(...)`` lookup with an ``(KeyError, AttributeError)``
        fallback. ``hass.localize`` is a frontend JavaScript API; it does not
        exist on the Python ``HomeAssistant`` object, so every call raised
        ``AttributeError`` and silently fell through to the English string —
        the localised branch never executed. Three calls × 0% hit-rate = dead
        code, removed wholesale.

        Localised time-unit suffixes ("Hour(s)" → "Heure(s)" / "Uur(en)") are
        intentionally **not** wired here. The display-language fix belongs in
        ``translations/*.json`` paired with HA's ``translation_key`` mechanism
        on the relevant entity, not in the coordinator's data layer
        (cross-references: story M11 returning raw state keys, story P7
        translation completeness).
        """
        return f"{hours} Hour(s) {minutes} Minute(s) {seconds} Second(s)"

    async def fetch_status(self, quick_setup=False) -> dict:
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

        # Apply any pending reset values from a manual stop command (H6).
        self._apply_pending_stop_reset(data)

        return data

    def _apply_pending_stop_reset(self, data: dict) -> dict:
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

        age_seconds = (dt_util.utcnow() - self._pending_stop_reset_at).total_seconds()
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

    async def _authenticate(self, force: bool = False):
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
                if self._debug_mode:
                    _LOGGER.debug("Skipping authenticate — token still valid")
                return
            if self._debug_mode:
                _LOGGER.debug("Authenticating with iAqualink API...")
            try:
                data = {
                    "apiKey": self._api_key,
                    "email": self._username,
                    "password": self._password
                }
                data = json.dumps(data)
                auth = await asyncio.wait_for(self.send_login(data, self._headers), timeout=30)

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
                if self._debug_mode:
                    _LOGGER.debug("Authentication successful, token expires at: %s", self._token_expires_at)
            except asyncio.CancelledError:
                if self._debug_mode:
                    _LOGGER.debug("Authentication cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior

    async def _discover_device(self, target_serial=None):
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
                _LOGGER.debug("🔍 Devices found : %s", device)
                if device_type in supported_device_types:
                    serial = device["serial_number"]
                    compatible_devices.append(device)

                    # If we're looking for a specific device and found it, select it
                    if target_serial and serial == target_serial:
                        self._serial = serial
                        self._device_type = device_type
                        _LOGGER.debug(f"✅ Target device selected: {device_type} - {serial}")
                        return
                else:
                    _LOGGER.debug(f"⏩ Device ignored (unsupported type): {device_type}")

            # If we have compatible devices but weren't looking for a specific one,
            # select the first compatible device
            if compatible_devices and not target_serial:
                device = compatible_devices[0]
                self._serial = device["serial_number"]
                self._device_type = device["device_type"]
                _LOGGER.debug(f"✅ Device selected: {self._device_type} - {self._serial}")
                return

            # If we were looking for a specific device but didn't find it,
            # or if we found no compatible devices at all
            if target_serial:
                _LOGGER.error(f"❌ Target device {target_serial} not found in the device list.")
                raise RuntimeError(f"Target device {target_serial} not found")
            else:
                _LOGGER.error("❌ No compatible robot found in the device list.")
                raise RuntimeError("No supported robot found")
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Device discovery cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    @classmethod
    async def discover_devices(cls, username, password, api_key):
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

    async def _ensure_websocket_connection(self):
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
        retry_delay = 2  # seconds

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
                _LOGGER.debug(f"Established persistent websocket connection for robot {self._serial}")
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
                        f"Websocket authentication failed (attempt {attempt + 1}): {e}"
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
                            _LOGGER.debug(f"Re-auth after WS 401 failed: {auth_err}")
                            # H9b review P8: include both the WS handshake error
                            # and the re-auth error in the message so the traceback
                            # surfaces full context (was only `auth_err` before).
                            raise AuthFailedError(
                                f"WS handshake 401 ({e}) and re-auth failed: {auth_err}"
                            ) from e
                        _LOGGER.debug(f"Retrying websocket connection in {retry_delay}s...")
                        await asyncio.sleep(retry_delay)
                        retry_delay *= 1.5
                        continue
                    # H9b review P6: drop the misleading "fall through to next
                    # iteration" comment — we `raise` immediately here.
                    raise AuthFailedError(f"WS handshake 401 after {max_retries} attempts") from e
                if e.status == 403:
                    _LOGGER.error(f"Websocket access forbidden (attempt {attempt + 1}): {e}")
                else:
                    _LOGGER.warning(f"Websocket handshake failed (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    _LOGGER.debug(f"Retrying websocket connection in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5
                else:
                    _LOGGER.error(
                        f"Failed to establish websocket connection after {max_retries} attempts: {e}"
                    )
                    raise
            except Exception as e:
                self._ws_consecutive_failures += 1
                await self._close_websocket()
                _LOGGER.warning(f"Websocket connection failed (attempt {attempt + 1}): {e}")
                if attempt < max_retries - 1:
                    _LOGGER.debug(f"Retrying websocket connection in {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    retry_delay *= 1.5  # Exponential backoff
                else:
                    _LOGGER.error(f"Failed to establish websocket connection after {max_retries} attempts: {e}")
                    raise

    async def _close_websocket(self):
        """Close the websocket connection and session if they exist."""
        if self._ws_connection:
            try:
                if not self._ws_connection.closed:
                    await self._ws_connection.close()
            except Exception as e:
                _LOGGER.debug(f"Error closing websocket connection: {e}")
            finally:
                self._ws_connection = None

        if self._ws_session:
            try:
                if not self._ws_session.closed:
                    await self._ws_session.close()
            except Exception as e:
                _LOGGER.debug(f"Error closing websocket session: {e}")
            finally:
                self._ws_session = None

        self._last_ws_activity = None

    async def _websocket_listener(self):
        """Listen for real-time websocket updates and notify coordinator of changes."""
        message_count = 0

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
            _LOGGER.debug(f"Started websocket listener for robot {self._serial}")

            # Listen for incoming messages
            async for message in self._ws_connection:
                if message.type == aiohttp.WSMsgType.TEXT:
                    message_count += 1
                    try:
                        data = message.json()

                        # Check if this is a state update message
                        if (data.get("service") == "StateStreamer" and
                                data.get("event") == "StateReported"):

                            payload = data.get("payload", {})
                            state = payload.get("state", {})

                            # Check for stepper updates (timing changes)
                            reported_state = state.get("reported", {})
                            equipment = reported_state.get("equipment", {})
                            robot_data = equipment.get("robot", {})

                            if "stepper" in robot_data:
                                new_stepper = robot_data["stepper"]
                                _LOGGER.debug(f"🎯 Real-time stepper update: {new_stepper}")

                                # Cache the new stepper value for button commands
                                self._cached_stepper_value = new_stepper
                                self._cached_stepper_time = time.time()

                                # Immediate entity update for ultra-fast responsiveness
                                if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                                    try:
                                        # Use asyncio.create_task for immediate execution without waiting
                                        asyncio.create_task(self._coordinator_callback())
                                    except Exception as e:
                                        _LOGGER.debug(f"Error calling coordinator callback: {e}")

                            # Check for other robot state changes
                            if any(key in robot_data for key in ['state', 'rmt_ctrl', 'errorState', 'cycleStartTime']):
                                _LOGGER.debug("🔄 Real-time robot state update received")

                                # Immediate entity update for ultra-fast responsiveness
                                if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                                    try:
                                        # Use asyncio.create_task for immediate execution without waiting
                                        asyncio.create_task(self._coordinator_callback())
                                    except Exception as e:
                                        _LOGGER.debug(f"Error calling coordinator callback: {e}")

                    except Exception as e:
                        _LOGGER.debug(f"Error processing websocket message: {e}")

                elif message.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.warning("Websocket error in listener")
                    break
                elif message.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.debug("Websocket connection closed in listener")
                    break

        except asyncio.CancelledError:
            _LOGGER.debug("Websocket listener cancelled")
            raise
        except Exception as e:
            _LOGGER.warning(f"Websocket listener error: {e}")
        finally:
            _LOGGER.debug(f"Websocket listener stopped after {message_count} messages")

    def set_coordinator_callback(self, callback):
        """Set the callback function to notify coordinator of real-time updates."""
        self._coordinator_callback = callback

    def set_coordinator_reference(self, coordinator):
        """Set reference to coordinator for adaptive polling control."""
        self._coordinator_ref = coordinator

    async def _delayed_callback(self, delay_seconds):
        """Trigger coordinator callback after a delay for catching delayed state changes."""
        await asyncio.sleep(delay_seconds)
        if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
            try:
                await self._coordinator_callback()
            except Exception as e:
                _LOGGER.debug(f"Error in delayed callback: {e}")

    async def _ws_subscribe(self):
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

    # Cache compiled regex patterns for better performance
    _VORTRAX_PATTERNS = [
        re.compile(pattern, re.IGNORECASE | re.DOTALL) for pattern in [
            r'container-[0-9a-f]+[^>]*>[^<]*<div[^>]*>[^<]*<div[^>]*>[^<]*<section[^>]*>[^<]*<div[^>]*>[^<]*<div[^>]*>[^<]*<ul[^>]*>[^<]*<li[^>]*>[^<]*<a[^>]*>[^<]*<h4[^>]*>([^<]+)</h4>',
            r'<h4[^>]*>(?:VortraX|VORTRAX)\s*([\w-]+)[^<]*</h4>',
            r'<div[^>]*class="[^"]*product-title[^"]*"[^>]*>\s*(?:VortraX|VORTRAX)\s*([\w-]+)\s*</div>',
            r'product-name[^>]*>\s*(?:VortraX|VORTRAX)\s*([\w-]+)\s*</',
            r'<title[^>]*>(?:VortraX|VORTRAX)\s*([\w-]+)',
            r'(?:VortraX|VORTRAX)\s*([\w-]+)'
        ]
    ]

    async def _get_vortrax_model_from_web(self, pn: str) -> str:
        """Get vortrax model from the zodiac website using product number."""
        # List of URLs to try
        urls = [
            f"https://www.zodiac-poolcare.com/search?key={pn}",
            f"https://www.zodiac-poolcare.be/content/zodiac/be/nl_be/search.html?key={pn}",
            f"https://www.zodiac-poolcare.fr/search?key={pn}"
        ]

        timeout = aiohttp.ClientTimeout(total=5)  # 5 second timeout
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in urls:
                try:
                    _LOGGER.debug(f"Trying to fetch VortraX model from {url}")
                    async with session.get(url) as response:
                        if response.status != 200:
                            _LOGGER.debug(f"Failed to fetch from {url}, status: {response.status}")
                            continue

                        html = await response.text()
                        _LOGGER.debug(f"Got HTML content length: {len(html)}")

                        # Try each pre-compiled pattern
                        for pattern in self._VORTRAX_PATTERNS:
                            matches = pattern.finditer(html)
                            for match in matches:
                                model = match.group(1).strip()
                                # Clean up the model string
                                model = re.sub(r'^(?:VortraX|VORTRAX)\s*', '', model, flags=re.IGNORECASE)
                                if model:
                                    _LOGGER.debug(f"Found VortraX model {model} for PN {pn}")
                                    # Only accept model numbers that look valid
                                    if re.match(r'^[A-Z0-9-]+$', model):
                                        return f"VortraX {model}"

                except asyncio.TimeoutError:
                    _LOGGER.debug(f"Timeout fetching from {url}")
                    continue
                except Exception as e:
                    _LOGGER.debug(f"Error fetching from {url}: {e}")
                    continue

        # If no model found after trying all URLs and patterns
        _LOGGER.debug(f"No VortraX model found for PN {pn}")
        return f"VortraX (PN: {pn})"

    async def _get_device_model(self) -> str:
        """Get device model information."""
        # Return cached model if available and valid (not "Unknown" or "Not Supported")
        if self._model and self._model not in ["Unknown", "Not Supported"]:
            _LOGGER.debug(f"Using cached model: {self._model}")
            return self._model

        # Check if we should retry fetching the model
        should_retry = self._should_retry_model_fetch()
        if not should_retry:
            _LOGGER.debug(f"Skipping model fetch - too many recent attempts. Current model: {self._model or 'None'}")
            return self._model or "Unknown"

        _LOGGER.debug(
            f"Fetching model for device type: {self._device_type}, serial: {self._serial} (attempt {self._model_fetch_attempts + 1})")

        # Record this attempt
        self._model_fetch_attempts += 1
        self._model_last_attempt = dt_util.utcnow()

        # For vortrax robots, get model from web based on product number
        if self._device_type == "vortrax":
            try:
                # Use the persistent websocket connection
                data = await self._ws_subscribe()
                if data and 'payload' in data and data['payload'].get('robot', {}).get('state', {}).get('reported', {}).get('eboxData', {}).get('completeCleanerPn'):
                    pn = data['payload']['robot']['state']['reported']['eboxData']['completeCleanerPn']
                    _LOGGER.debug(f"Found VortraX product number: {pn}")
                    if pn:
                        model = await self._get_vortrax_model_from_web(pn)
                        if model != "Unknown":
                            self._model = model  # Cache the successful result
                            self._model_fetch_attempts = 0  # Reset attempts on success
                            _LOGGER.debug(f"Successfully cached VortraX model: {self._model}")
                            return model
                else:
                    _LOGGER.debug("No product number data available for VortraX model detection")
            except Exception as e:
                _LOGGER.debug(f"Error getting vortrax product number: {e}")

        # For other robots, try the features API
        url = f"{URL_GET_DEVICE_FEATURES}{self._serial}/features"
        try:
            data = await asyncio.wait_for(self.get_device_features(url), timeout=30)

            # Handle empty or malformed responses
            if not data or not isinstance(data, dict):
                _LOGGER.debug(f"Empty or invalid features response for device {self._serial}")
                return self._handle_model_fetch_failure()

            # Try to get model, if not found or None, return Unknown
            model = data.get('model')
            if model is not None and model != "":
                self._model = str(model)  # Cache the successful result
                self._model_fetch_attempts = 0  # Reset attempts on success
                _LOGGER.debug(f"Successfully cached model from features API: {self._model}")
                return self._model

            # Only mark as "Not Supported" if we get an explicit indication
            if data.get('modelSupported') is False:
                self._model = "Not Supported"
                self._model_fetch_attempts = 0  # Don't retry for "Not Supported"
                _LOGGER.debug(f"Model not supported for device {self._serial}")
                return self._model

            return self._handle_model_fetch_failure()

        except Exception as e:
            _LOGGER.debug(f"Error getting device model: {e}")
            return self._handle_model_fetch_failure()

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
            _LOGGER.debug(f"Max model fetch attempts ({max_attempts}) reached for device {self._serial}")
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
            _LOGGER.debug(f"Retrying model fetch after {wait_minutes} minute wait")
            return True

        return False

    def _handle_model_fetch_failure(self) -> str:
        """Handle a failed model fetch attempt."""
        # Only cache "Unknown" if we've exhausted all attempts
        if self._model_fetch_attempts >= 5:
            self._model = "Unknown"
            _LOGGER.debug(f"Caching 'Unknown' model after {self._model_fetch_attempts} failed attempts")
        else:
            _LOGGER.debug(f"Model fetch failed (attempt {self._model_fetch_attempts}/5), will retry later")

        return self._model or "Unknown"

    async def _update_i2d_robot(self):
        """Update status for i2d_robot type."""
        request = {
            "command": "/command",
            "params": "request=OA11",
            "user_id": self._id
        }
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
        data = await asyncio.wait_for(self.post_command_i2d(url, request), timeout=30)

        result = {
            "serial_number": str(self._serial),
            "device_type": str(self._device_type),
            "status": "offline"
        }

        try:
            if data.get("command", {}).get("request") == "OA11":
                result["status"] = "connected"
                debug = data.get("command", {}).get("response", "")
                result["debug"] = str(debug) if self._debug_mode else ""

                # Clean up response string and convert to bytes
                hex_str = debug.replace(" ", "")
                if len(hex_str) != 36:  # 18 bytes * 2 characters per byte
                    raise ValueError(f"Expected 36 hex characters; got {len(hex_str)} characters.")

                try:
                    data_val = bytes.fromhex(hex_str)
                except ValueError as e:
                    raise ValueError(f"Invalid hex string: {e}")

                # Lookup tables for status codes
                state_map = {
                    0x01: "Idle / Docked",
                    0x02: "Cleaning just started",
                    0x03: "Finished",
                    0x04: "Actively cleaning",
                    0x0C: "Paused",
                    0x0D: "Error state D",
                    0x0E: "Error state E"
                }

                error_map = {
                    0x00: "no_error",
                    0x01: "pump_short_circuit",
                    0x02: "right_drive_motor_short_circuit",
                    0x03: "left_drive_motor_short_circuit",
                    0x04: "pump_motor_overconsumption",
                    0x05: "right_drive_motor_overconsumption",
                    0x06: "left_drive_motor_overconsumption",
                    0x07: "floats_on_surface",
                    0x08: "running_out_of_water",
                    0x0A: "communication_error"
                }

                mode_map = {
                    0x00: "Quick clean floor only (standard)",
                    0x03: "Deep clean floor + walls (high power)",
                    0x04: "Waterline only (standard)",
                    0x08: "Quick – floor only (standard)",
                    0x09: "Custom – floor only (high power)",
                    0x0A: "Custom – floor + walls (standard)",
                    0x0B: "Custom – floor + walls (high power)",
                    0x0C: "Waterline only (standard)",
                    0x0D: "Custom – waterline (high power)",
                    0x0E: "Custom – waterline (standard)"
                }

                # Parse fields
                state_code = data_val[2]
                error_code = data_val[3]
                mode_byte = data_val[4]
                # Extract mode code (lower nibble) and canister status (higher nibble)
                mode_code = mode_byte & 0x0F  # Get lower 4 bits for mode
                canister_full = (mode_byte & 0xF0) > 0  # Any non-zero value in upper nibble means canister is full
                time_remaining = data_val[5]
                uptime_min = int.from_bytes(data_val[6:9], byteorder='little')
                total_hours = int.from_bytes(data_val[9:12], byteorder='little')
                hardware_id = data_val[12:15].hex()
                firmware_id = data_val[15:18].hex()

                # Update result dictionary with parsed values (convert all values to strings)
                result["header"] = data_val[:2].hex()
                result["state_code"] = str(state_code)
                result["state"] = state_map.get(state_code, f"Unknown (0x{state_code:02X})")
                result["error_code"] = str(error_code)
                result["error"] = error_map.get(error_code, f"Unknown (0x{error_code:02X})")
                result["mode_code"] = str(mode_code)
                result["cycle"] = mode_map.get(mode_code, f"Unknown (0x{mode_code:02X})")
                result["mode"] = mode_map.get(mode_code, f"Unknown (0x{mode_code:02X})")
                result["canister"] = "100" if canister_full else "0"  # 100% if full, 0% if not full
                result["time_remaining"] = str(time_remaining)
                result["uptime_minutes"] = str(uptime_min)
                result["total_hours"] = str(total_hours)
                result["hardware_id"] = str(hardware_id)
                result["firmware_id"] = str(firmware_id)

                # Set fan speed based on mode code, regardless of activity state (using translation keys)
                if mode_code == 0x03:  # Deep clean mode
                    self._fan_speed = 3
                    result["fan_speed"] = "floor_and_walls"
                elif mode_code == 0x00:  # Quick clean floor only mode
                    self._fan_speed = 1
                    result["fan_speed"] = "floor_only"
                elif mode_code == 0x04:  # Waterline only
                    self._fan_speed = 2
                    result["fan_speed"] = "walls_only"

                # Update activity state based on state code
                if state_code == 0x04 or state_code == 0x02:  # Actively cleaning or Just started cleaning
                    self._activity = VacuumActivity.CLEANING
                    result["activity"] = "cleaning"
                elif error_code != 0x00:
                    self._activity = VacuumActivity.ERROR
                    result["activity"] = "error"
                    result["error_state"] = error_map.get(error_code, f"unknown_{error_code:02X}")
                else:
                    self._activity = VacuumActivity.IDLE
                    result["activity"] = "idle"
                    result["error_state"] = "no_error"

                # Calculate estimated end time if cleaning. H8: emit aware-UTC
                # datetime objects (not isoformat strings) so the sensor with
                # device_class=TIMESTAMP renders correctly in HA's local timezone.
                if time_remaining > 0 and self._activity == VacuumActivity.CLEANING:
                    estimated_end_time = self.add_minutes_to_datetime(
                        dt_util.utcnow(), time_remaining)
                    result["estimated_end_time"] = estimated_end_time
                    result["time_remaining_human"] = self._format_time_human(
                        time_remaining // 60, time_remaining % 60, 0)

        except Exception as e:
            if self._debug_mode:
                _LOGGER.error(f"Error updating i2d robot status: {e}")
            if isinstance(data, dict) and data.get("status") == "500":
                result["status"] = "offline"
            result["debug"] = str(data) if self._debug_mode else ""
            result["error_state"] = "update_failed"

        return result

    async def _update_other_robots(self):
        """Update status for non-i2d robot types."""
        try:
            data = await self._ws_subscribe()
            # Check if we got valid data
            if data is None:
                _LOGGER.debug("No data received from websocket subscribe")
                # Only increment failures if this was a websocket connection issue,
                # not if the device is legitimately offline
                # The get_device_status method already handles websocket failure counting
                return {
                    "serial_number": self._serial,
                    "device_type": self._device_type,
                    "status": self._get_resilient_status("no_data"),
                    "error_state": "no_data"
                }
            # Reset failure count on successful websocket operation
            self._ws_consecutive_failures = 0
        except asyncio.CancelledError:
            # If cancelled during setup, return minimal data to allow integration to continue
            _LOGGER.debug("Robot update cancelled - returning minimal data")
            self._ws_consecutive_failures += 1
            return {
                "serial_number": self._serial,
                "device_type": self._device_type,
                "status": self._get_resilient_status("setup_cancelled"),
                "error_state": "setup_cancelled"
            }
        except Exception as e:
            # If websocket fails, increment failure count
            self._ws_consecutive_failures += 1
            _LOGGER.debug(f"Robot update failed (attempt {self._ws_consecutive_failures}): {e}")

            return {
                "serial_number": self._serial,
                "device_type": self._device_type,
                "status": self._get_resilient_status("update_failed"),
                "error_state": "update_failed"
            }

        result = {
            "serial_number": self._serial,
            "device_type": self._device_type
        }

        try:
            if data and 'payload' in data:
                raw_status = data['payload']['robot']['state']['reported']['aws']['status']
                result["status"] = self._stabilize_status(raw_status)
                # Store the last known good status
                self._last_known_status = raw_status
            else:
                raw_status = self._get_resilient_status("no_data")
                result["status"] = self._stabilize_status(raw_status)
                result["debug"] = f"No data received: {data}" if self._debug_mode else ""
        except Exception:
            # Returns empty message sometimes, try second call
            result["debug"] = str(data) if self._debug_mode else ""  # Always store a string value

            try:
                data = await self._ws_subscribe()
                if data and 'payload' in data:
                    raw_status = data['payload']['robot']['state']['reported']['aws']['status']
                    result["status"] = self._stabilize_status(raw_status)
                    # Store the last known good status
                    self._last_known_status = raw_status
                else:
                    raw_status = self._get_resilient_status("no_data_retry")
                    result["status"] = self._stabilize_status(raw_status)
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
            # Update based on device type
            if self._device_type == "vr":
                self._update_vr_robot_data(data, result)
            elif self._device_type == "vortrax":
                self._update_vortrax_robot_data(data, result)
            elif self._device_type == "cyclobat":
                self._update_cyclobat_robot_data(data, result)
            elif self._device_type == "cyclonext":
                self._update_cyclonext_robot_data(data, result)
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

    def _update_vr_robot_data(self, data, result):
        """Update status for VR type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for VR robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for VR robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            result["temperature"] = robot_data['sensors']['sns_1']['val']
        except Exception:
            try:
                result["temperature"] = robot_data['sensors']['sns_1']['state']
            except Exception:
                # Zodiac XA 5095 iQ does not support temp
                result["temperature"] = '0'

        try:
            robot_state = robot_data['state']
            if robot_state == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                self._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Extract other attributes with safe access
        try:
            result["canister"] = robot_data['canister']*100
        except (KeyError, TypeError):
            result["canister"] = 0

        try:
            result["error_state"] = robot_data['errorState']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        try:
            result["total_hours"] = robot_data['totalHours']
        except (KeyError, TypeError):
            result["total_hours"] = 0

        # Get stepper information for timing adjustments
        try:
            result["stepper"] = robot_data['stepper']
            result["stepper_adj_time"] = robot_data.get('stepperAdjTime', 15)
            _LOGGER.debug(
                f"Stepper info: value={robot_data['stepper']}, adj_time={robot_data.get('stepperAdjTime', 15)}")
        except (KeyError, TypeError):
            result["stepper"] = 0
            result["stepper_adj_time"] = 15
            _LOGGER.debug("No stepper data found, using defaults")

        # Get current cycle (fan speed) and update it
        try:
            current_cycle = robot_data['prCyc']
            result["cycle"] = current_cycle
            # Map cycle to fan speed (using translation keys)
            cycle_map = {
                0: "wall_only",           # Wall only
                1: "floor_only",          # Floor only
                2: "smart_floor_and_walls",  # SMART mode
                3: "floor_and_walls"      # Floor and walls
            }
            self._fan_speed = cycle_map.get(current_cycle, "floor_only")
            result["fan_speed"] = self._fan_speed
        except Exception as e:
            _LOGGER.debug(f"Error setting fan speed for VR robot: {e}")
            self._fan_speed = "floor_only"  # Default to floor only if we can't determine
            result["fan_speed"] = self._fan_speed

        # Convert timestamp to datetime. H8: aware-UTC datetime objects stored
        # directly (no .isoformat()); sensor entity uses device_class=TIMESTAMP.
        # H8 review follow-up: emit None on never-cleaned (cycleStartTime=0)
        # and on parse failure so HA renders "Unknown" instead of "1970-01-01"
        # or "cycle started right now" pinned to the failure moment.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                cycle_duration = self._resolve_cycle_duration(
                    robot_data['durations'], result["cycle"]
                )
                result["cycle_duration"] = cycle_duration

                # Calculate end time and remaining time with stepper adjustments
                self._calculate_times(cycle_start_time, cycle_duration, result, robot_data)
            else:
                # Never-cleaned robot — emit None instead of a 1970 epoch render.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for VR robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _update_cyclobat_robot_data(self, data, result):
        """Update status for cyclobat type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for cyclobat robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
            main_data = robot_data['main']
            battery_data = robot_data['battery']
            stats_data = robot_data['stats']
            last_cycle_data = robot_data['lastCycle']
            cycles_data = robot_data['cycles']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for cyclobat robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Basic status and version info with safe access
        try:
            raw_status = data['payload']['robot']['state']['reported']['aws']['status']
            result["status"] = self._stabilize_status(raw_status)
        except (KeyError, TypeError):
            pass  # Status already set in calling method

        result["version"] = robot_data.get('vr', 'Unknown')
        result["serial"] = robot_data.get('sn', '')

        # Main state information with safe access
        try:
            robot_state = main_data['state']
            if robot_state == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                self._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Main control and mode information with safe access
        result["control_state"] = str(main_data.get('ctrl', 'unknown'))
        result["mode"] = str(main_data.get('mode', 'unknown'))
        result["error_code"] = str(main_data.get('error', 'unknown'))

        # Battery information with safe access
        result["battery_version"] = battery_data.get('vr', 'Unknown')
        result["battery_state"] = str(battery_data.get('state', 'unknown'))
        result["battery_percentage"] = str(battery_data.get('userChargePerc', '0'))
        result["battery_level"] = str(battery_data.get('userChargePerc', '0'))
        result["battery_charge_state"] = str(battery_data.get('userChargeState', 'unknown'))
        result["battery_cycles"] = str(battery_data.get('cycles', '0'))
        result["battery_warning_code"] = str(battery_data.get('warning', {}).get('code', 'unknown'))

        # Statistics with safe access
        result["total_hours"] = str(stats_data.get('totRunTime', '0'))
        result["diagnostic_code"] = str(stats_data.get('diagnostic', 'unknown'))
        result["temperature"] = str(stats_data.get('tmp', '0'))
        result["last_error_code"] = str(stats_data.get('lastError', {}).get('code', 'unknown'))
        result["last_error_cycle"] = str(stats_data.get('lastError', {}).get('cycleNb', 'unknown'))

        # Last cycle information with safe access
        result["last_cycle_number"] = str(last_cycle_data.get('cycleNb', 'unknown'))
        result["last_cycle_duration"] = str(last_cycle_data.get('duration', '0'))
        result["last_cycle_mode"] = str(last_cycle_data.get('mode', 'unknown'))
        result["cycle"] = str(last_cycle_data.get('endCycleType', '0'))
        result["last_cycle_error"] = str(last_cycle_data.get('errorCode', 'unknown'))

        # Cycle durations with safe access
        result["floor_duration"] = str(cycles_data.get('floorTim', {}).get('duration', '0'))
        result["floor_walls_duration"] = str(cycles_data.get('floorWallsTim', {}).get('duration', '0'))
        result["smart_duration"] = str(cycles_data.get('smartTim', {}).get('duration', '0'))
        result["waterline_duration"] = str(cycles_data.get('waterlineTim', {}).get('duration', '0'))
        result["first_smart_done"] = str(cycles_data.get('firstSmartDone', 'false'))
        result["lift_pattern_time"] = str(cycles_data.get('liftPatternTim', '0'))

        # Convert timestamp to datetime for cycle start time with safe access.
        # H8: aware-UTC datetime objects emitted directly (no .isoformat()).
        # H8 review follow-up: emit None on never-cleaned + parse failure.
        try:
            timestamp = main_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                # Get the appropriate cycle duration based on the current cycle type
                cycle_type = last_cycle_data['endCycleType']
                cycle_duration = None
                if cycle_type == 0:  # Floor only
                    cycle_duration = cycles_data['floorTim']['duration']
                elif cycle_type == 1:  # Floor and walls
                    cycle_duration = cycles_data['floorWallsTim']['duration']
                elif cycle_type == 2:  # Smart
                    cycle_duration = cycles_data['smartTim']['duration']
                elif cycle_type == 3:  # Waterline
                    cycle_duration = cycles_data['waterlineTim']['duration']

                if cycle_duration is not None:
                    result["cycle_duration"] = cycle_duration
                    # Calculate end time and remaining time
                    self._calculate_times(cycle_start_time, cycle_duration, result)
                else:
                    result["cycle_duration"] = 0
                    result["time_remaining"] = 0
                    result["time_remaining_human"] = self._format_time_human(0, 0, 0)
            else:
                # Never-cleaned robot.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for cyclobat robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _update_vortrax_robot_data(self, data, result):
        """Update status for vortrax type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for vortrax robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Store product number if available
        try:
            result["product_number"] = data['payload']['robot']['state']['reported']['eboxData']['completeCleanerPn']
        except Exception:
            result["product_number"] = None

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot data structure for vortrax robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        # Rest of the vortrax robot data update with safe access
        try:
            result["temperature"] = robot_data['sensors']['sns_1']['val']
        except Exception:
            try:
                result["temperature"] = robot_data['sensors']['sns_1']['state']
            except Exception:
                result["temperature"] = '0'

        try:
            robot_state = robot_data['state']
            if robot_state == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif robot_state == 3:
                self._activity = VacuumActivity.RETURNING
                result["activity"] = "returning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Extract other attributes with safe access
        try:
            result["canister"] = robot_data['canister']*100
        except (KeyError, TypeError):
            result["canister"] = 0

        try:
            result["error_state"] = robot_data['errorState']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        try:
            result["total_hours"] = robot_data['totalHours']
        except (KeyError, TypeError):
            result["total_hours"] = 0

        # Convert timestamp to datetime with safe access.
        # H8: aware-UTC datetime objects emitted directly (no .isoformat()).
        # H8 review follow-up: emit None on never-cleaned + parse failure.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                result["cycle"] = robot_data['prCyc']

                cycle_duration = self._resolve_cycle_duration(
                    robot_data['durations'], result["cycle"]
                )
                result["cycle_duration"] = cycle_duration

                # Calculate end time and remaining time
                self._calculate_times(cycle_start_time, cycle_duration, result)
            else:
                # Never-cleaned robot.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle"] = 0
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for vortrax robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _update_cyclonext_robot_data(self, data, result):
        """Update status for cyclonext type robot."""
        # Ensure we have valid data structure before proceeding
        if not data or 'payload' not in data:
            _LOGGER.debug("Invalid data structure for cyclonext robot update")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            robot_data = data['payload']['robot']['state']['reported']['equipment']['robot.1']
        except (KeyError, TypeError):
            _LOGGER.debug("Missing robot.1 data structure for cyclonext robot")
            result["activity"] = "unknown"
            result["error_state"] = "no_data"
            return

        try:
            if robot_data['mode'] == 1:
                self._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            else:
                self._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
        except (KeyError, TypeError):
            result["activity"] = "unknown"

        # Extract attributes with safe access
        try:
            result["canister"] = robot_data['canister']*100
        except (KeyError, TypeError):
            result["canister"] = 0

        try:
            result["error_state"] = robot_data['errors']['code']
        except (KeyError, TypeError):
            result["error_state"] = "unknown"

        try:
            result["total_hours"] = robot_data['totRunTime']
        except Exception:
            # Not supported by some cyclonext models
            result["total_hours"] = 0

        # Convert timestamp to datetime with safe access.
        # H8: aware-UTC datetime objects emitted directly (no .isoformat()).
        # H8 review follow-up: emit None on never-cleaned + parse failure.
        try:
            timestamp = robot_data['cycleStartTime']
            if timestamp > 0:
                cycle_start_time = dt_util.utc_from_timestamp(timestamp)
                result["cycle_start_time"] = cycle_start_time

                result["cycle"] = robot_data['cycle']

                cycle_duration = self._resolve_cycle_duration(
                    robot_data['durations'], result["cycle"]
                )
                result["cycle_duration"] = cycle_duration

                # Calculate end time and remaining time
                self._calculate_times(cycle_start_time, cycle_duration, result)
            else:
                # Never-cleaned robot.
                result["cycle_start_time"] = None
                result["cycle_end_time"] = None
                result["estimated_end_time"] = None
                result["cycle"] = 0
                result["cycle_duration"] = 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
        except Exception as e:
            _LOGGER.debug(f"Error processing cycle times for cyclonext robot: {e}")
            result["cycle_start_time"] = None
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["cycle"] = 0
            result["cycle_duration"] = 0
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def _calculate_times(self, start_time, duration_minutes, result, robot_data=None):
        """Calculate end time and remaining time using stepper-based adjustments."""
        try:
            # Get stepper information for accurate timing calculation
            stepper_value = 0
            stepper_adj_time = 15  # Default 15 minutes per step

            if robot_data:
                stepper_value = robot_data.get('stepper', 0)
                stepper_adj_time = robot_data.get('stepperAdjTime', 15)

                _LOGGER.debug(
                    f"Stepper calculation: base_duration={duration_minutes}, stepper={stepper_value}, stepper_adj_time={stepper_adj_time}")

            # Calculate adjusted duration using stepper system
            # Stepper value represents total minutes adjustment
            # Formula: final_duration = base_duration + stepper_value
            adjusted_duration = duration_minutes + stepper_value

            # Store both base and adjusted durations for debugging
            result["base_cycle_duration"] = duration_minutes
            result["stepper_value"] = stepper_value
            result["stepper_adjustment_minutes"] = stepper_value  # Direct minutes, not multiplied
            result["adjusted_cycle_duration"] = adjusted_duration

            # Duration calculation debug logging removed to prevent logbook flooding with 1-second polling

            # Calculate cycle end time using adjusted duration. H8: aware-UTC
            # datetime objects emitted directly (no .isoformat()) so the sensor
            # entity uses device_class=TIMESTAMP for local-timezone rendering.
            cycle_end_time = self.add_minutes_to_datetime(start_time, adjusted_duration)
            result["cycle_end_time"] = cycle_end_time
            result["estimated_end_time"] = cycle_end_time

            # If the device is idle (not cleaning/returning), always report 0 time remaining
            # regardless of what the webservice reports for cycle start time
            current_activity = result.get("activity", "idle")
            if current_activity not in ["cleaning", "returning"]:
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)
                # Debug logging removed to prevent logbook flooding with 1-second polling
                return

            # Calculate remaining time only if device is actively cleaning or returning.
            # H8: aware-UTC on both sides of the comparison/subtraction.
            now = dt_util.utcnow()

            # Only calculate time remaining if we have valid times
            if start_time and cycle_end_time:
                # If current time is before the cycle end time
                if now < cycle_end_time:
                    time_diff = cycle_end_time - now
                    total_seconds = time_diff.total_seconds()

                    # Store time remaining as integer minutes for numeric sensor
                    total_minutes = max(0, int(total_seconds / 60))
                    result["time_remaining"] = total_minutes  # This will be numeric minutes

                    # Format human readable time for display. Seconds are
                    # gated behind the `include_seconds_remaining` option:
                    # when disabled, the sensor changes only when the minute
                    # ticks over, eliminating ~20× of activity-log entries
                    # per cleaning cycle. When enabled (default), the
                    # historical seconds-precision behavior is preserved.
                    hours = int(total_seconds // 3600)
                    minutes = int((total_seconds % 3600) // 60)
                    seconds = (
                        int(total_seconds % 60)
                        if self._include_seconds_remaining
                        else 0
                    )
                    result["time_remaining_human"] = self._format_time_human(hours, minutes, seconds)

                    _LOGGER.debug("Time remaining (minutes): %d, human readable: %s",
                                  total_minutes,
                                  result["time_remaining_human"])
                else:
                    # If end time has passed, set to 0 (numeric) for time_remaining
                    result["time_remaining"] = 0
                    result["time_remaining_human"] = self._format_time_human(0, 0, 0)
            else:
                # If we don't have valid times, set to 0
                result["time_remaining"] = 0
                result["time_remaining_human"] = self._format_time_human(0, 0, 0)

            # Time calculation debug logging removed to prevent logbook flooding with 1-second polling

        except Exception as e:
            if self._debug_mode:
                _LOGGER.warning(f"Error calculating times: {e}")
            result["cycle_end_time"] = None
            result["estimated_end_time"] = None
            result["time_remaining"] = 0
            result["time_remaining_human"] = self._format_time_human(0, 0, 0)

    def add_minutes_to_datetime(self, dt, minutes):
        """Add minutes to an aware datetime object.

        H8: rejects naive datetimes at the boundary so a future regression
        anywhere upstream surfaces here rather than silently producing
        wrong-timezone arithmetic far away from the bug site.

        H8 review follow-up: raise `TypeError` rather than `assert` — `assert`
        is stripped under `python -O` / `PYTHONOPTIMIZE=1`, which defeats the
        fail-fast goal in optimized builds.
        """
        if dt.tzinfo is None:
            raise TypeError(
                "add_minutes_to_datetime requires a tz-aware datetime (H8)"
            )
        return dt + datetime.timedelta(minutes=minutes)

    async def send_login(self, data, headers):
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
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.post(URL_LOGIN, data=data) as response:
                    if response.status == 401:
                        if self._debug_mode:
                            _LOGGER.debug("Authentication failed: 401 Unauthorized.")
                        raise AuthFailedError("401 Unauthorized on send_login")

                    if response.status == 403:
                        if self._debug_mode:
                            _LOGGER.error("Authentication failed: 403 Forbidden. Check your credentials or API key.")
                        raise AuthFailedError("403 Forbidden on send_login")

                    content_type = response.headers.get('Content-Type', '')
                    if 'application/json' not in content_type:
                        text = await response.text()
                        if self._debug_mode:
                            _LOGGER.error(f"Unexpected content type: {content_type}. Response: {text[:200]}...")
                        # Cognito returns non-JSON only when the request is
                        # rejected (e.g. an HTML error page on auth failure).
                        raise AuthFailedError(
                            f"send_login returned non-JSON content type: {content_type}"
                        )

                    return await response.json()
        except asyncio.CancelledError:
            if self._debug_mode:
                _LOGGER.debug("Login request cancelled")
            raise  # Re-raise cancellation to preserve shutdown behavior

    async def get_devices(self, params, headers):
        """Get device list from the iaqualink_robots API.

        Implements the spec's Risks-clause mitigation: on HTTP 401, refresh
        the token once and retry; only the second 401 raises ``AuthFailedError``
        so a transient cloud 401 doesn't trip HA reauth unnecessarily.
        """
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(headers=headers) as session:
                    async with session.get(URL_GET_DEVICES, params=params) as response:
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
                        return await response.json()
            except asyncio.CancelledError:
                if self._debug_mode:
                    _LOGGER.debug("Get devices request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior

    async def get_device_features(self, url):
        """Get device features from the iaqualink_robots API.

        Implements the spec's one-retry mitigation (see ``get_devices``).
        """
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
                    async with session.get(url) as response:
                        if response.status == 401:
                            if attempt == 0:
                                _LOGGER.debug("401 on get_device_features — refreshing token and retrying once")
                                await self._authenticate(force=True)
                                continue  # next iteration re-reads self._id_token into a fresh session
                            raise AuthFailedError(
                                f"401 Unauthorized on {url} after re-auth retry"
                            )
                        return await response.json()
            except asyncio.CancelledError:
                if self._debug_mode:
                    _LOGGER.debug("Get device features request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior

    async def get_device_status(self, request):
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
                    return message.json()
                elif message.type == aiohttp.WSMsgType.CLOSED:
                    _LOGGER.debug("Websocket connection closed during device status request")
                    await self._close_websocket()  # Clean up closed connection
                    if attempt < max_retries - 1:
                        continue  # Try again
                    return None
                elif message.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.debug(f"Websocket error during device status request: {message.data}")
                    await self._close_websocket()  # Clean up error connection
                    if attempt < max_retries - 1:
                        continue  # Try again
                    return None
                else:
                    _LOGGER.debug(
                        f"Unexpected websocket message type {message.type} ({message.type.name if hasattr(message.type, 'name') else 'unknown'}) in device status request")
                    return None

            except asyncio.CancelledError:
                if self._debug_mode:
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
                    _LOGGER.debug(f"Device status attempt {attempt + 1} failed: {e}. Retrying...")
                    await self._close_websocket()
                    await asyncio.sleep(0.5)  # Brief pause before retry
                else:
                    # Only increment failure counter after all retries exhausted
                    self._ws_consecutive_failures += 1
                    _LOGGER.debug(f"Device status websocket connection failed after retries: {e}")
                    await self._close_websocket()
                    # Return None instead of raising to let callers handle gracefully
                    return None

    async def post_command_i2d(self, url, request):
        """Send command to i2d robot via the iaqualink_robots API.

        Implements the spec's one-retry mitigation (see ``get_devices``).
        """
        for attempt in range(2):
            try:
                async with aiohttp.ClientSession(
                    headers={"Authorization": self._id_token, "api_key": self._api_key}
                ) as session:
                    async with session.post(url, json=request) as response:
                        if response.status == 401:
                            if attempt == 0:
                                _LOGGER.debug("401 on post_command_i2d — refreshing token and retrying once")
                                await self._authenticate(force=True)
                                continue  # next iteration re-reads self._id_token
                            raise AuthFailedError(
                                f"401 Unauthorized on {url} after re-auth retry"
                            )
                        return await response.json()
            except asyncio.CancelledError:
                if self._debug_mode:
                    _LOGGER.debug("Post command i2d request cancelled")
                raise  # Re-raise cancellation to preserve shutdown behavior

    async def set_cleaner_state(self, request):
        """Set cleaner state via the iaqualink_robots API websocket.
        Implements resilient error handling to avoid device status disruption."""
        response_data = None
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
                        _LOGGER.debug(f"Command response received: {response_data}")
                        return response_data  # Success - return immediately
                    elif response.type == aiohttp.WSMsgType.CLOSED:
                        _LOGGER.debug("Websocket closed during command, will retry")
                        await self._close_websocket()
                        continue  # Retry
                except asyncio.TimeoutError:
                    _LOGGER.debug("No response received within timeout, command may still succeed")
                    return response_data  # Command sent successfully, just no response
                except Exception as e:
                    _LOGGER.debug(f"Error receiving response: {e}")
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
                    _LOGGER.debug(f"Websocket command attempt {attempt + 1} failed: {e}. Retrying...")
                    await self._close_websocket()
                    await asyncio.sleep(1)  # Brief pause before retry
                else:
                    # Only log as warning, not error, to avoid device unavailability
                    _LOGGER.warning(f"Websocket command failed after {max_retries} attempts: {e}")
                    await self._close_websocket()

        return response_data

    async def start_cleaning(self):
        """Start the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            # Record the start time when starting cleaning for i2d robots.
            # H8: aware-UTC datetime stored directly (no .isoformat()).
            now = dt_util.utcnow()
            result = {
                "cycle_start_time": now,
                "activity": "cleaning"
            }
            request = {
                "command": "/command",
                "params": "request=0A1240&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
            return result  # Return the time values to be used in the coordinator's update
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

            request = None
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Use exact format from MITM proxy capture
                request = {
                    "action": "setCleanerState",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "state": 1
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclobat":
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"ctrl": 1}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclonext":
                request = {
                    "action": "setCleanerState",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"mode": 1}}}}
                    },
                    "service": "StateController",
                    "target": self._serial,
                    "version": 1
                }

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

                # Trigger multiple immediate updates for ultra-fast control feedback
                if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                    try:
                        # Immediate callback
                        asyncio.create_task(self._coordinator_callback())
                        # Additional callbacks at intervals to catch delayed state changes
                        asyncio.create_task(self._delayed_callback(0.5))
                        asyncio.create_task(self._delayed_callback(1.0))
                    except Exception as e:
                        _LOGGER.debug(f"Error triggering immediate callback: {e}")

    async def clear_desired_state(self):
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

        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

        request = {
            "action": "setCleanerState",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "state": 0
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }

        try:
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)
            _LOGGER.debug("Cleared desired state to prevent auto-restart")
        except Exception as e:
            _LOGGER.warning(f"Failed to clear desired state: {e}")

    async def stop_cleaning(self):
        """Stop the vacuum cleaning."""
        request = None
        if self._device_type == "i2d_robot":
            i2d_request = {
                "command": "/command",
                "params": "request=0A1210&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, i2d_request), timeout=800)
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

            if self._device_type == "vr" or self._device_type == "vortrax":
                # Use exact format from MITM proxy capture
                request = {
                    "action": "setCleanerState",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "state": 0
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclobat":
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"ctrl": 0}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclonext":
                request = {
                    "action": "setCleanerState",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"mode": 0}}}}
                    },
                    "service": "StateController",
                    "target": self._serial,
                    "version": 1
                }

        # Create result with reset time values always, regardless of robot type.
        # H8 review follow-up: emit None for the two TIMESTAMP-classed sensors
        # so they render "Unknown" on stop, rather than the previous H8 shape
        # which set them to `utcnow()` and produced a misleading "cycle started
        # right now" display when the cycle had just ended.
        reset_values = {
            "estimated_end_time": None,
            "time_remaining": 0,
            "time_remaining_human": self._format_time_human(0, 0, 0),
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

            # Trigger multiple immediate updates for ultra-fast control feedback
            if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                try:
                    # Immediate callback
                    asyncio.create_task(self._coordinator_callback())
                    # Additional callbacks to catch any delayed state changes
                    asyncio.create_task(self._delayed_callback(0.5))
                    asyncio.create_task(self._delayed_callback(1.0))
                except Exception as e:
                    _LOGGER.debug(f"Error triggering immediate callback: {e}")

        return reset_values

    async def pause_cleaning(self):
        """Pause the vacuum cleaning."""
        if self._device_type == "i2d_robot":
            # i2d robots might not support pause - use stop instead
            return await self.stop_cleaning()
        else:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

            request = None
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Use exact format from MITM proxy capture - state 2 = pause
                request = {
                    "action": "setCleanerState",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "state": 2
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclobat":
                # Cyclobat pause might be different - using same format for now
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"ctrl": 2}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }
            elif self._device_type == "cyclonext":
                # Cyclonext pause might be different
                request = {
                    "action": "setCleanerState",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"mode": 2}}}}
                    },
                    "service": "StateController",
                    "target": self._serial,
                    "version": 1
                }

            if request:
                await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

                # Trigger multiple immediate updates for ultra-fast control feedback
                if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                    try:
                        # Immediate callback
                        asyncio.create_task(self._coordinator_callback())
                        # Additional callbacks to catch any delayed state changes
                        asyncio.create_task(self._delayed_callback(0.5))
                        asyncio.create_task(self._delayed_callback(1.0))
                    except Exception as e:
                        _LOGGER.debug(f"Error triggering immediate callback: {e}")

    async def return_to_base(self):
        """Set the vacuum cleaner to return to the dock."""
        if self._device_type == "i2d_robot":
            request = {
                "command": "/command",
                "params": "request=0A1701&timeout=800",
                "user_id": self._id
            }
            url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
            await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)
        elif self._device_type in ["vr", "vortrax", "cyclobat"]:
            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
            request = {
                "action": "setCleanerState",
                "namespace": self._device_type,
                "payload": {
                    "clientToken": clientToken,
                    "state": {"desired": {"equipment": {"robot": {"state": 3}}}}
                },
                "service": "StateController",
                "target": self._serial,
                "version": 1
            }
            await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

            # Trigger multiple immediate updates for ultra-fast control feedback
            if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                try:
                    # Immediate callback
                    asyncio.create_task(self._coordinator_callback())
                    # Additional callbacks to catch any delayed state changes
                    asyncio.create_task(self._delayed_callback(0.5))
                    asyncio.create_task(self._delayed_callback(1.0))
                except Exception as e:
                    _LOGGER.debug(f"Error triggering immediate callback: {e}")

    def _extract_fan_speed_from_response(self, response_data, requested_fan_speed):
        """Extract current fan speed from websocket response if available."""
        if not response_data:
            return None

        try:
            payload = response_data.get("payload", {})

            # For different robot types, the fan speed might be in different locations
            if self._device_type == "vr" or self._device_type == "vortrax":
                # Look for prCyc (program cycle) in the response
                robot_state = payload.get("robot", {}).get("state", {}).get(
                    "reported", {}).get("equipment", {}).get("robot", {})
                pr_cyc = robot_state.get("prCyc")
                if pr_cyc is not None:
                    cycle_map = {0: "wall_only", 1: "floor_only", 2: "smart_floor_and_walls", 3: "floor_and_walls"}
                    return cycle_map.get(pr_cyc, requested_fan_speed)

            elif self._device_type == "cyclobat":
                # Look for mode in cyclobat response
                robot_state = payload.get("robot", {}).get("state", {}).get(
                    "reported", {}).get("equipment", {}).get("robot", {}).get("main", {})
                mode = robot_state.get("mode")
                if mode is not None:
                    cycle_map = {3: "wall_only", 0: "floor_only", 2: "smart_floor_and_walls", 1: "floor_and_walls"}
                    return cycle_map.get(mode, requested_fan_speed)

            elif self._device_type == "cyclonext":
                # Look for cycle in cyclonext response
                robot_state = payload.get("robot", {}).get("state", {}).get(
                    "reported", {}).get("equipment", {}).get("robot.1", {})
                cycle = robot_state.get("cycle")
                if cycle is not None:
                    cycle_map = {1: "floor_only", 3: "floor_and_walls"}
                    return cycle_map.get(cycle, requested_fan_speed)

        except Exception as e:
            _LOGGER.debug(f"Error extracting fan speed from response: {e}")

        return None

    async def set_fan_speed(self, fan_speed, fan_speed_list):
        """Set fan speed (cleaning mode) for the vacuum cleaner."""
        if fan_speed not in fan_speed_list:
            raise ValueError('Invalid fan speed')

        if self._device_type == "i2d_robot":
            return await self._set_i2d_fan_speed(fan_speed)
        else:
            return await self._set_other_fan_speed(fan_speed)

    async def _set_i2d_fan_speed(self, fan_speed):
        """Set fan speed for i2d robot."""
        cycle_speed_map = {
            "walls_only": "0A1284",
            "floor_only": "0A1280",
            "floor_and_walls": "0A1283"
        }

        _cycle_speed = cycle_speed_map.get(fan_speed)
        if not _cycle_speed:
            return None

        request = {
            "command": "/command",
            "params": f"request={_cycle_speed}&timeout=800",
            "user_id": self._id
        }
        url = f"https://r-api.iaqualink.net/v2/devices/{self._serial}/control.json"
        response = await asyncio.wait_for(self.post_command_i2d(url, request), timeout=800)

        # For i2d robots, extract success/failure from response
        if response and response.get("command", {}).get("response"):
            _LOGGER.debug(f"i2d fan speed command response: {response}")
            return {"success": True, "fan_speed": fan_speed, "response": response}

        return {"success": False, "fan_speed": fan_speed}

    async def _set_other_fan_speed(self, fan_speed):
        """Set fan speed for non-i2d robots."""
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

        request = None
        if self._device_type == "vr" or self._device_type == "vortrax":
            cycle_speed_map = {
                "wall_only": "0",
                "floor_only": "1",
                "smart_floor_and_walls": "2",
                "floor_and_walls": "3"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                # Use exact format from MITM proxy capture matching setCleaningMode action
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": self._device_type,
                    "payload": {
                        "state": {
                            "desired": {
                                "equipment": {
                                    "robot": {
                                        "prCyc": int(_cycle_speed)
                                    }
                                }
                            }
                        },
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }

        elif self._device_type == "cyclobat":
            cycle_speed_map = {
                "wall_only": "3",
                "floor_only": "0",
                "smart_floor_and_walls": "2",
                "floor_and_walls": "1"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                request = {
                    "action": "setCleaningMode",
                    "version": 1,
                    "namespace": "cyclobat",
                    "payload": {
                        "state": {"desired": {"equipment": {"robot": {"main": {"mode": _cycle_speed}}}}},
                        "clientToken": clientToken
                    },
                    "service": "StateController",
                    "target": self._serial
                }

        elif self._device_type == "cyclonext":
            # The keys here MUST match the snake_case translation keys passed in
            # by `vacuum.py::async_set_fan_speed` (which forwards the internal
            # key after converting from any display-name input). Pre-issue-#76
            # this map used the legacy display-name keys ("Floor only",
            # "Floor and walls"), so `.get(fan_speed)` always returned None,
            # `request` stayed None, and the command silently fell through to
            # `{"success": False, ..., "error": "No valid request generated"}`.
            # That's the "fan speed no longer propagates from HA to AquaLink"
            # symptom reported in issue #76 (galletn comment on RE 4400 iQ).
            cycle_speed_map = {
                "floor_only": "1",
                "floor_and_walls": "3"
            }
            _cycle_speed = cycle_speed_map.get(fan_speed)
            if _cycle_speed:
                request = {
                    "action": "setCleaningMode",
                    "namespace": "cyclonext",
                    "payload": {
                        "clientToken": clientToken,
                        "state": {"desired": {"equipment": {"robot.1": {"cycle": _cycle_speed}}}}
                    },
                    "service": "StateController",
                    "target": self._serial,
                    "version": 1
                }

        if request:
            response_data = await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

            # Extract relevant information from the websocket response
            result = {"success": True, "fan_speed": fan_speed}
            if response_data:
                _LOGGER.debug(f"Fan speed command response: {response_data}")
                result["response"] = response_data

                # Try to extract the actual fan speed from the response
                confirmed_fan_speed = self._extract_fan_speed_from_response(response_data, fan_speed)
                if confirmed_fan_speed:
                    result["confirmed_fan_speed"] = confirmed_fan_speed
                    _LOGGER.debug(f"Websocket confirmed fan speed: {confirmed_fan_speed}")

                # For some robot types, we might get immediate state confirmation
                payload = response_data.get("payload", {})
                if payload:
                    result["payload"] = payload

            return result

        return {"success": False, "fan_speed": fan_speed, "error": "No valid request generated"}

    async def _enter_remote_control_mode(self):
        """Enter remote control mode by setting robot to pause state (state: 2)."""
        _LOGGER.debug("Entering remote control mode (state: 2)")
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        request = {
            "action": "setCleanerState",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "state": 2  # Pause mode enables remote control
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def _exit_remote_control_mode(self):
        """Exit remote control mode by setting robot to stopped state (state: 0)."""
        _LOGGER.debug("Exiting remote control mode (state: 0)")
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        request = {
            "action": "setCleanerState",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "state": 0  # Stopped state exits remote control
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=30)

    async def _send_remote_command(self, rmt_ctrl_value, command_name="remote"):
        """Send a remote control command with optimized timeout."""
        clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"
        request = {
            "action": "setRemoteSteeringControl",
            "version": 1,
            "namespace": self._device_type,
            "payload": {
                "state": {
                    "desired": {
                        "equipment": {
                            "robot": {
                                "rmt_ctrl": rmt_ctrl_value
                            }
                        }
                    }
                },
                "clientToken": clientToken
            },
            "service": "StateController",
            "target": self._serial
        }
        _LOGGER.debug(f"Sending {command_name} command (rmt_ctrl: {rmt_ctrl_value})")
        await asyncio.wait_for(self.set_cleaner_state(request), timeout=15)  # Shorter timeout, no artificial delays

    async def remote_forward(self):
        """Send forward command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote forward not supported for device type: {self._device_type}")
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(1, "forward")

    async def remote_backward(self):
        """Send backward command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote backward not supported for device type: {self._device_type}")
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(2, "backward")

    async def remote_rotate_left(self):
        """Send rotate left command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote rotate left not supported for device type: {self._device_type}")
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(4, "rotate left")

    async def remote_rotate_right(self):
        """Send rotate right command to the robot for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote rotate right not supported for device type: {self._device_type}")
            return

        # Enter remote control mode if not already active
        if not self._remote_control_active:
            await self._enter_remote_control_mode()
            self._remote_control_active = True

        await self._send_remote_command(3, "rotate right")

    async def remote_stop(self):
        """Send stop command for VR and VortraX robots and exit remote control mode."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Remote stop not supported for device type: {self._device_type}")
            return

        # Send stop command first
        await self._send_remote_command(0, "stop")

        # Exit remote control mode if currently active
        if self._remote_control_active:
            await self._exit_remote_control_mode()
            self._remote_control_active = False

    async def add_fifteen_minutes(self):
        """Add 15 minutes to cleaning time for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Add 15 minutes not supported for device type: {self._device_type}")
            return

        # Check if websocket operations should be attempted
        if not self._should_use_websocket():
            _LOGGER.warning("Websocket operations temporarily suspended due to connection issues. Please try again later.")
            return {
                "success": False,
                "error": "websocket_unavailable",
                "message": "Connection issues detected. Please try again in a moment."
            }

        try:
            # Check for button press rate limiting (debouncing)
            if hasattr(self, '_last_timing_command_time'):
                time_since_last = time.time() - self._last_timing_command_time
                if time_since_last < 3:  # 3 second debounce
                    remaining_cooldown = 3 - time_since_last
                    _LOGGER.warning(
                        f"Rate limiting timing commands. Please wait {remaining_cooldown:.1f} more seconds.")
                    return {
                        "success": False,
                        "error": "rate_limited",
                        "cooldown_remaining": remaining_cooldown,
                        "message": f"Please wait {remaining_cooldown:.1f} seconds before sending another timing command."
                    }

            # Mark timing command time for rate limiting
            self._last_timing_command_time = time.time()

            # Always fetch current stepper value to ensure accuracy
            # fetch_status() returns a flat dict with 'stepper' at the top level
            current_status = await self.fetch_status()
            current_stepper = current_status.get('stepper', 0)

            if self._debug_mode:
                _LOGGER.debug(f"🔍 Add 15min: Current stepper value = {current_stepper}")
                _LOGGER.debug(f"Adding 15 minutes: current stepper={current_stepper}")

            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

            # Get the new stepper value (current + 15 minutes)
            # Stepper represents total minutes adjustment
            new_stepper = current_stepper + 15

            if self._debug_mode:
                _LOGGER.debug(f"🔍 Add 15min: Setting stepper {current_stepper} → {new_stepper}")

            # Use the same format as the working test: setCleaningMode with stepper value
            request = {
                "action": "setCleaningMode",
                "namespace": self._device_type,
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": new_stepper
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial,
                "version": 1
            }

            if self._debug_mode:
                _LOGGER.debug(f"Sending add 15 minutes command: stepper {current_stepper} → {new_stepper}")
            result = await asyncio.wait_for(self.set_cleaner_state(request), timeout=15)
            if self._debug_mode:
                _LOGGER.debug(f"Add 15 minutes response: {result}")

            # Also send a null command to clear the desired state (as seen in websocket traffic)
            clear_request = {
                "action": "setCleaningMode",
                "version": 1,
                "namespace": self._device_type,
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": None
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial
            }

            await asyncio.wait_for(self.set_cleaner_state(clear_request), timeout=15)

            if self._debug_mode:
                _LOGGER.debug("✅ Add 15 minutes command completed successfully")

            # Trigger multiple immediate updates for ultra-fast button feedback
            if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                try:
                    # Immediate callback
                    asyncio.create_task(self._coordinator_callback())
                    # Additional callbacks to catch any delayed state changes
                    asyncio.create_task(self._delayed_callback(0.5))
                    asyncio.create_task(self._delayed_callback(1.0))
                except Exception as e:
                    _LOGGER.debug(f"Error triggering immediate callback: {e}")

            # Return success result with notification info for coordinator to handle
            return {
                "success": True,
                "action": "add_15_minutes",
                "previous_stepper": current_stepper,
                "new_stepper": new_stepper,
                "message": f"Added 15 minutes to cleaning time. Stepper: {current_stepper} → {new_stepper}"
            }

        except Exception as e:
            _LOGGER.error(f"❌ Add 15 minutes command failed: {e}")
            # Return error result for coordinator to handle
            return {
                "success": False,
                "error": str(e),
                "message": f"Failed to add 15 minutes: {str(e)}"
            }

    async def reduce_fifteen_minutes(self):
        """Reduce 15 minutes from cleaning time for VR and VortraX robots."""
        if self._device_type not in ["vr", "vortrax"]:
            _LOGGER.warning(f"Reduce 15 minutes not supported for device type: {self._device_type}")
            return

        # Check if websocket operations should be attempted
        if not self._should_use_websocket():
            _LOGGER.warning("Websocket operations temporarily suspended due to connection issues. Please try again later.")
            return {
                "success": False,
                "error": "websocket_unavailable",
                "message": "Connection issues detected. Please try again in a moment."
            }

        try:
            # Get current status for logging
            # fetch_status() returns a flat dict with 'stepper' at the top level
            current_status = await self.fetch_status()
            current_stepper = current_status.get('stepper', 0)

            if self._debug_mode:
                _LOGGER.debug(f"Reducing 15 minutes: current stepper={current_stepper}")

            # Check for button press rate limiting (debouncing)
            if hasattr(self, '_last_timing_command_time'):
                time_since_last = time.time() - self._last_timing_command_time
                if time_since_last < 3:  # 3 second debounce
                    remaining_cooldown = 3 - time_since_last
                    _LOGGER.warning(
                        f"Rate limiting timing commands. Please wait {remaining_cooldown:.1f} more seconds.")
                    return {
                        "success": False,
                        "error": "rate_limited",
                        "cooldown_remaining": remaining_cooldown,
                        "message": f"Please wait {remaining_cooldown:.1f} seconds before sending another timing command."
                    }

            # Mark timing command time for rate limiting
            self._last_timing_command_time = time.time()

            clientToken = f"{self._id}|{self._auth_token}|{self._app_client_id}"

            # Get the new stepper value (current - 15 minutes)
            # Stepper represents total minutes adjustment
            new_stepper = current_stepper - 15

            # Use the same format as the working test: setCleaningMode with stepper value
            request = {
                "action": "setCleaningMode",
                "namespace": self._device_type,
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": new_stepper
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial,
                "version": 1
            }

            if self._debug_mode:
                _LOGGER.debug(f"Sending reduce 15 minutes command: stepper {current_stepper} → {new_stepper}")
            result = await asyncio.wait_for(self.set_cleaner_state(request), timeout=15)
            if self._debug_mode:
                _LOGGER.debug(f"Reduce 15 minutes response: {result}")

            # Also send a null command to clear the desired state (as seen in websocket traffic)
            clear_request = {
                "action": "setCleaningMode",
                "version": 1,
                "namespace": self._device_type,
                "payload": {
                    "state": {
                        "desired": {
                            "equipment": {
                                "robot": {
                                    "stepper": None
                                }
                            }
                        }
                    },
                    "clientToken": clientToken
                },
                "service": "StateController",
                "target": self._serial
            }

            await asyncio.wait_for(self.set_cleaner_state(clear_request), timeout=15)

            if self._debug_mode:
                _LOGGER.debug("✅ Reduce 15 minutes command completed successfully")

            # Trigger multiple immediate updates for ultra-fast button feedback
            if hasattr(self, '_coordinator_callback') and self._coordinator_callback:
                try:
                    # Immediate callback
                    asyncio.create_task(self._coordinator_callback())
                    # Additional callbacks to catch any delayed state changes
                    asyncio.create_task(self._delayed_callback(0.5))
                    asyncio.create_task(self._delayed_callback(1.0))
                except Exception as e:
                    _LOGGER.debug(f"Error triggering immediate callback: {e}")

            # Return success result with notification info for coordinator to handle
            return {
                "success": True,
                "action": "reduce_15_minutes",
                "previous_stepper": current_stepper,
                "new_stepper": new_stepper,
                "message": f"Reduced 15 minutes from cleaning time. Stepper: {current_stepper} → {new_stepper}"
            }

        except Exception as e:
            _LOGGER.error(f"❌ Reduce 15 minutes command failed: {e}")
            # Return error result for coordinator to handle
            return {
                "success": False,
                "error": str(e),
                "message": f"Failed to reduce 15 minutes: {str(e)}"
            }


class AqualinkDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to poll AqualinkClient and update data."""

    def __init__(self, hass, client: AqualinkClient, interval: float, debug_mode: bool = False):
        super().__init__(
            hass,
            _LOGGER,
            name=client.robot_id,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client
        self.client.set_hass(hass)  # Set hass reference for translations
        self._debug_mode = debug_mode
        self._last_data = {}
        self._consecutive_failures = 0
        # Wall-clock timestamp of the first failure in the current outage
        # streak (story H7). Set on the 0→1 transition of
        # ``_consecutive_failures`` and cleared back to ``None`` whenever
        # the counter returns to 0 on a successful update. Consulted by the
        # ``is_long_outage`` property to gate when entities flip to
        # ``available=False`` — replaces the prior count-based gate which
        # could trigger after as little as 45 s of failures at the fast
        # adaptive polling rate.
        self._first_failure_at: "datetime | None" = None
        # Backs the public ``title`` property; set by ``__init__.py`` from
        # ``entry.title`` so entities and tests can read a stable display
        # name via the public API (story M15). Kept underscored so the
        # public name stays free for HA's ``DataUpdateCoordinator`` base
        # class — if a future HA version adds its own ``title`` attribute,
        # the subclass property here cleanly overrides it without an
        # attribute-shadowing surprise.
        self._title: str | None = None
        self._setup_complete = False  # Flag to track if initial setup is complete
        self._last_timing_command = 0  # Timestamp of last timing command for debouncing

        # Adaptive polling for efficiency
        self._base_interval = interval  # Store original interval (3 seconds)
        self._fast_interval = 1.5  # Fast interval when robot is active
        self._slow_interval = 10   # Slow interval when robot is idle
        self._last_activity_time = 0

        # Real-time websocket listener for instant updates
        self._websocket_listener_task = None
        self._should_stop_listener = False

        # Set up callback for real-time updates
        self.client.set_coordinator_callback(self._handle_realtime_update)
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

    async def _handle_realtime_update(self):
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
        via the public ``async_update_listeners`` path, and honours the
        default ~0.3 s debouncer which collapses rapid websocket-event
        bursts into a single update (a desirable property the old loop
        didn't have).
        """
        try:
            await self.async_request_refresh()
        except Exception as e:
            _LOGGER.debug(f"Error handling real-time update: {e}")

    async def _start_websocket_listener(self):
        """Start the websocket listener task for real-time updates."""
        if self._websocket_listener_task and not self._websocket_listener_task.done():
            return  # Already running

        try:
            self._should_stop_listener = False
            self._websocket_listener_task = asyncio.create_task(
                self.client._websocket_listener()
            )
            _LOGGER.debug("Started websocket listener for real-time updates")
        except Exception as e:
            _LOGGER.warning(f"Failed to start websocket listener: {e}")

    async def _stop_websocket_listener(self):
        """Stop the websocket listener task."""
        self._should_stop_listener = True

        if self._websocket_listener_task and not self._websocket_listener_task.done():
            self._websocket_listener_task.cancel()
            try:
                await self._websocket_listener_task
            except asyncio.CancelledError:
                pass
            _LOGGER.debug("Stopped websocket listener")

    def _update_polling_interval(self, data):
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
                _LOGGER.debug(f"Adaptive polling: {current_activity} -> {new_interval}s interval")

        except Exception as e:
            _LOGGER.debug(f"Error updating polling interval: {e}")

    def _mark_recent_activity(self):
        """Mark recent activity to trigger faster polling temporarily."""
        self._last_activity_time = time.time()

    async def _async_update_data(self):
        try:
            # Check if this is the first call (initial setup)
            is_first_call = not hasattr(self, '_setup_complete') or not self._setup_complete

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
                    not self.client._pending_stop_reset):  # Not a manual stop
                _LOGGER.info("Robot finished cleaning naturally - clearing desired state to prevent auto-restart")
                try:
                    await self.client.clear_desired_state()
                except Exception as e:
                    _LOGGER.warning(f"Failed to clear desired state after natural completion: {e}")

            # Reset failure count + outage timestamp on successful update.
            # H7: clearing ``_first_failure_at`` here flips
            # ``is_serving_stale_data`` back to ``False`` so the
            # ``restored`` entity attribute reverts to ``False`` once a
            # poll succeeds (AC #6).
            self._consecutive_failures = 0
            self._first_failure_at = None

            # Reset websocket failure count on successful status update
            self.client._reset_websocket_failures()

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

            # Get detailed error information
            import traceback
            error_details = traceback.format_exc()

            if not self.is_long_outage:
                if self._debug_mode:
                    _LOGGER.warning(
                        f"Update failed (attempt {self._consecutive_failures}, "
                        f"outage age {self._outage_age_seconds():.0f}s "
                        f"< {LONG_OUTAGE_THRESHOLD_SECONDS}s threshold): {err}")

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
                _LOGGER.error(
                    f"Update failed after {self._consecutive_failures} attempts "
                    f"({self._outage_age_seconds():.0f}s outage, "
                    f"> {LONG_OUTAGE_THRESHOLD_SECONDS}s threshold): {err}"
                    f"\nDetails:\n{error_details}")
                # Long-outage threshold exceeded — raise UpdateFailed so HA
                # marks the entity unavailable. The `is_long_outage` gate on
                # entity `available` properties also short-circuits to False
                # before this point if the platform queries it independently.
                raise UpdateFailed(
                    f"Failed after {self._consecutive_failures} attempts "
                    f"over {self._outage_age_seconds():.0f}s: {err}"
                )

    async def async_start_cleaning(self):
        """Start cleaning - centralized business logic."""
        await self.client.start_cleaning()

    async def async_stop_cleaning(self):
        """Stop cleaning - centralized business logic."""
        await self.client.stop_cleaning()

    async def async_return_to_base(self):
        """Return to base - centralized business logic."""
        await self.client.return_to_base()

    async def _execute_remote_command(self, command_method):
        """Execute a remote command with device type checking but no automatic refresh."""
        if self.client.device_type not in {"vr", "vortrax"}:
            return
        await command_method()
        # Remove automatic refresh to improve responsiveness - let normal polling handle updates

    async def async_remote_forward(self):
        """Send forward command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_forward)

    async def async_remote_backward(self):
        """Send backward command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_backward)

    async def async_remote_rotate_left(self):
        """Send rotate left command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_rotate_left)

    async def async_remote_rotate_right(self):
        """Send rotate right command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_rotate_right)

    async def async_remote_stop(self):
        """Send stop command to the robot for remote control - centralized business logic."""
        await self._execute_remote_command(self.client.remote_stop)

    async def async_add_fifteen_minutes(self):
        """Add 15 minutes to cleaning time - centralized business logic."""
        if self.client.device_type not in {"vr", "vortrax"}:
            return

        # Debouncing: Check if enough time has passed since last timing command
        current_time = time.time()
        time_since_last = current_time - self._last_timing_command
        debounce_seconds = 3  # 3 second cooldown

        if time_since_last < debounce_seconds:
            # Too soon - send notification about rate limiting
            try:
                await self.hass.services.async_call(
                    "browser_mod",
                    "notification",
                    {
                        "message": f"Please wait {debounce_seconds - time_since_last:.1f} more seconds before next timing adjustment.",
                        "duration": 2000,  # 2 seconds
                        "action_text": "OK"
                    }
                )
            except Exception:
                # Fallback to persistent notification if browser_mod not available
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Rate Limited",
                        "message": f"Please wait {debounce_seconds - time_since_last:.1f} more seconds before next timing adjustment.",
                        "notification_id": f"timing_rate_limit_{self.client.robot_id}"
                    }
                )
            return

        # Update timestamp for debouncing
        self._last_timing_command = current_time

        result = await self.client.add_fifteen_minutes()

        # Send notification to user
        if result and isinstance(result, dict):
            if result.get("success"):
                # Try browser_mod toast notification first (more elegant)
                try:
                    await self.hass.services.async_call(
                        "browser_mod",
                        "notification",
                        {
                            "message": result.get("message", "Added 15 minutes to cleaning time."),
                            "duration": 4000,  # 4 seconds
                            "action_text": "OK"
                        }
                    )
                except Exception:
                    # Fallback to persistent notification if browser_mod not available
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Pool Robot",
                            "message": result.get("message", "Added 15 minutes to cleaning time."),
                            "notification_id": f"robot_time_change_{self.client.serial}"
                        }
                    )
            else:
                # For errors, always use persistent notifications (more important)
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Pool Robot Error",
                        "message": result.get("message", "Failed to add 15 minutes."),
                        "notification_id": f"robot_error_{self.client.serial}"
                    }
                )

        # Request immediate refresh to get updated timing information
        await self.async_request_refresh()

    async def async_reduce_fifteen_minutes(self):
        """Reduce 15 minutes from cleaning time - centralized business logic."""
        if self.client.device_type not in {"vr", "vortrax"}:
            return

        # Debouncing: Check if enough time has passed since last timing command
        current_time = time.time()
        time_since_last = current_time - self._last_timing_command
        debounce_seconds = 3  # 3 second cooldown

        if time_since_last < debounce_seconds:
            # Too soon - send notification about rate limiting
            try:
                await self.hass.services.async_call(
                    "browser_mod",
                    "notification",
                    {
                        "message": f"Please wait {debounce_seconds - time_since_last:.1f} more seconds before next timing adjustment.",
                        "duration": 2000,  # 2 seconds
                        "action_text": "OK"
                    }
                )
            except Exception:
                # Fallback to persistent notification if browser_mod not available
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Rate Limited",
                        "message": f"Please wait {debounce_seconds - time_since_last:.1f} more seconds before next timing adjustment.",
                        "notification_id": f"timing_rate_limit_{self.client.robot_id}"
                    }
                )
            return

        # Update timestamp for debouncing
        self._last_timing_command = current_time

        result = await self.client.reduce_fifteen_minutes()

        # Send notification to user
        if result and isinstance(result, dict):
            if result.get("success"):
                # Try browser_mod toast notification first (more elegant)
                try:
                    await self.hass.services.async_call(
                        "browser_mod",
                        "notification",
                        {
                            "message": result.get("message", "Reduced cleaning time by 15 minutes."),
                            "duration": 4000,  # 4 seconds
                            "action_text": "OK"
                        }
                    )
                except Exception:
                    # Fallback to persistent notification if browser_mod not available
                    await self.hass.services.async_call(
                        "persistent_notification",
                        "create",
                        {
                            "title": "Pool Robot",
                            "message": result.get("message", "Reduced cleaning time by 15 minutes."),
                            "notification_id": f"robot_time_change_{self.client.serial}"
                        }
                    )
            else:
                # For errors, always use persistent notifications (more important)
                await self.hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Pool Robot Error",
                        "message": result.get("message", "Failed to reduce 15 minutes."),
                        "notification_id": f"robot_error_{self.client.serial}"
                    }
                )

        # Request immediate refresh to get updated timing information
        await self.async_request_refresh()

    async def cleanup(self):
        """Clean up resources when coordinator is being unloaded."""
        _LOGGER.debug("Starting coordinator cleanup")

        # Stop websocket listener task
        await self._stop_websocket_listener()

        # Close persistent websocket connection in client
        await self.client._close_websocket()

        _LOGGER.debug("Coordinator cleanup complete - websocket listener and connection closed")
