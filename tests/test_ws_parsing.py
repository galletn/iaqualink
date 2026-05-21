"""Inbound WS payload classifier tests for story P12.

P12 tightens the loose ``dict[str, Any]`` annotations the WebSocket
listener operates on into explicit ``TypedDict``s, with rate-limited
DEBUG/WARNING logging when the cloud's payload shape drifts.

These tests lock the AC contract:

* AC #1 — every well-known shape (state envelope, ACK, error, heartbeat)
  classifies into the matching ``TypedDict``.
* AC #2 — unknown payload shapes log a single ``_LOGGER.debug`` and
  return ``None`` (no exception, no false positive).
* AC #3 — a state envelope missing a required top-level field warns
  once per (process, field) pair; subsequent envelopes missing the same
  field do NOT re-warn.
* AC #4 — typing surface stays mypy-strict clean (locked at CI level
  via the static guards in ``tests/test_static.py``; no per-test
  assertion needed here).
* AC #5 — corrupted / non-dict / short payloads return ``None`` without
  raising (the listener stays alive).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from custom_components.iaqualink_robots.ws_envelope import (
    parse_ws_message,
    reset_drift_warnings,
    validate_state_envelope_fields,
)


# ---------------------------------------------------------------------------
# Fixture: clear the module-level drift-warning sets before every test so
# rate-limit state from a previous test doesn't leak.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_drift_warnings() -> None:
    reset_drift_warnings()


# ---------------------------------------------------------------------------
# Shape fixtures — minimal envelopes the cloud is observed to emit.
# ---------------------------------------------------------------------------


def _state_envelope() -> dict[str, Any]:
    """Minimal well-formed StateReported push."""
    return {
        "service": "StateStreamer",
        "event": "StateReported",
        "namespace": "robot",
        "target": "R23X12345678",
        "payload": {
            "state": {
                "reported": {
                    "equipment": {
                        "robot": {
                            "state": 1,
                            "prCyc": 0,
                            "cycleStartTime": 1716290000,
                        }
                    }
                }
            }
        },
    }


def _subscribe_ack() -> dict[str, Any]:
    return {
        "service": "Authorization",
        "action": "subscribe",
        "target": "R23X12345678",
        "namespace": "authorization",
    }


def _command_ack() -> dict[str, Any]:
    return {
        "service": "StateController",
        "event": "CommandAccepted",
        "target": "R23X12345678",
    }


def _heartbeat() -> dict[str, Any]:
    return {"type": "heartbeat"}


def _error() -> dict[str, Any]:
    return {"error": "InternalError", "code": 500, "message": "oops"}


# ---------------------------------------------------------------------------
# AC #1 — every well-known shape is recognised.
# ---------------------------------------------------------------------------


def test_state_envelope_recognized() -> None:
    """AC #1: a well-formed StateReported envelope classifies as
    ``WSStateEnvelope`` (returned as the dict, narrowed by typing.cast)."""
    result = parse_ws_message(_state_envelope())
    assert result is not None
    # The cast is a typing affordance; at runtime the value is the dict.
    assert result["service"] == "StateStreamer"
    assert result["event"] == "StateReported"


def test_subscribe_ack_recognized() -> None:
    """AC #1: an Authorization subscribe ACK classifies as ``WSAck``."""
    result = parse_ws_message(_subscribe_ack())
    assert result is not None
    assert result.get("action") == "subscribe"


def test_command_ack_recognized() -> None:
    """AC #1: a StateController CommandAccepted classifies as ``WSAck``."""
    result = parse_ws_message(_command_ack())
    assert result is not None
    assert result.get("service") == "StateController"


def test_heartbeat_recognized() -> None:
    """AC #1: a JSON-encoded heartbeat classifies as ``WSHeartbeat``."""
    result = parse_ws_message(_heartbeat())
    assert result is not None
    assert result.get("type") == "heartbeat"


def test_error_recognized() -> None:
    """AC #1: a server-side error response classifies as ``WSError``."""
    result = parse_ws_message(_error())
    assert result is not None
    assert "error" in result


