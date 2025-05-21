"""Constants used by IaqualinkRobots."""
import json
from datetime import timedelta
from pathlib import Path
from typing import Final
from homeassistant.const import Platform

PLATFORMS: Final = [Platform.VACUUM, Platform.SENSOR]

# API endpoints
URL_LOGIN="https://prod.zodiac-io.com/users/v1/login"
URL_GET_DEVICES="https://r-api.iaqualink.net/devices.json"
URL_GET_DEVICE_STATUS="https://prod.zodiac-io.com/devices/v1/"
URL_GET_DEVICE_FEATURES="https://prod.zodiac-io.com/devices/v2/"
URL_FEATURES = "https://prod.zodiac-io.com/devices/v2/"
URL_WS = "wss://prod-socket.zodiac-io.com/devices"

# API key is constant for all iAqualink devices
API_KEY = "EOOEMOW4YR6QNB07"

SCAN_INTERVAL = timedelta(seconds=30)

manifestfile = Path(__file__).parent / "manifest.json"
with open(manifestfile) as json_file:
    manifest_data = json.load(json_file)

DOMAIN = manifest_data.get("domain")
NAME = manifest_data.get("name")
VERSION = manifest_data.get("version")
ISSUEURL = manifest_data.get("issue_tracker")
STARTUP = """
-------------------------------------------------------------------
{name}
Version: {version}
This is a custom component
If you have any issues with this you need to open an issue here:
{issueurl}
-------------------------------------------------------------------
""".format(
    name=NAME, version=VERSION, issueurl=ISSUEURL
)
