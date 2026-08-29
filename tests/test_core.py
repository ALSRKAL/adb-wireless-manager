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
        """Serials are normalised on read so casing can never split a device."""
        fd, path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        orig = adbtray.CACHE_FILE
        adbtray.CACHE_FILE = path
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("my serial\t10.0.0.9\t5555\tNexus\t123\n")
            rows = adbtray.cached_entries()
            self.assertEqual(rows[0][0], "MY SERIAL")
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

    def test_reconnect_one_job_runs_without_unbound(self):
        ensure_qt_app()
        t = adbtray.Tray()
        msgs = []
        t.op_done.connect(lambda title, msg: msgs.append(msg))
        with mock.patch.object(adbtray, "sh", return_value=""), \
             mock.patch.object(adbtray, "list_devices", return_value=[]), \
             mock.patch.object(adbtray, "mdns_entries", return_value=[]), \
             mock.patch.object(adbtray, "device_serial",
                               return_value="TESTSER"):
            t.reconnect_one("1.2.3.4:5555", "")
            from PyQt5.QtWidgets import QApplication
            # the verified-connect path retries with backoff before giving up
            deadline = time.time() + 40
            while not msgs and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
        self.assertTrue(msgs, "operation must report a result")
        joined = " ".join(msgs)
        self.assertNotIn("cannot access local variable", joined)
        self.assertIn("1.2.3.4", joined)

    def test_foreign_release_ignored(self):
        ensure_qt_app()
        t = adbtray.Tray()
        t.set_busy(True, timeout=30)
        t.on_busy_release(object())
        self.assertTrue(t.busy)
        t.on_busy_release(None)
        self.assertFalse(t.busy)