def test_error_classified_by_code_alone() -> None:
    """AC #1: a top-level ``code`` without ``error`` still classifies as
    ``WSError`` — some cloud error shapes carry only the numeric code."""
    result = parse_ws_message({"code": 503, "message": "service unavailable"})
    assert result is not None


# ---------------------------------------------------------------------------
# AC #2 — unknown shapes log DEBUG once and return None.
# ---------------------------------------------------------------------------


def test_unknown_shape_returns_none(caplog: pytest.LogCaptureFixture) -> None:
    """AC #2: a payload with no recognised marker returns ``None`` and
    logs a single DEBUG line listing the sorted keys.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.iaqualink_robots.ws_envelope")
    result = parse_ws_message({"foo": "bar", "baz": 42})
    assert result is None
    debug_lines = [r for r in caplog.records if r.levelname == "DEBUG"]
    assert any("Unknown WS payload shape" in r.message for r in debug_lines), (
        f"expected an 'Unknown WS payload shape' DEBUG log; got: "
        f"{[r.message for r in debug_lines]}"
    )


def test_unknown_shape_rate_limited(caplog: pytest.LogCaptureFixture) -> None:
    """AC #2 + risks: the SAME unknown shape doesn't log DEBUG twice
    per process — first occurrence logs, subsequent ones short-circuit
    via the ``_warned_unknown_shapes`` set.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.iaqualink_robots.ws_envelope")
    same_shape = {"foo": "bar", "baz": 42}
    parse_ws_message(same_shape)
    parse_ws_message(same_shape)
    parse_ws_message(same_shape)
    debug_lines = [
        r for r in caplog.records
        if r.levelname == "DEBUG" and "Unknown WS payload shape" in r.message
    ]
    assert len(debug_lines) == 1, (
        f"expected exactly 1 DEBUG line for the repeated unknown shape; "
        f"got {len(debug_lines)}"
    )


def test_different_unknown_shapes_each_log_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC #2: two distinct unknown shapes each fire one DEBUG; the
    rate-limit is per key-tuple, not global.
    """
    caplog.set_level(logging.DEBUG, logger="custom_components.iaqualink_robots.ws_envelope")
    parse_ws_message({"alpha": 1})
    parse_ws_message({"beta": 2})
    debug_lines = [
        r for r in caplog.records
        if r.levelname == "DEBUG" and "Unknown WS payload shape" in r.message
    ]
    assert len(debug_lines) == 2


# ---------------------------------------------------------------------------
# AC #3 — missing-field WARNING is rate-limited per field per process.
# ---------------------------------------------------------------------------


def test_state_envelope_missing_field_warns_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC #3: when a state envelope is missing a required top-level
    field, ``validate_state_envelope_fields`` returns ``False`` and
    fires a single WARNING per field per process.
    """
    caplog.set_level(logging.WARNING, logger="custom_components.iaqualink_robots.ws_envelope")
    incomplete = {"service": "StateStreamer", "event": "StateReported"}  # no payload
    assert validate_state_envelope_fields(incomplete) is False  # type: ignore[arg-type]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "payload" in warnings[0].message


def test_state_envelope_missing_field_does_not_re_warn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC #3: calling validate a second time with the same missing field
    does NOT fire a new WARNING. The rate-limit is per (process, field).
    """
    caplog.set_level(logging.WARNING, logger="custom_components.iaqualink_robots.ws_envelope")
    incomplete = {"service": "StateStreamer", "event": "StateReported"}
    validate_state_envelope_fields(incomplete)  # type: ignore[arg-type]
    validate_state_envelope_fields(incomplete)  # type: ignore[arg-type]
    validate_state_envelope_fields(incomplete)  # type: ignore[arg-type]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_validate_resets_after_reset_drift_warnings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The listener's successful-push hook calls ``reset_drift_warnings``
    to clear the rate-limit so a transient drift phase doesn't
    permanently silence the WARNINGs.
    """
    caplog.set_level(logging.WARNING, logger="custom_components.iaqualink_robots.ws_envelope")
    incomplete = {"service": "StateStreamer", "event": "StateReported"}
    validate_state_envelope_fields(incomplete)  # type: ignore[arg-type]
    reset_drift_warnings()
    validate_state_envelope_fields(incomplete)  # type: ignore[arg-type]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 2


