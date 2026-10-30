"""FRITZ!Box smart home access (AHA-HTTP interface)."""

from .aha import Aha, AhaError
from .config import ConfigError, Settings
from .devices import parse_devicelist
from .models import Device, PowerMeter, Switch, Thermostat
from .session import Blocked, FritzError, LoginFailed, Session

def list_devices(settings: "Settings | None" = None) -> list[Device]:
    """One call for the common case: connect with private/.env and list devices.

        import fbsh
        for device in fbsh.list_devices():
            print(device.name, device.kind, device.capabilities)
    """
    settings = settings or Settings.load()
    session = Session(
        host=settings.host,
        user=settings.user,
        password=settings.password,
        scheme=settings.scheme,
        port=settings.port,
        verify_tls=settings.verify_tls,
        ca_file=settings.ca_file,
        timeout=settings.timeout,
    )
    return Aha(session).device_list()


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
    "list_devices",
    "parse_devicelist",
]

__version__ = "0.1.0"