class V13013Tests(unittest.TestCase):
    """APK install UX: error mapping, drop filtering, progress dialog."""

    def test_friendly_error_success_is_empty(self):
        self.assertEqual(adbtray.friendly_adb_error("Success"), "")
        self.assertEqual(
            adbtray.friendly_adb_error("Performing Streamed Install\nSuccess"),
            "")

    def test_friendly_error_storage_full(self):
        msg = adbtray.friendly_adb_error(
            "INSTALL_FAILED_INSUFFICIENT_STORAGE")
        self.assertTrue(msg)

    def test_friendly_error_signature(self):
        out = "INSTALL_FAILED_UPDATE_INCOMPATIBLE: package signatures"
        msg = adbtray.friendly_adb_error(out)
        self.assertTrue(msg)
        self.assertNotIn("unknown", msg.lower())

    def test_friendly_error_downgrade(self):
        self.assertTrue(
            adbtray.friendly_adb_error("INSTALL_FAILED_VERSION_DOWNGRADE"))

    def test_friendly_error_cancelled_on_device(self):
        self.assertTrue(
            adbtray.friendly_adb_error("Install canceled by user"))

    def test_friendly_error_invalid_apk(self):
        self.assertTrue(
            adbtray.friendly_adb_error("INSTALL_FAILED_INVALID_APK"))

    def test_friendly_error_offline(self):
        self.assertTrue(adbtray.friendly_adb_error("error: device offline"))

    def test_friendly_error_device_not_found(self):
        out = "adb: error: failed to get feature set: " \
              "device 'R5CX15D4P7P' not found"
        self.assertTrue(adbtray.friendly_adb_error(out))

    def test_friendly_error_unknown_keeps_raw_line(self):
        out = "adb: some totally novel failure"
        msg = adbtray.friendly_adb_error(out)
        self.assertIn("some totally novel failure", msg)

    def test_friendly_error_missing_file(self):
        out = "adb: failed to stat /path/with spaces/app.apk: " \
              "No such file or directory"
        msg = adbtray.friendly_adb_error(out)
        self.assertIn("app.apk", msg)

    def test_run_one_missing_file_reports_error(self):
        """A vanished/moved APK must fail gracefully, not crash."""
        ensure_qt_app()
        t = adbtray.Tray()
        msgs = []
        t.op_done.connect(lambda title, msg: msgs.append(msg))
        fake_dev = {"serial": "SERX", "model": "m", "state": "device"}
        missing = "/tmp/definitely_gone_%d.apk" % time.time_ns()
        with mock.patch.object(adbtray.subprocess, "Popen",
                               side_effect=FileNotFoundError("adb")):
            t._do_install([missing], fake_dev)
            from PyQt5.QtWidgets import QApplication
            deadline = time.time() + 5
            while not msgs and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
        self.assertTrue(msgs, "must report even when the file is gone")
        self.assertIn(os.path.basename(missing), "\n".join(msgs))

    def test_install_progress_dialog_flow(self):
        ensure_qt_app()
        from PyQt5.QtWidgets import QApplication
        dlg = adbtray.InstallProgressDialog(3, "t")
        dlg.show()
        self.assertFalse(dlg.cancel_requested())
        dlg.step_changed.emit("app.apk", 1)
        QApplication.processEvents()
        self.assertEqual(dlg.bar.value(), 1)
        self.assertIn("app.apk", dlg.file_label.text())
        dlg.request_cancel()
        self.assertTrue(dlg.cancel_requested())
        self.assertFalse(dlg.cancel_btn.isEnabled())
        # finish must close via the queued signal, not fake the bar
        bar_before = dlg.bar.value()
        dlg.finish()
        QApplication.processEvents()
        self.assertEqual(dlg.bar.value(), bar_before)

    def test_do_install_reports_reasons_and_skips(self):
        ensure_qt_app()
        from PyQt5.QtWidgets import QApplication
        t = adbtray.Tray()
        msgs = []
        t.op_done.connect(lambda title, msg: msgs.append(msg))
        fake_dev = {"serial": "SERX", "model": "m", "state": "device"}

        class FakeProc:
            def __init__(self, out):
                self._out = out

            def poll(self):
                return 0

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

        good = FakeProc("Success\n")
        bad = FakeProc("INSTALL_FAILED_INSUFFICIENT_STORAGE\n")

        def fake_popen(cmd, stdout=None, stderr=None, stdin=None):
            return bad if "bad.apk" in cmd else good

        with mock.patch.object(adbtray.subprocess, "Popen",
                               side_effect=fake_popen):
            t._do_install(["/tmp/good.apk", "/tmp/bad.apk"], fake_dev,
                          rejected=["notes.txt"])
            deadline = time.time() + 5
            while not msgs and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
        self.assertTrue(msgs, "install must report a result")
        joined = "\n".join(msgs)
        self.assertIn("good.apk", joined)
        self.assertIn("bad.apk", joined)

    def test_do_install_while_busy_never_orphans_dialog(self):
        """Queued install must not flash a dialog before it runs."""
        ensure_qt_app()
        from PyQt5.QtWidgets import QApplication
        t = adbtray.Tray()
        release = []

        def holder():  # occupies the gate like a real running operation
            time.sleep(1.0)
            release.append(1)

        t.run_job(holder, blocking=True, timeout=30)
        fake_dev = {"serial": "SERX", "model": "m", "state": "device"}
        install_calls = []

        def counting(*args, **kwargs):
            cmd = list(args[0])
            if len(cmd) >= 5 and cmd[3] == "install":
                install_calls.append(cmd)
            proc = mock.MagicMock()
            proc.poll.return_value = 0
            proc.wait.return_value = 0
            return proc

        with mock.patch.object(adbtray.subprocess, "Popen",
                               side_effect=counting):
            t._do_install(["/tmp/x.apk"], fake_dev)
            QApplication.processEvents()
            # run_job queued it (busy) — preflight must have skipped show()
            self.assertIsNotNone(t._queued)
            deadline = time.time() + 8
            while not release and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
            # after the holder finishes, queued install runs for real
            deadline = time.time() + 8
            while not install_calls and time.time() < deadline:
                QApplication.processEvents()
                time.sleep(0.02)
            # pump a little longer to catch any double execution
            t0 = time.time()
            while time.time() - t0 < 1.0:
                QApplication.processEvents()
                time.sleep(0.02)
        self.assertEqual(len(install_calls), 1,
                         f"queued install must execute exactly once, "
                         f"got {len(install_calls)}")


