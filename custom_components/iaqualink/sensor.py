import json
import datetime
import requests
from homeassistant.helpers.entity import Entity
from datetime import timedelta
from homeassistant.util import Throttle

URL_LOGIN="https://prod.zodiac-io.com/users/v1/login"
URL_GET_DEVICES="https://r-api.iaqualink.net/devices.json"
URL_GET_DEVICE_STATUS="https://prod.zodiac-io.com/devices/v1/"
URL_GET_DEVICE_FEATURES="https://prod.zodiac-io.com/devices/v2/"

SCAN_INTERVAL = timedelta(seconds=30)

def setup_platform(hass, config, add_devices, discovery_info=None):
    """Setup the sensor platform."""
    add_devices([iaqualinkRobotSensor(config)])

class iaqualinkRobotSensor(Entity):
    def __init__(self, config):
        self._name = config.get('name')
        self._username = config.get('username')
        self._password = config.get('password')
        self._api_key = config.get('api_key')

        self._headers = {"Content-Type": "application/json; charset=utf-8", "Connection": "keep-alive", "Accept": "*/*" }

        self._attributes = {}

        # Apply throttling to methods using configured interval
        self.update = Throttle(SCAN_INTERVAL)(self._update)

        self._update()

    @property
    def name(self):
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
        return self._serial_number

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return self._attributes

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attributes

    def _update(self):
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
                            error = error + ' temparture mapping error'
                            
                    self._attributes['temperature'] = self._temperature

                    self._pressure = data["state"]["reported"]["equipment"]["robot"]["sensors"]["sns_2"]["state"]
                    self._attributes['pressure'] = self._pressure
                    self._total_hours = data["state"]["reported"]["equipment"]["robot"]["totalHours"]
                    self._attributes['total_hours'] = self._total_hours
                    self._error_state = data["state"]["reported"]["equipment"]["robot"]["errorState"]
                    self._attributes['error_state'] = self._error_state
                    self._lift_control = data["state"]["reported"]["equipment"]["robot"]["liftControl"]
                    self._attributes['lift_control'] = self._lift_control
                    self._equipment_id = data["state"]["reported"]["equipment"]["robot"]["equipmentId"]
                    self._attributes['equipment_id'] = self._equipment_id
                    self._cycle_start_time = datetime_obj = datetime.datetime.fromtimestamp(data["state"]["reported"]["equipment"]["robot"]["cycleStartTime"])
                    self._attributes['cycle_start_time'] = self._cycle_start_time
                    self._canister = data["state"]["reported"]["equipment"]["robot"]["canister"] 
                    self._attributes['canister'] = self._canister 
                    self._running = data["state"]["reported"]["equipment"]["robot"]["state"] 
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
