"""Platform for sensor integration."""
from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from homeassistant.util import Throttle
import json
import datetime
import requests



from .const import (
    URL_LOGIN,
    URL_GET_DEVICES,
    URL_GET_DEVICE_STATUS,
    URL_GET_DEVICE_FEATURES,
    NAME,
    VERSION,
    DOMAIN,
    ISSUEURL,
    STARTUP,
    SCAN_INTERVAL
)

def setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None
) -> None:
    """Set up the sensor platform."""
    iaqualink_data = iaqualinkData(config)
    iaqualink_data.update()
    add_entities([iaqualinkRobotSensor(iaqualink_data, config)])

class iaqualinkRobotSensor(SensorEntity):
    """Representation of a Sensor."""

    def __init__(self, iaqualink_data, config):
        self.data = iaqualink_data
        self._attributes = {}
        # Apply throttling to methods using configured interval
        self.update = Throttle(SCAN_INTERVAL)(self.update)

    @property
    def state(self):
        """Return the state of the sensor."""
        return self.data.state

    @property
    def unique_id(self) -> str:
        """Return the name of the sensor."""
        return (
            f"{self.data.model} {self.data.name}"
        )

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return self._attributes

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attributes

    def update(self) -> None:
        """Fetch new state data for the sensor.
        This is the only method that should fetch new data for Home Assistant.
        """
        self.data.update()
        self._attr_native_value = self.data.model
        self._attributes = self.data.attributes

class iaqualinkData:
    """Get the latest data and update the states."""

    def __init__(self, config):
        """Initialize the data object."""
        self.available = True
        self._attributes = {}
        self._name = config.get('name')
        self._username = config.get('username')
        self._password = config.get('password')
        self._api_key = config.get('api_key')
        self._headers = {"Content-Type": "application/json; charset=utf-8", "Connection": "keep-alive", "Accept": "*/*" }
        self._model = ''
        self._state = 'initiating connection'
        self._last_name = ''
        self._serial_number = ''
        self._temperature = '0'

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def temperature(self):
        """Return the state of the sensor."""
        return self._temperature

    @property
    def attributes(self):
        """Return device attributes."""
        return self._attributes
    
    @property
    def model(self):
        """Return device model."""
        return self._model
    
    @property
    def name(self):
        """Return device model."""
        return self._name

    @property
    def username(self):
        return self._username

    @property
    def frist_name(self):
        return self._first_name

    @property
    def last_name(self):
        return self._last_name

    @property
    def serial_number(self):
        return self._number

    @Throttle(SCAN_INTERVAL)
    def update(self):
        self._attributes['username'] = self._username
        url = URL_LOGIN
        data = {"apikey": self._api_key, "email": self._username, "password": self._password}
        data = json.dumps(data)
        response = requests.post(url, headers = self._headers, data = data)
        self._state = response.status_code
        if response.status_code == 200:
            data = response.json()
            self._first_name = data["first_name"]
            self._last_name = data["last_name"]
            self._attributes['first_name'] = self._first_name
            self._attributes['last_name'] = self._last_name
            self._authentication_token = data["authentication_token"]
            self._id =  data["id"]
            self._id_token = data["userPoolOAuth"]["IdToken"]

            url = URL_GET_DEVICES
            data = None
            response = requests.get(url, headers = self._headers, params = {"authentication_token":self._authentication_token,"user_id":self._id,"api_key":self._api_key})
            
            if response.status_code == 200:
                data = response.json()
                self._serial_number = data[0]["serial_number"] #assumption only 1 robot for now
                self._attributes['serial_number'] = self._serial_number
                self._robot_name = data[0]["name"]
                self._attributes['robot_name'] = self._robot_name

                url = URL_GET_DEVICE_STATUS + self._serial_number + "/shadow"
                data = None
                self._headers = {"Content-Type": "application/json; charset=utf-8", "Connection": "keep-alive", "Accept": "*/*", "Authorization" : self._id_token}
                response = requests.get(url, headers = self._headers)
                if response.status_code == 200:
                    data = response.json()
                    self._state = data["state"]["reported"]["aws"]["status"]
                    self._last_online = datetime_obj = datetime.datetime.fromtimestamp((data["state"]["reported"]["aws"]["timestamp"]/1000)) #Convert Epoch To Unix
                    self._attributes['last_online'] = self._last_online

                    #temperature seems to depend on robot model and has 2 possible locations:
                    try:
                        self._temperature = data["state"]["reported"]["equipment"]["robot"]["sensors"]["sns_1"]["val"]
                    except:
                        try: 
                            self._temperature = data["state"]["reported"]["equipment"]["robot"]["sensors"]["sns_1"]["state"]
                        except:
                            self._temperature = '0' #Zodiac XA 5095 iQ does not support temp for example see https://github.com/galletn/iaqualink/issues/9
                            
                    self._attributes['temperature'] = self._temperature

                    try:
                        self._pressure = data["state"]["reported"]["equipment"]["robot"]["sensors"]["sns_2"]["state"]
                    except:
                        self._pressure = "N/A"
                    self._attributes['pressure'] = self._pressure
                
                    try:
                        self._total_hours = data["state"]["reported"]["equipment"]["robot"]["totalHours"]
                    except:
                        self._total_hours = 0
                    self._attributes['total_hours'] = self._total_hours

                    try:
                        self._error_state = data["state"]["reported"]["equipment"]["robot"]["errorState"]
                    except:
                        self._error_state = "N/A"
                    self._attributes['error_state'] = self._error_state

                    try:
                        self._lift_control = data["state"]["reported"]["equipment"]["robot"]["liftControl"]
                    except:
                        self._lift_control = "N/A"
                    self._attributes['lift_control'] = self._lift_control

                    try:
                        self._equipment_id = data["state"]["reported"]["equipment"]["robot"]["equipmentId"]
                    except:
                        self._equipment_id = "N/A"
                    self._attributes['equipment_id'] = self._equipment_id

                    try:
                        self._cycle_start_time = datetime_obj = datetime.datetime.fromtimestamp(data["state"]["reported"]["equipment"]["robot"]["cycleStartTime"])
                    except:
                        self._cycle_start_time = "2000-01-01T09:00:00.000000"
                    self._attributes['cycle_start_time'] = self._cycle_start_time

                    try:
                        self._canister = data["state"]["reported"]["equipment"]["robot"]["canister"] 
                    except:
                        self._canister = "N/A"
                    self._attributes['canister'] = self._canister 

                    try:
                        self._running = data["state"]["reported"]["equipment"]["robot"]["state"] 
                    except:
                        self._running = "N/A"
                    self._attributes['running'] = self._running 

                    #Get model Number
                    data = None
                    self._headers = {"Content-Type": "application/json; charset=utf-8", "Connection": "keep-alive", "Accept": "*/*", "Authorization" : self._id_token}
                    url = URL_GET_DEVICE_FEATURES + self._serial_number + "/features"
                    response = requests.get(url, headers = self._headers)
                    data = response.json()
                    if response.status_code == 200:
                        self._model = data["model"]
                        self._attributes['model'] = self._model
                    else:
                        self._state = response.text[:250]
                else:
                    self._state = response.text[:250]
            else:
                self._state = response.text[:250]
