# Changelog

All notable changes to the iAqualink Robots Home Assistant integration are documented here.

## Unreleased

### Added

- **Config flow now rejects duplicate robots.** Adding the same robot twice aborts with `already_configured` instead of silently spawning a second coordinator (story C2).
- **Config flow now rejects empty/whitespace serials.** If the cloud returns a device with no usable serial number, the flow aborts with `no_serial` instead of creating an entry. Guards against the previous M13 regression where the user's email got frozen as the entity unique_id (story C2).
- `no_serial` translation added to `en.json` and copied as English fallback to all other locale files (cs, de, es, fr, it, nl, pt, sk). Native translations welcomed via PR.
- **Button entity unique_ids now derive from the robot's serial number** rather than the (mutable) integration entry title. Renaming the integration entry no longer forks duplicate button entities in the registry (story M12).
- **Removed the dead `api_key` field from config entry data.** The integration has always read the API key from a constant in `const.py`; the stored copy in `entry.data` was never used. Existing entries are silently auto-migrated to drop the field on next setup (story M17).
- **Token refresh now uses the real JWT `exp` claim from the iAqualink Cognito IdToken** instead of a hardcoded 1-hour window. If AWS ever issues tokens with a non-1h lifetime (per-pool setting), refreshes fire at the correct moment rather than burning cycles or running stale. A 60-second safety margin is subtracted to absorb clock skew between AWS and the HA host. On the rare path where the token is missing or malformed, a `WARNING` is logged and the integration falls back to `now + 1h − 60s` (story H9a).
- **CI parity test** that asserts the button command list in `button.py` stays in sync with the migration tuple `_M12_BUTTON_COMMANDS` in `__init__.py`. Adding a new button command without updating the migration tuple is now a CI failure rather than a silent drift hazard that would surface as duplicate registry entries on next user upgrade (story M18).
- **`Include seconds in time-remaining display` option** in the config flow (and via Settings → Devices & Services → Configure after setup). Default is on (preserves the historical seconds-precision behavior). Turning it off makes the `time_remaining_human` sensor update only when the minute ticks over, eliminating ~20× of activity-log entries during long cleaning cycles. The global `SCAN_INTERVAL` is unchanged so remote-control button responsiveness is not affected. Power users who want exact-seconds precision can still derive it from `estimated_end_time` minus `now` in a template.

### Fixed

- **Wrong fan-speed list exposed for cyclonext robots (issue #76).** The CNX 30 iQ, RE 4400 iQ, RE 4600 iQ and other cyclonext-family devices were showing a non-functional "Walls only" cleaning mode added in 2.4.2. Cyclonext only supports `floor_only` and `floor_and_walls` on the cloud side, so picking "Walls only" left the entity state forked from the device. The fan-speed-list assignment in `vacuum.py` now branches per `device_type` and mirrors the per-type `cycle_speed_map` in `coordinator.py::_set_other_fan_speed`. Also tightens cyclobat to expose all 4 modes it actually supports (`wall_only`, `floor_only`, `smart_floor_and_walls`, `floor_and_walls`) instead of an i2d-shaped 3-entry list that hid `smart_floor_and_walls` and offered the non-existent `walls_only`. i2d_robot is unchanged — `walls_only` (plural) is a genuine i2d "waterline only" mode (0x04), distinct from `wall_only` (singular) used by vr/cyclobat.

### Changed

- **Defensive hardening bundle (story M19).** Ten small patches land together to tighten gaps surfaced in the C2, M12, and M17 code reviews. Highlights: config flow now rejects non-string `serial_number` values (int / list / dict) from the cloud API before they reach `async_set_unique_id`, complementing the existing whitespace-serial guard from C2; `async_setup_entry` lazily strips a stray `api_key` field that may persist after a backup-restore from before the M17 migration; and `async_migrate_entry` now returns `False` on failure and leaves the entry at its previous version so HA retries the migration on next setup rather than silently advancing past a half-applied state.

### Migration / manual cleanup notes

- **Pre-C2 duplicate entries are NOT auto-merged.** If you previously added the same robot twice (resulting in two config entries / two coordinators racing on the same cloud account), this release will NOT consolidate them. Both entries continue to run. To clean up: go to **Settings → Devices & Services → iAqualink Robots**, identify the duplicate entry, and **Delete** it manually. The remaining entry will keep operating.
- **Sensor entities with email frozen as unique_id are NOT migrated.** If you encountered the M13 regression (sensors named after your iAquaLink email rather than the robot serial), those existing entities keep their email-based unique_id. New entries created after this release use the serial. To clean up: open the entity registry for the affected sensors and rename them manually, or delete and re-add the integration.
- **Button unique_ids are auto-migrated on first restart after upgrade.** A one-shot migration walks the entity registry and rewrites legacy `<title>_<command>` button unique_ids to `<serial>_<command>`. No user action required in the common case — automation referencing the button `entity_id` continues to work. If a button somehow ended up with both shapes (rare: downgrade-then-upgrade scenarios), the migration leaves the legacy entry in place and logs a warning; resolve via **Settings → Devices & Services → Entities** by deleting the duplicate.
