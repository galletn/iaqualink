# Changelog

All notable changes to the iAqualink Robots Home Assistant integration are documented here.

## Unreleased

### Added

- **Config flow now rejects duplicate robots.** Adding the same robot twice aborts with `already_configured` instead of silently spawning a second coordinator (story C2).
- **Config flow now rejects empty/whitespace serials.** If the cloud returns a device with no usable serial number, the flow aborts with `no_serial` instead of creating an entry. Guards against the previous M13 regression where the user's email got frozen as the entity unique_id (story C2).
- `no_serial` translation added to `en.json` and copied as English fallback to all other locale files (cs, de, es, fr, it, nl, pt, sk). Native translations welcomed via PR.
- **Button entity unique_ids now derive from the robot's serial number** rather than the (mutable) integration entry title. Renaming the integration entry no longer forks duplicate button entities in the registry (story M12).
- **Removed the dead `api_key` field from config entry data.** The integration has always read the API key from a constant in `const.py`; the stored copy in `entry.data` was never used. Existing entries are silently auto-migrated to drop the field on next setup (story M17).

### Migration / manual cleanup notes

- **Pre-C2 duplicate entries are NOT auto-merged.** If you previously added the same robot twice (resulting in two config entries / two coordinators racing on the same cloud account), this release will NOT consolidate them. Both entries continue to run. To clean up: go to **Settings → Devices & Services → iAqualink Robots**, identify the duplicate entry, and **Delete** it manually. The remaining entry will keep operating.
- **Sensor entities with email frozen as unique_id are NOT migrated.** If you encountered the M13 regression (sensors named after your iAquaLink email rather than the robot serial), those existing entities keep their email-based unique_id. New entries created after this release use the serial. To clean up: open the entity registry for the affected sensors and rename them manually, or delete and re-add the integration.
- **Button unique_ids are auto-migrated on first restart after upgrade.** A one-shot migration walks the entity registry and rewrites legacy `<title>_<command>` button unique_ids to `<serial>_<command>`. No user action required in the common case — automation referencing the button `entity_id` continues to work. If a button somehow ended up with both shapes (rare: downgrade-then-upgrade scenarios), the migration leaves the legacy entry in place and logs a warning; resolve via **Settings → Devices & Services → Entities** by deleting the duplicate.
