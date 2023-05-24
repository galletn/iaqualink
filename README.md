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
  - platform: iaqualink
    username: <username>
    password: <password>
    api_key: EOOEMOW4YR6QNB07
    name: <Robot name, will also be the sensor name>
```
#Supported Models:

- EX 4000 iQ
- RA 6570 iQ

#Known Models to have issues:

- OV 5490 IQ
