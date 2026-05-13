"""Constants shared across the test suite."""

from custom_components.iaqualink_robots.const import DOMAIN as REAL_DOMAIN

# Re-export so tests don't import from the integration directly for this.
DOMAIN = REAL_DOMAIN

MOCK_USERNAME = "test@example.com"
MOCK_PASSWORD = "hunter2"
MOCK_NAME = "Test Pool Robot"
MOCK_SERIAL = "R23X12345678"
MOCK_DEVICE_TYPE = "vortrax"

MOCK_DEVICE_SINGLE: dict = {
    "name": MOCK_NAME,
    "serial_number": MOCK_SERIAL,
    "device_type": MOCK_DEVICE_TYPE,
}

MOCK_DEVICE_SECOND: dict = {
    "name": "Spare Robot",
    "serial_number": "R23X87654321",
    "device_type": "vr",
}

# Tests rely on these two fixtures representing distinct robots; without
# distinct serials the duplicate-via-select_device test silently degenerates.
assert MOCK_DEVICE_SINGLE["serial_number"] != MOCK_DEVICE_SECOND["serial_number"]

MOCK_USER_INPUT: dict = {
    "name": MOCK_NAME,
    "username": MOCK_USERNAME,
    "password": MOCK_PASSWORD,
}

MOCK_ENTRY_DATA: dict = {
    "name": MOCK_NAME,
    "username": MOCK_USERNAME,
    "password": MOCK_PASSWORD,
    "api_key": "EOOEMOW4YR6QNB07",  # see const.py API_KEY
    "serial_number": MOCK_SERIAL,
    "device_type": MOCK_DEVICE_TYPE,
}
