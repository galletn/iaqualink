"""Coordinator and client for iaqualinkRobots integration."""
import json
import datetime
import aiohttp
import logging

from datetime import timedelta
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from .const import URL_LOGIN, URL_GET_DEVICES, URL_WS, URL_FEATURES

_LOGGER = logging.getLogger(__name__)

class AqualinkClient:
    """Client to interact with iAqualink API for multiple robot types."""
    def __init__(self, username: str, password: str, api_key: str):
        self._username = username
        self._password = password
        self._api_key = api_key
        self._auth_token = None
        self._id = None
        self._id_token = None
        self._app_client_id = None
        self._serial = None
        self._device_type = None

    @property 
    def username(self) -> str:
        return self._username
    
    @property 
    def password(self) -> str:
        return self._password

    @property 
    def api_key(self) -> str:
        return self._api_key

    @property
    def robot_id(self) -> str:
        return self._serial or self._username

    @property
    def robot_name(self) -> str:
        # Use provided title if available
        return getattr(self, "_title", f"{self._device_type}_{self._serial}")

    async def fetch_status(self) -> dict:
        """Authenticate, discover device, subscribe, and parse status."""
        await self._authenticate()
        if not self._serial:
            await self._discover_device()
        data = await self._ws_subscribe()
        return self._parse_payload(data)

    async def _authenticate(self):
        """Authenticate with iAqualink API."""
        payload = json.dumps({
            "email": self._username,
            "password": self._password,
            "apikey": self._api_key
        })
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Connection": "keep-alive",
            "Accept": "*/*"
        }
        async with aiohttp.ClientSession() as session:
            resp = await session.post(URL_LOGIN, data=payload, headers=headers)
            resp.raise_for_status()
            auth = await resp.json()
        self._id = auth["id"]
        self._auth_token = auth["authentication_token"]
        self._id_token = auth["userPoolOAuth"]["IdToken"]
        self._app_client_id = auth["cognitoPool"]["appClientId"]

    async def _discover_device(self):
        """Get list of devices and pick the pool robot."""
        params = {
            "authentication_token": self._auth_token,
            "user_id": self._id,
            "api_key": self._api_key
        }
        async with aiohttp.ClientSession() as session:
            resp = await session.get(URL_GET_DEVICES, params=params)
            resp.raise_for_status()
            devices = await resp.json()
        for dev in devices:
            dtype = dev.get("device_type")
            if dtype in {"vr", "cyclobat", "cyclonext", "i2d_robot"}:
                self._serial = dev["serial_number"]
                self._device_type = dtype
                return
        raise RuntimeError("No supported robot found")

    async def _ws_subscribe(self):
        """Subscribe via websocket to get live updates."""
        req = {
            "action": "subscribe",
            "namespace": "authorization",
            "payload": {"userId": self._id},
            "service": "Authorization",
            "target": self._serial,
            "version": 1
        }
        headers = {"Authorization": self._id_token}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.ws_connect(URL_WS) as ws:
                await ws.send_json(req)
                msg = await ws.receive(timeout=10)
        return msg.json()

    async def fetch_features(self) -> str:
        """Fetch device features to retrieve the model."""
        url = f"{URL_FEATURES}{self._serial}/features"
        headers = {"Authorization": self._id_token}
        async with aiohttp.ClientSession(headers=headers) as session:
            resp = await session.get(url)
            resp.raise_for_status()
            data = await resp.json()
        return data.get("model")

    def _parse_payload(self, data: dict) -> dict:
        """Extract and normalize data for all robot types."""
        payload = data.get("payload", {})
        if self._device_type in {"vr", "i2d_robot"}:
            reported = payload.get("robot", {}).get("state", {}).get("reported", {})
            equip = reported.get("equipment", {}).get("robot", {})
        elif self._device_type == "cyclonext":
            reported = payload.get("robot", {}).get("state", {}).get("reported", {})
            equip = reported.get("features", {}).get("robot", {})
        elif self._device_type == "cyclobat":
            reported = payload.get("robot", {}).get("state", {}).get("reported", {})
            equip = reported.get("equipment", {}).get("cyclobat", {})
        else:
            reported = payload
            equip = payload

        result = {
            "serial_number": self._serial,
            "device_type": self._device_type,
            "battery_level": equip.get("battery", {}).get("userChargePerc"),
            "total_hours": equip.get("totalHours") or equip.get("stats", {}).get("totRunTime"),
            "canister": equip.get("canister"),
            "error_state": equip.get("errorState") or equip.get("errors", {}).get("code"),
        }

        # Temperature handling
        # Sensors structure varies by model; primary path under equipment
        sensors = equip.get("sensors", {})
        # Fallback to reported sensors if absent
        if not sensors:
            sensors = reported.get("sensors", {})
        sns1 = sensors.get("sns_1", {})
        # Prefer 'val', else 'state'
        temp = sns1.get("val") if sns1.get("val") is not None else sns1.get("state")
        # Legacy fallback
        if temp is None:
            temp = equip.get("temperature") or equip.get("stats", {}).get("temp")
        result["temperature"] = temp

        # Cycle metrics
        start_ts = equip.get("cycleStartTime")
        if start_ts:
            start = datetime.datetime.fromtimestamp(start_ts)
            result["cycle_start_time"] = start.isoformat()
            durations = equip.get("durations") or equip.get("cycles", {})
            cycle_no = equip.get("cycle") or next(iter(durations), None)
            result["cycle"] = cycle_no
            duration = durations.get(cycle_no)
            result["cycle_duration"] = duration
            end = start + datetime.timedelta(minutes=duration or 0)
            remaining = max((end - datetime.datetime.now()).total_seconds(), 0)
            result["time_remaining_human"] = f"{int(remaining//60)}:{int(remaining%60):02d}"
            result["estimated_end_time"] = end.isoformat()

        # Model
        try:
            model = asyncio.get_event_loop().run_until_complete(self.fetch_features())
        except Exception:
            model = None
        result["model"] = model

        return result

class AqualinkDataUpdateCoordinator(DataUpdateCoordinator):
    """Coordinator to poll AqualinkClient and update data."""
    def __init__(self, hass, client: AqualinkClient, interval: float):
        super().__init__(
            hass,
            _LOGGER,
            name=client.robot_id,
            update_interval=timedelta(seconds=interval),
        )
        self.client = client

    async def _async_update_data(self):
        try:
            status = await self.client.fetch_status()
            # Fetch model asynchronously
            try:
                model = await self.client.fetch_features()
            except Exception:
                model = None
            status['model'] = model
            return status
        except Exception as err:
            raise UpdateFailed(err)
