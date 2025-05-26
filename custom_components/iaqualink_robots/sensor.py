"""Sensor platform for iaqualinkRobots integration."""

from homeassistant.components.sensor import SensorEntity
from .const import DOMAIN

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
    "time_remaining_human":"mdi:clock-outline",
    "estimated_end_time":  "mdi:calendar-clock",
    "model":               "mdi:information-outline",
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
    ("time_remaining_human","Time Remaining"),
    ("estimated_end_time",  "Estimated End Time"),
    ("model",               "Model"),
]

async def async_setup_entry(hass, entry, async_add_entities):
    """Set up sensors for an entry, filtering out battery unless cyclobat."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator = data["coordinator"]
    client = data["client"]

    # Only include battery_level if this is a cyclobat
    if client._device_type == "cyclobat":
        sensor_types = ALL_SENSOR_TYPES
    else:
        sensor_types = [
            (key, name) for key, name in ALL_SENSOR_TYPES
            if key != "battery_level"
        ]

    entities = [
        AqualinkSensor(coordinator, client, key, name)
        for key, name in sensor_types
    ]
    async_add_entities(entities)

class AqualinkSensor(SensorEntity):
    """Representation of a sensor tied to the vacuum data coordinator."""

    def __init__(self, coordinator, client, key, name):
        self.coordinator = coordinator
        self.client = client
        self._key = key

        device_name = getattr(self.coordinator, "_title", client.robot_id)
        self._attr_name = f"{device_name} {name}"
        self._attr_unique_id = f"{client.robot_id}_{key}"
        self._attr_icon = ICON_MAP.get(key)
        # Set unit if defined
        unit = UNIT_MAP.get(key)
        if unit:
            self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self):
        return self.coordinator.data.get(self._key)

    @property
    def available(self):
        return self.coordinator.last_update_success

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.client.robot_id)},
            "name": getattr(self.coordinator, "_title", self.client.robot_id),
            "manufacturer": "Zodiac",
            "model": self.coordinator.data.get("model"),
        }

    async def async_update(self):
        await self.coordinator.async_request_refresh()
