"""Diagnostics support for iaqualink_robots (story P5).

`async_get_config_entry_diagnostics` is the HA-native hook surfaced in the
device card's "Download diagnostics" button. Returning a redacted snapshot
of the integration's state lets users attach reproducible context to a
GitHub issue without leaking their iAqualink credentials, JWT tokens, or
the full robot serial (a serial is the cloud-account pivot key).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .types import IaqualinkConfigEntry

# Field names that ``async_redact_data`` will replace with ``"**REDACTED**"``
# anywhere they appear in a dict-like payload (entry.data, entry.options,
# coordinator.data, nested websocket frames captured in coordinator state).
#
# Synchronise this set when adding a new credential / token / PII field
# anywhere reachable from the diagnostic payload — there is a test
# (``test_diagnostics_redacts_unknown_token_like_keys``) that asserts no
# key matching ``*token*``, ``*password*``, or ``username`` ever appears
# unredacted in the rendered output. That test plus the centralised set
# is the long-term defence against drift.
#
# ``api_key`` is left here as defence-in-depth even though M17 dropped
# the field from ``entry.data`` at the v2 → v3 migration: a user
# restoring an older HA backup may still have ``api_key`` present in
# their entry until ``async_setup_entry`` strips it on the next setup
# (M19 AC#8). Redacting it from the diagnostic output protects the
# transient window before that strip fires.
TO_REDACT = frozenset({
    "username",
    "password",
    "api_key",
    "id_token",
    "auth_token",
    "refresh_token",
    "first_name",
    "last_name",
    "email",
})


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: IaqualinkConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Shape (top-level keys are stable for tooling; nested keys may grow):

        {
            "entry": {data, options, version, title, state},
            "coordinator": {data, consecutive_failures, last_update_success, ...},
            "client": {device_type, serial_redacted, ws_consecutive_failures, ...},
        }

    The ``coordinator`` and ``client`` blocks are omitted if the integration
    is mid-setup (entry hasn't reached ``async_setup_entry`` yet, so
    ``entry.runtime_data`` isn't populated). That keeps the diagnostic
    useful for "stuck loading" reports too — the user still gets the
    entry block back even when the platforms haven't finished setting up.
    """
    # P3: read coordinator/client from ``entry.runtime_data`` instead of
    # ``hass.data[DOMAIN][entry.entry_id]``. The mid-setup window where the
    # attribute is unset surfaces as the ``getattr(..., None)`` fallback.
    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = runtime_data.coordinator if runtime_data is not None else None
    client = coordinator.client if coordinator is not None else None

    payload: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
            "version": entry.version,
            "title": entry.title,
            "state": str(entry.state),
        },
    }

    if coordinator is not None:
        payload["coordinator"] = {
            "data": async_redact_data(
                dict(coordinator.data) if coordinator.data else {},
                TO_REDACT,
            ),
            "consecutive_failures": getattr(coordinator, "_consecutive_failures", None),
            "last_update_success": getattr(coordinator, "last_update_success", None),
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if getattr(coordinator, "update_interval", None) is not None
                else None
            ),
        }

    if client is not None:
        # ``client._token_expires_at`` is an aware-UTC datetime post-H8.
        # Serialise as ISO so the diagnostic JSON round-trips cleanly.
        # Note: ``model`` is intentionally NOT read from ``client._model``
        # here — the M15 / P9 contract (locked by ``tests/test_static.py``)
        # is that the live model lookup goes through ``coordinator.data``.
        # The model is already surfaced via ``payload["coordinator"]["data"]``
        # above, so duplicating it here would be both a contract violation
        # and noise.
        token_expires_at = getattr(client, "_token_expires_at", None)
        payload["client"] = {
            "device_type": getattr(client, "device_type", None),
            "serial_redacted": _partial_redact(getattr(client, "serial", None)),
            "ws_consecutive_failures": getattr(client, "_ws_consecutive_failures", None),
            "token_expires_at": (
                token_expires_at.isoformat()
                if token_expires_at is not None
                else None
            ),
        }

    return payload


def _partial_redact(value: Any) -> str:
    """Show only the first and last 3 characters of a value.

    Used for the robot serial. Fully redacting it makes the diagnostic
    useless for triage (the maintainer can't correlate cross-issue
    reports against a single device); revealing it in full puts the
    user one cloud-account-pivot step away from a hijack. The 3-on /
    3-off shape keeps enough entropy for disambiguation without
    exposing the full identifier.

    Values **<= 10 chars** collapse to ``"**"`` rather than leaking a
    high fraction of the original. Pre-review threshold was 6, but a
    7-char value yielded ``abc***efg`` — 86% disclosure, defeating the
    partial-redaction purpose. Some i2d-family device serials are 7–9
    chars per the cloud docs, so 10 is the minimum that keeps any
    real-world serial under ~60% disclosure. Long iAqualink serials
    (typically 11–12 chars like ``R23X12345678``) are unaffected.
    """
    if value is None:
        return ""
    text = str(value)
    if len(text) <= 10:
        return "**"
    return f"{text[:3]}***{text[-3:]}"
