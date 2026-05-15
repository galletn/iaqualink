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

# Maximum age (seconds) of a `_pending_stop_reset` snapshot before it is
# discarded as stale (story H6). Pre-H6 the reset was applied on the very
# next poll regardless of elapsed time, which meant a user who restarted
# cleaning between issuing stop and the next poll would see their live
# "cleaning" state silently overwritten back to "idle". 10 s is well above
# typical cloud roundtrips (≈1 s on the websocket path, ≤3 s on REST) but
# short enough that a reset queued before a long disconnect cannot zombie-
# apply minutes later. Tune in soak via this constant rather than the
# inline magic-number.
PENDING_STOP_RESET_MAX_AGE_SECONDS: Final = 10

# User-toggleable option keys (configurable via config flow + options flow).
# Whether the `time_remaining_human` sensor includes seconds. Default True
# preserves the historical behavior; users complaining about activity-log
# churn can flip it off via Settings → Devices & Services → Configure to
# get minute-granular updates without slowing down the global SCAN_INTERVAL
# (which would also slow remote-control button responsiveness).
CONF_INCLUDE_SECONDS_REMAINING: Final = "include_seconds_remaining"
DEFAULT_INCLUDE_SECONDS_REMAINING: Final = True

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
