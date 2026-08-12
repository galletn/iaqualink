# iAqualink Robots Integration for Home Assistant

![GitHub Repo stars](https://img.shields.io/github/stars/galletn/iaqualink?style=for-the-badge&color=%2318bcf2)
![Github Repo issues](https://img.shields.io/github/issues/galletn/iaqualink?style=for-the-badge&color=%2318bcf2)
![HACS](https://img.shields.io/badge/HACS-Custom-41BDF5?style=for-the-badge)
![GitHub Release Date](https://img.shields.io/github/release-date/galletn/iaqualink?style=for-the-badge&label=Latest%20Release&color=%2318bcf2)
![GitHub Release Version](https://img.shields.io/github/v/release/galletn/iaqualink?style=for-the-badge&color=%2318bcf2)

A comprehensive Home Assistant integration for **iAqualink robotic pool cleaners**, providing **full control**, **real-time monitoring**, and **multi-language support**.

## 🌟 Features

### Device Control & Monitoring

* **Full Robot Control**: Start, stop, return to base, remote directional movement
* **Real-time Monitoring**: Battery, cleaning status, error states, temperature
* **Fan Speed Control**: Multiple cleaning modes (Floor only, Walls only, etc.)
* **Comprehensive Sensors**: 20+ sensor types for detailed device info

### Multi-Language Support

* 🇺🇸 English (Default)
* 🇫🇷 Français
* 🇪🇸 Español
* 🇩🇪 Deutsch
* 🇳🇱 Nederlands
* 🇵🇹 Português
* 🇨🇿 Čeština
* 🇮🇹 Italiano
* 🇸🇰 Slovenčina

## 🚀 Installation

### HACS

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=galletn&repository=iaqualink&category=integration)

1. Open **HACS** in Home Assistant
2. Search for **"iAqualink Robots"**
3. Click **Install** and restart Home Assistant

> **If not found in search:** Add custom repository `galletn/iaqualink` as type **Integration**.

### Manual installation

1. Copy `custom_components/iaqualink_robots/` into your Home Assistant
   `config/custom_components/` directory.
2. Restart Home Assistant.

## ⚙️ Setup

1. **Settings** → **Devices & Services** → **Add Integration**
2. Search for **"iAqualink Robots"**
3. Enter your iAqualink credentials
4. Select your robot from the detected devices

Adding a **second robot on the same account** is the same flow — sign in
again and pick the other robot; robots you already added are filtered out of
the list.

### Options

Reachable via *Settings → Devices & Services → iAqualink Robots → Configure*.

* **Include seconds in time-remaining display** — when on, the
  `Time Remaining` sensor updates every poll (a few seconds) and shows
  seconds. Turn it off to update only when the minute changes, which
  substantially reduces activity-log entries during a long cleaning cycle
  without slowing down the remote-control buttons. Changing it reloads the
  integration.

### Changing your iAqualink password

If you change the password on your iAqualink account, Home Assistant will
show a **Reconfigure / repair** prompt for the integration. Enter the new
password and it resumes — entity IDs, history and automations are all kept.
The prompt also appears automatically if Home Assistant was restarted while
the old password was still stored.

### Moving the robot to a different iAqualink account

Reachable via *Settings → Devices & Services → iAqualink Robots → ⋮ →
Reconfigure*. Enter the new account's username and password. The robot must still be
listed on that account — if it isn't, the integration says so and leaves the
entry untouched rather than accepting credentials that could never work.
Because the entry keeps its serial number, nothing else changes: same
entities, same history, same automations.

### Removing the integration

Use *Settings → Devices & Services → iAqualink Robots → ⋮ → Delete*. This
removes the config entry and all of its entities. Nothing is left behind on
the robot or in your iAqualink account.

## 📱 Entities

### Vacuum Entity

* Controls: Start/Stop, Return to base, Fan speed selection
* Status: Cleaning mode, Activity, Battery level
* Features: Remote directional control (Forward, Backward, Rotate)

### Sensors

* Serial Number, Device Type, Model
* Battery Level, Total Hours, Temperature
* Cleaning cycle info (Start time, Duration, Type)
* Canister Level, Error State
* Time Remaining, Estimated End Time
* Fan Speed, Activity, Status
* Time adjustments (Time Adjustments, Adjustment Increment, Original /
  Total Duration, Time Added/Removed)

Grouped under **Diagnostic** on the device page:

* Firmware Version, Error Code, Diagnostic Code, Last Cloud Contact

> **Not every sensor appears on every robot.** The integration only creates
> sensors your model can actually populate — for example CycloNext robots
> report no water temperature, so no Temperature sensor is created, and
> Battery Level exists only on battery-powered (CycloBat) models. A sensor
> reading `Unknown` on your model is worth reporting.

### Selects

* **Cleaning pattern** per cycle (Floor, Floor & Wall, SMART) — `Standard`
  or `S-Pattern`, matching the toggle in the iAquaLink app.

> Only created for robots whose firmware advertises pattern support, and only
> for the cycles that actually have a pattern. If your robot doesn't show
> these, it doesn't support the feature.

### Buttons

Remote control and time adjustment are only available on **VR** and
**VortraX** robots — the cloud exposes no equivalent commands for the other
families, so these buttons are not created for them.

* Remote Forward / Backward
* Remote Rotate Left / Right
* Remote Stop
* Add 15 Minutes / Reduce 15 Minutes

## 📋 Supported Models

### Fully Supported

* EX 4000 iQ / EX 5000 iQ / EX 6000 iQ
* RA 6500 iQ / RA 6570 iQ / RA 6900 iQ
* Polaris VRX iQ+
* CNX 30 iQ / CNX 40 iQ / CNX 50 iQ / CNX 4090 iQ
* OV 5490 iQ / RF 5600 iQ
* OA 6400 iQ
* P965 iQ / 9650 iQ
* VortraX TRX 8500 iQ
* Polaris Freedom Cordless
* Aqua Products EVO 614iQ / Polaris Epic 8642iQ / Scout 42iQ
* Zodiac XA 5095 iQ
* Zodiac RE 4400 iQ / RE 4600 iQ
* Zodiac GV 5480 iQ
* Cyclobot & CycloNext models
* Vortrax models

### Known Issues

* **CNX 868 iQ (CycloNext) can show `Idle` while physically cleaning**
  ([#100](https://github.com/galletn/iaqualink/issues/100)). Not yet
  explained: the two CycloNext robots available for testing report the
  relevant cloud field normally, so the obvious cause has been ruled out.
  If you can reproduce it, please attach **Download diagnostics** taken
  _while the robot is running_ — that captures the raw cloud values and
  would very likely settle it.
* **`Model` can read `Unknown (WR0xxxxx)`** on some robots. The cloud
  derives the model name from a part number stored against your device; when
  that field is blank in their records, no model can be resolved by anyone —
  including the iAquaLink app. Report the `WR0…` string and the model can at
  least be mapped locally
  ([#88](https://github.com/galletn/iaqualink/issues/88)).

## 🖥️ Example: Start/Stop in Home Assistant

[Video Example](https://github.com/user-attachments/assets/0390dc52-5c24-455a-b5ae-6e725579ce71)

## 🌍 Language Configuration

1. Go to System → **[General](https://my.home-assistant.io/redirect/general/)** → **Language**
2. Select your preferred language
3. Restart Home Assistant and reload the integration

## 🔧 Troubleshooting

* **Robot shows unavailable** → first check the robot is reachable in the
  iAqualink mobile app. The integration keeps entities available through
  short cloud outages (serving the last known values, with a `restored`
  attribute set to `true`) and only marks them unavailable once an outage
  passes 30 minutes.
* **Home Assistant asks you to re-enter your password** → your iAqualink
  credentials were rejected by the cloud. See
  [Changing your iAqualink password](#changing-your-iaqualink-password).
* **"This robot is not listed on that iAquaLink account"** → you signed in
  with a different account than the one the robot is registered to. Nothing
  was changed; retry with the correct account.
* **A button press shows an error** → since 3.0.0-rc.4, a command that fails
  to reach the robot surfaces as an error in the UI instead of silently doing
  nothing. The message includes the underlying cause.

### Reporting a problem

Attach a **diagnostics download**: *Settings → Devices & Services →
iAqualink Robots → ⋮ → Download diagnostics*. It contains the raw cloud
values the integration is working from, with credentials, tokens and personal
details redacted, and usually removes the need for any follow-up questions.

### Debug logging

Add to `configuration.yaml` and restart:

```yaml
logger:
  logs:
    custom_components.iaqualink_robots: debug
```

Routine operation logs nothing above `debug`; anything at `warning` or
`error` in your log is worth reporting.

## 🤝 Contributing

We welcome:

* Language translations
* Testing on different robot models
* Bug reports & feature requests

## 🙏 Credits

Maintained by [@galletn](https://github.com/galletn), built on a
reverse-engineered iAqualink API — and on a lot of community work.

**Translations**

* 🇨🇿 Čeština + 🇸🇰 Slovenčina — [@FejbyK](https://github.com/FejbyK)
* 🇮🇹 Italiano — [@ppastur](https://github.com/ppastur)
* Other locales started as English fallbacks; native-speaker PRs are always
  welcome.

**Code contributions**

* [@flimzy](https://github.com/flimzy) — diagnosed and fixed the
  auto-restart-after-cycle bug (#89 / #94)
* [@CLARENNE-Q](https://github.com/CLARENNE-Q) — i2d estimated-end-time fix
  and compatible-device filtering (#48 / #49)
* [@FejbyK](https://github.com/FejbyK) — README and CI improvements

**Hardware access and verification**

Several features exist only because owners let their robot be poked at.
Notably **Brian**, whose CycloBat made it possible to verify the
cleaning-pattern (Standard / S-Pattern) support against real hardware in both
directions — reading *and* writing — rather than inferring it. A control that
silently does nothing is the failure mode behind issue #76; that is what
hands-on verification prevents.

**Reports, testing and model confirmations**

[@AlexeiakaTechnik](https://github.com/AlexeiakaTechnik),
[@beit-no1](https://github.com/beit-no1),
[@chansearrington](https://github.com/chansearrington),
[@ConRodLou](https://github.com/ConRodLou),
[@craigcurtin-dev](https://github.com/craigcurtin-dev),
[@danielegger1](https://github.com/danielegger1),
[@DeltaFox0018](https://github.com/DeltaFox0018),
[@fdarden](https://github.com/fdarden),
[@filmackay](https://github.com/filmackay),
[@Gamester17](https://github.com/Gamester17),
[@GizmoGoody](https://github.com/GizmoGoody),
[@jiggahertz86](https://github.com/jiggahertz86),
[@johntdyer](https://github.com/johntdyer),
[@nylesmail](https://github.com/nylesmail),
[@Osconse](https://github.com/Osconse),
[@OtisPresley](https://github.com/OtisPresley),
[@piejanssens](https://github.com/piejanssens),
[@quartit](https://github.com/quartit),
[@smuliv](https://github.com/smuliv),
[@socalsharky](https://github.com/socalsharky),
[@sv-kov](https://github.com/sv-kov),
[@virtualbitzz](https://github.com/virtualbitzz).

Every supported model on the list above got there because someone reported
theirs, ran a pre-release, and said whether it worked. If you did that and
aren't listed, please open an issue — it's an oversight, not a judgement.

## 📄 License

Released under the [MIT License](LICENSE) — © 2023-2026 Nicolas Gallet
([@galletn](https://github.com/galletn)) and contributors.

The iAqualink cloud protocol this integration speaks is not publicly
documented; the implementation is the result of reverse-engineering by the
maintainer and contributors. If you build on that work, an acknowledgement is
appreciated.
