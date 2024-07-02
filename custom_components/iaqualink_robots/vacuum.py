import asyncio
import json
import datetime
import aiohttp

from homeassistant.components.vacuum import (
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_IDLE,
    SUPPORT_BATTERY,
    SUPPORT_PAUSE,
    SUPPORT_RETURN_HOME,
    SUPPORT_START,
    SUPPORT_STOP,
    StateVacuumEntity,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    STATE_OFF,
    STATE_ON,
)

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

# Define the supported features of our vacuum entity
SUPPORT_IAQUALINK_ROBOTS = (
    SUPPORT_START
    | SUPPORT_STOP
)


# Define the domain and platform for our vacuum entity
DOMAIN = "iaqualink_robots"
PLATFORM = "vacuum"


async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the iaqualink_robots vacuum platform."""
    # Your setup code here
    async_add_entities([IAquaLinkRobotVacuum(config)])


class IAquaLinkRobotVacuum(StateVacuumEntity):
    """Represents an iaqualink_robots vacuum."""

    def __init__(self, config):
        """Initialize the vacuum."""
        self._name = config.get('name')
        self._attributes = {}
        self._state = STATE_IDLE
        self._battery_level = None
        self._supported_features = SUPPORT_IAQUALINK_ROBOTS
        self._username = config.get('username')
        self._password = config.get('password')
        self._api_key = config.get('api_key')
        self._first_name = None
        self._last_name = None
        self._id = None
        self._headers = {"Content-Type": "application/json; charset=utf-8", "Connection": "keep-alive", "Accept": "*/*" }
        self._temperature = None
        self._authentication_token = None
        self._canister = None
        self._error_state = None
        self._total_hours = None
        self._cycle_start_time = None
        self._app_client_id = None

    @property
    def temperature(self):
        """Return the state of the sensor."""
        return self._temperature

    @property
    def name(self):
        """Return the name of the vacuum."""
        return self._name

    @property
    def username(self):
        return self._username

    @property
    def password(self):
        return self._password

    @property
    def api_key(self):
        return self._api_key

    @property
    def frist_name(self):
        return self._first_name

    @property
    def last_name(self):
        return self._last_name

    @property
    def model(self):
        """Return device model."""
        return self._model

    @property
    def serial_number(self):
        return self._number

    @property
    def id(self):
        return self._id

    @property
    def id_token(self):
        return self._id_token
        
    @property
    def error_state(self):
        return self._error_state

    @property
    def total_hours(self):
        return self._total_hours 

    @property
    def cycle_start_time(self):
        return self._cycle_start_time

    @property
    def should_poll(self):
        """Return True if the vacuum should be polled for state updates."""
        return True

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        return self._attributes

    @property
    def extra_state_attributes(self):
        """Return entity specific state attributes."""
        return self._attributes

    @property
    def state(self):
        """Return the state of the vacuum."""
        return self._state

    @property
    def status(self):
        """Return the state of the vacuum."""
        return self._status

    @property
    def supported_features(self):
        """Return the supported features of the vacuum."""
        return self._supported_features

    async def async_start(self):
        """Start the vacuum."""
        # Your start code here
        if self._status == "connected":
            self._state = STATE_CLEANING
            clientToken = str ( self._id ) + "|" + self._authentication_token + "|" + self._app_client_id

            if self._device_type == "vr":
                request = { "action": "setCleanerState", "namespace": "vr", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot": { "state": 1 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
            if self._device_type == "cyclonext":
                message = { "action": "setCleanerState", "namespace": "cyclonext", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot.1": { "mode":1 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
            data = await self.setCleanerState(request)
            
    async def async_stop(self, **kwargs):
        """Stop the vacuum."""
        # Your stop code here
        if self._status == "connected":
            self._state = STATE_IDLE
            clientToken = str ( self._id ) + "|" + self._authentication_token + "|" + self._app_client_id

            if self._device_type == "vr":
                request = { "action": "setCleanerState", "namespace": "vr", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot": { "state": 0 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
            if self._device_type == "cyclonext":
                message = { "action": "setCleanerState", "namespace": "cyclonext", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot.1": { "mode":0 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
            
            data = await self.setCleanerState(request)

    async def async_update(self):
        """Get the latest state of the vacuum."""

        data = {"apikey": self._api_key, "email": self._username, "password": self._password}
        data = json.dumps(data)
        data = await self.send_login(data,self._headers)
        self._first_name = data["first_name"]
        self._last_name = data["last_name"]
        self._id = data["id"]
        self._authentication_token = data["authentication_token"]
        self._id_token = data["userPoolOAuth"]["IdToken"]
        self._app_client_id = data["cognitoPool"]["appClientId"]

        self._attributes['username'] = self._username
        self._attributes['first_name'] = self._first_name
        self._attributes['last_name'] = self._last_name
        self._attributes['id'] = self._id

        data = None
        params = {"authentication_token":self._authentication_token,"user_id":self._id,"api_key":self._api_key}
        data =  await self.get_devices(params, self._headers)
    
        #check needed in case other devices are registered under same account. Some devices seem not to have an owned ID resulting in errors.
        index = 0
        if data[0]['device_type'] == "iaqua":
            index = 1    
        else:
            index = 0
                    
        #serial number
        self._serial_number = data[index]["serial_number"]
        self._attributes['serial_number'] = self._serial_number

        #device type will define further mappings and value locations
        self._device_type = data[index]["device_type"]
        self._attributes['device_type'] = self._device_type


        #request device status over websocket
        request = { "action": "subscribe", "namespace": "authorization", "payload": { "userId": self.id }, "service": "Authorization", "target": self._serial_number, "version": 1 }

        data= None
        data = await self.get_device_status(request)

        self._status = data['payload']['robot']['state']['reported']['aws']['status']
        self._attributes['status'] = self._status

        self._last_online = datetime_obj = datetime.datetime.fromtimestamp((data['payload']['robot']['state']['reported']['aws']['timestamp']/1000)) #Convert Epoch To Unix
        self._attributes['last_online'] = self._last_online

        #For VR device type device mapping
        if self._device_type == "vr":
            try:
                self._temperature = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['val']
            except:
                self._temperature = None #Zodiac XA 5095 iQ does not support temp for example see https://github.com/galletn/iaqualink/issues/9
                            
            self._attributes['temperature'] = self._temperature

            if data['payload']['robot']['state']['reported']['equipment']['robot']['state'] == 1:
                self._state = STATE_CLEANING
            else:
                self._state = STATE_IDLE

            self._canister = data['payload']['robot']['state']['reported']['equipment']['robot']['canister']
            self._attributes['canister'] = self._canister

            self._error_state = data['payload']['robot']['state']['reported']['equipment']['robot']['errorState']
            self._attributes['error_state'] = self._error_state

            self._total_hours = data['payload']['robot']['state']['reported']['equipment']['robot']['totalHours']
            self._attributes['total_hours'] = self._total_hours

            self._cycle_start_time = datetime_obj = datetime.datetime.fromtimestamp((data['payload']['robot']['state']['reported']['equipment']['robot']['cycleStartTime'])) #Convert Epoch To Unix
            self._attributes['cycle_start_time'] = self._cycle_start_time

        #For cyclonext device type device mapping
        if self._device_type == "cyclonext":
            
            if data['payload']['robot']['state']['reported']['equipment']['robot.1']['mode']== 1:
                self._state = STATE_CLEANING
            else:
                self._state = STATE_IDLE

            self._canister = data['payload']['robot']['state']['reported']['equipment']['robot.1']['canister']
            self._attributes['canister'] = self._canister

            self._error_state = data['payload']['robot']['state']['reported']['equipment']['robot.1']['errors']['code']
            self._attributes['error_state'] = self._error_state

            self._total_hours = data['payload']['robot']['state']['reported']['equipment']['robot.1']['totRunTime']
            self._attributes['total_hours'] = self._total_hours

            self._cycle_start_time = datetime_obj = datetime.datetime.fromtimestamp((data['payload']['robot']['state']['reported']['equipment']['robot.1']['cycleStartTime'])) #Convert Epoch To Unix
            self._attributes['cycle_start_time'] = self._cycle_start_time

        #If other device types add here

        #Get Model
        data = None
        url = URL_GET_DEVICE_FEATURES + self._serial_number + "/features"
        data =  await self.get_device_features(url)

        self._model = data['model']
        self._attributes['model'] = self.model


    async def send_login(self, data, headers):
        """Post a login request to the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.post(URL_LOGIN, data=data) as response:
                return await response.json()

    async def get_devices(self, params, headers):
        """Get device list from the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(URL_GET_DEVICES, params=params) as response:
                return await response.json()

    async def get_device_features(self, url):
        """Get device list from the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            async with session.get(url) as response:
                return await response.json()

    async def get_device_status(self, request):
        """Get device status of the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            async with session.ws_connect("wss://prod-socket.zodiac-io.com/devices") as websocket:
                await websocket.send_json(request)
                message = await websocket.receive()
            return message.json()
    
    async def setCleanerState(self, request):
        """Get device status of the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            async with session.ws_connect("wss://prod-socket.zodiac-io.com/devices") as websocket:
                await websocket.send_json(request)
                message = await websocket.receive()
            return message.json()

    