PROBE_SAMPLE = """AWM_SERIAL=r5cx15d4p7p
AWM_MODEL=SM_S928U1
AWM_SDK=34
AWM_WIFI=1
AWM_BATTERY=73
"""


class IdentityTests(unittest.TestCase):
    """Serial normalisation is the anchor for every de-duplication rule."""

    def test_norm_serial_trims_and_uppercases(self):
        self.assertEqual(adbtray.norm_serial("  r5cx15d4p7p\n"), "R5CX15D4P7P")
        self.assertEqual(adbtray.norm_serial(None), "")
        self.assertEqual(adbtray.norm_serial(""), "")

    def test_split_target_handles_usb_serial(self):
        self.assertEqual(adbtray.split_target("192.168.1.5:41234"),
                         ("192.168.1.5", "41234"))
        self.assertEqual(adbtray.split_target("R5CX15D4P7P"),
                         ("R5CX15D4P7P", ""))
        self.assertEqual(adbtray.target_host("10.0.0.9:5555"), "10.0.0.9")

    def test_is_network_target(self):
        self.assertTrue(adbtray.is_network_target("10.0.0.9:5555"))
        self.assertTrue(adbtray.is_network_target("adb-XYZ-abc._tcp"))
        self.assertFalse(adbtray.is_network_target("R5CX15D4P7P"))


class DeviceProbeTests(unittest.TestCase):
    def test_parses_every_field(self):
        p = adbtray.parse_device_probe(PROBE_SAMPLE)
        self.assertEqual(p["serial"], "R5CX15D4P7P")
        self.assertEqual(p["model"], "SM S928U1")
        self.assertEqual(p["sdk"], 34)
        self.assertIs(p["adb_wifi"], True)
        self.assertEqual(p["battery"], 73)
        self.assertTrue(p["alive"])

    def test_missing_fields_do_not_shift_others(self):
        p = adbtray.parse_device_probe(
            "AWM_SERIAL=ABC\nAWM_MODEL=\nAWM_SDK=\nAWM_WIFI=null\n"
            "AWM_BATTERY=\n")
        self.assertEqual(p["serial"], "ABC")
        self.assertEqual(p["model"], "")
        self.assertIsNone(p["sdk"])
        self.assertIsNone(p["adb_wifi"])
        self.assertIsNone(p["battery"])

    def test_no_answer_is_not_alive(self):
        p = adbtray.parse_device_probe("")
        self.assertFalse(p["alive"])
        self.assertEqual(p["serial"], "")

    def test_garbage_output_is_not_alive(self):
        p = adbtray.parse_device_probe("error: device offline\n")
        self.assertFalse(p["alive"])

    def test_wifi_off_is_false_not_none(self):
        self.assertIs(adbtray.parse_device_probe("AWM_WIFI=0")["adb_wifi"],
                      False)


