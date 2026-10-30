"""Parse the XML returned by getdevicelistinfos / getdeviceinfos."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from . import models
from .models import Device, PowerMeter, Switch, Thermostat


def _local(tag: str) -> str:
    """Strip a namespace prefix if the box sends one."""
    return tag.rsplit("}", 1)[-1]


def _child(node: ET.Element, name: str) -> ET.Element | None:
    for child in node:
        if _local(child.tag) == name:
            return child
    return None


def _text(node: ET.Element, name: str) -> str | None:
    child = _child(node, name)
    if child is None:
        return None
    return (child.text or "").strip()


def _int(node: ET.Element, name: str) -> int | None:
    return models._num(_text(node, name))


def _bool(node: ET.Element, name: str) -> bool | None:
    return models.boolean(_text(node, name))


def _parse_switch(node: ET.Element) -> Switch:
    return Switch(
        state=_bool(node, "state"),
        mode=(_text(node, "mode") or None),
        locked=_bool(node, "lock"),
        device_locked=_bool(node, "devicelock"),
    )


def _parse_powermeter(node: ET.Element) -> PowerMeter:
    return PowerMeter(
        power_w=models.watts(_text(node, "power")),
        energy_wh=_int(node, "energy"),
        voltage_v=models.volts(_text(node, "voltage")),
    )


def _parse_thermostat(node: ET.Element) -> Thermostat:
    tsoll = _text(node, "tsoll")
    thermostat = Thermostat(
        actual_c=models.hkr_celsius(_text(node, "tist")),
        setpoint_c=models.hkr_celsius(tsoll),
        setpoint_state=models.hkr_state(tsoll),
        comfort_c=models.hkr_celsius(_text(node, "komfort")),
        eco_c=models.hkr_celsius(_text(node, "absenk")),
        battery=_int(node, "battery"),
        battery_low=_bool(node, "batterylow"),
        window_open=_bool(node, "windowopenactiv"),
        boost_active=_bool(node, "boostactive"),
        holiday_active=_bool(node, "holidayactive"),
        summer_active=_bool(node, "summeractive"),
        error_code=_int(node, "errorcode"),
        locked=_bool(node, "lock"),
    )
    nxt = _child(node, "nextchange")
    if nxt is not None:
        thermostat.next_change_at = _int(nxt, "endperiod") or None
        tchange = _text(nxt, "tchange")
        if tchange:
            value = models._num(tchange)
            if value not in (None, 0, 255, models.HKR_ON, models.HKR_OFF):
                thermostat.next_change_c = value / 2
    return thermostat


def parse_device(node: ET.Element) -> Device:
    local = _local(node.tag)
    device = Device(
        ain=(node.get("identifier") or "").strip(),
        name=(_text(node, "name") or "").strip(),
        present=_bool(node, "present"),
        manufacturer=node.get("manufacturer") or "",
        productname=node.get("productname") or "",
        firmware=node.get("fwversion") or "",
        functionbitmask=int(node.get("functionbitmask") or 0),
        is_group=(local == "group"),
        battery=_int(node, "battery"),
        battery_low=_bool(node, "batterylow"),
    )

    switch = _child(node, "switch")
    if switch is not None:
        device.switch = _parse_switch(switch)

    powermeter = _child(node, "powermeter")
    if powermeter is not None:
        device.power = _parse_powermeter(powermeter)

    thermostat = _child(node, "hkr")
    if thermostat is not None:
        device.thermostat = _parse_thermostat(thermostat)

    temperature = _child(node, "temperature")
    if temperature is not None:
        device.temperature_c = models.celsius(_text(temperature, "celsius"))

    level = _child(node, "levelcontrol")
    if level is not None:
        device.level_percent = _int(level, "levelpercentage")

    alert = _child(node, "alert")
    if alert is not None:
        device.alert_state = _int(alert, "state")

    humidity = _child(node, "humidity")
    if humidity is not None:
        device.humidity = _int(humidity, "rel_humidity")
        if device.humidity == -9999:
            device.humidity = None

    groupinfo = _child(node, "groupinfo")
    if groupinfo is not None:
        members = _text(groupinfo, "members") or ""
        device.members = [int(m) for m in members.split(",") if m.strip().isdigit()]

    return device


def parse_devicelist(xml_text: str) -> list[Device]:
    """Parse a full <devicelist> document."""
    root = ET.fromstring(xml_text)
    devices = []
    for node in list(root):
        if _local(node.tag) in ("device", "group"):
            devices.append(parse_device(node))
    return devices
