"""i2d-family protocol — Zodiac OV 5490 iQ, EX 4000 iQ etc. (story R25-i2d).

The i2d family is the outlier among the five device families: it talks
to the iAqualink cloud over **HTTP POST** instead of the WebSocket
shadow stream every other family uses. Commands are encoded as hex
``params`` strings in the HTTP body (``request=0A1240&timeout=800``
to start, ``request=0A1210`` to stop, etc.), and status responses are
raw 18-byte hex frames the parser unpacks byte-by-byte into a 14-field
``ParsedI2DFrame``.

The fold of R27 (the "138-line god-function" the original team review
called out) lives in this module:

* ``parse_i2d_payload(hex_str: str) -> ParsedI2DFrame`` is a **pure
  function** — no I/O, no logging, no globals beyond the module-level
  lookup tables. Trivially unit-testable by passing hex strings.
* ``STATE_MAP`` / ``ERROR_MAP`` / ``MODE_MAP`` are module-level
  constants; pre-R25 they were inline dicts rebuilt on every parse
  call.
* The side-effects (``client._fan_speed``, ``client._activity``)
  remain inside ``I2DProtocol.parse_status`` for consistency with the
  other 4 protocols' shape — the spec's "set in coordinator" cut was
  evaluated and rejected: keeping side-effects in parse_status matches
  the pattern every other R25 family follows, so the per-family
  asymmetry would have outweighed the purity win.

For the envelope-builder methods (``start_payload`` etc.) the i2d
protocol returns HTTP-body dicts (``{"command", "params", "user_id"}``)
rather than the WebSocket envelopes every other family returns. The
shape difference is intentional and the abstract base's ``dict``
return type accommodates both. The coordinator's command sites know
which transport to use based on ``device_type``: i2d branches send
via ``post_command_i2d(url, request)``, other branches via
``set_cleaner_state(request)``. ``i2d_url(client)`` is an i2d-only
helper that constructs the cloud URL; not part of the abstract base.

Cyclonext-style "no pause command" applies here too — ``pause_payload``
returns the same envelope as ``stop_payload`` because the pre-R25
``pause_cleaning`` method just called ``stop_cleaning`` for i2d.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.vacuum import VacuumActivity
from homeassistant.util import dt as dt_util

from .base import RobotProtocol

if TYPE_CHECKING:
    from ..coordinator import AqualinkClient

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level lookup tables (story R27 fold — hoisted out of the
# pre-R25 god-function so they're constructed once and unit-testable).
# ---------------------------------------------------------------------------

#: ``data_val[2]`` → human display string. Pre-R25 these were title-
#: cased English; preserved as-is for byte-identical contract. Note
#: these are display strings, not the M11 raw activity keys — the
#: M11 contract lives at the higher-level ``result["activity"]``
#: which the parser sets via the ``state_code`` ladder.
STATE_MAP: dict[int, str] = {
    0x01: "Idle / Docked",
    0x02: "Cleaning just started",
    0x03: "Finished",
    0x04: "Actively cleaning",
    0x0C: "Paused",
    0x0D: "Error state D",
    0x0E: "Error state E",
}

#: ``data_val[3]`` → snake_case error key. ``0x00`` is the no-error
#: sentinel; the values double as the raw ``error_state`` key the
#: integration surfaces.
ERROR_MAP: dict[int, str] = {
    0x00: "no_error",
    0x01: "pump_short_circuit",
    0x02: "right_drive_motor_short_circuit",
    0x03: "left_drive_motor_short_circuit",
    0x04: "pump_motor_overconsumption",
    0x05: "right_drive_motor_overconsumption",
    0x06: "left_drive_motor_overconsumption",
    0x07: "floats_on_surface",
    0x08: "running_out_of_water",
    0x0A: "communication_error",
}

#: ``data_val[4] & 0x0F`` → human display string for the cleaning mode.
MODE_MAP: dict[int, str] = {
    0x00: "Quick clean floor only (standard)",
    0x03: "Deep clean floor + walls (high power)",
    0x04: "Waterline only (standard)",
    0x08: "Quick – floor only (standard)",
    0x09: "Custom – floor only (high power)",
    0x0A: "Custom – floor + walls (standard)",
    0x0B: "Custom – floor + walls (high power)",
    0x0C: "Waterline only (standard)",
    0x0D: "Custom – waterline (high power)",
    0x0E: "Custom – waterline (standard)",
}

#: i2d HTTP params (hex command) per fan-speed translation key. Note
#: the i2d family uses ``walls_only`` (plural, waterline mode) which is
#: distinct from the VR/cyclobat ``wall_only`` (singular, dedicated
#: wall-scrub). The distinction is locked at the translation layer
#: too (see ``tests/test_translations.py::test_fan_speed_states_
#: include_both_wall_and_walls_variants``).
_I2D_FAN_SPEED_TO_PARAMS: dict[str, str] = {
    "walls_only": "0A1284",
    "floor_only": "0A1280",
    "floor_and_walls": "0A1283",
}

#: i2d fan-speed mode_code → fan-speed translation key. Only 3 of the
#: 10 mode_map values map to translation keys the integration surfaces;
#: the rest are inferred or custom modes that map silently.
_I2D_MODE_CODE_TO_FAN_SPEED: dict[int, tuple[int, str]] = {
    0x03: (3, "floor_and_walls"),
    0x00: (1, "floor_only"),
    0x04: (2, "walls_only"),
}

#: Expected length of the i2d hex payload (18 bytes × 2 hex chars).
I2D_PAYLOAD_HEX_LENGTH = 36


# ---------------------------------------------------------------------------
# Parsed-frame dataclass.
# ---------------------------------------------------------------------------


@dataclass
class ParsedI2DFrame:
    """Strongly-typed view over a parsed 18-byte i2d HTTP status payload.

    The dataclass keeps the parser pure (no in-place mutation of a
    coordinator-shaped ``result`` dict) and makes the field set
    explicit at the type level. ``I2DProtocol.parse_status`` lifts the
    dataclass fields into the coordinator's ``result`` dict.
    """

    header: str
    state_code: int
    state: str
    error_code: int
    error: str
    mode_code: int
    cycle: str
    canister_full: bool
    time_remaining: int
    uptime_minutes: int
    total_hours: int
    hardware_id: str
    firmware_id: str


# ---------------------------------------------------------------------------
# Pure parser — no I/O, no globals beyond the module-level lookup
# tables, no ``_LOGGER`` calls.
# ---------------------------------------------------------------------------


def parse_i2d_payload(hex_str: str) -> ParsedI2DFrame:
    """Parse a 36-hex-char i2d status payload into a ``ParsedI2DFrame``.

    Pure function (story R25-i2d AC #5): no logging, no global state,
    no side effects. Raises ``ValueError`` on a malformed hex string
    (wrong length or non-hex chars) so the caller can decide how to
    surface the failure. The pre-R25 inline ``_update_i2d_robot``
    caught both error shapes in the same outer except and set
    ``error_state=update_failed``; the protocol preserves that path
    by catching ``ValueError`` at the call site.

    Each parsed field is documented inline against the 18 byte
    positions in the cloud's response shape.
    """
    hex_str = hex_str.replace(" ", "")
    if len(hex_str) != I2D_PAYLOAD_HEX_LENGTH:
        raise ValueError(
            f"Expected {I2D_PAYLOAD_HEX_LENGTH} hex characters; "
            f"got {len(hex_str)} characters."
        )
    raw = bytes.fromhex(hex_str)  # raises ValueError on non-hex chars

    state_code = raw[2]
    error_code = raw[3]
    mode_byte = raw[4]
    # Lower 4 bits encode the cleaning mode; upper 4 bits encode the
    # canister-full flag (any non-zero value in the upper nibble means
    # the canister is full). Bit-twiddling matches pre-R25 verbatim.
    mode_code = mode_byte & 0x0F
    canister_full = (mode_byte & 0xF0) > 0
    time_remaining = raw[5]
    uptime_minutes = int.from_bytes(raw[6:9], byteorder='little')
    total_hours = int.from_bytes(raw[9:12], byteorder='little')

    return ParsedI2DFrame(
        header=raw[:2].hex(),
        state_code=state_code,
        state=STATE_MAP.get(state_code, f"Unknown (0x{state_code:02X})"),
        error_code=error_code,
        error=ERROR_MAP.get(error_code, f"Unknown (0x{error_code:02X})"),
        mode_code=mode_code,
        cycle=MODE_MAP.get(mode_code, f"Unknown (0x{mode_code:02X})"),
        canister_full=canister_full,
        time_remaining=time_remaining,
        uptime_minutes=uptime_minutes,
        total_hours=total_hours,
        hardware_id=raw[12:15].hex(),
        firmware_id=raw[15:18].hex(),
    )


# ---------------------------------------------------------------------------
# Protocol implementation.
# ---------------------------------------------------------------------------


class I2DProtocol(RobotProtocol):
    """i2d family — Zodiac OV 5490 iQ, EX 4000 iQ and rebrands."""

    namespace = "i2d_robot"

    # -- HTTP transport helper --------------------------------------------

    def i2d_url(self, client: AqualinkClient) -> str:
        """Build the iAqualink HTTP control URL for this client's robot.

        Not part of the abstract base — i2d is the only family that
        uses HTTP POST instead of the WebSocket shadow stream.
        """
        return (
            f"https://r-api.iaqualink.net/v2/devices/{client._serial}/control.json"
        )

    # -- Envelope builders (HTTP-body dicts, not WS envelopes) -----------

    def start_payload(self, client: AqualinkClient) -> dict[str, Any]:
        """Return the HTTP request body that starts an i2d cycle.

        Shape is the HTTP body dict ``{"command", "params", "user_id"}``,
        NOT a WebSocket envelope. The coordinator dispatches based on
        device_type — i2d branches send via ``post_command_i2d``.
        """
        return {
            "command": "/command",
            "params": "request=0A1240&timeout=800",
            "user_id": client._id,
        }

    def stop_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return {
            "command": "/command",
            "params": "request=0A1210&timeout=800",
            "user_id": client._id,
        }

    def pause_payload(self, client: AqualinkClient) -> dict[str, Any]:
        """i2d has no dedicated pause command — pre-R25
        ``pause_cleaning`` for i2d delegated to ``stop_cleaning``. The
        protocol mirrors the same shape: pause returns the stop body.
        """
        return self.stop_payload(client)

    def return_to_base_payload(self, client: AqualinkClient) -> dict[str, Any]:
        return {
            "command": "/command",
            "params": "request=0A1701&timeout=800",
            "user_id": client._id,
        }

    # ``clear_desired_payload`` inherits the base ``None`` default —
    # PR #94's auto-restart fix is VR-only.

    # -- Cloud-encoding helpers --------------------------------------------

    def fan_speed_codes(self) -> dict[str, str]:
        return dict(_I2D_FAN_SPEED_TO_PARAMS)

    def set_fan_speed_payload(
        self, client: AqualinkClient, fan_speed: str
    ) -> dict[str, Any] | None:
        """Override the base default — i2d uses the HTTP params string
        keyed off the translation key, NOT a WebSocket envelope.
        """
        codes = self.fan_speed_codes()
        if fan_speed not in codes:
            return None
        return {
            "command": "/command",
            "params": f"request={codes[fan_speed]}&timeout=800",
            "user_id": client._id,
        }

    def extract_fan_speed_from_response(
        self,
        payload: dict[str, Any],
        requested_fan_speed: str,
    ) -> str | None:
        """i2d HTTP responses don't carry a "confirmed fan-speed" field
        in the way the WS families do (the WS reply echoes ``prCyc``
        / ``mode`` / ``cycle``; i2d returns a flat success/failure dict).
        The next polled status frame surfaces the cloud's actual mode
        via ``parse_status`` instead. Returns ``None`` to signal the
        caller should use the requested value as-is.
        """
        return None

    # -- Status parser -----------------------------------------------------

    def parse_status(
        self,
        client: AqualinkClient,
        data: Any,
        result: dict[str, Any],
    ) -> None:
        """Parse an i2d HTTP status response into ``result`` in place.

        ``data`` is the dict returned by ``post_command_i2d`` (the
        HTTP transport layer), NOT a WebSocket payload. Extracts the
        ``command.response`` hex string, validates the request was the
        status request (``OA11``), routes through the pure
        ``parse_i2d_payload`` helper, and writes the parsed-frame
        fields into ``result``. Also computes the M11-compatible raw
        ``activity`` key from the state+error code combination and
        sets ``client._activity`` / ``client._fan_speed`` for the
        vacuum entity's enum mirror.

        The ``error → activity`` mapping is unique to i2d: a non-zero
        ``error_code`` flips activity to ``"error"`` (the only family
        that does this — VR/Vortrax/cyclobat/cyclonext route errors
        through their ``error_state`` field but keep activity at
        idle/cleaning/returning).
        """
        # Seed default-not-set keys so the caller always sees a
        # well-formed result even on parse failure.
        result.setdefault("serial_number", str(client._serial))
        result.setdefault("device_type", str(client._device_type))
        result.setdefault("status", "offline")

        try:
            if data.get("command", {}).get("request") != "OA11":
                # Not a status response — the caller asked for a
                # non-status command (e.g. a fan-speed set ACK).
                # Nothing to parse out of the body.
                return

            result["status"] = "connected"
            debug = data.get("command", {}).get("response", "")
            result["debug"] = str(debug) if client._debug_mode else ""

            frame = parse_i2d_payload(debug)

            # Lift the parsed-frame fields into the result dict.
            result["header"] = frame.header
            result["state_code"] = str(frame.state_code)
            result["state"] = frame.state
            result["error_code"] = str(frame.error_code)
            result["error"] = frame.error
            result["mode_code"] = str(frame.mode_code)
            result["cycle"] = frame.cycle
            result["mode"] = frame.cycle  # i2d reuses cycle for mode display
            result["canister"] = "100" if frame.canister_full else "0"
            result["time_remaining"] = str(frame.time_remaining)
            result["uptime_minutes"] = str(frame.uptime_minutes)
            result["total_hours"] = str(frame.total_hours)
            result["hardware_id"] = str(frame.hardware_id)
            result["firmware_id"] = str(frame.firmware_id)

            # Fan-speed side-effect: only the 3 known mode codes map
            # to translation keys; other modes don't surface a
            # ``fan_speed`` in the polled path (pre-R25 behaviour).
            fan_mapping = _I2D_MODE_CODE_TO_FAN_SPEED.get(frame.mode_code)
            if fan_mapping is not None:
                client._fan_speed, result["fan_speed"] = fan_mapping

            # Activity decoding: state_code 0x02 or 0x04 → cleaning;
            # any non-zero error → "error" (unique to i2d among the 5
            # families); else idle.
            if frame.state_code in (0x02, 0x04):
                client._activity = VacuumActivity.CLEANING
                result["activity"] = "cleaning"
            elif frame.error_code != 0x00:
                client._activity = VacuumActivity.ERROR
                result["activity"] = "error"
                result["error_state"] = frame.error
            else:
                client._activity = VacuumActivity.IDLE
                result["activity"] = "idle"
                result["error_state"] = "no_error"

            # H8: aware-UTC datetime for the estimated end time, only
            # when we're actively cleaning and have remaining minutes.
            if frame.time_remaining > 0 and client._activity == VacuumActivity.CLEANING:
                result["estimated_end_time"] = client.add_minutes_to_datetime(
                    dt_util.utcnow(), frame.time_remaining
                )
                result["time_remaining_human"] = client._format_time_human(
                    frame.time_remaining // 60, frame.time_remaining % 60, 0
                )

        except Exception as e:
            if client._debug_mode:
                _LOGGER.error(f"Error updating i2d robot status: {e}")
            if isinstance(data, dict) and data.get("status") == "500":
                result["status"] = "offline"
            result["debug"] = str(data) if client._debug_mode else ""
            result["error_state"] = "update_failed"
