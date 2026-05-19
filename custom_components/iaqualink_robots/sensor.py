"""Sensor platform for iaqualink_robots integration."""

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from .const import DOMAIN
from .device import build_device_info

# Sensor keys that surface an aware-UTC datetime from the coordinator (H8).
# Marking these with device_class=TIMESTAMP makes HA's frontend render the
# value in the user's local timezone instead of as a raw ISO string.
_TIMESTAMP_SENSOR_KEYS = frozenset({
    "cycle_start_time",
    "estimated_end_time",
    "last_online",
})

_LOGGER = logging.getLogger(__name__)

ICON_MAP = {
    "serial_number":       "mdi:barcode",
    "device_type":         "mdi:robot",
    "cycle_start_time":    "mdi:clock-start",
    "cycle_duration":      "mdi:timer-sand",
    "cycle":               "mdi:format-list-numbered",
    "battery_level":       "mdi:battery",
    "total_hours":         "mdi:timer",
    "canister":            "mdi:recycle",
    "error_state":         "mdi:alert-circle",
    "temperature":         "mdi:thermometer",
    "time_remaining_human": "mdi:clock-outline",
    "time_remaining":      "mdi:timer",
    "estimated_end_time":  "mdi:calendar-clock",
    "model":               "mdi:information-outline",
    "fan_speed":           "mdi:fan",
    "activity":            "mdi:robot-vacuum",
    "status":              "mdi:connection",
    "stepper":             "mdi:stairs",
    "stepper_adj_time":    "mdi:timer-plus",
    "base_cycle_duration": "mdi:timer-sand",
    "stepper_adjustment_minutes": "mdi:timer-plus-outline",
    "adjusted_cycle_duration": "mdi:timer",
}

# Unit of measurement map
UNIT_MAP = {
    "battery_level":       "%",
    "total_hours":         "h",
    "canister":            "%",
    "temperature":         "°C",
    "cycle_duration":      "min",
    "time_remaining":      "min",  # Numeric minutes
    "time_remaining_human": None,  # Human readable string, no unit
    "stepper_adj_time":    "min",
    "base_cycle_duration": "min",
    "stepper_adjustment_minutes": "min",
    "adjusted_cycle_duration": "min",
}

# All possible sensors
ALL_SENSOR_TYPES = [
    ("serial_number",       "Serial Number"),
    ("device_type",         "Device Type"),
    ("cycle_start_time",    "Cycle Start Time"),
    ("cycle_duration",      "Cycle Duration"),
    ("cycle",               "Cycle"),
    ("battery_level",       "Battery Level"),
    ("total_hours",         "Total Hours"),
    ("canister",            "Canister Level"),
    ("error_state",         "Error State"),
    ("temperature",         "Temperature"),
    ("time_remaining_human", "Time Remaining"),
    ("time_remaining",      "Time Remaining (Minutes)"),
    ("estimated_end_time",  "Estimated End Time"),
    ("model",               "Model"),
    ("fan_speed",           "Fan Speed"),
    ("activity",            "Activity"),
    ("status",              "Status"),
    ("stepper",             "Time Adjustments"),
    ("stepper_adj_time",    "Adjustment Increment"),
    ("base_cycle_duration", "Original Duration"),
    ("stepper_adjustment_minutes", "Time Added/Removed"),
    ("adjusted_cycle_duration", "Total Duration"),
]


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up sensors for an entry, filtering based on robot type."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]

    # Efficiently filter sensor types based on device type
    device_type = client.device_type

    if device_type == "cyclobat":
        # Include all sensors for cyclobat
        sensor_types = ALL_SENSOR_TYPES
    elif device_type == "i2d_robot":
        # Exclude specified sensors for i2d robots
        excluded_sensors = {"cycle_duration", "cycle_start_time", "model", "temperature", "battery_level"}
        sensor_types = [
            (key, name) for key, name in ALL_SENSOR_TYPES
            if key not in excluded_sensors
        ]
    else:
        # For other types, just exclude battery
        sensor_types = [
            (key, name) for key, name in ALL_SENSOR_TYPES
            if key != "battery_level"
        ]

    entities = [
        AqualinkSensor(coordinator, client, key, name)
        for key, name in sensor_types
    ]
    async_add_entities(entities)


