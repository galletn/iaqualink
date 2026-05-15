"""Story H8 — timezone hygiene tests.

Locks in the contract that every datetime the integration produces is
timezone-aware (UTC), so HA renders timestamps correctly in the user's local
timezone and remaining-time arithmetic is smooth across DST boundaries.

The helpers under test are pure-Python (the JWT decoder is module-level; the
client methods are bypassed via ``__new__`` per the established
``tests/test_set_fan_speed.py`` and ``tests/test_coordinator.py`` convention).
No HA event loop is required for the helper tests; only the integration tests
that go through ``_calculate_times`` need the client wiring.
"""

from __future__ import annotations

import base64
import datetime
import json

import pytest

from custom_components.iaqualink_robots.coordinator import (
    AqualinkClient,
    _TOKEN_EXP_SAFETY_MARGIN_S,
    _decode_jwt_exp,
    _resolve_token_expiry,
)


UTC = datetime.timezone.utc


def _make_jwt(payload: dict) -> str:
    """Build a JWT with the given payload (signature is not verified)."""
    header_b64 = base64.urlsafe_b64encode(
        b'{"alg":"HS256","typ":"JWT"}'
    ).rstrip(b"=").decode()
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload).encode()
    ).rstrip(b"=").decode()
    return f"{header_b64}.{payload_b64}.fake-sig"


def _build_client() -> AqualinkClient:
    """Build a bare AqualinkClient bypassing __init__ (mirrors test_set_fan_speed).

    Sets just enough state for `_calculate_times` / `add_minutes_to_datetime`
    to run without hitting AttributeError. `_hass = None` makes
    `_format_time_human` take its no-translation fallback branch.
    """
    client = AqualinkClient.__new__(AqualinkClient)
    client._device_type = "vr"
    client._serial = "TEST-SERIAL"
    client._debug_mode = False
    client._include_seconds_remaining = True
    client._hass = None
    return client


# ---------------------------------------------------------------------------
# _decode_jwt_exp returns aware-UTC datetimes (H9a-review absorbed item).
# ---------------------------------------------------------------------------


def test_decode_jwt_exp_returns_aware_utc_datetime() -> None:
    """Decoded `exp` is a tz-aware datetime in UTC, not naive-local."""
    future_epoch = int(datetime.datetime.now(tz=UTC).timestamp()) + 3600
    token = _make_jwt({"exp": future_epoch})

    result = _decode_jwt_exp(token)

    assert result is not None
    assert result.tzinfo is not None, "_decode_jwt_exp must return aware datetime"
    assert result.utcoffset() == datetime.timedelta(0), (
        "_decode_jwt_exp must return UTC-anchored datetime"
    )


def test_decode_jwt_exp_uses_utc_epoch() -> None:
    """The aware-UTC datetime matches `epoch - margin` UTC-anchored."""
    epoch = 1_800_000_000
    token = _make_jwt({"exp": epoch})

    result = _decode_jwt_exp(token)

    assert result is not None
    expected = datetime.datetime.fromtimestamp(
        epoch - _TOKEN_EXP_SAFETY_MARGIN_S, tz=UTC
    )
    assert result == expected


# ---------------------------------------------------------------------------
# _resolve_token_expiry — happy + fallback paths both aware-UTC.
# ---------------------------------------------------------------------------


def test_resolve_token_expiry_happy_path_returns_aware_utc() -> None:
    """Parseable token: result inherits aware-UTC from `_decode_jwt_exp`."""
    future_epoch = int(datetime.datetime.now(tz=UTC).timestamp()) + 3600
    token = _make_jwt({"exp": future_epoch})

    result = _resolve_token_expiry(token)

    assert result.tzinfo is not None
    assert result.utcoffset() == datetime.timedelta(0)


def test_resolve_token_expiry_fallback_returns_aware_utc() -> None:
    """Malformed token: fallback `now + 1h - margin` is also aware-UTC."""
    # Reset the rate-limit flag so a prior test can't suppress the fallback path.
    from custom_components.iaqualink_robots import coordinator as coord_mod
    coord_mod._JWT_EXP_FALLBACK_WARNED = False

    result = _resolve_token_expiry("garbage")

    assert result.tzinfo is not None, (
        "fallback path must return aware datetime (H8 fix)"
    )
    assert result.utcoffset() == datetime.timedelta(0)


# ---------------------------------------------------------------------------
# _is_token_expired works with aware `_token_expires_at`.
# ---------------------------------------------------------------------------


def test_is_token_expired_returns_true_when_token_is_in_past() -> None:
    """An aware-UTC `_token_expires_at` in the past is correctly seen as expired."""
    client = _build_client()
    client._token_expires_at = datetime.datetime.now(tz=UTC) - datetime.timedelta(
        hours=1
    )

    assert client._is_token_expired() is True


def test_is_token_expired_returns_false_when_token_is_in_future() -> None:
    """An aware-UTC `_token_expires_at` in the future is correctly seen as valid."""
    client = _build_client()
    client._token_expires_at = datetime.datetime.now(tz=UTC) + datetime.timedelta(
        hours=1
    )

    assert client._is_token_expired() is False


def test_is_token_expired_returns_true_when_token_unset() -> None:
    """`None` (initial state) still reports expired so the auth flow refreshes."""
    client = _build_client()
    client._token_expires_at = None

    assert client._is_token_expired() is True


