#!/usr/bin/env python3
"""Unit tests for ADB Wireless Manager core logic (no GUI / no device needed)."""
import os
import re
import importlib.util
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tray"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import adbtray  # noqa: E402


_qt_app = None


def ensure_qt_app():
    global _qt_app
    from PyQt5.QtWidgets import QApplication
    if QApplication.instance() is None:
        _qt_app = QApplication([])


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


WIN_NETSTAT_SAMPLE = """
  TCP    0.0.0.0:5555           0.0.0.0:0              LISTENING
  TCP    [::]:445               [::]:0                 LISTENING
"""


class PortTests(unittest.TestCase):
    def test_ss_parser(self):
        self.assertTrue(adbtray._busy_from_ss(SS_BUSY_SAMPLE, 5555))
        self.assertTrue(adbtray._busy_from_ss(SS_BUSY_SAMPLE, 5556))
        self.assertFalse(adbtray._busy_from_ss(SS_BUSY_SAMPLE, 5557))

    def test_netstat_windows_parser(self):
        self.assertTrue(adbtray._busy_from_netstat(WIN_NETSTAT_SAMPLE, 5555))
        self.assertFalse(adbtray._busy_from_netstat(WIN_NETSTAT_SAMPLE, 5599))

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
                  "\tadb-x._adb-tls-pairing._tcp\t192.168.1.9:39999\n"
                  "\tadb-y._adb-tls-connect._tcp\t192.168.1.8:44444\n")
        with mock.patch.object(adbtray, "sh", return_value=sample):
            tg = adbtray.mdns_pairing_targets()
        self.assertEqual(tg, ["192.168.1.9:39999"])

    def test_config_dir_shape(self):
        d = adbtray.config_dir()
        self.assertTrue(d.endswith("adbconnect"))


class V1301Tests(unittest.TestCase):
    def test_build_pairing_uri_format(self):
        uri = adbtray.build_pairing_uri("awm-192-168-1-5", "123456@192.168.1.5:37843")
        self.assertEqual(uri,
                         "WIFI:T:ADB;S:awm-192-168-1-5;"
                         "P:123456@192.168.1.5:37843;;")

    def test_readiness_unknown_state_gives_guidance(self):
        items = adbtray.evaluate_readiness(None, None)
        self.assertEqual(len(items), 3)
        self.assertTrue(all(ok is None for ok, _, _ in items))

    def test_readiness_authorized_wireless_on(self):
        items = adbtray.evaluate_readiness("device", True)
        flags = [ok for ok, _, _ in items]
        self.assertEqual(flags, [True, True, True])

    def test_readiness_unauthorized_flags_fix(self):
        items = adbtray.evaluate_readiness("unauthorized", False)
        flags = [ok for ok, _, _ in items]
        self.assertEqual(flags, [True, False, False])

    @unittest.skipUnless(importlib.util.find_spec("qrcode"),
                         "qrcode not installed")
    def test_qr_png_generated_and_decodable_size(self):
        ensure_qt_app()
        pm = adbtray.make_qr_pixmap(
            adbtray.build_pairing_uri("test", "123456@1.2.3.4:5555"))
        self.assertIsNotNone(pm)
        self.assertGreater(pm.width(), 50)

    def test_version_patch_compare_for_release_flow(self):
        self.assertTrue(adbtray.version_is_newer("13.0.1", "13.0.0"))
        self.assertFalse(adbtray.version_is_newer("13.0.1", "13.0.1"))


class GuiSmokeTests(unittest.TestCase):
    """Headless construction tests — catch silent slot crashes."""

    def test_pair_dialog_builds(self):
        ensure_qt_app()
        d = adbtray.PairDialog()
        self.assertTrue(d.readiness.text())
        self.assertEqual(d._tabs.currentIndex(), 1)

    def test_pair_dialog_tab_order_scan_first(self):
        ensure_qt_app()
        d = adbtray.PairDialog(start_tab="scan")
        self.assertEqual(d._tabs.currentIndex(), 0)
        self.assertTrue(d.scan_uri.startswith("WIFI:T:ADB;"))

    @unittest.skipUnless(importlib.util.find_spec("qrcode"),
                         "qrcode not installed")
    def test_pair_dialog_tabs_actually_visible(self):
        ensure_qt_app()
        d = adbtray.PairDialog(start_tab="scan")
        d.resize(500, 660)
        d.show()
        try:
            self.assertTrue(d._tabs.isVisible(),
                            "tabs must be added to the dialog layout")
            self.assertTrue(d.scan_qr_img.pixmap() is not None
                            and not d.scan_qr_img.pixmap().isNull(),
                            "scan tab must show a QR pixmap")
        finally:
            d.hide()

    def test_pair_dialog_qr_start_tab(self):
        ensure_qt_app()
        d = adbtray.PairDialog(start_tab="qr")
        self.assertEqual(d._tabs.currentIndex(), 2)

    def test_mdns_target_for_serial(self):
        entries = [("R5CX15D4P7P", "192.168.1.5:1234"),
                   ("RZCN8017T9E", "192.168.1.6:5678")]
        self.assertEqual(
            adbtray.mdns_target_for_serial(entries, "r5cx15d4p7p"),
            "192.168.1.5:1234")
        self.assertIsNone(adbtray.mdns_target_for_serial(entries, "NOPE"))

    def test_dropzone_has_close_button(self):
        ensure_qt_app()
        z = adbtray.DropZone(tray=None)
        self.assertTrue(hasattr(z, "close_btn"))
        self.assertTrue(z.close_btn.isVisibleTo(z))

    def test_dropzone_close_hides_and_saves(self):
        ensure_qt_app()
        z = adbtray.DropZone(tray=None)
        orig = adbtray.S.get("dropzone_geometry")
        try:
            z.show()
            z.close_me()
            self.assertFalse(z.isVisible())
        finally:
            adbtray.S.set("dropzone_geometry", orig)
            adbtray.S.save()


