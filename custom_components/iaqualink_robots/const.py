"""Constants used by IaqualinkRobots."""
import json
from datetime import timedelta
from pathlib import Path
from typing import Final
from homeassistant.const import Platform

PLATFORMS: Final = [Platform.VACUUM, Platform.SENSOR, Platform.BUTTON]

# API endpoints - cleaned up duplicates
URL_LOGIN: Final = "https://prod.zodiac-io.com/users/v1/login"
URL_GET_DEVICES: Final = "https://r-api.iaqualink.net/devices.json"
URL_GET_DEVICE_FEATURES: Final = "https://prod.zodiac-io.com/devices/v2/"
URL_WS: Final = "wss://prod-socket.zodiac-io.com/devices"

# API key is constant for all iAqualink devices
API_KEY: Final = "EOOEMOW4YR6QNB07"

SCAN_INTERVAL: Final = timedelta(seconds=3)  # Balanced update interval for efficiency
# Real-time websocket listener provides instant updates, moderate polling backup

# Load manifest data efficiently
def _load_manifest_data():
    """Load manifest data once and cache it."""
    manifestfile = Path(__file__).parent / "manifest.json"
    with open(manifestfile, encoding="utf-8") as json_file:
        return json.load(json_file)

_MANIFEST_DATA = _load_manifest_data()

DOMAIN: Final = _MANIFEST_DATA.get("domain")
NAME: Final = _MANIFEST_DATA.get("name")
VERSION: Final = _MANIFEST_DATA.get("version")
ISSUEURL: Final = _MANIFEST_DATA.get("issue_tracker")

STARTUP: Final = f"""
-------------------------------------------------------------------
{NAME}
Version: {VERSION}
This is a custom component
If you have any issues with this you need to open an issue here:
{ISSUEURL}
-------------------------------------------------------------------
"""
