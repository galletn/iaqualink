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

## ⚙️ Setup

1. **Settings** → **Devices & Services** → **Add Integration**
2. Search for **"iAqualink Robots"**
3. Enter your iAqualink credentials
4. Select your robot from the detected devices

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

### Buttons

* Remote Forward / Backward
* Remote Rotate Left / Right
* Remote Stop

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

*(List currently empty — please report if you encounter problems.)*

## 🖥️ Example: Start/Stop in Home Assistant

[Video Example](https://github.com/user-attachments/assets/0390dc52-5c24-455a-b5ae-6e725579ce71)

## 🌍 Language Configuration

1. Go to System → **[General](https://my.home-assistant.io/redirect/general/)** → **Language**
2. Select your preferred language
3. Restart Home Assistant and reload the integration

## 🔧 Troubleshooting

* **Robot shows unavailable** → Check connection in iAqualink mobile app

Enable debug logging by adding to `configuration.yaml`:

```yaml
logger:
  logs:
    custom_components.iaqualinkRobots: debug
```

## 🤝 Contributing

We welcome:

* Language translations
* Testing on different robot models
* Bug reports & feature requests

## 🙏 Credits

* Developed by [@galletn](https://github.com/galletn)
* Translation help from Home Assistant community
* Based on reverse-engineered iAqualink API