class ConnectStrategyTests(unittest.TestCase):
    """`adb tcpip` restarts adbd and switches Wireless debugging off, so it
    must be the last option, never the first."""

    def test_existing_mdns_advert_wins(self):
        self.assertEqual(
            adbtray.plan_connect_strategy(34, True, "10.0.0.9:41234"),
            (adbtray.STRATEGY_MDNS, "10.0.0.9:41234"))

    def test_mdns_advert_wins_even_on_legacy_android(self):
        self.assertEqual(
            adbtray.plan_connect_strategy(28, None, "10.0.0.9:5555"),
            (adbtray.STRATEGY_MDNS, "10.0.0.9:5555"))

    def test_modern_without_advert_enables_wireless_debugging(self):
        self.assertEqual(adbtray.plan_connect_strategy(30, False, ""),
                         (adbtray.STRATEGY_ENABLE_WIFI, ""))
        self.assertEqual(adbtray.plan_connect_strategy(34, None, ""),
                         (adbtray.STRATEGY_ENABLE_WIFI, ""))

    def test_modern_with_toggle_on_waits_instead_of_restarting_adbd(self):
        self.assertEqual(adbtray.plan_connect_strategy(33, True, ""),
                         (adbtray.STRATEGY_AWAIT_MDNS, ""))

    def test_legacy_android_uses_tcpip(self):
        self.assertEqual(adbtray.plan_connect_strategy(29, None, ""),
                         (adbtray.STRATEGY_TCPIP, ""))

    def test_unknown_sdk_uses_tcpip(self):
        self.assertEqual(adbtray.plan_connect_strategy(None, None, ""),
                         (adbtray.STRATEGY_TCPIP, ""))

    def test_boundary_is_android_11(self):
        self.assertEqual(adbtray.WIRELESS_DEBUGGING_SDK, 30)
        self.assertEqual(adbtray.plan_connect_strategy(30, None, "")[0],
                         adbtray.STRATEGY_ENABLE_WIFI)
        self.assertEqual(adbtray.plan_connect_strategy(29, None, "")[0],
                         adbtray.STRATEGY_TCPIP)


class NoAdbdRestartOnHealingTests(unittest.TestCase):
    """Source guard for the reported bug: disconnecting or healing a link must
    never restart adbd, because that is what switches the phone's
    Wireless-debugging toggle off."""

    ROOT = os.path.join(os.path.dirname(__file__), "..")

    def _read(self, rel):
        with open(os.path.join(self.ROOT, rel), encoding="utf-8") as f:
            return f.read()

    # invocation shapes only, so the field name `"usb"` and prose mentioning
    # the command in comments do not trip the guard
    ADB_USB_CALLS = (
        r'\[\s*"adb"[^\]]*"usb"',          # python:  ["adb", "-s", s, "usb"]
        r'\badbq?\s+-s\s+"?\$\{?\w+\}?"?\s+usb\b',   # bash:  adb -s "$s" usb
        r'\badb\s+-s\s+\$\w+\s+usb\b',     # powershell: adb -s $s usb
    )

    def test_no_source_invokes_adb_usb(self):
        for rel in ("tray/adbtray.py", "scripts/adbconnect.sh",
                    "scripts/adbconnect.ps1"):
            text = self._read(rel)
            for pattern in self.ADB_USB_CALLS:
                self.assertIsNone(
                    re.search(pattern, text),
                    f"{rel} must not run `adb usb`: it drops the wireless "
                    f"listener and turns the toggle off")

    def test_healing_uses_adb_reconnect_offline(self):
        self.assertIn("reconnect", adbtray.heal_offline_transport.__doc__ or "")
        for rel in ("tray/adbtray.py", "scripts/adbconnect.sh",
                    "scripts/adbconnect.ps1"):
            self.assertIn("reconnect", self._read(rel))

    def test_tcpip_paths_restore_the_wireless_toggle(self):
        tray = self._read("tray/adbtray.py")
        self.assertIn("adb_wifi_enabled", tray)
        self.assertIn("enable_wireless_debugging", tray)
        sh_src = self._read("scripts/adbconnect.sh")
        self.assertIn("enable_wireless_debugging", sh_src)
        self.assertIn("adb_wifi_enabled", sh_src)

    def test_tool_output_carries_no_emoji(self):
        """No emoji anywhere in the shipped tools."""
        for rel in ("tray/adbtray.py", "scripts/adbconnect.sh",
                    "scripts/adbconnect.ps1", "install.sh", "install.ps1",
                    "uninstall.sh", "tests/run_tests.sh",
                    "README.md", "README.ar.md"):
            for ch in self._read(rel):
                o = ord(ch)
                self.assertFalse(
                    0x1F000 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF
                    or o in (0x2B50, 0xFE0F, 0x2705, 0x274C, 0x23F3, 0x23FA),
                    f"{rel} contains the decorative glyph {ch!r}")


