"""FRITZ!Box smart home access (AHA-HTTP interface)."""

from .aha import Aha, AhaError
from .config import ConfigError, Settings
from .devices import parse_devicelist
from .models import Device, PowerMeter, Switch, Thermostat
from .session import Blocked, FritzError, LoginFailed, Session

__all__ = [
    "Aha",
    "AhaError",
    "Blocked",
    "ConfigError",
    "Device",
    "FritzError",
    "LoginFailed",
    "PowerMeter",
    "Session",
    "Settings",
    "Switch",
    "Thermostat",
    "parse_devicelist",
]

__version__ = "0.1.0"
