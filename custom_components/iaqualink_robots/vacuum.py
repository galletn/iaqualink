import asyncio
import json
import datetime
import aiohttp
from datetime import timedelta

from homeassistant.components.vacuum import (
    STATE_CLEANING,
    STATE_DOCKED,
    STATE_IDLE,
    STATE_RETURNING,
    SUPPORT_BATTERY,
    SUPPORT_PAUSE,
    SUPPORT_RETURN_HOME,
    SUPPORT_START,
    SUPPORT_STOP,
    SUPPORT_FAN_SPEED,
    StateVacuumEntity,
    VacuumEntityFeature,
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
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
)

SUPPORT_IAQUALINK_ROBOTS_vr = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
        | VacuumEntityFeature.RETURN_HOME
)

SUPPORT_IAQUALINK_ROBOTS_cyclonext = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
)

SUPPORT_IAQUALINK_ROBOTS_i2d = (
        VacuumEntityFeature.START
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.STATUS
        | VacuumEntityFeature.RETURN_HOME
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
        self._cycle_duration = None
        self._cycle_end_time = None
        self._time_remaining = None
        self._serial_number = None
        self._model = None
        self._fan_speed_list = ["Floor only", "Floor and walls"]
        self._fan_speed = self._fan_speed_list[0]
        self._debug = None
        self._debug_mode=True

    @property
    def fan_speed(self):
        return self._fan_speed

    @property
    def fan_speed_list(self):
        return self._fan_speed_list

    @property
    def temperature(self):
        return self._temperature

    @property
    def name(self):
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
        """Return the status of the vacuum."""
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
            if self._device_type == "i2d_robot":
                request = { "command": "/command","params": "request=0A1240&timeout=800","user_id": self._id }
                url = "https://r-api.iaqualink.net/v2/devices/" + self._serial_number + "/control.json"
                data= None

                data = await asyncio.wait_for(self.post_command_i2d(url,request), timeout=800)
            else:
                if self._status == "connected":
                    clientToken = str ( self._id ) + "|" + self._authentication_token + "|" + self._app_client_id

                    if self._device_type == "vr":
                        request = { "action": "setCleanerState", "namespace": "vr", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot": { "state": 1 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
                    if self._device_type == "cyclonext":
                        request = { "action": "setCleanerState", "namespace": "cyclonext", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot.1": { "mode":1 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
                    data = await asyncio.wait_for(self.setCleanerState(request), timeout=30)
            
    async def async_stop(self, **kwargs):
        """Stop the vacuum."""
        # Your stop code here
        if self._status == "connected":
            self._state = STATE_IDLE
            if self._device_type == "i2d_robot":
                request = { "command": "/command","params": "request=0A1210&timeout=800","user_id": self._id }
                url = "https://r-api.iaqualink.net/v2/devices/" + self._serial_number + "/control.json"
                data= None

                data = await asyncio.wait_for(self.post_command_i2d(url,request), timeout=800)
            else:
                clientToken = str ( self._id ) + "|" + self._authentication_token + "|" + self._app_client_id

                if self._device_type == "vr":
                    request = { "action": "setCleanerState", "namespace": "vr", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot": { "state": 0 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
                if self._device_type == "cyclonext":
                    request = { "action": "setCleanerState", "namespace": "cyclonext", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot.1": { "mode":0 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
            
                data = await asyncio.wait_for(self.setCleanerState(request), timeout=30)

    async def async_update(self):
        """Get the latest state of the vacuum."""

        data = {"apikey": self._api_key, "email": self._username, "password": self._password}
        data = json.dumps(data)
        data = await asyncio.wait_for(self.send_login(data,self._headers), timeout=30)
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

        #Only get serial number if its initial to avoid too many calls and load
        if self._serial_number == None:
            data = None
            params = {"authentication_token":self._authentication_token,"user_id":self._id,"api_key":self._api_key}
            data =  await asyncio.wait_for(self.get_devices(params, self._headers), timeout=30)
    
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

            #Set modes based on model
            if self._device_type == "vr":

                self._fan_speed_list = ["Floor only", "SMART Floor and walls", "Floor and walls"]
                self._supported_features = SUPPORT_IAQUALINK_ROBOTS_vr

            if self._device_type == "cyclonext":

                self._fan_speed_list = ["Floor only", "SMART Floor and walls"]
                self._supported_features = SUPPORT_IAQUALINK_ROBOTS_cyclonext

            if self._device_type == "i2d_robot":

                self._fan_speed_list = ["Floor only", "Walls only", "Floor and walls"]
                self._supported_features = SUPPORT_IAQUALINK_ROBOTS_i2d

        #request status for i2d type
        if self._device_type == "i2d_robot":
            request = { "command": "/command","params": "request=OA11","user_id": self._id }

            url = "https://r-api.iaqualink.net/v2/devices/" + self._serial_number + "/control.json"
            data= None
            data = await asyncio.wait_for(self.post_command_i2d(url,request), timeout=30)

            try:
                if data["command"]["request"] == "OA11":
                    self._status = "connected"
                    self._attributes['status'] = self._status
                    self._debug = data["command"]["response"]
                    if self._debug_mode == True:
                        self._attributes['debug'] = self._debug
            except:
                try:
                    if data["status"] == "500":
                        self._status = "offline"
                        self._attributes['status'] = self._status
                except:
                    self._debug = data
                    if self._debug_mode == True:
                        self._attributes['debug'] = self._debug

            if self._state == STATE_CLEANING:
                minutes_remaining = data[11:13]
                try:
                    self.add_minutes_to_datetime(datetime.datetime.now(), minutes_remaining)
                    self._attributes['time_remaining'] = self._time_remaining
                except:
                    self._attributes['time_remaining'] = None

        else:
        #request device status & attributes over websocket for cyclonext and vr device types
            request = { "action": "subscribe", "namespace": "authorization", "payload": { "userId": self.id }, "service": "Authorization", "target": self._serial_number, "version": 1 }

            data= None
            data = await asyncio.wait_for(self.get_device_status(request), timeout=30)

            try:
                self._status = data['payload']['robot']['state']['reported']['aws']['status']
                self._attributes['status'] = self._status
            except:
            #returns empty message somethimes, try second call
                self._debug = data
                if self._debug_mode == True:
                    self._attributes['debug'] = self._debug
            
                data = await asyncio.wait_for(self.get_device_status(request), timeout=30)

                self._status = data['payload']['robot']['state']['reported']['aws']['status']
                self._attributes['status'] = self._status
            

            self._last_online = datetime_obj = datetime.datetime.fromtimestamp((data['payload']['robot']['state']['reported']['aws']['timestamp']/1000)) #Convert Epoch To Unix
            self._attributes['last_online'] = self._last_online

            #For VR device type device mapping
            if self._device_type == "vr":

                try:
                    self._temperature = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['val']
                except:
                    try: 
                        self._temperature = data['payload']['robot']['state']['reported']['equipment']['robot']['sensors']['sns_1']['state']
                    except:
                        self._temperature = '0' #Zodiac XA 5095 iQ does not support temp for example see https://github.com/galletn/iaqualink/issues/9
                            
                self._attributes['temperature'] = self._temperature

                if data['payload']['robot']['state']['reported']['equipment']['robot']['state'] == 1:
                    self._state = STATE_CLEANING
                else:
                    if data['payload']['robot']['state']['reported']['equipment']['robot']['state'] == 3:
                        self._state = STATE_RETURNING
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

                self._cycle = data['payload']['robot']['state']['reported']['equipment']['robot']['prCyc']
                self._attributes['cycle'] = self._cycle

                cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot']['durations']

                self._cycle_duration = list(cycle_duration_values.values())[self._cycle]
                self._attributes['cycle_duration'] = self._cycle_duration

                minutes = self._cycle_duration

                try:
                    self._cycle_end_time = datetime_obj = self.add_minutes_to_datetime(self._cycle_start_time, minutes)
                    self._attributes['cycle_end_time'] = self._cycle_end_time
                except:
                    self._attributes['cycle_end_time'] = None

                try:
                    self._time_remaining = self.subtract_dates(datetime.datetime.now(), self._cycle_end_time)
                    self._attributes['time_remaining'] = self._time_remaining
                except:
                    self._attributes['time_remaining'] = None


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

                self._cycle = data['payload']['robot']['state']['reported']['equipment']['robot.1']['cycle']
                self._attributes['cycle'] = self._cycle

                cycle_duration_values = data['payload']['robot']['state']['reported']['equipment']['robot.1']['durations']

                self._cycle_duration = list(cycle_duration_values.values())[self._cycle]
                self._attributes['cycle_duration'] = self._cycle_duration

                minutes = self._cycle_duration

                try:
                    self._cycle_end_time = datetime_obj = self.add_minutes_to_datetime(self._cycle_start_time, minutes)
                    self._attributes['cycle_end_time'] = self._cycle_end_time
                except:
                    self._attributes['cycle_end_time'] = None

                try:
                    self._time_remaining = self.subtract_dates(datetime.datetime.now(), self._cycle_end_time)
                    self._attributes['time_remaining'] = self._time_remaining
                except:
                    self._attributes['time_remaining'] = None

        #Get model only first time to avoid load.
            if self._model == None:
                data = None
                url = URL_GET_DEVICE_FEATURES + self._serial_number + "/features"
                data =  await asyncio.wait_for(self.get_device_features(url), timeout=30)

                self._model = data['model']
                self._attributes['model'] = self._model


    async def send_login(self, data, headers):
        """Post a login request to the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.post(URL_LOGIN, data=data) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def get_devices(self, params, headers):
        """Get device list from the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers=headers) as session:
            try:
                async with session.get(URL_GET_DEVICES, params=params) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)


    async def get_device_features(self, url):
        """Get device list from the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.get(url) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def get_device_status(self, request):
        """Get device status of the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.ws_connect("wss://prod-socket.zodiac-io.com/devices") as websocket:
                    await websocket.send_json(request)
                    message = await websocket.receive()
                return message.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)

    async def post_command_i2d(self, url, request):
        """Get device status of the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token, "api_key": self._api_key}) as session:
            try:
                async with session.post(url,json = request) as response:
                    return await response.json()
            finally:
                await asyncio.wait_for(session.close(), timeout=30)
    
    async def setCleanerState(self, request):
        """Get device status of the iaqualink_robots API."""
        async with aiohttp.ClientSession(headers={"Authorization": self._id_token}) as session:
            try:
                async with session.ws_connect("wss://prod-socket.zodiac-io.com/devices") as websocket:
                    await websocket.send_json(request)
            finally:
                await asyncio.wait_for(session.close(), timeout=30)
                #wait 2 seconds to avoid flooding iaqualink server with fetching statusses again otherwise it causes empty message returns
                await asyncio.sleep(2)
                await self.async_update()

    def add_minutes_to_datetime(self, dt, minutes):
        return dt + datetime.timedelta(minutes=minutes)

    def subtract_dates(self, date1, date2):
        """ Subtract the two dates """
        try:
            time_diff = date2 - date1
        except:
            return 0

        """ If the result is negative, return 0 """
        if time_diff < timedelta():
            return 0
    
        """ Otherwise, calculate the number of hours, minutes and remaining seconds """
        total_seconds = time_diff.total_seconds()
        hours = int(total_seconds // 3600)
        minutes = int((total_seconds % 3600) // 60)
        remaining_seconds = int(total_seconds % 60)

        """ Create a string that includes the number of hours, minutes and remaining seconds """
        time_str = f"{hours} Hour(s) {minutes} Minute(s) {remaining_seconds} Second(s)"

        return time_str

    async def async_set_fan_speed(self, fan_speed):
        """ code to send fan speed to vacuum cleaner """
        if fan_speed not in self._fan_speed_list:
            raise ValueError('Invalid fan speed')
        self._fan_speed = fan_speed
        if self._device_type == "i2d_robot":
            if fan_speed == "Walls only":
                _cycle_speed = "0A1284"

            if fan_speed == "Floor only":
                _cycle_speed = "0A1280"

            if fan_speed == "Floor and walls":
                _cycle_speed = "0A1283"

            request = { "command": "/command","params": "request=" + _cycle_speed + "&timeout=800","user_id": self._id }
            url = "https://r-api.iaqualink.net/v2/devices/" + self._serial_number + "/control.json"
            data= None
            data = await asyncio.wait_for(self.post_command_i2d(url,request), timeout=800)
        
        else:
            clientToken = str ( self._id ) + "|" + self._authentication_token + "|" + self._app_client_id

            if self._device_type == "vr":
                if fan_speed == "Floor only":
                    _cycle_speed = "1"

                if fan_speed == "SMART Floor and walls":
                    _cycle_speed = "2"

                if fan_speed == "Floor and walls":
                    _cycle_speed = "3"

                request = {"action":"setCleaningMode","version":1,"namespace":"vr","payload":{"state":{"desired":{"equipment":{"robot":{"prCyc":_cycle_speed}}}},"clientToken": clientToken},"service":"StateController","target": self._serial_number}

            if self._device_type == "cyclonext":
                if fan_speed == "Floor only":
                    _cycle_speed = "1"

                if fan_speed == "Floor and walls":
                    _cycle_speed = "3"

                request = {"action":"setCleaningMode","namespace":"cyclonext","payload":{"clientToken":clientToken,"state":{"desired":{ "equipment":{"robot.1":{"cycle":_cycle_speed}}}}},"service":"StateController","target":self._serial_number,"version":1}
            
            data = await asyncio.wait_for(self.setCleanerState(request), timeout=30)

        if self._debug_mode == True:
            self._attributes['debug'] = self._debug

    async def async_return_to_base(self, **kwargs):
        """Set the vacuum cleaner to return to the dock."""
        if self._status == "connected":
            self._state = STATE_RETURNING

            if self._device_type == "vr":
                clientToken = str ( self._id ) + "|" + self._authentication_token + "|" + self._app_client_id
                request = { "action": "setCleanerState", "namespace": "vr", "payload": { "clientToken": clientToken, "state": { "desired": { "equipment": { "robot": { "state": 3 } } } } }, "service": "StateController", "target": self._serial_number, "version": 1 }
                        
                data = await asyncio.wait_for(self.setCleanerState(request), timeout=30)
            
            if self._device_type == "i2d_robot":
                request = { "command": "/command","params": "request=0A1701&timeout=800","user_id": self._id }
                url = "https://r-api.iaqualink.net/v2/devices/" + self._serial_number + "/control.json"
                data= None

                data = await asyncio.wait_for(self.post_command_i2d(url,request), timeout=800)