class ConnectCandidateTests(unittest.TestCase):
    """A refused mDNS advert must not end the search: adverts outlive the
    service they describe, and a phone taken wireless the legacy way is still
    listening on the fixed port."""

    SER = "R5CX15D4P7P"

    def test_fresh_advert_comes_first_then_saved_then_classic(self):
        got = adbtray.connect_candidates(
            self.SER, "10.17.111.145:5555",
            [(self.SER, "10.17.111.145:36947")], 5555)
        self.assertEqual(got, ["10.17.111.145:36947", "10.17.111.145:5555"])

    def test_classic_port_is_added_for_an_advert_only_device(self):
        got = adbtray.connect_candidates(
            self.SER, "", [(self.SER, "10.17.111.145:36947")], 5555)
        self.assertEqual(got, ["10.17.111.145:36947", "10.17.111.145:5555"])

    def test_second_host_also_gets_the_classic_port(self):
        got = adbtray.connect_candidates(
            self.SER, "192.168.1.9:41111",
            [(self.SER, "10.0.0.5:36947")], 5555)
        self.assertEqual(got, ["10.0.0.5:36947", "192.168.1.9:41111",
                               "10.0.0.5:5555", "192.168.1.9:5555"])

    def test_no_duplicates_when_saved_port_is_the_classic_one(self):
        got = adbtray.connect_candidates(self.SER, "10.0.0.5:5555", [], 5555)
        self.assertEqual(got, ["10.0.0.5:5555"])

    def test_custom_classic_port_is_honoured(self):
        got = adbtray.connect_candidates(self.SER, "10.0.0.5:41000", [], 5599)
        self.assertEqual(got, ["10.0.0.5:41000", "10.0.0.5:5599"])

    def test_advert_for_another_serial_is_ignored(self):
        got = adbtray.connect_candidates(
            self.SER, "10.0.0.5:5555", [("OTHER", "10.0.0.9:36947")], 5555)
        self.assertEqual(got, ["10.0.0.5:5555"])

    def test_nothing_known_yields_nothing(self):
        self.assertEqual(adbtray.connect_candidates("", "", [], 5555), [])

    def test_serial_casing_still_matches_the_advert(self):
        got = adbtray.connect_candidates(
            "r5cx15d4p7p", "", [(self.SER, "10.0.0.5:36947")], 5555)
        self.assertEqual(got[0], "10.0.0.5:36947")


class FriendlyConnectErrorTests(unittest.TestCase):
    def test_success_has_no_reason(self):
        self.assertEqual(
            adbtray.friendly_connect_error("connected to 10.0.0.9:5555"), "")

    def test_refused_explains_the_toggle(self):
        msg = adbtray.friendly_connect_error(
            "failed to connect to '10.0.0.9:5555': Connection refused")
        self.assertTrue(msg)
        self.assertNotIn("unknown", msg.lower())

    def test_unreachable_and_timeout_are_distinct(self):
        a = adbtray.friendly_connect_error("No route to host")
        b = adbtray.friendly_connect_error("Operation timed out")
        self.assertTrue(a and b)
        self.assertNotEqual(a, b)

    def test_unknown_output_keeps_the_raw_line(self):
        self.assertIn("something odd",
                      adbtray.friendly_connect_error("something odd"))


