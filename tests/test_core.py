#!/usr/bin/env python3
"""Unit tests for ADB Wireless Manager core logic (no GUI / no device needed)."""
import os
import re
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


class SuspendTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        self._orig = adbtray.SUSPENDED_FILE
        adbtray.SUSPENDED_FILE = self.path

    def tearDown(self):
        adbtray.SUSPENDED_FILE = self._orig
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_add_list_clear_roundtrip(self):
        self.assertEqual(adbtray.suspended_serials(), [])
        adbtray.suspend_add("R5CX15D4P7P")
        self.assertTrue(adbtray.is_suspended("R5CX15D4P7P"))
        adbtray.suspend_add("RZCN8017T9E")
        self.assertEqual(len(adbtray.suspended_serials()), 2)
        adbtray.suspend_del("R5CX15D4P7P")
        self.assertFalse(adbtray.is_suspended("R5CX15D4P7P"))
        self.assertTrue(adbtray.is_suspended("RZCN8017T9E"))

    def test_add_same_serial_no_duplicates(self):
        adbtray.suspend_add("SER1")
        adbtray.suspend_add("SER1")
        self.assertEqual(adbtray.suspended_serials(), ["SER1"])

    def test_case_insensitive_matching(self):
        adbtray.suspend_add("serial1")
        self.assertTrue(adbtray.is_suspended("SERIAL1"))

    def test_empty_serial_is_ignored(self):
        adbtray.suspend_add("")
        self.assertFalse(adbtray.is_suspended(""))


class MdnsEntryTests(unittest.TestCase):
    def test_entries_include_serial_and_target(self):
        with mock.patch.object(adbtray, "sh", return_value=MDNS_SAMPLE):
            entries = adbtray.mdns_entries()
        pairs = {t: s for s, t in entries}
        self.assertEqual(pairs["192.168.100.100:37123"], "R5CX15D4P7P")
        self.assertEqual(pairs["192.168.100.65:44115"], "RZCN8017T9E")

    def test_cached_entries_include_serial(self):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        orig = adbtray.CACHE_FILE
        adbtray.CACHE_FILE = path
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("MY serial\t10.0.0.9\t5555\tNexus\t123\n")
            rows = adbtray.cached_entries()
            self.assertEqual(rows[0][0], "MY serial".upper().replace(
                "MY SERIAL", "MY serial"))
            self.assertEqual(rows[0][1], "10.0.0.9:5555")
            self.assertEqual(rows[0][2], "Nexus")
        finally:
            adbtray.CACHE_FILE = orig
            os.unlink(path)


class PlatformTests(unittest.TestCase):
    def test_data_dir_contains_app_folder(self):
        self.assertTrue(adbtray.data_dir().endswith("adbconnect"))

    def test_paths_are_absolute(self):
        self.assertTrue(os.path.isabs(adbtray.CACHE_FILE))
        self.assertTrue(os.path.isabs(adbtray.LOG_FILE))

    def test_version_defined(self):
        self.assertGreaterEqual(int(adbtray.__version__.split(".")[0]), 12)


class V13FeatureTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self._orig = adbtray.SETTINGS_FILE
        self._sdata = dict(adbtray.S.data)

    def tearDown(self):
        adbtray.S = adbtray.Settings(self._orig)
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_settings_roundtrip(self):
        s = adbtray.Settings(self.path)
        s.set("poll_interval_sec", 42)
        s.set("aliases", {"ABC123": "هاتفي"})
        s.save()
        s2 = adbtray.Settings(self.path)
        self.assertEqual(s2.get("poll_interval_sec"), 42)
        self.assertEqual(s2.get("aliases")["ABC123"], "هاتفي")

    def test_settings_defaults_on_missing_file(self):
        s = adbtray.Settings("/nonexistent/x.json")
        self.assertEqual(s.get("poll_interval_sec"),
                         adbtray.DEFAULT_SETTINGS["poll_interval_sec"])

    def test_version_is_newer(self):
        self.assertTrue(adbtray.version_is_newer("13.1.0", "13.0.0"))
        self.assertTrue(adbtray.version_is_newer("14", "13.9.9"))
        self.assertFalse(adbtray.version_is_newer("13.0.0", "13.0.0"))
        self.assertFalse(adbtray.version_is_newer("12.9", "13.0"))

    def test_resolve_label_alias_priority(self):
        aliases = {"SER1": "Work phone"}
        self.assertEqual(
            adbtray.resolve_label("SER1", "Pixel 7", aliases), "Work phone")
        self.assertEqual(
            adbtray.resolve_label("OTHER", "Pixel 7", aliases), "Pixel 7")

    def test_parse_device_info_full_sample(self):
        sample = ("SM S928U1\n"
                  "15\n"
                  "R5CX15D4P7P\n"
                  "  level: 73\n"
                  "109G   38G   71G  35%  /data\n"
                  "2: wlan0    inet 192.168.100.100/24 brd scope global wlan0\n"
                  "6: rmnet0    inet 10.0.0.5/30 scope global rmnet0\n")
        info = adbtray.parse_device_info(sample)
        self.assertEqual(info["model"], "SM S928U1")
        self.assertEqual(info["android"], "15")
        self.assertEqual(info["serial"], "R5CX15D4P7P")
        self.assertEqual(info["battery"], 73)
        self.assertEqual(info["storage"][2], "35")
        self.assertIn("192.168.100.100", info["ips"])
        self.assertNotIn("10.0.0.5", info["ips"])

    def test_battery_regex_from_dumpsys(self):
        raw = "  AC powered: false\n  level: 58\n  Charge counter: 1\n"
        m = re.search(r"level:\s*(\d+)", raw)
        self.assertEqual(int(m.group(1)), 58)

    def test_is_apk_filter(self):
        self.assertTrue(adbtray.is_apk("app.APK"))
        self.assertFalse(adbtray.is_apk("photo.png"))

    def test_tr_respects_language(self):
        original = adbtray.S.get("lang")
        try:
            adbtray.S.set("lang", "en")
            self.assertEqual(adbtray.tr("مرحبا", "Hello"), "Hello")
            adbtray.S.set("lang", "ar")
            self.assertEqual(adbtray.tr("مرحبا", "Hello"), "مرحبا")
        finally:
            adbtray.S.set("lang", original)

    def test_mdns_pairing_targets_parse(self):
        sample = ("List of mdns services\n"
                  "\tabd-x._adb-tls-pairing._tcp\t192.168.1.9:39999\n"
                  "\tabd-y._adb-tls-connect._tcp\t192.168.1.8:44444\n")
        with mock.patch.object(adbtray, "sh", return_value=sample):
            tg = adbtray.mdns_pairing_targets()
        self.assertEqual(tg, ["192.168.1.9:39999"])

    def test_config_dir_shape(self):
        d = adbtray.config_dir()
        self.assertTrue(d.endswith("adbconnect"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
