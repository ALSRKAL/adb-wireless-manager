#!/usr/bin/env python3
"""Unit tests for ADB Wireless Manager core logic (no GUI / no device needed)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tray"))

import adbtray  # noqa: E402


ADB_DEVICES_MIXED = """List of devices attached
R5CX15D4P7P             device usb:3-1 product:e3q model:SM_S928U1 device:e3q
192.168.100.65:5555     device product:c2s model:SM_N986B device:c2s
emulator-5554           offline
ABC123XYZ               unauthorized
"""

MDNS_SAMPLE = """List of discovered mdns services
	adb-R5CX15D4P7P-abc123._adb-tls-connect._tcp	192.168.100.100:37123
	adb-RZCN8017T9E-xyz789._adb-tls-connect._tcp	192.168.100.65:44115
	adb-RZCN8017T9E-xyz789._adb-tls-connect._tcp	192.168.100.65:44115
	adb-something._adb-tls-pairing._tcp	192.168.100.99:39999
"""

PHONE_IP_SAMPLE = """2: wlan0    inet 192.168.100.100/24 brd 192.168.100.255 scope global wlan0
5: rmnet0    inet 10.44.12.9/30 scope global rmnet0
7: tun0      inet 192.0.0.4/32 scope global tun0
8: eth0      inet 192.168.55.2/24 scope global eth0
"""

SS_BUSY_SAMPLE = """Netid State  Recv-Q Send-Q Local Address:Port Peer Address:Port
udp   UNCONN 0      0      0.0.0.0:5555       0.0.0.0:*
tcp   LISTEN 0      5      0.0.0.0:5556       0.0.0.0:*
"""


class ListDevicesTests(unittest.TestCase):
    def test_parses_wifi_usb_and_models(self):
        with mock.patch.object(adbtray, "sh",
                               return_value=ADB_DEVICES_MIXED):
            devs = adbtray.list_devices()
        self.assertEqual(len(devs), 4)
        by_serial = {d["serial"]: d for d in devs}
        self.assertEqual(by_serial["R5CX15D4P7P"]["model"], "SM S928U1")
        self.assertTrue(by_serial["R5CX15D4P7P"]["usb"])
        self.assertEqual(by_serial["192.168.100.65:5555"]["model"],
                         "SM N986B")
        self.assertFalse(by_serial["192.168.100.65:5555"]["usb"])

    def test_states_are_preserved(self):
        with mock.patch.object(adbtray, "sh",
                               return_value=ADB_DEVICES_MIXED):
            devs = adbtray.list_devices()
        states = {d["serial"]: d["state"] for d in devs}
        self.assertEqual(states["R5CX15D4P7P"], "device")
        self.assertEqual(states["emulator-5554"], "offline")
        self.assertEqual(states["ABC123XYZ"], "unauthorized")

    def test_adb_failure_returns_empty(self):
        with mock.patch.object(adbtray, "sh", return_value=""):
            self.assertEqual(adbtray.list_devices(), [])


class CacheTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        self._orig = adbtray.CACHE_FILE
        adbtray.CACHE_FILE = self.path

    def tearDown(self):
        adbtray.CACHE_FILE = self._orig
        os.unlink(self.path)

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_cached_targets_parse(self):
        self._write("SER\t1.2.3.4\t5555\tPixel_6\t1700000000\n")
        rows = adbtray.cached_targets()
        self.assertEqual(rows, [("1.2.3.4:5555", "Pixel 6")])

    def test_missing_file_yields_empty(self):
        adbtray.CACHE_FILE = "/nonexistent/nope.tsv"
        self.assertEqual(adbtray.cached_targets(), [])

    def test_drop_from_cache_removes_only_target_row(self):
        self._write("A\t1.1.1.1\t5555\tDev_A\t1\n"
                    "B\t2.2.2.2\t5556\tDev_B\t2\n")
        self.assertTrue(adbtray.drop_from_cache("1.1.1.1:5555"))
        rows = adbtray.cached_targets()
        self.assertEqual(rows, [("2.2.2.2:5556", "Dev B")])

    def test_drop_from_cache_no_match_keeps_all(self):
        self._write("A\t1.1.1.1\t5555\tDev_A\t1\n")
        self.assertTrue(adbtray.drop_from_cache("9.9.9.9:9999"))
        self.assertEqual(len(adbtray.cached_targets()), 1)


class MdnsTests(unittest.TestCase):
    def test_parses_connect_targets_only_and_dedupes(self):
        with mock.patch.object(adbtray, "sh", return_value=MDNS_SAMPLE):
            targets = adbtray.mdns_targets()
        self.assertEqual(targets,
                         ["192.168.100.100:37123", "192.168.100.65:44115"])

    def test_empty_output(self):
        with mock.patch.object(adbtray, "sh", return_value=""):
            self.assertEqual(adbtray.mdns_targets(), [])


class PhoneIpsTests(unittest.TestCase):
    def test_wlan_first_mobile_and_vpn_skipped(self):
        with mock.patch.object(adbtray, "sh", return_value=PHONE_IP_SAMPLE):
            ips = adbtray.phone_ips("ANY")
        self.assertEqual(ips[0], "192.168.100.100")
        self.assertIn("192.168.55.2", ips)
        self.assertNotIn("10.44.12.9", ips)
        self.assertNotIn("192.0.0.4", ips)


class PortTests(unittest.TestCase):
    def test_port_busy_detects_listeners_linux_ss(self):
        with mock.patch.object(adbtray, "sh", return_value=SS_BUSY_SAMPLE), \
             mock.patch.object(adbtray.shutil, "which", return_value="/usr/bin/ss"):
            self.assertTrue(adbtray.port_busy(5555))
            self.assertTrue(adbtray.port_busy(5556))
            self.assertFalse(adbtray.port_busy(5557))

    def test_next_free_port_skips_busy(self):
        busy = {5555, 5556}
        with mock.patch.object(adbtray, "port_busy",
                               side_effect=lambda p: p in busy):
            self.assertEqual(adbtray.next_free_port(), 5557)
        with mock.patch.object(adbtray, "port_busy", return_value=False):
            self.assertEqual(adbtray.next_free_port(5555), 5555)


class PlatformTests(unittest.TestCase):
    def test_data_dir_contains_app_folder(self):
        self.assertTrue(adbtray.data_dir().endswith("adbconnect"))

    def test_paths_are_absolute(self):
        self.assertTrue(os.path.isabs(adbtray.CACHE_FILE))
        self.assertTrue(os.path.isabs(adbtray.LOG_FILE))

    def test_version_defined(self):
        self.assertGreaterEqual(int(adbtray.__version__.split(".")[0]), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