def test_validate_well_formed_envelope_returns_true_no_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A complete envelope returns True and fires zero WARNINGs."""
    caplog.set_level(logging.WARNING, logger="custom_components.iaqualink_robots.ws_envelope")
    assert validate_state_envelope_fields(_state_envelope()) is True  # type: ignore[arg-type]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings == []


def test_validate_each_missing_field_warns_independently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If two distinct fields are missing, each fires one WARNING."""
    caplog.set_level(logging.WARNING, logger="custom_components.iaqualink_robots.ws_envelope")
    incomplete = {"service": "StateStreamer"}  # missing event AND payload
    validate_state_envelope_fields(incomplete)  # type: ignore[arg-type]
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    missing_fields = {
        r.message.rsplit(": ", 1)[-1].strip() for r in warnings
    }
    assert missing_fields == {"event", "payload"}


# ---------------------------------------------------------------------------
# AC #5 — corrupted / short / non-dict payloads handled gracefully.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        "",
        "not a dict",
        42,
        3.14,
        [],
        ["a", "list"],
        b"\x00\x01",
        (1, 2, 3),
    ],
)
def test_corrupted_payload_returns_none_without_raising(garbage: object) -> None:
    """AC #5: any non-dict input returns ``None`` without raising —
    the listener must continue past a broken message rather than die.
    """
    result = parse_ws_message(garbage)
    assert result is None


def test_short_payload_classifies_or_returns_none() -> None:
    """AC #5: a one-key dict that doesn't match any known marker returns
    ``None``; a one-key dict that DOES match (heartbeat / error / state)
    still classifies. Locks the partial-payload behaviour.
    """
    # Recognised one-key shapes:
    assert parse_ws_message({"type": "heartbeat"}) is not None
    assert parse_ws_message({"error": "x"}) is not None
    assert parse_ws_message({"code": 500}) is not None
    # Unrecognised one-key shape:
    assert parse_ws_message({"unrelated": "data"}) is None


def test_state_envelope_with_only_marker_keys_still_classifies() -> None:
    """AC #5: a minimal state envelope (just service + event, no payload)
    still classifies as a StateEnvelope — the field-validation step
    catches the missing payload separately.
    """
    minimal = {"service": "StateStreamer", "event": "StateReported"}
    result = parse_ws_message(minimal)
    assert result is not None
    # And the validator catches the missing field.
    assert validate_state_envelope_fields(result) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# AC #4 — typing contract: the classifier return type is a Union the
# downstream code can match-narrow against. Exercised via mypy in CI; the
# tests below cover the runtime classification surface.
# ---------------------------------------------------------------------------


def test_state_envelope_is_distinct_from_ack() -> None:
    """A StateController action ack (service=StateController, action=...)
    must NOT classify as a state envelope — only StateStreamer + StateReported
    does.
    """
    not_state = {"service": "StateController", "action": "setCleaningMode"}
    result = parse_ws_message(not_state)
    assert result is not None
    assert result.get("service") != "StateStreamer" or result.get("event") != "StateReported"


def test_foreign_service_returns_unknown() -> None:
    """A message from a service we don't recognise (not Authorization
    nor StateController) and with no known action/event markers
    classifies as unknown. Locks the conservative default-bucket
    behaviour: we don't accidentally swallow a brand-new shape from
    the cloud just because it has a ``service`` key.
    """
    assert parse_ws_message({"service": "OtherService"}) is None
    assert parse_ws_message({"service": "OtherService", "action": "foo"}) is None


def test_known_action_classifies_even_with_foreign_service() -> None:
    """A ``subscribe`` action on a foreign service is still recognised
    as an ACK — the classifier matches on any one of the ACK markers,
    not all of them.
    """
    result = parse_ws_message({"service": "OtherService", "action": "subscribe"})
    assert result is not None
    assert result.get("action") == "subscribe"
