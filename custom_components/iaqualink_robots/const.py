"""Constants used by IaqualinkRobots."""
from __future__ import annotations

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

PLATFORMS: Final = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
]

# ---------------------------------------------------------------------------
# Cleaning pattern ("route") — the navigation path within a cycle.
#
# Verified end-to-end against a live cyclobat (RA-family, firmware
# V22E3.25.3): the cloud carries it per-cycle at
# ``equipment.robot.cycles.<cycleName>.route``, and a write of
# ``setCleaningMode`` with that nesting changes it (confirmed by flipping the
# value and observing the cloud echo the change back over StateStreamer,
# then restoring the original).
#
# It is NOT a global setting: each cycle keeps its own pattern, and only
# cycles that carry a ``route`` key support one. On the captured device
# ``floorTim`` / ``floorWallsTim`` / ``smartTim`` had one and
# ``waterlineTim`` / ``wallsTim`` did not — matching the iAquaLink app, which
# shows the Standard/S-Pattern toggle under Floor only.
#
# Gated on the ``quick_snake_pattern`` capability rather than on device type:
# no vr or cyclonext payload captured so far contains a ``route`` key at all,
# so this appears cyclobat-only today, but the feature flag is the cloud's own
# statement of support and is the safer thing to trust.
PATTERN_FEATURE: Final = "quick_snake_pattern"

#: ``route`` value → HA select option (also the translation key).
PATTERN_BY_ROUTE: Final[dict[int, str]] = {
    0: "standard",
    1: "s_pattern",
}

#: Inverse, for writes.
ROUTE_BY_PATTERN: Final[dict[str, int]] = {
    v: k for k, v in PATTERN_BY_ROUTE.items()
}

#: Cloud cycle key → the sub-name used in the entity's translation key and
#: unique_id. Only cycles observed to carry ``route``; a device exposing
#: ``route`` on some other cycle is handled by falling back to the raw cloud
#: key, so a new cycle type degrades to a working-but-untranslated entity
#: rather than vanishing.
PATTERN_CYCLE_NAMES: Final[dict[str, str]] = {
    "floorTim": "floor",
    "floorWallsTim": "floor_walls",
    "smartTim": "smart",
}

# API endpoints - cleaned up duplicates
# ---------------------------------------------------------------------------
# Cleaner part number → marketing model name.
#
# The cloud reports a part number at
# ``state.reported.eboxData.completeCleanerPn`` — verified present for every
# family (vr, vortrax, cyclonext, cyclobat), not just vortrax. That value is
# what the cloud itself uses to derive the ``model`` field returned by
# ``/devices/v2/{serial}/features``: across four live captures, ``model`` was
# present exactly when ``completeCleanerPn`` was non-empty, and absent when it
# was blank. So this table is a *fallback* for devices whose ``features`` call
# omits ``model`` while still reporting a part number.
#
# Why a static table instead of a lookup:
#
# * The old ``_get_vortrax_model_from_web`` scraped
#   ``zodiac-poolcare.com/search?key=<pn>``. That page no longer renders
#   results server-side — it returns a ~34 KB Adobe Experience Manager shell
#   containing zero product names — so all six of its regexes matched nothing
#   and the function returned "Unknown" for every input. Verified by running
#   it against the three real part numbers below.
# * ``pro.fluidra.com/bg_en/catalogsearch/result/?q=<pn>`` DOES resolve
#   correctly (Fluidra owns Zodiac), but it throttles aggressively: a few
#   sequential requests get 212–960 byte stubs indistinguishable from "no
#   results". That makes it unfit for a runtime lookup inside HA, and it would
#   reintroduce the third-party-session concern documented in story L19.
# * A table needs no network call, cannot be rate-limited, and cannot break
#   when a vendor site is redesigned.
#
# Entries below are verified against live hardware captures. To add one, run
# ``tools/capture_payload.py`` — it prints ``completeCleanerPn`` — and pair it
# with the model shown in the iAquaLink app. Unknown part numbers surface as
# ``"Unknown (WR0xxxxx)"`` in the Model sensor, which is both more useful to
# the user than a bare "Unknown" and directly actionable as a table addition.
MODEL_BY_CLEANER_PN: Final[dict[str, str]] = {
    "WR000219": "RA 6570 iQ",       # vr        — verified, live capture
    "WR000323": "RE 4600 iQ",       # cyclonext — verified, live capture
    "WR000500": "CNX 50 iQ",        # cyclonext — verified, live capture
}

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