# ---------------------------------------------------------------------------
# add_minutes_to_datetime — rejects naive, preserves tzinfo.
# ---------------------------------------------------------------------------


def test_add_minutes_to_datetime_rejects_naive_input() -> None:
    """Naive datetimes fail fast at the helper boundary (H8 spec note)."""
    client = _build_client()
    naive = datetime.datetime(2026, 5, 15, 12, 0, 0)  # no tzinfo

    with pytest.raises(AssertionError):
        client.add_minutes_to_datetime(naive, 15)


def test_add_minutes_to_datetime_preserves_utc_tzinfo() -> None:
    """Aware-UTC input produces aware-UTC output."""
    client = _build_client()
    aware = datetime.datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)

    result = client.add_minutes_to_datetime(aware, 15)

    assert result.tzinfo is not None
    assert result.utcoffset() == datetime.timedelta(0)
    assert result == datetime.datetime(2026, 5, 15, 12, 15, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _calculate_times — produces aware datetimes; remaining-time math works.
# ---------------------------------------------------------------------------


def test_calculate_times_emits_aware_datetime_objects() -> None:
    """`result["cycle_end_time"]` / `estimated_end_time` are aware datetime objects.

    Pre-H8 they were ISO strings produced by ``.isoformat()`` on naive datetimes.
    Post-H8 we hand HA real datetime objects so the sensor entity (with
    ``device_class=TIMESTAMP``) renders in user-local time.
    """
    client = _build_client()
    start = datetime.datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    duration = 60  # minutes
    result: dict = {"activity": "cleaning"}

    client._calculate_times(start, duration, result, robot_data=None)

    assert isinstance(result["cycle_end_time"], datetime.datetime)
    assert result["cycle_end_time"].tzinfo is not None
    assert isinstance(result["estimated_end_time"], datetime.datetime)
    assert result["estimated_end_time"].tzinfo is not None


def test_calculate_times_remaining_works_with_aware_inputs() -> None:
    """Remaining-time math runs without naive/aware comparison errors."""
    client = _build_client()
    # Start 30 minutes ago so we know the result is positive and < 30.
    start = datetime.datetime.now(tz=UTC) - datetime.timedelta(minutes=30)
    duration = 60
    result: dict = {"activity": "cleaning"}

    client._calculate_times(start, duration, result, robot_data=None)

    assert "time_remaining" in result
    assert 0 < result["time_remaining"] <= 30


# ---------------------------------------------------------------------------
# DST boundary — UTC has no DST, so the remaining-time delta across a
# notional 02:00→03:00 spring-forward instant has no 1-hour jump.
# ---------------------------------------------------------------------------


def test_dst_boundary_remaining_time_continuous(monkeypatch) -> None:
    """No 1-hour discontinuity in remaining-time math across a DST transition.

    Constructs a cycle starting just before a Northern-hemisphere spring-forward
    instant and monkeypatches ``dt_util.utcnow`` to return three samples
    bracketing the 01:59:59 / 02:00:00 local boundary. Since `_calculate_times`
    runs on aware-UTC throughout, no DST transition exists internally and the
    `time_remaining` delta between successive samples should be exactly 1
    minute, not ~60 (which would indicate a naive-local DST jump leaked in).

    H8 review follow-up — the pre-patch shape didn't actually patch utcnow,
    so the three samples used real wall-clock and trivially differed by ~1
    minute regardless of whether DST math was broken. This test now
    structurally exercises the DST boundary.
    """
    from custom_components.iaqualink_robots import coordinator as coord_mod

    client = _build_client()
    # In Europe/Paris on 2026-03-29, 02:00 local jumps to 03:00. That's
    # 01:00 UTC. Build a cycle that starts at 00:30 UTC and runs 2 hours,
    # so it brackets the local DST instant. Three "now" samples around
    # the 01:00 UTC boundary: 00:59:30, 01:00:00, 01:00:30 UTC.
    start = datetime.datetime(2026, 3, 29, 0, 30, 0, tzinfo=UTC)
    duration = 120  # 2-hour cycle
    sample_nows = [
        datetime.datetime(2026, 3, 29, 0, 59, 30, tzinfo=UTC),
        datetime.datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC),
        datetime.datetime(2026, 3, 29, 1, 0, 30, tzinfo=UTC),
    ]

    samples = []
    for fake_now in sample_nows:
        monkeypatch.setattr(coord_mod.dt_util, "utcnow", lambda fn=fake_now: fn)
        result: dict = {"activity": "cleaning"}
        client._calculate_times(start, duration, result, robot_data=None)
        samples.append(result.get("time_remaining", 0))

    # With aware-UTC math, the three samples are 0.5s apart in real time
    # — when rounded to integer minutes, time_remaining should differ by at
    # most 1 between consecutive samples. A 60-minute jump anywhere would
    # signal a DST regression.
    for prev, curr in zip(samples, samples[1:]):
        assert abs(prev - curr) <= 1, (
            f"remaining-time discontinuity at DST boundary: {samples}"
        )
    # Sanity: at least one of the three samples must be > 0 (cycle is
    # active across all three sample points).
    assert any(s > 0 for s in samples), (
        f"all samples 0 — _calculate_times not exercising the cleaning branch: {samples}"
    )
