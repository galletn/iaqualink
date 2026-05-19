"""Constants used by IaqualinkRobots."""
from datetime import timedelta
from typing import Final
from homeassistant.const import Platform

# L22: ``DOMAIN`` is hardcoded as a string literal — not read from
# ``manifest.json`` at import time — so the integration does no synchronous
# file I/O on module load. HA's blocking-call detector in dev mode flagged
# the prior read because ``async_setup_entry``'s first import of any
# ``custom_components.*`` module runs on the event loop thread on first
# load. The value here MUST stay in sync with the ``domain`` field in
# ``manifest.json``; hassfest catches drift (it errors when the directory
# name, ``domain`` in manifest, and ``DOMAIN`` constant disagree) and
# ``tests/test_const.py::test_domain_matches_manifest`` provides a faster
# unit-level guard so CI fails before hassfest gets a chance to.
DOMAIN: Final = "iaqualink_robots"

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

# Long-outage threshold for marking entities unavailable (story H7). Pre-H7
# entities flipped to `available=False` after 30 consecutive update failures
# regardless of how much time those failures spanned. Combined with adaptive
# polling (1.5 s ↔ 10 s) that meant a transient ISP blip of 45 s – 5 min
# could disable user automations that depend on the `available` state. H7
# replaces the count-based gate with a wall-clock threshold: entities stay
# `available=True` with stale data through any outage shorter than this
# value, then flip to `available=False` once it's exceeded. The
# `is_serving_stale_data` (a.k.a. `restored`) attribute exposed on every
# entity tells advanced users which window they're in. 30 minutes is a
# starting point — soak observations may tune it.
LONG_OUTAGE_THRESHOLD_SECONDS: Final = 30 * 60

# User-toggleable option keys (configurable via config flow + options flow).
# Whether the `time_remaining_human` sensor includes seconds. Default True
# preserves the historical behavior; users complaining about activity-log
# churn can flip it off via Settings → Devices & Services → Configure to
# get minute-granular updates without slowing down the global SCAN_INTERVAL
# (which would also slow remote-control button responsiveness).
CONF_INCLUDE_SECONDS_REMAINING: Final = "include_seconds_remaining"
DEFAULT_INCLUDE_SECONDS_REMAINING: Final = True
