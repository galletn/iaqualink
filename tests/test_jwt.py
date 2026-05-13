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

    Chosen payload size 13 bytes -> base64 length 18 with 2 stripped padding
    chars, so the decoder must repad to make `urlsafe_b64decode` happy.
    """
    payload = json.dumps({"exp": 12345}).encode()
    assert len(payload) == 14  # noqa: PLR2004 — guards the padding precondition
    payload_b64 = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    assert len(payload_b64) % 4 != 0, "test setup: payload chosen does not require padding"
    token = f"header.{payload_b64}.sig"

    result = _decode_jwt_exp(token)

    assert result is not None
    assert int(result.timestamp()) == 12345 - _TOKEN_EXP_SAFETY_MARGIN_S
