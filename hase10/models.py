"""Data model for FRITZ!Box smart home devices.

Units follow AVM's AHA-HTTP documentation:

    temperature  0.1 °C        power  0.001 W
    hkr values   0.5 °C        energy Wh
    voltage      0.001 V       humidity %

Thermostat setpoints use two magic values: 254 means "on", 253 means "off".
Everything unknown is reported as an empty element by the box and becomes None here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# functionbitmask bits (AVM AHA-HTTP-API, section 3.2)
BIT_HANFUN = 0
BIT_LIGHT = 2
BIT_ALARM = 4
BIT_BUTTON = 5
BIT_HKR = 6
BIT_POWERMETER = 7
BIT_TEMPERATURE = 8
BIT_SWITCH = 9
BIT_REPEATER = 10
BIT_MICROPHONE = 11
BIT_HANFUN_UNIT = 13
BIT_ONOFF = 15
BIT_LEVEL = 16
BIT_COLOR = 17
BIT_BLIND = 18
BIT_HUMIDITY = 20

HKR_ON = 254
HKR_OFF = 253


def _num(text: str | None) -> int | None:
    if text is None:
        return None
    text = text.strip()
    if not text or text == "inval":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def celsius(value: str | None) -> float | None:
    """0.1 °C units -> °C."""
    raw = _num(value)
    return None if raw is None else raw / 10


def hkr_celsius(value: str | None) -> float | None:
    """0.5 °C units -> °C, returns None for the on/off magic values."""
    raw = _num(value)
    if raw is None or raw in (HKR_ON, HKR_OFF):
        return None
    return raw / 2


def hkr_state(value: str | None) -> str | None:
    raw = _num(value)
    if raw == HKR_ON:
        return "on"
    if raw == HKR_OFF:
        return "off"
    return None


def watts(value: str | None) -> float | None:
    """0.001 W -> W."""
    raw = _num(value)
    return None if raw is None else raw / 1000


def volts(value: str | None) -> float | None:
    raw = _num(value)
    return None if raw is None else raw / 1000


def boolean(value: str | None) -> bool | None:
    raw = _num(value)
    return None if raw is None else bool(raw)


@dataclass
class Switch:
    state: bool | None = None          # <switch><state>
    mode: str | None = None            # "auto" | "manuell"
    locked: bool | None = None         # <lock>
    device_locked: bool | None = None  # <devicelock>


@dataclass
class PowerMeter:
    power_w: float | None = None
    energy_wh: int | None = None
    voltage_v: float | None = None


@dataclass
class Thermostat:
    actual_c: float | None = None       # <tist>
    setpoint_c: float | None = None     # <tsoll>
    setpoint_state: str | None = None   # "on" | "off" | None
    comfort_c: float | None = None      # <komfort>
    eco_c: float | None = None          # <absenk>
    battery: int | None = None
    battery_low: bool | None = None
    window_open: bool | None = None
    boost_active: bool | None = None
    holiday_active: bool | None = None
    summer_active: bool | None = None
    error_code: int | None = None
    next_change_at: int | None = None
    next_change_c: float | None = None
    locked: bool | None = None


@dataclass
class Device:
    ain: str
    name: str = ""
    present: bool | None = None
    manufacturer: str = ""
    productname: str = ""
    firmware: str = ""
    functionbitmask: int = 0
    is_group: bool = False
    members: list[int] = field(default_factory=list)

    switch: Switch | None = None
    power: PowerMeter | None = None
    thermostat: Thermostat | None = None
    temperature_c: float | None = None
    humidity: int | None = None
    level_percent: int | None = None
    battery: int | None = None
    battery_low: bool | None = None
    alert_state: int | None = None

    # -- capabilities --------------------------------------------------------
    def has(self, bit: int) -> bool:
        return bool(self.functionbitmask & (1 << bit))

    @property
    def is_switchable(self) -> bool:
        return self.has(BIT_ONOFF) or self.has(BIT_SWITCH) or self.has(BIT_LIGHT)

    @property
    def is_thermostat(self) -> bool:
        return self.has(BIT_HKR)

    @property
    def has_power_meter(self) -> bool:
        return self.has(BIT_POWERMETER)

    @property
    def has_temperature_sensor(self) -> bool:
        return self.has(BIT_TEMPERATURE)

    @property
    def is_blind(self) -> bool:
        return self.has(BIT_BLIND)

    @property
    def kind(self) -> str:
        if self.is_group:
            return "group"
        if self.is_thermostat:
            return "thermostat"
        if self.is_blind:
            return "blind"
        if self.has(BIT_COLOR):
            return "color-light"
        if self.has(BIT_LEVEL) and self.has(BIT_LIGHT):
            return "dimmable-light"
        if self.has(BIT_LIGHT):
            return "light"
        if self.is_switchable:
            return "switch"
        if self.has(BIT_BUTTON):
            return "button"
        if self.has(BIT_ALARM):
            return "sensor"
        if self.has_temperature_sensor:
            return "temperature-sensor"
        return "device"

    # -- output --------------------------------------------------------------
    def summary(self) -> dict:
        data = {
            "ain": self.ain,
            "name": self.name,
            "kind": self.kind,
            "present": self.present,
            "product": self.productname,
        }
        if self.switch is not None:
            data["switch"] = {
                "state": self.switch.state,
                "mode": self.switch.mode,
                "locked": self.switch.locked,
            }
        if self.power is not None:
            data["power"] = {
                "watts": self.power.power_w,
                "energy_wh": self.power.energy_wh,
                "voltage_v": self.power.voltage_v,
            }
        if self.temperature_c is not None:
            data["temperature_c"] = self.temperature_c
        if self.humidity is not None:
            data["humidity"] = self.humidity
        if self.thermostat is not None:
            t = self.thermostat
            data["thermostat"] = {
                "actual_c": t.actual_c,
                "setpoint_c": t.setpoint_c,
                "setpoint_state": t.setpoint_state,
                "comfort_c": t.comfort_c,
                "eco_c": t.eco_c,
                "battery": t.battery,
                "battery_low": t.battery_low,
                "window_open": t.window_open,
                "boost_active": t.boost_active,
                "error_code": t.error_code,
            }
        return data

    def line(self) -> str:
        """One readable line for the CLI table."""
        state = ""
        if self.switch is not None and self.switch.state is not None:
            state = "on" if self.switch.state else "off"

        setpoint = ""
        if self.thermostat is not None:
            if self.thermostat.setpoint_c is not None:
                setpoint = f"{self.thermostat.setpoint_c:.1f}\u00b0C set"
            elif self.thermostat.setpoint_state:
                setpoint = self.thermostat.setpoint_state

        current = self.temperature_c
        if current is None and self.thermostat is not None:
            current = self.thermostat.actual_c
        temperature = f"{current:.1f}\u00b0C now" if current is not None else ""

        power = ""
        if self.power is not None and self.power.power_w is not None:
            power = f"{self.power.power_w:.1f} W"

        line = (f"{self.name or '(unnamed)':<24}  {self.kind:<17}  {state:<5}  "
                f"{setpoint:<11}  {temperature:<11}  {power:>8}")
        if self.present is False:
            line += "  OFFLINE"
        return line.rstrip()
