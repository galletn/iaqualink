"""Device-registry hook for iAqualink Robots (story P9).

Centralises the ``DeviceInfo`` construction so every entity platform --
vacuum, sensor, button -- reports the **same** identifiers, name,
manufacturer, model, and firmware-version values for a given robot.
HA's device registry merges entities into a single device card iff
the ``identifiers`` set matches across them, so keeping that contract
in one place is the load-bearing invariant.

Why this lives in its own module (not on ``coordinator.py``):
the coordinator file is already past 3000 lines and the device-registry
logic is conceptually orthogonal to update orchestration. A separate
module also keeps the story-R26 split easier when that lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import AqualinkDataUpdateCoordinator


def build_device_info(coordinator: "AqualinkDataUpdateCoordinator") -> DeviceInfo:
    """Return the canonical ``DeviceInfo`` for the robot owned by ``coordinator``.

    Identifier resolution:
      - Prefer ``client.serial`` (set after cloud discovery completes).
      - Fall back to ``client.robot_id`` (which itself is ``_serial or
        _username``) so a config entry mid-setup -- before the first
        ``_discover_device`` call returns -- still produces a stable
        device card rather than a transient empty-serial card that
        merges/unmerges as the serial flips in.

    Name resolution mirrors ``sensor.py`` post-M15: ``coordinator.title``
    (set from ``entry.title`` by ``__init__.py``) when not ``None``,
    otherwise ``client.robot_id``. We intentionally do **not** use
    ``coordinator.title or client.robot_id`` -- an empty-string title
    should be preserved verbatim, not silently rewritten to the serial.

    Model resolution prefers the live value off ``coordinator.data``
    (populated by the cloud's model-fetch path) and falls back to
    ``client.device_type`` (e.g. ``"vortrax"``, ``"cyclobat"``) so the
    user sees something meaningful before the first model fetch lands.
    Final fallback is the literal ``"Unknown"``. The placeholder strings
    ``"Unknown"`` and ``"Hidden"`` -- which the coordinator unconditionally
    writes into ``data["model"]`` during quick-setup, on the i2d_robot
    path, and on exhausted-retry caching respectively -- are treated as
    falsy so the ``device_type`` fallback actually fires. Without this,
    every i2d_robot device card would display ``Model: Hidden`` forever
    and every pre-discovery card would display ``Model: Unknown`` even
    when ``device_type`` already disambiguates the family.

    ``sw_version`` is the **device firmware** -- we do not surface the
    integration's manifest version here, even though pre-P9 ``vacuum.py``
    and ``button.py`` both did (with ``VERSION`` and a hardcoded
    ``"1.0"`` respectively). ``None`` is the correct signal when the
    cloud hasn't reported firmware yet; HA renders the device card
    without a firmware line in that case.

    ``configuration_url`` is intentionally omitted: iAqualink robots
    are cloud-only with no local web UI, and no stable customer-portal
    URL exists today. Per AC #3 the field is qualified "if known" and
    we deliberately don't know one.
    """
    client = coordinator.client
    data = coordinator.data or {}

    serial = client.serial or client.robot_id

    if coordinator.title is not None:
        name = coordinator.title
    else:
        name = client.robot_id

    model_raw = data.get("model")
    if model_raw and model_raw not in ("Unknown", "Hidden"):
        model = model_raw
    else:
        model = client.device_type or "Unknown"

    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=name,
        manufacturer="Zodiac",
        model=model,
        sw_version=data.get("sw_version"),
    )
