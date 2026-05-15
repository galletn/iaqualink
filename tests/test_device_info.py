"""DeviceInfo tests (story P9).

Lock in that every entity platform — vacuum, sensors, buttons — returns the
same ``DeviceInfo`` identifiers for a given robot. Before P9 the three
platforms each built their own dict in-place with diverging ``name`` /
``manufacturer`` / ``model`` / ``sw_version`` values and (in the button
case) a private-attribute reach-through to ``client._model``. P9
consolidates the whole device-registry contract into ``build_device_info``
in ``custom_components/iaqualink_robots/device.py`` and the entity classes
now defer to it.

The first test below is the load-bearing one: if any platform's
``device_info`` returns a different ``identifiers`` set than the others
for the same robot, HA's device registry creates separate device cards
and the user sees their entities split across two cards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tests.const import MOCK_DEVICE_TYPE, MOCK_SERIAL


def _coordinator_for(
    *,
    serial: str = MOCK_SERIAL,
    device_type: str = MOCK_DEVICE_TYPE,
    title: str | None = "Backyard Robot",
    data: dict | None = None,
) -> MagicMock:
    """Build a MagicMock coordinator with the surface ``build_device_info`` reads."""
    coordinator = MagicMock()
    coordinator.title = title
    coordinator.data = data if data is not None else {"model": "VRX iQ+", "sw_version": "3.2.1"}
    coordinator.client = MagicMock()
    coordinator.client.serial = serial
    coordinator.client.device_type = device_type
    coordinator.client.robot_id = serial or "fallback-id"
    return coordinator


def test_build_device_info_returns_typed_DeviceInfo() -> None:
    """The helper returns ``homeassistant.helpers.device_registry.DeviceInfo``.

    Shape check rather than ``isinstance``: ``DeviceInfo`` is a ``TypedDict``
    (PEP 589) and ``isinstance(obj, DeviceInfo)`` raises ``TypeError`` at
    runtime -- TypedDicts intentionally don't support instance / class
    checks. We instead assert the shape: it's a ``dict`` and carries the
    required ``DeviceInfo`` keys. The remaining tests in this module exercise
    the value semantics for each key.
    """
    from custom_components.iaqualink_robots.device import build_device_info

    info = build_device_info(_coordinator_for())
    assert isinstance(info, dict), (
        f"build_device_info must return a DeviceInfo-shaped mapping, got {type(info).__name__}"
    )
    # DeviceInfo's required-for-merging field is ``identifiers``; the others
    # are total=False. Asserting ``identifiers`` is present is the smallest
    # contract that prevents a regression to a bare/empty dict.
    assert "identifiers" in info


def test_build_device_info_identifiers_keyed_on_serial() -> None:
    """``identifiers`` is ``{(DOMAIN, client.serial)}`` so HA can merge all platforms."""
    from custom_components.iaqualink_robots.const import DOMAIN
    from custom_components.iaqualink_robots.device import build_device_info

    info = build_device_info(_coordinator_for(serial="ROBOT-A-123"))
    assert info["identifiers"] == {(DOMAIN, "ROBOT-A-123")}


def test_build_device_info_falls_back_to_robot_id_when_serial_blank() -> None:
    """Pre-discovery, ``client.serial`` is ``""``; identifiers must still be stable.

    Aligns with ``client.robot_id`` (which returns ``serial or username``)
    so a config entry mid-setup doesn't create a transient "empty-serial"
    device card that vanishes after discovery completes.
    """
    from custom_components.iaqualink_robots.const import DOMAIN
    from custom_components.iaqualink_robots.device import build_device_info

    coord = _coordinator_for(serial="")
    coord.client.robot_id = "user@example.com"  # robot_id falls back to username
    info = build_device_info(coord)
    assert info["identifiers"] == {(DOMAIN, "user@example.com")}


def test_build_device_info_name_uses_coordinator_title() -> None:
    """``name`` is the friendly entry title — not the device-type-and-serial fallback.

    Resolves the M15-deferred asymmetry where ``button.py`` used
    ``client.robot_name`` (always ``"<device_type>_<serial>"``) while
    ``sensor.py`` already used ``coordinator.title``. Post-P9 every
    platform reads ``coordinator.title`` via the shared helper.
    """
    from custom_components.iaqualink_robots.device import build_device_info

    info = build_device_info(_coordinator_for(title="Pool Robot Alpha"))
    assert info["name"] == "Pool Robot Alpha"


def test_build_device_info_name_falls_back_when_title_is_none() -> None:
    """Pre-``__init__.py``-line, ``coordinator.title`` is ``None``; fall to robot_id.

    Preserves M15's behavior-equivalence with the prior
    ``getattr(coord, "_title", robot_id)`` pattern: ``None`` falls
    through to robot_id, but an explicit empty string is preserved
    verbatim (we do **not** use ``or`` here).
    """
    from custom_components.iaqualink_robots.device import build_device_info

    coord = _coordinator_for(title=None)
    coord.client.robot_id = "ROBOT-A-123"
    info = build_device_info(coord)
    assert info["name"] == "ROBOT-A-123"


def test_build_device_info_manufacturer_is_zodiac() -> None:
    """All platforms now report ``"Zodiac"``.

    Fixes the pre-P9 split where ``vacuum.py`` returned the lowercase
    ``"iaqualink"`` while ``sensor.py`` and ``button.py`` returned
    ``"Zodiac"``. Zodiac is the manufacturing parent company (URLs in
    ``const.py``: ``prod.zodiac-io.com``); lowercase ``iaqualink`` was
    a stray inconsistency.
    """
    from custom_components.iaqualink_robots.device import build_device_info

    info = build_device_info(_coordinator_for())
    assert info["manufacturer"] == "Zodiac"


def test_build_device_info_model_prefers_coordinator_data_then_device_type() -> None:
    """``model`` resolves in priority: ``data["model"]`` → ``device_type`` → ``"Unknown"``.

    Before P9, ``button.py`` froze ``model`` once in ``__init__`` using
    ``getattr(client, '_model', 'Unknown')`` — a private reach-through
    and stale (never updated post-discovery). The shared helper reads
    ``coordinator.data["model"]`` live every call, with ``device_type``
    as a more-informative fallback than ``"Unknown"`` (e.g., ``"vr"``,
    ``"cyclobat"``) while the model fetch is still pending.
    """
    from custom_components.iaqualink_robots.device import build_device_info

    # Case 1: model present in coordinator.data wins.
    info = build_device_info(_coordinator_for(data={"model": "RA 6500 iQ"}))
    assert info["model"] == "RA 6500 iQ"

    # Case 2: data lacks model -> device_type fallback.
    info = build_device_info(_coordinator_for(device_type="cyclobat", data={}))
    assert info["model"] == "cyclobat"

    # Case 3: data is None and device_type is "" -> "Unknown".
    coord = _coordinator_for(device_type="", data=None)
    info = build_device_info(coord)
    assert info["model"] == "Unknown"

    # Case 4: coordinator writes "Unknown" as the quick-setup placeholder ->
    # treated as falsy so device_type wins (the docstring's promised
    # behavior; without the Unknown/Hidden filter this used to silently
    # return "Unknown" and the device_type fallback was dead code).
    info = build_device_info(_coordinator_for(device_type="vortrax", data={"model": "Unknown"}))
    assert info["model"] == "vortrax"

    # Case 5: coordinator writes "Hidden" for i2d_robot -> treated as falsy
    # so the user sees the device-type instead of a "Hidden" device card.
    info = build_device_info(_coordinator_for(device_type="i2d_robot", data={"model": "Hidden"}))
    assert info["model"] == "i2d_robot"


def test_build_device_info_sw_version_is_none_when_unknown() -> None:
    """``sw_version`` is the device firmware -- ``None`` when not yet fetched.

    Hard rule: we do **not** report the integration's ``manifest.json``
    version (``2.5.1``) as the device's ``sw_version``. The pre-P9
    ``vacuum.py`` did exactly that via ``data.get("sw_version", VERSION)``
    and ``button.py`` hardcoded ``"1.0"``. Both were misleading -- HA
    surfaces this string in the device card as if it were the robot's
    firmware. ``None`` is the honest signal; HA renders nothing.
    """
    from custom_components.iaqualink_robots.device import build_device_info

    info = build_device_info(_coordinator_for(data={}))
    assert info.get("sw_version") is None

    info = build_device_info(_coordinator_for(data={"sw_version": "3.4.5"}))
    assert info["sw_version"] == "3.4.5"


def test_all_three_entity_platforms_share_device_identifiers() -> None:
    """vacuum + sensor + button entities for the same robot share identifiers.

    The load-bearing AC #1 / #4 lock: instantiate one of each entity
    class against the same coordinator and confirm their ``device_info``
    returns identical ``identifiers`` sets. Pre-P9 ``vacuum.py`` keyed
    on ``entry.data["serial_number"]`` while sensor/button keyed on
    ``client.robot_id``; if those ever diverged, HA created two device
    cards. P9 routes all three through ``build_device_info`` so they
    can't diverge.
    """
    from custom_components.iaqualink_robots.button import AqualinkRemoteButton
    from custom_components.iaqualink_robots.const import DOMAIN
    from custom_components.iaqualink_robots.sensor import AqualinkSensor
    from custom_components.iaqualink_robots.vacuum import IAquaLinkRobotVacuum

    coord = _coordinator_for(serial="SHARED-SERIAL-001")
    client = coord.client

    button = AqualinkRemoteButton(coord, client, "forward", "remote_forward", "mdi:x")

    # AqualinkSensor takes (coordinator, sensor_type) tuple as a key,name pair.
    # We instantiate via __new__ to avoid pulling in the full entity wiring.
    sensor = AqualinkSensor.__new__(AqualinkSensor)
    sensor.coordinator = coord
    sensor.client = client

    # IAquaLinkRobotVacuum.__init__ takes (coordinator, client, device_name, serial_number, hass).
    vacuum = IAquaLinkRobotVacuum.__new__(IAquaLinkRobotVacuum)
    vacuum.coordinator = coord  # type: ignore[attr-defined]
    vacuum._client = client
    vacuum._serial_number = "SHARED-SERIAL-001"
    vacuum._name = "Backyard Robot"

    button_ids = button.device_info["identifiers"]  # type: ignore[index]
    sensor_ids = sensor.device_info["identifiers"]
    vacuum_ids = vacuum.device_info["identifiers"]

    expected = {(DOMAIN, "SHARED-SERIAL-001")}
    assert button_ids == expected
    assert sensor_ids == expected
    assert vacuum_ids == expected


def test_two_robots_yield_two_distinct_device_identifiers() -> None:
    """Multi-robot accounts get one device card per robot (AC #4)."""
    from custom_components.iaqualink_robots.const import DOMAIN
    from custom_components.iaqualink_robots.device import build_device_info

    coord_a = _coordinator_for(serial="ROBOT-A", title="Robot A")
    coord_b = _coordinator_for(serial="ROBOT-B", title="Robot B")

    info_a = build_device_info(coord_a)
    info_b = build_device_info(coord_b)

    assert info_a["identifiers"] == {(DOMAIN, "ROBOT-A")}
    assert info_b["identifiers"] == {(DOMAIN, "ROBOT-B")}
    assert info_a["identifiers"] != info_b["identifiers"]
