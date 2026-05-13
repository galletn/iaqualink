"""Tests for the JWT `exp` decoder added by story H9a.

The decoder is a pure module-level function — these tests don't need the
HA event loop or any fixtures. Standard pytest.
"""

from __future__ import annotations

import base64
import datetime
import json

import pytest

from custom_components.iaqualink_robots.coordinator import (
    _TOKEN_EXP_SAFETY_MARGIN_S,
    _decode_jwt_exp,
    _resolve_token_expiry,
)


def _make_jwt(payload: dict, *, signature: str = "fake-signature") -> str:
    """Build a syntactically-valid JWT string with the given payload claims.

    Signature is not verified, so any string after the second dot is fine.
    """
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.{signature}"


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_decodes_future_exp() -> None:
    """A JWT with a future `exp` returns a datetime 60s before that epoch."""
    future_epoch = int(datetime.datetime.now().timestamp()) + 3600  # 1 hour out
    token = _make_jwt({"exp": future_epoch, "sub": "user@example.com"})

    result = _decode_jwt_exp(token)

    assert result is not None
    expected = datetime.datetime.fromtimestamp(future_epoch - _TOKEN_EXP_SAFETY_MARGIN_S)
    # exact match (no tz arithmetic involved)
    assert result == expected


def test_decodes_past_exp() -> None:
    """Past exp still decodes — caller logic (is_token_expired) handles 'expired'."""
    past_epoch = int(datetime.datetime.now().timestamp()) - 3600  # 1 hour ago
    token = _make_jwt({"exp": past_epoch})

    result = _decode_jwt_exp(token)

    assert result is not None
    assert result < datetime.datetime.now()


def test_safety_margin_applied() -> None:
    """The returned datetime is exactly `exp - _TOKEN_EXP_SAFETY_MARGIN_S` seconds."""
    epoch = 1_800_000_000  # arbitrary, well in the future
    token = _make_jwt({"exp": epoch})

    result = _decode_jwt_exp(token)

    assert result is not None
    delta = epoch - int(result.timestamp())
    assert delta == _TOKEN_EXP_SAFETY_MARGIN_S


# ---------------------------------------------------------------------------
# Sad paths — all must return None, never raise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_token,case",
    [
        ("", "empty string"),
        ("not.a.jwt.too.many.parts", "too many segments"),
        ("only.two", "only two segments"),
        ("onepart", "single segment"),
    ],
)
def test_wrong_segment_count_returns_none(bad_token: str, case: str) -> None:
    """A JWT must have exactly 3 dot-separated segments."""
    assert _decode_jwt_exp(bad_token) is None, f"failure case: {case}"


def test_missing_exp_claim_returns_none() -> None:
    """A JWT with no `exp` claim returns None (fall back to default)."""
    token = _make_jwt({"sub": "user@example.com"})  # no exp

    assert _decode_jwt_exp(token) is None


def test_non_numeric_exp_returns_none() -> None:
    """An `exp` claim that isn't a number returns None instead of crashing."""
    token = _make_jwt({"exp": "soon-ish"})

    assert _decode_jwt_exp(token) is None


def test_malformed_base64_returns_none() -> None:
    """Garbage in the payload segment returns None, doesn't raise."""
    bad_token = "header.this_is_not_valid_base64_!@#$.signature"

    assert _decode_jwt_exp(bad_token) is None


def test_valid_base64_but_not_json_returns_none() -> None:
    """Payload segment decodes to bytes but isn't JSON — returns None."""
    not_json = base64.urlsafe_b64encode(b"this is not json").rstrip(b"=").decode()
    bad_token = f"header.{not_json}.signature"

    assert _decode_jwt_exp(bad_token) is None


def test_unpadded_base64_handled() -> None:
    """JWTs strip base64 `=` padding; the decoder must add it back.

    The chosen payload `{"exp": 12345}` is 14 bytes of JSON; base64 of 14
    bytes is 20 chars with 2 stripped trailing `=` pads. The decoder must
    repad to make `urlsafe_b64decode` happy.
    """
    payload = json.dumps({"exp": 12345}).encode()
    assert len(payload) == 14  # noqa: PLR2004 — guards the padding precondition
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    assert len(payload_b64) % 4 != 0, "test setup: payload chosen requires repadding"
    token = f"header.{payload_b64}.sig"

    result = _decode_jwt_exp(token)

    assert result is not None
    assert int(result.timestamp()) == 12345 - _TOKEN_EXP_SAFETY_MARGIN_S


