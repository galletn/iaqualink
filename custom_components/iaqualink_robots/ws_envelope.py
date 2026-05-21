"""TypedDict definitions and classifier for inbound iAqualink WS payloads.

Story P12: tightens the loose ``dict[str, Any]`` annotations the WebSocket
listener operates on into explicit ``TypedDict``s so missing or unexpected
fields surface as actionable DEBUG / WARNING log lines instead of silent
``KeyError``s or wrong state.

The TypedDict shapes are based on observed iAqualink cloud envelopes
across the four WebSocket families (VR, Vortrax, CycloBat, CycloNext) —
i2d uses HTTP, not WebSocket. ``total=False`` is set on every shape
because the cloud can omit any field for any family; the parser layer
already handles missing fields with ``.get()`` defaults, so these types
are documentation + mypy guidance, not runtime contracts.

This module is intentionally pure — no instance state, no I/O. Process-
level state lives in two module-level sets (``_warned_missing_fields``
and ``_warned_unknown_shapes``) that the listener clears via
``reset_drift_warnings()`` on each successful poll so a transient
cloud-side partial-payload phase doesn't permanently silence the
diagnostics.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, TypedDict, cast

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TypedDict definitions — one per inbound shape we classify.
#
# AC #1: the four shapes the spec enumerates. Every field is optional
# (total=False) because the cloud frequently omits fields — the parser
# layer treats absent fields as "no update for this state slot".
# ---------------------------------------------------------------------------


class WSEquipmentRobot(TypedDict, total=False):
    """Per-robot fields inside ``payload.state.reported.equipment.robot``.

    All four WebSocket families' parsers read from this dict; not every
    key applies to every family (e.g. ``prCyc`` is VR/Vortrax, ``mode``
    is CycloNext). Keys collected from the live parsers in
    ``protocols/{vr,vortrax,cyclobat,cyclonext}.py``.
    """

    state: int
    mode: int
    cycle: str
    prCyc: int
    cycleStartTime: int
    stepper: int
    fanSpeed: int
    errorState: int
    totalHours: int
    canister: float
    main: dict[str, Any]
    battery: dict[str, Any]
    errors: dict[str, Any]
    sensors: dict[str, Any]
    stats: dict[str, Any]
    eboxData: dict[str, Any]
    lastCycle: dict[str, Any]
    cycles: dict[str, Any]


class WSReportedState(TypedDict, total=False):
    """The ``payload.state.reported`` block (or ``payload.state.desired`` —
    same shape).

    CycloNext nests under ``robot.1`` instead of ``robot``; the dict-of-
    string-to-WSEquipmentRobot reflects that variation.
    """

    equipment: dict[str, WSEquipmentRobot]


class WSStatePayload(TypedDict, total=False):
    """The ``payload`` block carrying ``state.desired`` + ``state.reported``."""

    state: dict[Literal["desired", "reported"], WSReportedState]
    robot: dict[str, Any]


class WSStateEnvelope(TypedDict, total=False):
    """A ``StateStreamer`` / ``StateReported`` envelope — the only inbound
    shape the integration actually parses for state.
    """

    service: Literal["StateStreamer"]
    event: Literal["StateReported"]
    payload: WSStatePayload
    target: str
    namespace: str


class WSAck(TypedDict, total=False):
    """A subscribe / command acknowledgement from the cloud.

    Service is one of ``"Authorization"`` (subscribe ACK) or
    ``"StateController"`` (command ACK).
    """

    service: str
    action: str
    target: str
    namespace: str
    event: str


class WSError(TypedDict, total=False):
    """A server-side error response from the cloud.

    Field presence varies by error class — the classifier matches on the
    presence of ``error`` or ``code`` at the top level.
    """

    error: str
    code: int
    message: str
    service: str


class WSHeartbeat(TypedDict, total=False):
    """A keepalive message.

    In practice aiohttp delivers WebSocket heartbeats as PING / PONG
    frames at the transport layer, not as ``WSMsgType.TEXT`` JSON
    payloads. This shape is included for completeness should the cloud
    ever emit a JSON-encoded heartbeat over TEXT (some implementations
    do this for proxies that strip control frames).
    """

    type: Literal["heartbeat"]


# Union of all classifier return types except None — exported so callers
# can type-narrow with ``match`` / ``isinstance`` patterns.
WSPayload = WSStateEnvelope | WSAck | WSError | WSHeartbeat


# ---------------------------------------------------------------------------
# Process-level drift telemetry.
#
# AC #2 + AC #3: log DEBUG on unknown shapes and WARNING on missing fields,
# each at most once per (key-tuple / field-name) per process so a chatty
# cloud session doesn't spam the log. ``reset_drift_warnings()`` clears
# both sets and is called by the listener after each successful poll —
# this way a transient partial-payload phase doesn't permanently silence
# the diagnostics.
# ---------------------------------------------------------------------------


_warned_missing_fields: set[str] = set()
_warned_unknown_shapes: set[tuple[str, ...]] = set()
_warned_non_dict_types: set[str] = set()
# Dirty flag: True once any drift warning has been logged since the last
# reset. Gates ``reset_drift_warnings()`` so a healthy WS stream that
# emits hundreds of successful pushes between two drift events doesn't
# wipe the rate-limit state between them — pre-review-fix the reset
# fired unconditionally on every push, so the same field re-warned on
# every drift recurrence (the rate-limit was effectively absent on any
# stream where healthy pushes outnumber drift events, i.e. all real
# streams). See P12 review HIGH #3.
_drift_warnings_logged: bool = False


def reset_drift_warnings() -> None:
    """Clear the per-process shape-drift warning sets.

    Called by the listener after a successful state push so the next
    round of missing fields / unknown shapes fires fresh diagnostics
    after a transient cloud-side partial-payload phase resolves. The
    rate-limit is a noise filter, not a once-and-forget switch.

    No-op when no drift warning has been logged since the last reset
    (dirty-flag gate). Without this gate a healthy stream emitting one
    successful push every few seconds would clear the rate-limit
    constantly, so any drifted message between two healthy pushes would
    re-log every time — defeating the rate-limit's purpose. With the
    gate, drift logs once per "drift cluster" (run of drifted messages),
    not once per drifted message inside a cluster.
    """
    global _drift_warnings_logged
    if not _drift_warnings_logged:
        return
    _warned_missing_fields.clear()
    _warned_unknown_shapes.clear()
    _warned_non_dict_types.clear()
    _drift_warnings_logged = False


# ---------------------------------------------------------------------------
# Classifier — the AC #1 entry point.
# ---------------------------------------------------------------------------


def parse_ws_message(raw: object) -> WSPayload | None:
    """Classify an inbound WebSocket JSON payload into a known typed shape.

    AC #1: returns one of ``WSStateEnvelope`` / ``WSAck`` / ``WSError`` /
    ``WSHeartbeat`` for recognised shapes, or ``None`` for anything else.
    AC #2: unrecognised shapes log a DEBUG line with the sorted key
    tuple, rate-limited so a chatty cloud session doesn't spam the log.
    AC #5: corrupted / non-dict inputs (a stray string, a short payload,
    a JSON number) return ``None`` without raising.

    The classifier is intentionally heuristic + cheap — TypedDict at
    runtime is just a dict, and pydantic-style strict validation would
    add per-message CPU cost on a hot loop. Callers that need stronger
    guarantees should call ``validate_state_envelope_fields()`` on the
    returned envelope.

    Match order (review HIGH #1): state push → ACK → heartbeat → error
    → unknown. ACK detection runs BEFORE the error fallback so a
    success acknowledgement carrying a status ``code: 200`` (the
    dominant cloud success convention) is correctly classified as
    ``WSAck`` rather than swallowed as ``WSError``.
    """
    if not isinstance(raw, dict):
        _debug_log_non_dict(type(raw).__name__)
        return None

    # State pushes — the only shape the parser actually consumes.
    if raw.get("service") == "StateStreamer" and raw.get("event") == "StateReported":
        return cast(WSStateEnvelope, raw)

    # Subscribe / command acknowledgement — checked BEFORE error so a
    # success ACK with ``code: 200`` doesn't fall through to the error
    # fallback (review HIGH #1).
    if (
        raw.get("action") in ("subscribe", "subscribe_ack")
        or raw.get("event") in ("Subscribed", "CommandAccepted")
        or raw.get("service") in ("Authorization", "StateController")
    ):
        return cast(WSAck, raw)

    # JSON-encoded heartbeat (rare — aiohttp normally handles these as
    # transport-layer frames).
    if raw.get("type") == "heartbeat":
        return cast(WSHeartbeat, raw)

    # Server-side error response — ``error`` OR ``code``-only fallback
    # for the case where the cloud emits a numeric error with no
    # ``error`` key. ACKs have already been ruled out above so this
    # branch doesn't capture success codes.
    if raw.get("error") is not None or "code" in raw:
        return cast(WSError, raw)

    _debug_log_unknown_shape(tuple(sorted(raw.keys())))
    return None


def _debug_log_unknown_shape(key_tuple: tuple[str, ...]) -> None:
    """Emit a DEBUG line for an unknown payload shape, rate-limited so
    the same shape doesn't log on every message.
    """
    global _drift_warnings_logged
    if key_tuple in _warned_unknown_shapes:
        return
    _warned_unknown_shapes.add(key_tuple)
    _drift_warnings_logged = True
    _LOGGER.debug("Unknown WS payload shape: keys=%s", list(key_tuple))


def _debug_log_non_dict(type_name: str) -> None:
    """Emit a DEBUG line for a non-dict payload, rate-limited per type.

    Kept in a dedicated namespace (``_warned_non_dict_types: set[str]``)
    separate from ``_warned_unknown_shapes`` so the sentinel for a
    non-dict input can't collide with a real dict whose sorted-key tuple
    happens to share the same shape (review MED — sentinel namespace
    collision).
    """
    global _drift_warnings_logged
    if type_name in _warned_non_dict_types:
        return
    _warned_non_dict_types.add(type_name)
    _drift_warnings_logged = True
    _LOGGER.debug("Unknown WS payload: not a dict, got %s", type_name)


# ---------------------------------------------------------------------------
# Field validation — AC #3 / AC #4 helper.
# ---------------------------------------------------------------------------


# Top-level keys we expect on every well-formed state envelope. Anything
# absent here surfaces as a WARNING the first time per process. Tuple
# (not list) so the contract is immutable.
_STATE_ENVELOPE_REQUIRED_FIELDS: tuple[str, ...] = ("service", "event", "payload")


def validate_state_envelope_fields(
    envelope: WSStateEnvelope | dict[str, Any],
    required: tuple[str, ...] = _STATE_ENVELOPE_REQUIRED_FIELDS,
) -> bool:
    """Check that ``envelope`` carries the required top-level fields.

    AC #3: each missing field fires a single WARNING per process — the
    rate-limit prevents one cloud-shape drift from blowing up the log.
    Returns True if every required field is present **with a non-None
    value**; False otherwise (callers can short-circuit further parsing
    on False).

    A field with a ``None`` value is treated as missing because the
    cloud sometimes emits ``{"payload": null}`` during a partial-render
    drift phase — pre-review the membership check (``field not in
    envelope``) considered that valid, so the WARNING never fired on
    the exact case P12 was built to surface (review HIGH #2). The
    ``envelope.get(field) is None`` check fires the WARNING for both
    truly-missing and null-valued required fields, which is the user-
    facing signal the spec promised.

    Parameter is typed ``WSStateEnvelope | dict[str, Any]`` to match
    ``_parse_ws_state_message``'s widened signature — tests pass plain
    dicts directly and the type union avoids forcing ``# type: ignore``
    at every call site (review LOW).
    """
    missing = [field for field in required if envelope.get(field) is None]
    if not missing:
        return True
    global _drift_warnings_logged
    for field in missing:
        if field in _warned_missing_fields:
            continue
        _warned_missing_fields.add(field)
        _drift_warnings_logged = True
        _LOGGER.warning("WS state envelope missing field: %s", field)
    return False