class AqualinkSensor(CoordinatorEntity, SensorEntity):
    """Representation of a sensor tied to the vacuum data coordinator."""

    def __init__(self, coordinator, client, key, name):
        super().__init__(coordinator)
        self.coordinator = coordinator
        self.client = client
        self._key = key
        self._last_value = None  # Track last value for change detection

        # Use Home Assistant's translation system for sensor names
        # Don't set _attr_name - let HA translate using translation_key
        # This allows friendly names to be translated while keeping entity IDs stable
        self._attr_unique_id = f"{client.robot_id}_{key}"
        self._attr_icon = ICON_MAP.get(key)
        self._attr_should_poll = False  # Use coordinator updates only
        # Set unit if defined
        unit = UNIT_MAP.get(key)
        if unit:
            self._attr_native_unit_of_measurement = unit
        # H8: HA's frontend renders timestamp entities in the user's local tz
        # when device_class=TIMESTAMP and native_value is an aware datetime.
        if key in _TIMESTAMP_SENSOR_KEYS:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

        # Set translation key for entity name (HA will handle translation)
        self._attr_translation_key = key

        # Set device name prefix for the entity (this gets translated too)
        self._attr_has_entity_name = True

    @property
    def native_value(self):
        """Return the current value with resilient handling for temporary data unavailability."""
        # Check if we have coordinator data and it's not in an error state
        if self.coordinator.data:
            # Check if this is a no_data or connection error state - preserve cached values in these cases
            error_state = self.coordinator.data.get("error_state")
            if error_state in ["no_data", "update_failed", "setup_cancelled", "connection_failed"]:
                # During connection/data errors, return last known value to preserve sensor state
                cached_value = getattr(self, '_last_value', None)
                if cached_value is not None:
                    _LOGGER.debug(
                        f"Sensor {self._key} preserving cached value '{cached_value}' during {error_state} error")
                    return cached_value
                # If no cached value, fall through to try getting current data

            # M11: native_value returns raw cloud keys (e.g. "cleaning",
            # "floor_only", "connected"). HA's frontend looks up
            # ``entity.sensor.<key>.state.<value>`` from the locale file via
            # the ``translation_key`` declared at __init__ and renders the
            # localized text. Pre-M11 we title-cased the value here, which
            # bypassed translation_key entirely (locale files were dead
            # text) AND broke ``is_state(..., 'cleaning')`` for users.
            current_value = self.coordinator.data.get(self._key)

            # Only update cached value if we have valid current data (not None and not "unknown")
            if current_value is not None and current_value != "unknown":
                # Log value changes for important sensors to help debug update timing
                if (self._key in ["fan_speed", "activity", "status", "time_remaining"] and
                        current_value != getattr(self, '_last_value', None)):
                    _LOGGER.debug(
                        f"Sensor {self._key} value changed: {getattr(self, '_last_value', None)} -> {current_value}")

                self._last_value = current_value
                return current_value
            else:
                # If current value is None or "unknown", return cached value if available
                cached_value = getattr(self, '_last_value', None)
                if cached_value is not None:
                    _LOGGER.debug(
                        f"Sensor {self._key} using cached value '{cached_value}' instead of '{current_value}'")
                    return cached_value
                # If no cached value and current is None/unknown, return the current value anyway
                return current_value
        else:
            # During temporary connection issues, return last known value
            # This prevents sensors from showing "unknown" during brief outages
            return getattr(self, '_last_value', None)

    @property
    def available(self):
        """Sensor stays available across short cloud blips, unavailable on long outage.

        H7: pre-rewrite this only checked ``coordinator.data is not None``,
        so sensors stayed available indefinitely as the coordinator served
        stale ``_last_data`` through the broad-except path. The new
        ``coordinator.is_long_outage`` property flips to True once the
        outage exceeds ``LONG_OUTAGE_THRESHOLD_SECONDS``, so user
        automations bound to ``available`` no longer break on ISP blips
        but are still honestly notified of a real multi-minute outage.
        """
        return (
            self.coordinator.data is not None
            and not self.coordinator.is_long_outage
        )

    @property
    def extra_state_attributes(self):
        """Expose the ``restored`` flag (H7, AC #3).

        ``True`` whenever the sensor's last update came from the
        coordinator's cached ``_last_data`` rather than a fresh poll —
        i.e. the integration is in a transient outage and returning
        stale values. Power users can branch automations on this:
        ``{{ state_attr('sensor.x', 'restored') }}``. Flips back to
        ``False`` automatically on the next successful poll.
        """
        return {"restored": self.coordinator.is_serving_stale_data}

    @property
    def device_info(self):
        return build_device_info(self.coordinator)