# ---------------------------------------------------------------------------
# Edge cases: bool, non-finite floats, negative/zero exp, list payload,
# non-string token. All must return None, never raise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exp_value,case",
    [
        (True, "bool True (isinstance(True, int) is True)"),
        (False, "bool False"),
        (float("inf"), "positive infinity"),
        (float("-inf"), "negative infinity"),
        (float("nan"), "NaN"),
        (0, "zero epoch"),
        (-1, "negative one"),
        (-1_700_000_000, "large negative epoch"),
    ],
)
def test_pathological_exp_values_return_none(exp_value, case: str) -> None:
    """Tight type/finite/positive guard: bool, inf, nan, <= 0 all fall back."""
    token = _make_jwt({"exp": exp_value})
    assert _decode_jwt_exp(token) is None, f"failure case: {case}"


def test_list_payload_returns_none() -> None:
    """A JWT payload that decodes to a JSON list (not dict) must NOT crash."""
    list_payload = json.dumps([1, 2, 3]).encode()
    payload_b64 = base64.urlsafe_b64encode(list_payload).rstrip(b"=").decode()
    token = f"header.{payload_b64}.sig"

    assert _decode_jwt_exp(token) is None


@pytest.mark.parametrize("bad_token", [None, 123, 1.5, [], {}])
def test_non_string_token_returns_none(bad_token) -> None:
    """A non-string token must NOT raise AttributeError/TypeError."""
    assert _decode_jwt_exp(bad_token) is None


# ---------------------------------------------------------------------------
# _resolve_token_expiry — the wrapper that combines decode + fallback + warn.
# This is where AC #2's "warning logged" requirement is locked in.
# ---------------------------------------------------------------------------


def test_resolve_token_expiry_uses_decoded_exp_when_valid() -> None:
    """A parseable token returns the decoded expiry, no warning emitted."""
    import logging

    future_epoch = int(datetime.datetime.now().timestamp()) + 3600
    token = _make_jwt({"exp": future_epoch})

    result = _resolve_token_expiry(token)

    expected = datetime.datetime.fromtimestamp(future_epoch - _TOKEN_EXP_SAFETY_MARGIN_S)
    assert result == expected


def test_resolve_token_expiry_falls_back_and_warns(caplog) -> None:
    """A malformed token returns `now + 1h - margin` AND logs a warning."""
    import logging

    caplog.set_level(logging.WARNING, logger="custom_components.iaqualink_robots.coordinator")
    before = datetime.datetime.now()

    result = _resolve_token_expiry("garbage")

    # Warning was emitted.
    assert any(
        "JWT exp claim missing or unparseable" in record.message
        for record in caplog.records
    ), f"expected warning not found in caplog: {[r.message for r in caplog.records]}"
    # Result is roughly now + 1h - margin (allow a few seconds of test runtime slack).
    expected = before + datetime.timedelta(hours=1) - datetime.timedelta(
        seconds=_TOKEN_EXP_SAFETY_MARGIN_S
    )
    delta = abs((result - expected).total_seconds())
    assert delta < 5, f"fallback expiry off by {delta}s (expected near {expected}, got {result})"


def test_resolve_token_expiry_fallback_applies_safety_margin() -> None:
    """The fallback branch subtracts the same safety margin as the happy path,
    so the two branches don't have asymmetric effective lifetimes.
    """
    before = datetime.datetime.now()
    result = _resolve_token_expiry(None)
    naive_no_margin = before + datetime.timedelta(hours=1)

    # Result must be EARLIER than naive `now + 1h` by approximately the margin.
    drift = (naive_no_margin - result).total_seconds()
    assert _TOKEN_EXP_SAFETY_MARGIN_S - 5 < drift < _TOKEN_EXP_SAFETY_MARGIN_S + 5, (
        f"fallback margin drift {drift}s outside expected ~{_TOKEN_EXP_SAFETY_MARGIN_S}s"
    )
