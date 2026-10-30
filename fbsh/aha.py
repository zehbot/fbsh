"""Client for the AHA-HTTP interface (webservices/homeautoswitch.lua).

Every command is a GET with the switchcmd, the optional AIN and the SID:

    /webservices/homeautoswitch.lua?switchcmd=<cmd>&ain=<ain>&sid=<sid>

The box answers text/plain for commands and text/xml for getdevicelistinfos,
getbasicdevicestats, getsubscriptionstate.
"""

from __future__ import annotations

from . import models
from .devices import parse_devicelist
from .models import Device
from .session import FritzError, Session

PATH = "/webservices/homeautoswitch.lua"


class AhaError(FritzError):
    """The box answered a command with an error."""


class Aha:
    def __init__(self, session: Session) -> None:
        self.session = session

    # -- transport -----------------------------------------------------------
    def call(self, switchcmd: str, ain: str | None = None, **params: object) -> str:
        """Run one command and return the body, renewing the SID once on 403."""
        for attempt in (1, 2):
            query: dict[str, object] = {"switchcmd": switchcmd, "sid": self.session.sid}
            if ain:
                query["ain"] = ain
            for key, value in params.items():
                if value is not None:
                    query[key] = value
            status, body = self.session.request(PATH, query)
            if status == 403 and attempt == 1:
                self.session.invalidate()
                continue
            if status != 200:
                raise AhaError(f"{switchcmd}: HTTP {status} - {body.strip()[:120]}")
            return body.strip()
        raise AhaError(f"{switchcmd}: session could not be renewed")

    # -- queries -------------------------------------------------------------
    def device_list(self) -> list[Device]:
        return parse_devicelist(self.call("getdevicelistinfos"))

    def device(self, ain: str) -> Device:
        for device in self.device_list():
            if device.ain.replace(" ", "") == ain.replace(" ", ""):
                return device
        raise AhaError(f"device {ain} not found")

    def switch_list(self) -> list[str]:
        body = self.call("getswitchlist")
        return [entry.strip() for entry in body.split(",") if entry.strip()]

    def switch_state(self, ain: str) -> bool | None:
        return _bool(self.call("getswitchstate", ain))

    def power(self, ain: str) -> float | None:
        return models.watts(self.call("getswitchpower", ain))

    def energy(self, ain: str) -> int | None:
        return models._num(self.call("getswitchenergy", ain))

    def temperature(self, ain: str) -> float | None:
        return models.celsius(self.call("gettemperature", ain))

    def setpoint(self, ain: str) -> str:
        return self.call("gethkrtsoll", ain)

    # -- commands ------------------------------------------------------------
    def switch_on(self, ain: str) -> bool | None:
        return _bool(self.call("setswitchon", ain))

    def switch_off(self, ain: str) -> bool | None:
        return _bool(self.call("setswitchoff", ain))

    def switch_toggle(self, ain: str) -> bool | None:
        return _bool(self.call("setswitchtoggle", ain))

    def set_temperature(self, ain: str, celsius: float) -> str:
        """Set a thermostat setpoint; the box expects 0.5 °C units, 16..56."""
        if celsius <= 8:
            value = 16
        elif celsius >= 28:
            value = 56
        else:
            value = int(round(celsius * 2))
        value = max(16, min(56, value))
        body = self.call("sethkrtsoll", ain, param=value)
        if body.strip() not in (str(value), "inval") and not body.strip().isdigit():
            raise AhaError(f"sethkrtsoll rejected: {body.strip()[:120]}")
        return str(value)

    def set_temperature_off(self, ain: str) -> str:
        return self.call("sethkrtsoll", ain, param=253)

    def set_temperature_on(self, ain: str) -> str:
        return self.call("sethkrtsoll", ain, param=254)

    def set_simple_onoff(self, ain: str, on: bool) -> str:
        return self.call("setsimpleonoff", ain, onoff=1 if on else 0)

    def set_level_percent(self, ain: str, percent: int) -> str:
        percent = max(0, min(100, int(percent)))
        return self.call("setlevelpercentage", ain, level=percent)


def _bool(body: str) -> bool | None:
    body = body.strip()
    if body == "1":
        return True
    if body == "0":
        return False
    return None