class V1303Tests(unittest.TestCase):
    def test_gen_pair_creds_format(self):
        name, pwd = adbtray.gen_pair_creds()
        self.assertTrue(re.match(r"^awm-\d{6}$", name))
        self.assertEqual(len(pwd), 6)
        self.assertTrue(re.match(r"^[A-Z0-9]{6}$", pwd))

    def test_find_pairing_service_matches_exact_name(self):
        out = ("\tawm-123456._adb-tls-pairing._tcp\t192.168.1.9:39999\n"
               "\tother._adb-tls-pairing._tcp\t10.0.0.1:11111\n")
        self.assertEqual(adbtray.find_pairing_service(out, "awm-123456"),
                         "192.168.1.9:39999")
        self.assertIsNone(adbtray.find_pairing_service(out, "nope"))

    def test_find_connect_service(self):
        out = "\tadb-x._adb-tls-connect._tcp\t192.168.100.100:37123\n"
        self.assertEqual(adbtray.find_connect_service(out),
                         "192.168.100.100:37123")
        self.assertIsNone(adbtray.find_connect_service("nothing here"))

    def test_mdns_entries_include_classic_adb_tcp(self):
        out = ("List of discovered mdns services\n"
               "\tadb-R5CX15D4P7P\t_adb._tcp\t192.168.100.100:5555\n"
               "\tadb-R5CX15D4P7P-abc123._adb-tls-connect._tcp"
               "\t192.168.100.100:37123\n")
        with mock.patch.object(adbtray, "sh", return_value=out):
            entries = adbtray.mdns_entries()
        self.assertIn(("R5CX15D4P7P", "192.168.100.100:5555"), entries)
        self.assertIn(("R5CX15D4P7P", "192.168.100.100:37123"), entries)

    def test_save_cache_entry_inserts_and_updates(self):
        fd, path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        orig = adbtray.CACHE_FILE
        adbtray.CACHE_FILE = path
        try:
            adbtray.save_cache_entry("S1", "10.0.0.5:5555", "Dev A")
            adbtray.save_cache_entry("S2", "10.0.0.6:5556", "Dev B")
            rows = dict((s, (t, l)) for s, t, l in
                        [(c[0], c[1], c[2]) for c in adbtray.cached_entries()])
            self.assertEqual(rows["S1"], ("10.0.0.5:5555", "Dev A"))
            adbtray.save_cache_entry("S1", "172.16.0.9:4444", "Dev A")
            entries = {c[0]: c[1] for c in adbtray.cached_entries()}
            self.assertEqual(entries["S1"], "172.16.0.9:4444")
            self.assertEqual(len(entries), 2)
        finally:
            adbtray.CACHE_FILE = orig
            os.unlink(path)

    def test_scan_uri_uses_generated_creds(self):
        name, pwd = adbtray.gen_pair_creds()
        uri = adbtray.build_pairing_uri(name, pwd)
        self.assertIn(f"S:{name};", uri)
        self.assertIn(f"P:{pwd};;", uri)


    def test_ensure_official_adb_path_prepends(self):
        fd_dir = tempfile.mkdtemp()
        try:
            open(os.path.join(fd_dir, "adb" if os.name != "nt"
                              else "adb.exe"), "w").close()
            saved = os.environ.get("PATH", "")
            orig = adbtray.official_tools_dir
            adbtray.official_tools_dir = lambda: fd_dir
            try:
                res = adbtray.ensure_official_adb_path()
                self.assertTrue(res)
                self.assertTrue(os.environ["PATH"].startswith(fd_dir))
            finally:
                adbtray.official_tools_dir = orig
                os.environ["PATH"] = saved
        finally:
            import shutil as _sh
            _sh.rmtree(fd_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class OpGateTests(unittest.TestCase):
    """The operation gate must never stick: expiry, queue, watchdog."""

    def test_gate_expiry_runs_immediately(self):
        ensure_qt_app()
        t = adbtray.Tray()
        ran = []
        t.set_busy(True, timeout=0.15)
        time.sleep(0.2)
        t.run_job(lambda: ran.append(1), blocking=True, timeout=30)
        self.assertEqual(ran, [1])
        self.assertTrue(t.busy)
        t.on_busy_release(t._busy_owner)
        self.assertFalse(t.busy)

    def test_gate_queues_while_busy_then_releases(self):
        ensure_qt_app()
        t = adbtray.Tray()
        ran = []

        def first():
            time.sleep(1.2)
            ran.append("first")

        t.run_job(first, blocking=True, timeout=30)
        deadline = time.time() + 2
        while not ran and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(ran, ["first"])
        t.run_job(lambda: ran.append("q"), blocking=True)
        self.assertEqual(ran, ["first"])
        self.assertIsNotNone(t._queued)
        t.on_busy_release(t._busy_owner)
        deadline = time.time() + 2
        while len(ran) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(ran, ["first", "q"])
        t.on_busy_release(t._busy_owner)
        self.assertFalse(t.busy)

    def test_watchdog_clears_expired_busy(self):
        ensure_qt_app()
        t = adbtray.Tray()
        t.set_busy(True, timeout=0.1)
        time.sleep(0.2)
        t.watchdog_tick()
        self.assertFalse(t.busy)

    def test_foreign_release_ignored(self):
        ensure_qt_app()
        t = adbtray.Tray()
        t.set_busy(True, timeout=30)
        t.on_busy_release(object())
        self.assertTrue(t.busy)
        t.on_busy_release(None)
        self.assertFalse(t.busy)


if __name__ == "__main__":
    unittest.main(verbosity=2)
