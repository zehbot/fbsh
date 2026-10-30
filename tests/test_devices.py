"""Tests for XML parsing: units, capabilities, edge cases."""

from __future__ import annotations

import unittest

from hase10 import models
from hase10.devices import parse_devicelist
from tests.mock_fritz import MockBox


class ParseDevicelistTest(unittest.TestCase):
    def setUp(self):
        self.box = MockBox().start()
        self.addCleanup(self.box.stop)
        self.devices = parse_devicelist(self.box.devicelist_xml())
        self.by_name = {device.name: device for device in self.devices}

    def test_all_devices_and_group_are_parsed(self):
        self.assertEqual(len(self.devices), 4)
        group = self.by_name["Gruppe Wohnzimmer"]
        self.assertTrue(group.is_group)
        self.assertEqual(group.members, [16])

    def test_kinds_follow_the_functionbitmask(self):
        self.assertEqual(self.by_name["Kaffeemaschine"].kind, "switch")
        self.assertEqual(self.by_name["Wohnzimmer"].kind, "thermostat")
        self.assertEqual(self.by_name["Flur Taster"].kind, "button")

    def test_capabilities(self):
        plug = self.by_name["Kaffeemaschine"]
        self.assertTrue(plug.is_switchable)
        self.assertTrue(plug.has_power_meter)
        self.assertTrue(plug.has_temperature_sensor)
        self.assertFalse(plug.is_thermostat)
        self.assertTrue(self.by_name["Wohnzimmer"].is_thermostat)

    def test_units_are_converted(self):
        plug = self.by_name["Kaffeemaschine"]
        self.assertAlmostEqual(plug.temperature_c, 28.5)      # 285 * 0.1
        self.assertAlmostEqual(plug.power.power_w, 12.345)    # 12345 mW
        self.assertAlmostEqual(plug.power.voltage_v, 230.252)  # 230252 mV
        self.assertEqual(plug.power.energy_wh, 707)

    def test_thermostat_values(self):
        thermostat = self.by_name["Wohnzimmer"].thermostat
        self.assertAlmostEqual(thermostat.actual_c, 21.0)     # tist 42 in 0.5 degree steps
        self.assertAlmostEqual(thermostat.setpoint_c, 21.0)   # tsoll 42
        self.assertAlmostEqual(thermostat.comfort_c, 22.0)
        self.assertAlmostEqual(thermostat.eco_c, 17.0)
        self.assertEqual(thermostat.battery, 80)
        self.assertIs(thermostat.battery_low, False)
        self.assertIs(thermostat.window_open, False)
        self.assertEqual(thermostat.error_code, 0)
        self.assertAlmostEqual(thermostat.next_change_c, 17.0)

    def test_empty_values_become_none(self):
        button = self.by_name["Flur Taster"]
        self.assertIsNone(button.temperature_c, "an empty <celsius> means unknown")
        self.assertFalse(button.present)
        self.assertTrue(button.battery_low)
        self.assertEqual(button.battery, 12)

    def test_switch_state(self):
        plug = self.by_name["Kaffeemaschine"]
        self.assertIs(plug.switch.state, True)
        self.assertEqual(plug.switch.mode, "auto")

    def test_summary_is_json_ready(self):
        summary = self.by_name["Wohnzimmer"].summary()
        self.assertEqual(summary["kind"], "thermostat")
        self.assertEqual(summary["thermostat"]["setpoint_c"], 21.0)
        self.assertIn("ain", summary)


class ThermostatMagicValuesTest(unittest.TestCase):
    def test_on_and_off_magic_values(self):
        self.assertEqual(models.hkr_state("254"), "on")
        self.assertEqual(models.hkr_state("253"), "off")
        self.assertIsNone(models.hkr_celsius("254"))
        self.assertIsNone(models.hkr_celsius("253"))
        self.assertEqual(models.hkr_celsius("32"), 16.0)

    def test_invalid_and_empty(self):
        self.assertIsNone(models.celsius("inval"))
        self.assertIsNone(models.celsius(""))
        self.assertIsNone(models.celsius(None))
        self.assertIsNone(models.watts("inval"))

    def test_hkr_xml_with_on_off(self):
        xml = (
            '<devicelist version="1">'
            '<device identifier="1" id="1" functionbitmask="64" fwversion="1" '
            'manufacturer="AVM" productname="FRITZ!DECT 301">'
            "<present>1</present><name>Test</name>"
            "<hkr><tist></tist><tsoll>254</tsoll><komfort>44</komfort><absenk>34</absenk></hkr>"
            "</device></devicelist>"
        )
        device = parse_devicelist(xml)[0]
        self.assertEqual(device.thermostat.setpoint_state, "on")
        self.assertIsNone(device.thermostat.setpoint_c)
        self.assertIsNone(device.thermostat.actual_c)


if __name__ == "__main__":
    unittest.main()