class MergeDeviceViewTests(unittest.TestCase):
    """The duplicate-row bug: one phone answers to a USB serial, a stale
    cached ip:5555 and a fresh mDNS ip:41234 at the same time."""

    SER = "R5CX15D4P7P"

    def test_stale_cache_and_fresh_mdns_collapse_into_one_row(self):
        rows = adbtray.merge_device_view(
            devices=[{"serial": "192.168.1.5:41234", "state": "device",
                      "model": "SM S928U1", "usb": False}],
            serial_by_target={"192.168.1.5:41234": self.SER},
            cached=[(self.SER, "192.168.1.5:5555", "SM S928U1")],
            mdns=[(self.SER, "192.168.1.5:41234")],
            suspended=[])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["online"])
        self.assertEqual(rows[0]["net_target"], "192.168.1.5:41234")
        self.assertEqual(rows[0]["tag"], adbtray.TAG_WIFI)

    def test_usb_and_wireless_link_are_the_same_device(self):
        rows = adbtray.merge_device_view(
            devices=[{"serial": self.SER, "state": "device",
                      "model": "SM S928U1", "usb": True},
                     {"serial": "192.168.1.5:41234", "state": "device",
                      "model": "SM S928U1", "usb": False}],
            serial_by_target={self.SER: self.SER,
                              "192.168.1.5:41234": self.SER},
            cached=[(self.SER, "192.168.1.5:41234", "SM S928U1")],
            mdns=[],
            suspended=[])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tag"], adbtray.TAG_BOTH)
        # device operations prefer the steadier USB link
        self.assertEqual(rows[0]["target"], self.SER)

    def test_serial_casing_never_splits_a_device(self):
        rows = adbtray.merge_device_view(
            devices=[],
            serial_by_target={},
            cached=[("r5cx15d4p7p", "192.168.1.5:5555", "Phone")],
            mdns=[("R5CX15D4P7P", "192.168.1.5:41234")],
            suspended=[])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["serial"], self.SER)

    def test_fresh_mdns_port_is_preferred_for_reconnecting(self):
        rows = adbtray.merge_device_view(
            devices=[], serial_by_target={},
            cached=[(self.SER, "192.168.1.5:5555", "Phone")],
            mdns=[(self.SER, "192.168.1.5:41234")],
            suspended=[])
        self.assertEqual(rows[0]["known_target"], "192.168.1.5:41234")
        self.assertFalse(rows[0]["online"])

    def test_two_phones_stay_two_rows(self):
        rows = adbtray.merge_device_view(
            devices=[], serial_by_target={},
            cached=[("SER_A", "192.168.1.5:5555", "A"),
                    ("SER_B", "192.168.1.6:5555", "B")],
            mdns=[], suspended=[])
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["serial"] for r in rows}, {"SER_A", "SER_B"})

    def test_recycled_dhcp_lease_does_not_fuse_two_phones(self):
        rows = adbtray.merge_device_view(
            devices=[], serial_by_target={},
            cached=[("SER_OLD", "192.168.1.5:5555", "old phone")],
            mdns=[("SER_NEW", "192.168.1.5:41234")],
            suspended=[])
        self.assertEqual(len(rows), 2,
                         "same address but different serials are not one phone")

    def test_cache_row_without_serial_merges_on_address(self):
        rows = adbtray.merge_device_view(
            devices=[], serial_by_target={},
            cached=[("", "192.168.1.5:5555", "legacy row")],
            mdns=[(self.SER, "192.168.1.5:41234")],
            suspended=[])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["serial"], self.SER)

    def test_suspended_flag_only_applies_while_offline(self):
        offline = adbtray.merge_device_view(
            devices=[], serial_by_target={},
            cached=[(self.SER, "192.168.1.5:5555", "Phone")],
            mdns=[], suspended=[self.SER.lower()])
        self.assertTrue(offline[0]["suspended"])
        online = adbtray.merge_device_view(
            devices=[{"serial": "192.168.1.5:5555", "state": "device",
                      "model": "Phone", "usb": False}],
            serial_by_target={"192.168.1.5:5555": self.SER},
            cached=[(self.SER, "192.168.1.5:5555", "Phone")],
            mdns=[], suspended=[self.SER])
        self.assertFalse(online[0]["suspended"])

    def test_unauthorized_device_is_not_online(self):
        rows = adbtray.merge_device_view(
            devices=[{"serial": self.SER, "state": "unauthorized",
                      "model": self.SER, "usb": True}],
            serial_by_target={}, cached=[], mdns=[], suspended=[])
        self.assertFalse(rows[0]["online"])
        self.assertEqual(rows[0]["state"], "unauthorized")

    def test_alias_wins_over_model(self):
        rows = adbtray.merge_device_view(
            devices=[{"serial": self.SER, "state": "device",
                      "model": "SM S928U1", "usb": True}],
            serial_by_target={self.SER: self.SER},
            cached=[], mdns=[], suspended=[],
            aliases={self.SER: "Work phone"})
        self.assertEqual(rows[0]["label"], "Work phone")

    def test_battery_is_carried_on_the_row(self):
        rows = adbtray.merge_device_view(
            devices=[{"serial": self.SER, "state": "device",
                      "model": "P", "usb": True}],
            serial_by_target={self.SER: self.SER},
            cached=[], mdns=[], suspended=[],
            batteries={self.SER: 42})
        self.assertEqual(rows[0]["battery"], 42)

    def test_empty_world_is_an_empty_view(self):
        self.assertEqual(
            adbtray.merge_device_view([], {}, [], [], []), [])

    def test_signature_changes_only_with_visible_state(self):
        args = ([], {}, [(self.SER, "1.2.3.4:5555", "P")], [], [])
        a = adbtray.view_signature(adbtray.merge_device_view(*args), "ar")
        b = adbtray.view_signature(adbtray.merge_device_view(*args), "ar")
        self.assertEqual(a, b)
        c = adbtray.view_signature(adbtray.merge_device_view(*args), "en")
        self.assertNotEqual(a, c)


class CacheDeduplicationTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".tsv")
        os.close(fd)
        self._orig = adbtray.CACHE_FILE
        adbtray.CACHE_FILE = self.path

    def tearDown(self):
        adbtray.CACHE_FILE = self._orig
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_rotated_port_replaces_the_row(self):
        adbtray.save_cache_entry("SER1", "10.0.0.5:5555", "Phone")
        adbtray.save_cache_entry("SER1", "10.0.0.5:41234", "Phone")
        rows = adbtray.cached_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "10.0.0.5:41234")

    def test_serial_casing_does_not_create_a_second_row(self):
        adbtray.save_cache_entry("ser1", "10.0.0.5:5555", "Phone")
        adbtray.save_cache_entry("SER1", "10.0.0.9:5555", "Phone")
        self.assertEqual(len(adbtray.cached_entries()), 1)

    def test_new_device_on_a_recycled_address_replaces_the_old_row(self):
        adbtray.save_cache_entry("OLD", "10.0.0.5:5555", "Old")
        adbtray.save_cache_entry("NEW", "10.0.0.5:5555", "New")
        rows = adbtray.cached_entries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "NEW")

    def test_legacy_duplicated_file_is_repaired_on_read(self):
        self._write("SER1\t10.0.0.5\t5555\tPhone\t1\n"
                    "ser1\t10.0.0.5\t41234\tPhone\t2\n"
                    "SER2\t10.0.0.6\t5555\tOther\t3\n")
        rows = adbtray.cached_entries()
        self.assertEqual(len(rows), 2)
        by_serial = {s: t for s, t, _ in rows}
        self.assertEqual(by_serial["SER1"], "10.0.0.5:41234")

    def test_short_and_blank_lines_are_ignored(self):
        self._write("\nbroken\nSER1\t10.0.0.5\t5555\tPhone\t1\n")
        self.assertEqual(len(adbtray.cached_entries()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
