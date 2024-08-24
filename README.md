# iaqualink

![CC0](https://logovtor.com/wp-content/uploads/2020/10/iaqualink-logo-vector.png)

Home Assistant Iaqualink Vacuums Robots

## How to

#### Requirements

- account for iaqualink

#### Installations via HACS [![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)

- In HACS, look for "iaqualink" and install and restart
- If integration was not found, please add custom repository `galletn/iaqualink` as integration

#### Setup

add the following entry to the config file:

```yaml
sensor:
  - platform: iaqualinkRobots
    username: <username>
    password: <password>
    api_key: EOOEMOW4YR6QNB07
    name: <Robot name, will also be the sensor name>
```

From now on please use the vacuum entity, I will no longer update the sensor integration as more is possible with the vacuum entity

```yaml
vacuum:
  - platform: iaqualinkRobots
    username: <username>
    password: <password>
    api_key: EOOEMOW4YR6QNB07
    name: <Robot name, will also be the sensor name>
```


## Supported Models:

- EX 4000 iQ
- RA 6570 iQ
- RA 6900 iQ
- Polaris - VRX iQ+
- CNX 30 iQ
- CNX 40 IQ
- CNX 4090 iQ
- OV 5490 IQ

## Known Models to have issues:

- 


## Stop Start Example:

https://github.com/user-attachments/assets/0390dc52-5c24-455a-b5ae-6e725579ce71

## Trademark Legal Notices
All product names, trademarks and registered trademarks in the images in this repository, are property of their respective owners. All images in this repository are used by the Home Assistant project for identification purposes only.

The use of these names, trademarks and brands appearing in these image files, do not imply endorsement.

