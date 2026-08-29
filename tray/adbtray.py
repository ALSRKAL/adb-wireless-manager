#!/usr/bin/env python3
"""ADB Wireless Manager - cross-platform system tray app (Windows/Linux/macOS).

Repo: https://github.com/ALSRKAL/adb-wireless-manager
"""
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime

from PyQt5.QtCore import QRect, Qt, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
                             QFileDialog, QFormLayout, QHBoxLayout,
                             QInputDialog, QLabel, QLineEdit,
                             QMenu, QMessageBox, QProgressBar, QPushButton,
                             QSpinBox, QSystemTrayIcon, QVBoxLayout, QWidget)

__version__ = "14.0.0"
REPO = "ALSRKAL/adb-wireless-manager"
REPO_URL = f"https://github.com/{REPO}"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

C_GREEN = "#2ecc71"
C_YELLOW = "#f1c40f"
C_RED = "#e74c3c"
C_GRAY = "#95a5a6"

POLL_MS_DEFAULT = 8
RECORD_SECONDS = 30
SINGLE_INSTANCE_PORT = 48765


def data_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "adbconnect")
    if IS_MACOS:
        return os.path.expanduser("~/Library/Application Support/adbconnect")
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(base, "adbconnect")


def config_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "adbconnect")
    if IS_MACOS:
        return os.path.expanduser("~/Library/Application Support/adbconnect")
    base = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return os.path.join(base, "adbconnect")


CACHE_FILE = os.path.join(data_dir(), "devices.tsv")
SUSPENDED_FILE = os.path.join(data_dir(), "suspended.tsv")
SETTINGS_FILE = os.path.join(config_dir(), "settings.json")


def log_file():
    if IS_WINDOWS:
        return os.path.join(
            tempfile.gettempdir(), "adbconnect.log")
    return "/tmp/adbconnect.log"


LOG_FILE = log_file()


def official_tools_dir():
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        return os.path.join(base, "AWM", "platform-tools")
    return os.path.expanduser("~/.local/share/awm/platform-tools")


def ensure_official_adb_path():
    """Shadow a mDNS-less distro adb with Google's official platform-tools."""
    d = official_tools_dir()
    if os.path.isdir(os.path.join(d, "platform-tools")):
        d = os.path.join(d, "platform-tools")
    if os.path.isfile(os.path.join(d, "adb" if not IS_WINDOWS else "adb.exe")):
        if os.path.abspath(d) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
        return True
    return False


ensure_official_adb_path()

DEFAULT_SETTINGS = {
    "lang": "ar",
    "poll_interval_sec": POLL_MS_DEFAULT,
    "watch_interval_sec": 20,
    "start_port": 5555,
    "max_retries": 3,
    "connect_timeout_sec": 10,
    "auto_scrcpy": True,
    "scrcpy_args": "",
    "check_updates": True,
    "show_dropzone": False,
    "dropzone_geometry": None,
    "aliases": {},
}


class Settings:
    def __init__(self, path=SETTINGS_FILE):
        self.path = path
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key):
        return self.data.get(key, DEFAULT_SETTINGS.get(key))

    def set(self, key, value):
        self.data[key] = value


S = Settings()


def tr(ar, en):
    return en if S.get("lang") == "en" else ar


# ---------------------------------------------------------------------------
# Device identity - one normalisation rule for the whole app.
# A phone answers to several names at once (USB serial, cached ip:5555, a
# fresh mDNS ip:41234). Every comparison goes through these helpers so the
# same phone is never counted twice.
# ---------------------------------------------------------------------------
def norm_serial(value):
    """Canonical form of a hardware serial used as the identity key."""
    return (value or "").strip().upper()


def is_network_target(value):
    """True when the adb transport name is a wireless one, not a USB serial."""
    value = value or ""
    return ":" in value or value.startswith("adb-")


def split_target(target):
    """'192.168.1.5:41234' -> ('192.168.1.5', '41234'); USB serial -> (s, '')."""
    host, sep, port = (target or "").rpartition(":")
    return (host, port) if sep else ((target or ""), "")


def target_host(target):
    return split_target(target)[0]


_tray_ref = None


def sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout or ""
    except Exception:
        return ""


def notify(title, body=""):
    if not IS_WINDOWS and not IS_MACOS and shutil.which("notify-send"):
        try:
            subprocess.Popen(
                ["notify-send", "-a", "ADB Wireless", "-i", "phone", title, body],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return
        except Exception:
            pass
    if _tray_ref is not None:
        _tray_ref.showMessage(title, body, QSystemTrayIcon.Information, 6000)


_notify_actions_ok = None


def notify_supports_actions():
    global _notify_actions_ok
    if _notify_actions_ok is None:
        out = sh(["notify-send", "--help"], 5)
        _notify_actions_ok = "--action" in out or "-A" in out
    return _notify_actions_ok


def actionable_notify(title, body, actions):
    """Blocking; returns chosen action key or None. Call from a worker thread."""
    if not IS_WINDOWS and not IS_MACOS and notify_supports_actions():
        args = ["notify-send", "-a", "ADB Wireless", "-i", "phone", title, body]
        for key, label in actions.items():
            args += ["-A", f"{key}={label}"]
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=120)
            out = (r.stdout or "").strip()
            if out in actions:
                return out
        except Exception:
            return None
    else:
        notify(title, body)
    return None


def list_devices():
    out = sh(["adb", "devices", "-l"])
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2 or parts[0] in ("List",):
            continue
        serial, state = parts[0], parts[1]
        model = serial
        for p in parts[2:]:
            if p.startswith("model:"):
                model = p[6:].replace("_", " ")
        devs.append({
            "serial": serial,
            "state": state,
            "model": model,
            "usb": ":" not in serial and not serial.startswith("adb-"),
        })
    return devs


def cached_entries():
    """Saved devices as (serial, target, label). Last row per serial wins.

    Older builds wrote the same phone twice when its serial casing or its
    wireless port changed, so collapsing on the normalised serial here also
    repairs caches written by those builds.
    """
    rows = {}
    order = []
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) < 4:
                    continue
                serial = norm_serial(c[0])
                key = serial or f"{c[1]}:{c[2]}"
                if key not in rows:
                    order.append(key)
                rows[key] = (serial, f"{c[1]}:{c[2]}",
                             c[3].replace("_", " "))
    except OSError:
        pass
    return [rows[k] for k in order]


def cached_targets():
    return [(t, lbl) for _, t, lbl in cached_entries()]


def suspended_serials():
    out = []
    try:
        with open(SUSPENDED_FILE, encoding="utf-8") as f:
            for line in f:
                s = line.split("\t")[0].strip()
                if s:
                    out.append(s)
    except OSError:
        pass
    return out


def is_suspended(serial):
    return serial.upper() in {s.upper() for s in suspended_serials()}


def suspend_add(serial):
    if not serial:
        return
    try:
        os.makedirs(os.path.dirname(SUSPENDED_FILE), exist_ok=True)
        keep = [l for l in suspended_lines()
                if l.split("\t")[0].strip().upper() != serial.upper()]
        keep.append(f"{serial}\t{int(time.time())}\n")
        with open(SUSPENDED_FILE, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except OSError:
        pass


def suspend_del(serial):
    if not serial:
        return
    try:
        keep = [l for l in suspended_lines()
                if l.split("\t")[0].strip().upper() != serial.upper()]
        with open(SUSPENDED_FILE, "w", encoding="utf-8") as f:
            f.writelines(keep)
    except OSError:
        pass


def suspended_lines():
    try:
        with open(SUSPENDED_FILE, encoding="utf-8") as f:
            return [l for l in f.readlines() if l.strip()]
    except OSError:
        return []


def mdns_entries():
    out = sh(["adb", "mdns", "services"], 10)
    entries, seen = [], set()

    def add(serial, target):
        if target and target not in seen:
            seen.add(target)
            entries.append((serial.upper(), target))

    for line in out.splitlines():
        if "_adb-tls-connect" in line:
            m = re.search(
                r"adb-([A-Za-z0-9]+)-[A-Za-z0-9]+\._adb-tls-connect"
                r".*?(\S+:\d+)\s*$", line.strip())
            if m:
                add(m.group(1), m.group(2))
        elif "_adb._tcp" in line:
            m = re.search(r"adb-([A-Za-z0-9]+)", line)
            a = re.search(r"(\S+:\d+)\s*$", line.strip())
            if m and a:
                add(m.group(1), a.group(1))
    return entries


def mdns_targets():
    return [t for _, t in mdns_entries()]


def mdns_pairing_targets():
    out = sh(["adb", "mdns", "services"], 10)
    tg = []
    for line in out.splitlines():
        if "_adb-tls-pairing" not in line:
            continue
        m = re.search(r"(\S+:\d+)\s*$", line.strip())
        if m:
            tg.append(m.group(1))
    return sorted(set(tg))


def build_pairing_uri(name, password):
    return f"WIFI:T:ADB;S:{name};P:{password};;"


def gen_pair_creds():
    alphabet = string.ascii_uppercase + string.digits
    name = "awm-" + "".join(secrets.choice(string.digits)
                            for _ in range(6))
    pwd = "".join(secrets.choice(alphabet) for _ in range(6))
    return name, pwd


def find_pairing_service(out, name):
    for line in out.splitlines():
        m = re.match(
            rf"\s*{re.escape(name)}\._adb-tls-pairing\._tcp\s+(\S+:\d+)"
            r"\s*$", line)
        if m:
            return m.group(1)
    return None


def find_connect_service(out):
    for line in out.splitlines():
        if "_adb-tls-connect" not in line:
            continue
        m = re.search(r"(\S+:\d+)\s*$", line.strip())
        if m:
            return m.group(1)
    return None


def mdns_target_for_serial(entries, serial):
    s = (serial or "").upper()
    for mserial, t in entries:
        if mserial.upper() == s:
            return t
    return None


def save_cache_entry(serial, target, label):
    """Upsert one row per device, keyed on the normalised serial.

    Also drops any other row pointing at the same address, so a phone whose
    wireless port changed replaces its old entry instead of adding one.
    """
    serial = norm_serial(serial)
    host, port = split_target(target)
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        rows = []
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                for line in f.readlines():
                    c = line.rstrip("\n").split("\t")
                    if len(c) < 4:
                        continue
                    if serial and norm_serial(c[0]) == serial:
                        continue
                    if host and c[1] == host:
                        continue
                    rows.append(line if line.endswith("\n") else line + "\n")
        except OSError:
            pass
        rows.append(f"{serial}\t{host}\t{port}\t{label}\t"
                    f"{int(time.time())}\n")
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.writelines(rows)
        return True
    except OSError:
        return False


def make_qr_pixmap(text, box_pixels=4):
    """Returns QPixmap or None when qrcode lib is unavailable."""
    try:
        import qrcode
    except ImportError:
        return None
    qr = qrcode.QRCode(border=2, box_size=box_pixels)
    qr.add_data(text)
    qr.make(fit=True)
    from qrcode.image.pil import PilImage  # noqa: F401
    img = qr.make_image(fill_color="black", back_color="white")
    import tempfile as _tf
    tmp = os.path.join(_tf.gettempdir(), f"awm_qr_{int(time.time()*1000)}.png")
    img.save(tmp)
    pm = QPixmap(tmp)
    try:
        os.unlink(tmp)
    except OSError:
        pass
    return pm if not pm.isNull() else None


def save_qr_png(text, path):
    try:
        import qrcode
    except ImportError:
        return False
    qrcode.make(text, box_size=6).save(path)
    return True


def get_wireless_debugging_enabled(serial):
    out = sh(["adb", "-s", serial, "shell",
              "settings", "get", "global", "adb_wifi_enabled"], 6).strip()
    return out == "1"


def evaluate_readiness(dev_state=None, wifi_dbg=None):
    """Pure: dev_state in {None,'device','unauthorized','offline'};
    wifi_dbg in {None,True,False}. Returns [(ok, ar, en)] where ok may
    be True/False/None(unknown -> guidance)."""
    items = []
    if dev_state is None:
        items.append((None,
                      "افتح خيارات المطوّر (اضغط على رقم البناء 7 مرات)",
                      "Enable Developer options (tap Build number 7 times)"))
    else:
        items.append((True,
                      "خيارات المطوّر مفعّلة",
                      "Developer options enabled"))

    if dev_state == "unauthorized":
        items.append((False,
                      "اقبل نافذة التخويل على شاشة الهاتف",
                      "Accept the authorization prompt on the phone"))
    elif dev_state == "device":
        items.append((True,
                      "USB debugging مفعّل ويعمل",
                      "USB debugging enabled and working"))
    elif dev_state == "offline":
        items.append((False,
                      "الجهاز offline - أعد توصيل الكابل",
                      "Device offline - reconnect the cable"))
    else:
        items.append((None,
                      "لتوصيل USB: فعّل USB debugging من خيارات المطوّر",
                      "For USB: enable USB debugging in Developer options"))

    if wifi_dbg is True:
        items.append((True,
                      "Wireless debugging مفعّل - تابع الاقتران",
                      "Wireless debugging is on - proceed to pairing"))
    elif wifi_dbg is False:
        items.append((False,
                      "فعّل Wireless debugging من خيارات المطوّر",
                      "Enable Wireless debugging in Developer options"))
    else:
        items.append((None,
                      "للاتصال اللاسلكي: فعّل Wireless debugging ثم الإقران برمز",
                      "For wireless: enable Wireless debugging then pair "
                      "with code"))
    return items


def _busy_from_ss(out, p):
    return bool(re.search(rf"[:.]{p}(\s|$)", out))


def _busy_from_netstat(out, p):
    if re.search(rf":{p}\s.*LISTENING", out):
        return True
    return bool(re.search(rf"[:.]{p}\s", out))


def port_busy(p):
    if IS_WINDOWS:
        return _busy_from_netstat(sh(["netstat", "-an"], 6), p)
    if IS_MACOS or not shutil.which("ss"):
        return _busy_from_netstat(sh(["netstat", "-an"], 6), p)
    return _busy_from_ss(sh(["ss", "-Htuln"], 5), p)


def next_free_port(start=None):
    p = start if start is not None else int(S.get("start_port") or 5555)
    while p < 5700 and port_busy(p):
        p += 1
    return p


def phone_ips(serial):
    out = sh(["adb", "-s", serial, "shell",
              "ip", "-o", "-4", "addr", "show", "scope", "global"], 8)
    wlan, other = [], []
    for line in out.splitlines():
        if any(x in line for x in ("rmnet", "ccmni", "tun")):
            continue
        m = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", line)
        if not m:
            continue
        ip = m.group(1)
        if ip in wlan or ip in other:
            continue
        (wlan if "wlan" in line else other).append(ip)
    return wlan + other


# One round-trip per device instead of one per fact. Values are echoed with
# explicit keys so a missing field cannot shift the rest of the output.
DEVICE_PROBE_CMD = (
    "echo AWM_SERIAL=$(getprop ro.serialno); "
    "echo AWM_MODEL=$(getprop ro.product.model); "
    "echo AWM_SDK=$(getprop ro.build.version.sdk); "
    "echo AWM_WIFI=$(settings get global adb_wifi_enabled); "
    "echo AWM_BATTERY=$(dumpsys battery | sed -n 's/^  level: //p')"
)

EMPTY_PROBE = {"serial": "", "model": "", "sdk": None,
               "adb_wifi": None, "battery": None, "alive": False}


def parse_device_probe(text):
    """Pure parser for DEVICE_PROBE_CMD output.

    `alive` means the shell actually answered, which is the only honest
    proof that a transport listed as `device` can still run commands.
    """
    out = dict(EMPTY_PROBE)
    for line in (text or "").splitlines():
        key, sep, val = line.strip().partition("=")
        if not sep or not key.startswith("AWM_"):
            continue
        out["alive"] = True
        val = val.strip()
        if key == "AWM_SERIAL":
            out["serial"] = norm_serial(val)
        elif key == "AWM_MODEL":
            out["model"] = val.replace("_", " ")
        elif key == "AWM_SDK":
            out["sdk"] = int(val) if val.isdigit() else None
        elif key == "AWM_WIFI":
            out["adb_wifi"] = {"1": True, "0": False}.get(val)
        elif key == "AWM_BATTERY":
            out["battery"] = int(val) if val.isdigit() else None
    return out


def probe_device(target, timeout=8):
    """Ask a live transport who it is. Never raises."""
    return parse_device_probe(
        sh(["adb", "-s", target, "shell", DEVICE_PROBE_CMD], timeout))


def device_serial(target):
    """Hardware serial behind an adb transport, mDNS as the fallback."""
    serial = probe_device(target, 6)["serial"]
    if serial:
        return serial
    for mserial, t in mdns_entries():
        if t == target:
            return norm_serial(mserial)
    return ""


# ---------------------------------------------------------------------------
# Connection layer
#
# `adb tcpip <port>` and `adb usb` both restart adbd on the phone. On
# Android 11+ that restart tears down the Wireless-debugging session and
# leaves the developer-options toggle switched off, which is exactly what
# users hit after a failed reconnect. Both commands are therefore treated
# as a last resort, and never as part of routine healing.
# ---------------------------------------------------------------------------
WIRELESS_DEBUGGING_SDK = 30          # Android 11
MDNS_WAIT_SECONDS = 20
STRATEGY_MDNS = "mdns"
STRATEGY_ENABLE_WIFI = "enable_wifi"
STRATEGY_AWAIT_MDNS = "await_mdns"
STRATEGY_TCPIP = "tcpip"


def plan_connect_strategy(sdk, adb_wifi_enabled, mdns_target):
    """Pure: decide how to take a USB-attached phone wireless.

    Returns (strategy, target):
      mdns         already advertising - just connect, touch nothing
      enable_wifi  Android 11+ - switch Wireless debugging on, then wait
      await_mdns   Android 11+ toggle already on - wait for the advert
      tcpip        legacy fallback - restarts adbd, drops the toggle
    """
    modern = sdk is not None and sdk >= WIRELESS_DEBUGGING_SDK
    if mdns_target:
        return STRATEGY_MDNS, mdns_target
    if not modern:
        return STRATEGY_TCPIP, ""
    if adb_wifi_enabled is True:
        return STRATEGY_AWAIT_MDNS, ""
    return STRATEGY_ENABLE_WIFI, ""


HARD_CONNECT_FAILURES = ("refused", "no route to host",
                         "network is unreachable", "host is unreachable",
                         "name or service not known", "unknown host")


def is_hard_connect_failure(output):
    """True when retrying the same address cannot help.

    A refused connection means nothing is listening on that port right now,
    usually a stale mDNS advert or a wireless-debugging toggle that went off.
    Retrying it three times only makes the tool feel slow; moving on to the
    next candidate address is what actually recovers the link.
    """
    out = (output or "").lower()
    return any(sig in out for sig in HARD_CONNECT_FAILURES)


def friendly_connect_error(output):
    """Turn raw `adb connect` output into a reason worth showing a user."""
    out = (output or "").lower()
    if "connected to" in out:
        return ""
    if not out.strip():
        # adb was killed by our timeout before it printed anything, which is
        # what an unroutable address looks like from here
        return tr("لا استجابة خلال المهلة - العنوان غير قابل للوصول",
                  "no answer within the timeout - the address is unreachable")
    if "refused" in out:
        return tr("الهاتف رفض الاتصال - التصحيح اللاسلكي مغلق أو المنفذ تغيّر",
                  "connection refused - wireless debugging is off or the "
                  "port changed")
    if "no route to host" in out or "network is unreachable" in out:
        return tr("لا يوجد مسار للهاتف - تأكد أنكما على نفس الشبكة",
                  "no route to the phone - check you are on the same network")
    if "timed out" in out or "timeout" in out:
        return tr("انتهت المهلة - الهاتف نائم أو الشبكة بطيئة",
                  "timed out - the phone is asleep or the network is slow")
    if "failed to authenticate" in out or "unauthorized" in out:
        return tr("التخويل مرفوض - أعد الاقتران",
                  "not authorised - pair again")
    if "cannot connect" in out or "failed to connect" in out:
        return tr("تعذّر الوصول إلى العنوان",
                  "the address could not be reached")
    line = next((l.strip() for l in (output or "").splitlines() if l.strip()),
                "")
    return line or tr("سبب غير معروف", "unknown reason")


def target_state(target, devices=None):
    """adb's own opinion about one transport, or None when it is unknown."""
    for d in (devices if devices is not None else list_devices()):
        if d["serial"] == target:
            return d["state"]
    return None


def heal_offline_transport(target):
    """Clear a half-open transport without restarting adbd on the phone.

    `adb reconnect offline` is adb's own remedy for stuck transports and
    costs the phone nothing, unlike `adb usb` / `adb tcpip`.
    """
    sh(["adb", "disconnect", target], 6)
    sh(["adb", "reconnect", "offline"], 8)


def enable_wireless_debugging(serial):
    """Switch Wireless debugging on over an existing transport.

    The adb shell user holds WRITE_SECURE_SETTINGS, so this is also how a
    toggle that a previous adbd restart switched off gets restored.
    """
    sh(["adb", "-s", serial, "shell", "settings", "put", "global",
        "adb_wifi_enabled", "1"], 8)
    return probe_device(serial, 8)["adb_wifi"] is True


def wait_for_mdns_target(serial, deadline_sec=MDNS_WAIT_SECONDS):
    """Poll mDNS for this phone's wireless-debugging advert."""
    want = norm_serial(serial)
    deadline = time.time() + deadline_sec
    while True:
        entries = mdns_entries()
        hit = mdns_target_for_serial(entries, want) if want else None
        if hit:
            return hit
        if time.time() >= deadline:
            return ""
        time.sleep(2)


def connect_and_verify(target, expect_serial="", attempts=None):
    """Connect, then prove the link with a real command round-trip.

    Returns (ok, serial, reason). The identity check is what stops the
    tool from silently adopting a different phone that picked up the same
    DHCP address.
    """
    tries = attempts or max(1, int(S.get("max_retries") or 3))
    timeout = max(4, int(S.get("connect_timeout_sec") or 10))
    want = norm_serial(expect_serial)
    reason = tr("لم تبدأ أي محاولة", "no attempt was made")
    for attempt in range(1, tries + 1):
        out = sh(["adb", "connect", target], timeout)
        time.sleep(1)
        state = target_state(target)
        if state == "device":
            probe = probe_device(target)
            if not probe["alive"]:
                reason = tr("الاتصال قائم لكن الجهاز لا يستجيب للأوامر",
                            "linked but the device answers no commands")
                heal_offline_transport(target)
            elif want and probe["serial"] and probe["serial"] != want:
                sh(["adb", "disconnect", target], 6)
                return False, probe["serial"], tr(
                    f"العنوان {target} يخص جهازًا آخر ({probe['serial']})",
                    f"{target} belongs to a different device "
                    f"({probe['serial']})")
            else:
                return True, probe["serial"], ""
        elif state == "offline":
            reason = tr("الاتصال معلّق في حالة offline",
                        "the transport is stuck offline")
            heal_offline_transport(target)
        else:
            reason = friendly_connect_error(out)
            if is_hard_connect_failure(out):
                return False, "", reason      # try the next address instead
        if attempt < tries:
            time.sleep(min(3, attempt))
    return False, "", reason


def scrcpy_running(target):
    if IS_WINDOWS:
        out = sh(["powershell", "-NoProfile", "-Command",
                  "Get-CimInstance Win32_Process -Filter "
                  "\"Name='scrcpy.exe'\" | Select-Object -ExpandProperty "
                  "CommandLine"], 8)
        return f"-s {target}" in out
    out = sh(["pgrep", "-af", "scrcpy"], 5)
    return f"-s {target}" in out


def launch_scrcpy(target, extra_args=""):
    if scrcpy_running(target):
        notify(tr("Scrcpy يعمل بالفعل", "Scrcpy already running"), target)
        return False
    flags = 0x00000008 if IS_WINDOWS else 0
    cmd = ["scrcpy", "-s", target]
    if extra_args:
        cmd += extra_args.split()
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=flags, start_new_session=not IS_WINDOWS,
    )
    return True


def drop_from_cache(target):
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            rows = f.readlines()
        kept = []
        for r in rows:
            c = r.rstrip("\n").split("\t")
            if len(c) >= 4 and c[1] + ":" + c[2] == target:
                continue
            kept.append(r)
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.writelines(kept)
        return True
    except OSError:
        return False


def open_in_file_manager(path):
    try:
        if IS_WINDOWS:
            os.startfile(path)
        elif IS_MACOS:
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        notify(tr("خطأ", "Error"), f"{path}")


def desktop_dir():
    d = os.path.join(os.path.expanduser("~"), "Desktop")
    return d if os.path.isdir(d) else os.path.expanduser("~")


def is_apk(path):
    return str(path).lower().endswith(".apk")


def friendly_adb_error(output):
    """Translate raw `adb install` output into a human-readable reason."""
    out = (output or "").lower()
    if "success" in out:
        return ""
    if "insufficient_storage" in out or "not enough space" in out \
            or "no space left" in out:
        return tr("مساحة التخزين ممتلئة على الجهاز",
                  "Device storage is full")
    if "install_failed_version_downgrade" in out:
        return tr("نسخة أقدم من المثبتة حاليًا - احذف القديم أولًا",
                  "older than the installed one - uninstall first")
    if "install_failed_already_exists" in out:
        return tr("نفس النسخة مثبتة مسبقًا",
                  "the same version is already installed")
    if "incompatible" in out:
        return tr("التطبيق غير متوافق مع هذا الجهاز",
                  "app is incompatible with this device")
    if "signature" in out or "update_ownership" in out:
        return tr("توقيع مختلف عن النسخة المثبتة - احذف القديم أولًا",
                  "different signature than the installed "
                  "copy - uninstall first")
    if "install_canceled_by_user" in out or "cancelled" in out \
            or "canceled" in out:
        return tr("أُلغي التثبيت من الجهاز (وافق على النافذة على الشاشة)",
                  "cancelled on the device (accept the prompt on screen)")
    if "install_failed_invalid_apk" in out or "corrupt" in out:
        return tr("ملف APK تالف أو غير صالح", "APK file is corrupt/invalid")
    if "offline" in out or "device not found" in out \
            or "device .* offline" in out or "'adb devices'" in out:
        return tr("الجهاز غير متصل - تأكد من الاتصال",
                  "device is offline - check the connection")
    if "timeout" in out or "timed out" in out:
        return tr("انتهت المهلة - الاتصال بطيء",
                  "timed out - connection too slow")
    line = next((ln.strip() for ln in (output or "").splitlines()
                 if ln.strip() and "performing streamed install"
                 not in ln.lower()), "")
    return line or tr("خطأ غير معروف", "unknown error")


class InstallProgressDialog(QDialog):
    """Live progress window for APK installs: per-file bar + real cancel."""

    step_changed = pyqtSignal(str, int)
    close_requested = pyqtSignal()
    show_requested = pyqtSignal()

    def __init__(self, total, title):
        super().__init__()
        self.setWindowTitle(title)
        self.setModal(False)
        self.setWindowFlags(Qt.Tool | Qt.WindowStaysOnTopHint)
        lay = QVBoxLayout(self)
        self.file_label = QLabel("...")
        lay.addWidget(self.file_label)
        self.bar = QProgressBar()
        self.bar.setRange(0, max(1, total))
        self.bar.setValue(0)
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel_btn = QPushButton(tr("إلغاء", "Cancel"))
        self.cancel_btn.clicked.connect(self.request_cancel)
        row.addWidget(self.cancel_btn)
        lay.addLayout(row)
        self.resize(380, 90)
        self.step_changed.connect(self._on_step)
        self.close_requested.connect(self._on_finish)
        self.show_requested.connect(self.show)
        self._cancel_requested = False

    def _on_step(self, name, index):
        self.file_label.setText(f"[{index}/{self.total()}] {name}")
        self.bar.setValue(index)

    def _on_finish(self):
        # runs on the GUI thread (queued from the worker via the signal);
        # bar stays honest - no fake jump to 100% on cancel
        self.cancel_btn.setEnabled(False)
        self.close()

    def total(self):
        return self.bar.maximum()

    def cancel_requested(self):
        # plain bool read: safe across threads
        return self._cancel_requested

    def request_cancel(self):
        self._cancel_requested = True
        self.cancel_btn.setEnabled(False)
        self.file_label.setText(
            tr("جارٍ إلغاء العملية الحالية...", "Cancelling current file..."))

    def finish(self):
        # thread-safe: emitting a signal queues onto the GUI event loop
        self.close_requested.emit()


INFO_CMD = ("getprop ro.product.model; "
            "getprop ro.build.version.release; "
            "getprop ro.serialno; "
            "dumpsys battery | grep -E 'level'; "
            "df -h /data | grep -v Filesystem | tail -1; "
            "ip -o -4 addr show scope global")


def parse_device_info(text):
    info = {"model": "?", "android": "?", "serial": "?",
            "battery": None, "storage": None, "ips": []}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        info["model"] = lines[0].strip() or "?"
    for l in lines:
        m = re.match(r"^(\d+(?:\.\d+)*)$", l)
        if m and info["android"] == "?":
            info["android"] = m.group(1)
            continue
        if re.match(r"^[A-Za-z0-9]{6,}$", l) and info["serial"] == "?":
            info["serial"] = l.upper()
            continue
        m = re.search(r"level:\s*(\d+)", l)
        if m and info["battery"] is None:
            info["battery"] = int(m.group(1))
            continue
        m = re.search(r"([\d.]+[GMK]?)\s+([\d.]+[GMK]?)\s+([\d.]+[GMK]?)"
                      r"\s+(\d+)%", l)
        if m and info["storage"] is None:
            info["storage"] = (m.group(2), m.group(1), m.group(4))
            continue
        if any(x in l for x in ("rmnet", "ccmni", "tun")):
            continue
        for ipm in re.finditer(r"inet\s+(\d+\.\d+\.\d+\.\d+)/", l):
            ip = ipm.group(1)
            if ip not in info["ips"]:
                info["ips"].append(ip)
    return info


def version_is_newer(a, b):
    def nums(v):
        return tuple(int(x) for x in re.findall(r"\d+", v))
    try:
        return nums(a) > nums(b)
    except ValueError:
        return False


def resolve_label(serial, model, aliases):
    aliases = aliases or {}
    serial = serial or ""
    a = aliases.get(serial) or aliases.get(serial.upper()) \
        or aliases.get(serial.lower())
    return a if a else model


# ---------------------------------------------------------------------------
# Device view: collapse every name a phone answers to into a single row.
# ---------------------------------------------------------------------------
TAG_USB = "USB"
TAG_WIFI = "WiFi"
TAG_BOTH = "USB+WiFi"


def merge_device_view(devices, serial_by_target, cached, mdns, suspended,
                      aliases=None, batteries=None):
    """Pure: one row per physical device out of transports, cache and mDNS.

    The same phone routinely appears three times at once - as a USB serial,
    as the stale `ip:5555` in the cache, and as a fresh `ip:41234` mDNS
    advert after its wireless port rotated. Rows are keyed on the hardware
    serial, falling back to the host address when no serial is known yet.
    Two rows are never merged on address alone if both carry serials that
    disagree, so a recycled DHCP lease cannot fuse two different phones.
    """
    aliases = aliases or {}
    batteries = batteries or {}
    suspended = {norm_serial(s) for s in (suspended or ())}
    rows, by_serial, by_host = [], {}, {}

    def find(serial, host):
        row = by_serial.get(serial) if serial else None
        if row is not None:
            return row
        row = by_host.get(host) if host else None
        if row is not None and serial and row["serial"] \
                and row["serial"] != serial:
            return None
        return row

    def ensure(serial, host):
        serial = norm_serial(serial)
        row = find(serial, host)
        if row is None:
            row = {"serial": serial, "state": None, "usb_target": "",
                   "net_target": "", "mdns_target": "", "cached_target": "",
                   "live_model": "", "cached_label": "", "battery": None}
            rows.append(row)
        if serial:
            if not row["serial"]:
                row["serial"] = serial
            by_serial.setdefault(serial, row)
        if host:
            by_host.setdefault(host, row)
        return row

    # Live transports first: they carry the most trustworthy identity.
    for d in devices:
        target = d["serial"]
        usb = d["usb"]
        serial = target if usb else serial_by_target.get(target, "")
        row = ensure(serial, "" if usb else target_host(target))
        if usb:
            row["usb_target"] = target
        else:
            row["net_target"] = target
        if row["state"] != "device":
            row["state"] = d["state"]
        if d["model"] and d["model"] != target:
            row["live_model"] = d["model"]
        bat = batteries.get(target)
        if bat is not None:
            row["battery"] = bat

    for serial, target in mdns:
        ensure(serial, target_host(target))["mdns_target"] = target

    for serial, target, label in cached:
        row = ensure(serial, target_host(target))
        row["cached_target"] = target
        if label and label != "mDNS" and not row["cached_label"]:
            row["cached_label"] = label

    out = []
    for row in rows:
        row["online"] = bool(row["usb_target"] or row["net_target"]) \
            and row["state"] == "device"
        if row["usb_target"] and row["net_target"]:
            row["tag"] = TAG_BOTH
        elif row["net_target"]:
            row["tag"] = TAG_WIFI
        elif row["usb_target"]:
            row["tag"] = TAG_USB
        else:
            # nothing live: a saved or advertised device is a wireless one
            row["tag"] = TAG_WIFI
        # Device operations prefer USB when both links are up; it is the
        # faster and steadier of the two.
        row["target"] = row["usb_target"] or row["net_target"]
        # Reconnecting prefers a fresh mDNS advert over a stale cached port.
        row["known_target"] = (row["mdns_target"] or row["cached_target"]
                               or row["net_target"])
        row["suspended"] = bool(row["serial"]) \
            and row["serial"] in suspended and not row["online"]
        row["label"] = resolve_label(
            row["serial"],
            row["live_model"] or row["cached_label"] or row["serial"]
            or row["known_target"] or "?",
            aliases)
        out.append(row)
    return out


def view_signature(rows, lang):
    """Stable fingerprint of what the menu shows, to avoid pointless rebuilds."""
    return repr((lang, [(r["serial"], r["label"], r["state"], r["tag"],
                         r["usb_target"], r["net_target"],
                         r["known_target"], r["online"], r["suspended"],
                         r["battery"]) for r in rows]))


_dark_cache = {"value": None}


def system_is_dark():
    if _dark_cache["value"] is not None:
        return _dark_cache["value"]
    dark = False
    try:
        if IS_WINDOWS:
            out = sh(["reg", "query",
                      r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes"
                      r"Personalize", "/v", "AppsUseLightTheme"], 5)
            dark = bool(re.search(r"0x0\b", out))
        elif IS_MACOS:
            dark = True
        else:
            out = sh(["gsettings", "get", "org.gnome.desktop.interface",
                      "color-scheme"], 4)
            dark = "prefer-dark" in out
    except Exception:
        pass
    _dark_cache["value"] = dark
    return dark


_icon_cache = {}


def make_icon(color, label="", outline=None):
    key = (color, label, outline)
    if key in _icon_cache:
        return _icon_cache[key]
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(6, 6, 52, 52)
    o = outline if outline is not None else ("#ffffff" if system_is_dark()
                                             else "#2c3e50")
    pen = QPen(QColor(o))
    pen.setWidth(2)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(7, 7, 50, 50)
    if label:
        f = QFont()
        f.setBold(True)
        f.setPixelSize(30 if len(label) <= 2 else 22)
        p.setFont(f)
        p.setPen(QPen(QColor("white")))
        p.drawText(QRect(0, 0, 64, 64), Qt.AlignCenter, str(label))
    p.end()
    icon = QIcon(pm)
    _icon_cache[key] = icon
    return icon


def collect_state():
    """One snapshot of the world, already de-duplicated. Runs off the GUI thread."""
    devices = list_devices()
    serial_by_target, batteries, sdk_by_target = {}, {}, {}
    for d in devices:
        if d["state"] != "device":
            continue
        probe = probe_device(d["serial"])
        if probe["serial"]:
            serial_by_target[d["serial"]] = probe["serial"]
        if probe["battery"] is not None:
            batteries[d["serial"]] = probe["battery"]
        if probe["sdk"] is not None:
            sdk_by_target[d["serial"]] = probe["sdk"]
    cached = cached_entries()
    mdns = mdns_entries()
    suspended = suspended_serials()
    return {
        "devices": devices,
        "cached": cached,
        "mdns": mdns,
        "suspended": suspended,
        "batteries": batteries,
        "sdks": sdk_by_target,
        "serials": serial_by_target,
        "view": merge_device_view(devices, serial_by_target, cached, mdns,
                                 suspended, S.get("aliases"), batteries),
        "dark": system_is_dark(),
    }


class SettingsDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("الإعدادات - ADB Wireless",
                               "Settings - ADB Wireless"))
        self.setMinimumWidth(420)
        form = QFormLayout(self)

        self.lang = QComboBox()
        self.lang.addItem("العربية", "ar")
        self.lang.addItem("English", "en")
        self.lang.setCurrentIndex(0 if S.get("lang") != "en" else 1)
        form.addRow(tr("اللغة", "Language"), self.lang)

        self.poll = QSpinBox()
        self.poll.setRange(3, 120)
        self.poll.setValue(int(S.get("poll_interval_sec")))
        form.addRow(tr("فحص كل (ثانية)", "Poll every (sec)"), self.poll)

        self.watch = QSpinBox()
        self.watch.setRange(5, 600)
        self.watch.setValue(int(S.get("watch_interval_sec")))
        form.addRow(tr("مراقبة الخدمة كل (ثانية)", "Watch interval (sec)"),
                     self.watch)

        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(S.get("start_port")))
        form.addRow(tr("منفذ البداية", "Start port"), self.port)

        self.retries = QSpinBox()
        self.retries.setRange(1, 10)
        self.retries.setValue(int(S.get("max_retries")))
        form.addRow(tr("المحاولات", "Retries"), self.retries)

        self.auto_scrcpy = QCheckBox(tr("تشغيل الشاشة تلقائيًا عند الاتصال",
                                        "Auto-launch scrcpy on connect"))
        self.auto_scrcpy.setChecked(bool(S.get("auto_scrcpy")))
        form.addRow("", self.auto_scrcpy)

        self.scrcpy_args = QLineEdit(str(S.get("scrcpy_args") or ""))
        self.scrcpy_args.setPlaceholderText(
            "--video-bit-rate 8M --max-fps 60 --turn-screen-off")
        form.addRow(tr("خيارات scrcpy", "scrcpy args"), self.scrcpy_args)

        self.updates = QCheckBox(tr("فحص وجود تحديثات للمشروع",
                                    "Check for project updates"))
        self.updates.setChecked(bool(S.get("check_updates")))
        form.addRow("", self.updates)

        self.dropzone = QCheckBox(
            tr("إظهار منطقة إفلات APK عند البدء",
               "Show APK drop-zone on startup"))
        self.dropzone.setChecked(bool(S.get("show_dropzone")))
        form.addRow("", self.dropzone)

        row = QHBoxLayout()
        ok = QPushButton(tr("حفظ", "Save"))
        cancel = QPushButton(tr("إلغاء", "Cancel"))
        ok.clicked.connect(self.save)
        cancel.clicked.connect(self.reject)
        row.addStretch(1)
        row.addWidget(ok)
        row.addWidget(cancel)
        form.addRow(row)

    def save(self):
        lang_before = S.get("lang")
        S.set("lang", self.lang.currentData())
        S.set("poll_interval_sec", self.poll.value())
        S.set("watch_interval_sec", self.watch.value())
        S.set("start_port", self.port.value())
        S.set("max_retries", self.retries.value())
        S.set("auto_scrcpy", self.auto_scrcpy.isChecked())
        S.set("scrcpy_args", self.scrcpy_args.text().strip())
        S.set("check_updates", self.updates.isChecked())
        S.set("show_dropzone", self.dropzone.isChecked())
        S.save()
        if _tray_ref is not None:
            _tray_ref.apply_settings(lang_changed=S.get("lang") != lang_before)
        self.accept()


class PairDialog(QDialog):
    scan_done = pyqtSignal()

    def __init__(self, start_tab="code"):
        super().__init__()
        self._stop_scan = False
        self.scan_done.connect(self.accept)
        self.setWindowTitle(tr("اقتران لاسلكي (أندرويد 11+)",
                               "Wireless pairing (Android 11+)"))
        self.setMinimumWidth(470)
        self.resize(500, 660)
        lay = QVBoxLayout(self)

        self.readiness = QLabel()
        self.readiness.setWordWrap(True)
        self.readiness.setTextFormat(Qt.RichText)
        lay.addWidget(self.readiness)
        row_r = QHBoxLayout()
        btn_refresh = QPushButton(tr("إعادة الفحص", "Re-check"))
        btn_refresh.clicked.connect(self.refresh_readiness)
        row_r.addStretch(1)
        row_r.addWidget(btn_refresh)
        lay.addLayout(row_r)
        self.refresh_readiness()

        from PyQt5.QtWidgets import QTabWidget
        tabs = QTabWidget()

        scan_w = QWidget()
        sl = QVBoxLayout(scan_w)
        self.scan_qr_img = QLabel()
        self.scan_qr_img.setAlignment(Qt.AlignCenter)
        sl.addWidget(self.scan_qr_img)
        self._scan_name, self._scan_pwd = gen_pair_creds()
        self.scan_uri = build_pairing_uri(self._scan_name, self._scan_pwd)
        qrpm = make_qr_pixmap(self.scan_uri, box_pixels=4)
        if qrpm is not None:
            self.scan_qr_img.setPixmap(qrpm.scaled(
                240, 240, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.scan_qr_img.setText(
                tr("مكتبة qrcode غير مثبتة:\npip install qrcode[pil]",
                   "qrcode lib missing:\npip install qrcode[pil]"))
        hint2 = QLabel(tr(
            f"{tr('اسم الخدمة', 'Service')}: {self._scan_name}    "
            f"{tr('الرمز', 'Code')}: {self._scan_pwd}\n"
            "على الهاتف: خيارات المطوّر > تصحيح لاسلكي > "
            "'إقران الجهاز برمز QR' ثم امسح هذا الرمز.",
            "On phone: Developer options > Wireless debugging > "
            "'Pair device with QR code' then scan this."))
        hint2.setWordWrap(True)
        sl.addWidget(hint2)
        self.scan_status = QLabel(tr("بانتظار مسح الهاتف للرمز...",
                                     "Waiting for the phone to scan..."))
        self.scan_status.setWordWrap(True)
        sl.addWidget(self.scan_status)
        tabs.addTab(scan_w, tr("مسح تلقائي", "Auto-scan"))

        code_w = QWidget()
        cl = QVBoxLayout(code_w)
        hint = QLabel(tr(
            "على الهاتف: خيارات المطوّر > تصحيح لاسلكي > الإقران برمز.\n"
            "أدخل عنوان الاقتران والرمز الظاهر على شاشة الهاتف.",
            "On phone: Developer options > Wireless debugging > Pair with "
            "code.\nEnter the pairing address and code shown on the phone."))
        hint.setWordWrap(True)
        cl.addWidget(hint)
        row1 = QHBoxLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText(tr("العنوان مثل 192.168.1.5:37843",
                                        "Address e.g. 192.168.1.5:37843"))
        btn_scan = QPushButton(tr("بحث", "Scan"))
        btn_scan.clicked.connect(self.scan)
        row1.addWidget(self.addr, 1)
        row1.addWidget(btn_scan)
        cl.addLayout(row1)
        row2 = QHBoxLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText(tr("رمز من 6 أرقام", "6-digit code"))
        row2.addWidget(self.code, 1)
        cl.addLayout(row2)
        go = QPushButton(tr("اقتران واتصال", "Pair && Connect"))
        go.clicked.connect(self.pair)
        cancel = QPushButton(tr("إلغاء", "Cancel"))
        cancel.clicked.connect(self.reject)
        rowb = QHBoxLayout()
        rowb.addStretch(1)
        rowb.addWidget(go)
        rowb.addWidget(cancel)
        cl.addLayout(rowb)
        tabs.addTab(code_w, tr("برمز", "Code"))

        qr_w = QWidget()
        ql = QVBoxLayout(qr_w)
        qhint = QLabel(tr(
            "يولّد رمز QR يحمل بيانات الاقتران (اسم الخدمة + الرمز) - "
            "احفظه أو امسحه بجهاز آخر لنقل الإعداد بسرعة.\n"
            "ملاحظة: مسح الهاتف لـ QR من الشاشة يتطلب Wi-Fi Aware "
            "وغير مدعوم عبر adb.",
            "Generates a QR carrying the pairing payload (service name + "
            "code) - save it or scan with another device to transfer the "
            "setup.\nNote: phone-scans-screen QR needs Wi-Fi Aware and is "
            "not available via adb."))
        qhint.setWordWrap(True)
        ql.addWidget(qhint)
        self.qr_img = QLabel(tr("أدخل العنوان والرمز في تبويب 'برمز' أولًا",
                                "Fill address & code in the Code tab first"))
        self.qr_img.setAlignment(Qt.AlignCenter)
        self.qr_img.setMinimumHeight(180)
        ql.addWidget(self.qr_img)
        rowq = QHBoxLayout()
        btn_gen = QPushButton(tr("توليد / تحديث QR", "Generate / refresh QR"))
        btn_gen.clicked.connect(self.refresh_qr)
        btn_save = QPushButton(tr("حفظ PNG...", "Save PNG..."))
        btn_save.clicked.connect(self.save_qr)
        rowq.addWidget(btn_gen)
        rowq.addWidget(btn_save)
        rowq.addStretch(1)
        ql.addLayout(rowq)
        tabs.addTab(qr_w, tr("QR يدوي", "Manual QR"))

        self._tabs = tabs
        lay.addWidget(tabs)
        idx = {"scan": 0, "code": 1, "qr": 2}.get(start_tab, 0)
        tabs.setCurrentIndex(idx)

        self.addr.textChanged.connect(self._live_qr)
        self.code.textChanged.connect(self._live_qr)

        from PyQt5.QtCore import QTimer
        self._t = QTimer(self)
        self._t.timeout.connect(self.refresh_readiness)
        self._t.start(4000)

        if start_tab in ("qr", "scan"):
            threading.Thread(target=self.scan_watch_loop,
                             daemon=True).start()

    def scan_watch_loop(self):
        deadline = time.time() + 180
        paired_addr = None
        while time.time() < deadline:
            if getattr(self, "_stop_scan", False):
                return
            out = sh(["adb", "mdns", "services"], 6)
            addr = find_pairing_service(out, self._scan_name)
            if addr:
                status = tr("تم العثور على الهاتف - جارٍ الاقتران...",
                            "Phone found - pairing...")
                self.scan_status_safe(status)
                pout = sh(["adb", "pair", addr, self._scan_pwd], 25)
                if "Success" not in pout and "success" not in pout:
                    self.scan_status_safe(tr("فشل الاقتران x أعد المحاولة",
                                             "Pairing failed x retry"))
                    return
                paired_addr = addr
                break
            time.sleep(1)
        if paired_addr is None:
            self.scan_status_safe(tr("انتهت المهلة - لم يتم مسح الرمز",
                                     "Timed out - QR was not scanned"))
            return
        conn_addr = None
        deadline2 = time.time() + 30
        while time.time() < deadline2 \
                and not getattr(self, "_stop_scan", False):
            out = sh(["adb", "mdns", "services"], 6)
            conn_addr = find_connect_service(out)
            if conn_addr:
                break
            time.sleep(1)
        target = conn_addr or paired_addr
        sh(["adb", "connect", target], 12)
        states = {d["serial"] for d in list_devices()
                  if d["state"] == "device"}
        if target in states or conn_addr:
            serial = device_serial(target)
            model = sh(["adb", "-s", target, "shell",
                        "getprop ro.product.model"], 6).strip() or "?"
            save_cache_entry(serial, target, model.replace("_", " "))
            notify(tr("تم الاقتران والاتصال ",
                      "Paired && connected "), target)
            self.kick_refresh()
            self.scan_done.emit()
            return
        self.scan_status_safe(tr("اقترن لكن لم يتصل - جرّب زر إعادة الاتصال",
                                 "Paired but connect failed - try reconnect"))

    def done(self, r):
        self._stop_scan = True
        super().done(r)

    def scan_status_safe(self, text):
        from PyQt5.QtCore import QTimer as _Q
        _Q.singleShot(0, lambda: self.scan_status.setText(text))

    def _live_qr(self):
        if self._tabs.currentIndex() == 1 and \
                self.addr.text().strip() and self.code.text().strip():
            self.refresh_qr()

    def refresh_readiness(self):
        usb = [d for d in list_devices() if d["usb"]]
        dev_state = None
        if usb:
            st = usb[0]["state"]
            dev_state = st if st in ("device", "unauthorized", "offline") \
                else None
        wifi_dbg = None
        if dev_state == "device":
            w = get_wireless_debugging_enabled(usb[0]["serial"])
            wifi_dbg = {True: True, False: False}.get(w)
        marks = {True: "<span style='color:#2ecc71'>[ OK ]</span>",
                 False: "<span style='color:#e74c3c'>[FAIL]</span>",
                 None: "<span style='color:#f39c12'>[TODO]</span>"}
        lines = []
        for okk, ar, en in evaluate_readiness(dev_state, wifi_dbg):
            lang_en = S.get("lang") == "en"
            lines.append(f"{marks[okk]} {en if lang_en else ar}")
        self.readiness.setText("<br>".join(lines))

    def current_uri(self):
        addr = self.addr.text().strip()
        code = self.code.text().strip()
        if not re.match(r"^\S+:\d+$", addr) or not re.match(r"^\d{6}$", code):
            return None
        return build_pairing_uri(f"awm-{addr.split(':')[0].replace('.', '-')}",
                                 code + "@" + addr)

    def refresh_qr(self):
        uri = self.current_uri()
        if uri is None:
            notify(tr("بيانات ناقصة", "Invalid input"),
                   tr("أكمل العنوان والرمز أولًا",
                      "Complete the address and code first"))
            return
        pm = make_qr_pixmap(uri, box_pixels=4)
        if pm is None:
            self.qr_img.setText(
                tr("مكتبة qrcode غير مثبتة:\npip install qrcode[pil]",
                   "qrcode lib missing:\npip install qrcode[pil]"))
            return
        self.qr_img.setPixmap(pm.scaled(
            200, 200, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def save_qr(self):
        uri = self.current_uri()
        if uri is None:
            notify(tr("بيانات ناقصة", "Invalid input"),
                   tr("أكمل العنوان والرمز أولًا",
                      "Complete the address and code first"))
            return
        path, _ = QFileDialog.getSaveFileName(
            None, tr("حفظ رمز QR", "Save QR"),
            os.path.join(desktop_dir(), "adb_pairing_qr.png"),
            "PNG (*.png)")
        if not path:
            return
        okflag = save_qr_png(uri, path)
        notify(tr("حفظ QR", "QR save"),
               path if okflag else tr("مكتبة qrcode غير مثبتة",
                                      "qrcode lib missing"))

    def scan(self):
        targets = mdns_pairing_targets()
        if targets:
            name, ok = QInputDialog.getItem(
                self, tr("خدمات الاقتران", "Pairing services"),
                tr("اختر خدمة:", "Pick a service:"), targets, 0, False)
            if ok and name:
                self.addr.setText(name)
        else:
            notify(tr("لا نتائج", "Nothing found"),
                   tr("افتح نافذة الإقران على الهاتف أولًا",
                      "Open the pairing dialog on the phone first"))

    def pair(self):
        addr = self.addr.text().strip()
        code = self.code.text().strip()
        if not re.match(r"^\S+:\d+$", addr) or not re.match(r"^\d{6}$", code):
            notify(tr("بيانات ناقصة", "Invalid input"),
                   tr("تحقق من العنوان والرمز", "Check address and code"))
            return

        def job():
            out = sh(["adb", "pair", addr, code], 25)
            ok = "Success" in out or "success" in out
            if ok:
                conn = mdns_targets()[:1]
                if conn:
                    sh(["adb", "connect", conn[0]], 12)
                notify(tr("تم الاقتران ", "Paired "),
                       tr("يمكنك الاتصال الآن", "You can now connect"))
            else:
                notify(tr("فشل الاقتران x", "Pairing failed x"),
                       tr("تأكد من الرمز وأن النافذة مفتوحة",
                          "Check the code and keep the dialog open"))
            if _tray_ref is not None:
                _tray_ref.kick_refresh()
        threading.Thread(target=job, daemon=True).start()
        self.accept()


class DropZone(QWidget):
    def __init__(self, tray=None):
        super().__init__()
        self.tray = tray
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self.setAcceptDrops(True)
        lbl = QLabel(tr("أفلت ملفات APK هنا", "Drop APK files here"))
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            "background:#17202a;color:#ecf0f1;border:3px dashed #3498db;"
            "border-radius:16px;font-size:15px;font-weight:bold;")
        self.lbl = lbl
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(lbl)
        self.setFixedSize(150, 150)
        from PyQt5.QtWidgets import QToolButton
        self.close_btn = QToolButton(self)
        self.close_btn.setText("x")
        self.close_btn.setFixedSize(24, 24)
        self.close_btn.setStyleSheet(
            "QToolButton{border:none;background:#e74c3c;color:white;"
            "border-radius:12px;font-weight:bold;}"
            "QToolButton:hover{background:#c0392b;}")
        self.close_btn.clicked.connect(self.close_me)
        g = QApplication.primaryScreen().availableGeometry()
        self.move(g.right() - self.width() - 30,
                  g.bottom() - self.height() - 40)

    def place_saved(self):
        geo = S.get("dropzone_geometry")
        if isinstance(geo, list) and len(geo) == 2:
            x, y = geo
            screen = QApplication.primaryScreen().availableGeometry()
            if screen.contains(x + self.width(), y + self.height()):
                self.move(x, y)

    def resizeEvent(self, e):
        self.close_btn.move(self.width() - 28, 4)
        super().resizeEvent(e)

    def close_me(self):
        g = self.geometry()
        S.set("dropzone_geometry", [g.x(), g.y()])
        S.save()
        self.hide()

    def dragEnterEvent(self, e):
        if any(is_apk(u.toLocalFile()) for u in e.mimeData().urls()):
            self.lbl.setStyleSheet(
                "background:#1a5276;color:#ffffff;border:3px solid #2ecc71;"
                "border-radius:16px;font-size:15px;font-weight:bold;")
            e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.lbl.setStyleSheet(
            "background:#17202a;color:#ecf0f1;border:3px dashed #3498db;"
            "border-radius:16px;font-size:15px;font-weight:bold;")

    def dropEvent(self, e):
        self.dragLeaveEvent(e)
        paths = [u.toLocalFile() for u in e.mimeData().urls()]
        apks = [p for p in paths if is_apk(p)]
        skipped = len(paths) - len(apks)
        if not apks:
            notify(tr("ملفات غير مدعومة", "Unsupported files"),
                   tr("أفلت ملفات APK فقط", "Drop APK files only"))
            return
        if skipped and self.tray is not None:
            notify(tr("ملفات متجاهَلة", "Files skipped"),
                   tr(f"{skipped} ليست APK وسيُتجاهل",
                      f"{skipped} non-APK file(s) will be ignored"))
        if apks and self.tray is not None:
            self.tray.install_flow(apks)


class Tray(QSystemTrayIcon):
    op_done = pyqtSignal(str, str)
    op_refresh = pyqtSignal()
    state_ready = pyqtSignal(object)
    info_ready = pyqtSignal(str)
    busy_release = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        global _tray_ref
        _tray_ref = self
        self.busy = False
        self.devices = []
        self.view = []          # merged, de-duplicated device rows
        self.suspended = set()
        self._sig = None
        self.pending_rebuild = False
        self._prev_online = None
        self._dropzone = None
        self._busy_deadline = 0.0
        self._busy_owner = None
        self._queued = None
        self.op_done.connect(self.on_op_done)
        self.op_refresh.connect(self.kick_refresh)
        self.state_ready.connect(self.on_state_ready)
        self.busy_release.connect(self.on_busy_release)
        self.info_ready.connect(lambda text: QMessageBox.information(
            None, tr("معلومات الجهاز", "Device info"), text))

        self.menu = QMenu()
        self.build_static()

        self.setContextMenu(self.menu)
        self.activated.connect(self.on_activated)
        self.setIcon(make_icon(C_GRAY, "!"))
        self.setToolTip("ADB Wireless: scanning...")

        self.menu.aboutToHide.connect(self.on_menu_hidden)
        threading.Thread(target=self._poll_loop, daemon=True).start()

        from PyQt5.QtCore import QTimer
        self._wd = QTimer(self)
        self._wd.timeout.connect(self.watchdog_tick)
        self._wd.start(3000)

    def build_static(self):
        self.menu.clear()
        self.device_actions = []
        self.header = self.menu.addAction("")
        self.header.setEnabled(False)
        self.sep1 = self.menu.addSeparator()
        act_recon = self.menu.addAction(tr("إعادة اتصال الكل",
                                           "Reconnect all"))
        act_recon.triggered.connect(self.reconnect_all)
        act_usb = self.menu.addAction(tr("توصيل جهاز عبر USB",
                                         "Connect a device over USB"))
        act_usb.triggered.connect(lambda: self.usb_connect_flow())
        act_pair = self.menu.addAction(tr("اقتران لاسلكي (بدون كابل)",
                                          "Wireless pairing (no cable)"))
        act_pair.triggered.connect(lambda: PairDialog().exec_())
        act_qr = self.menu.addAction(tr("اقتران برمز QR (مسح تلقائي)",
                                        "QR pairing (auto-scan)"))
        act_qr.triggered.connect(
            lambda: PairDialog(start_tab="scan").exec_())
        act_apk = self.menu.addAction(tr("تثبيت ملفات APK",
                                         "Install APK files"))
        act_apk.triggered.connect(lambda: self.install_flow(None))
        act_zone = self.menu.addAction(tr("منطقة إفلات APK",
                                          "APK drop-zone"))
        act_zone.triggered.connect(self.toggle_dropzone)
        act_disc = self.menu.addAction(tr("فصل الكل", "Disconnect all"))
        act_disc.triggered.connect(self.disconnect_all)
        self.sep2 = self.menu.addSeparator()
        act_doc = self.menu.addAction(tr("فحص النظام", "Doctor"))
        act_doc.triggered.connect(self.doctor)
        act_set = self.menu.addAction(tr("الإعدادات", "Settings"))
        act_set.triggered.connect(lambda: SettingsDialog().exec_())
        act_log = self.menu.addAction(tr("عرض السجل", "Open log"))
        act_log.triggered.connect(lambda: open_in_file_manager(LOG_FILE))
        self.menu.addSeparator()
        act_quit = self.menu.addAction(tr("إنهاء", "Quit"))
        act_quit.triggered.connect(QApplication.quit)

    def ensure_dropzone(self):
        if self._dropzone is None:
            self._dropzone = DropZone(self)
            self._dropzone.place_saved()
            self._dropzone.show()

    def toggle_dropzone(self):
        if self._dropzone is not None and self._dropzone.isVisible():
            self.close_me_dropzone()
        else:
            self.ensure_dropzone()
            self._dropzone.show()

    def close_me_dropzone(self):
        if self._dropzone is not None:
            self._dropzone.close_me()

    def install_flow(self, paths):
        rejected = []
        if paths:
            apk_paths = [p for p in paths if is_apk(p)]
            rejected = [os.path.basename(p) for p in paths
                        if not is_apk(p)]
            paths = apk_paths
            if not paths:
                notify(tr("ملفات غير مدعومة", "Unsupported files"),
                       tr(f"تجاهُلت {len(rejected)} - المطلوب ملفات APK",
                          f"skipped {len(rejected)} - APK files only"))
                return
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(
                None, tr("اختر ملفات APK", "Choose APK files"),
                os.path.expanduser("~"), "APK (*.apk)")
        if not paths:
            return
        # picker fed from the merged view, so a phone on USB and Wi-Fi at the
        # same time is offered once, not twice
        online = [{"serial": r["target"], "model": r["label"],
                   "state": "device"}
                  for r in self.view if r["online"]]
        if not online:
            notify(tr("لا توجد أجهزة", "No devices"),
                   tr("وصّل جهازًا أولًا", "Connect a device first"))
            return
        if len(online) == 1:
            self._do_install(paths, online[0], rejected)
            return
        menu = QMenu()
        for d in online:
            menu.addAction(d["model"]).triggered.connect(
                lambda _, dd=d: self._do_install(paths, dd, rejected))
        menu.exec_(QCursor.pos())

    # per-file adb timeout; the outer run_job gate must outlive it so a
    # slow multi-APK batch never trips the watchdog mid-install
    INSTALL_FILE_TIMEOUT = 300

    def _do_install(self, paths, dev, rejected=None):
        title = tr("تثبيت APK", "APK install")
        dlg = InstallProgressDialog(len(paths), title)
        cancelled_reason = tr("أُلغي", "cancelled")
        queued = [False]

        # preflight: if the gate will queue us (another op is running),
        # don't flash an orphan dialog - run_job notifies instead
        if self.busy and self._busy_owner is not None \
                and time.time() < self._busy_deadline:
            queued[0] = True
        else:
            dlg.show()  # GUI thread here (menu/drop callbacks)

        def run_one(path):
            """Install one APK; returns (ok, reason). Cancellable live."""
            tmpo = tempfile.TemporaryFile(mode="w+", encoding="utf-8",
                                          errors="replace")
            try:
                proc = subprocess.Popen(
                    ["adb", "-s", dev["serial"], "install", "-r", path],
                    stdout=tmpo, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL)
            except Exception as e:  # adb missing etc.
                return False, str(e)
            deadline = time.time() + self.INSTALL_FILE_TIMEOUT
            timed_out = False
            try:
                while proc.poll() is None:
                    if dlg.cancel_requested():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except Exception:
                            proc.kill()
                            proc.wait()
                        return False, cancelled_reason
                    if time.time() > deadline:
                        timed_out = True
                        proc.kill()
                        proc.wait()
                        break
                    time.sleep(0.25)
            finally:
                tmpo.seek(0)
                out = tmpo.read()
                tmpo.close()
            if timed_out:
                return False, tr("انتهت المهلة - الاتصال بطيء أو مقطوع",
                                 "timed out - connection slow or broken")
            reason = friendly_adb_error(out)
            return (not reason), reason

        def job():
            try:
                if queued[0]:
                    # worker thread - must reach show() via a queued signal
                    dlg.show_requested.emit()
                results = []  # (name, ok, reason)
                for i, p in enumerate(paths, 1):
                    if dlg.cancel_requested():
                        break
                    name = os.path.basename(p)
                    dlg.step_changed.emit(name, i)
                    ok, why = run_one(p)
                    results.append((name, ok, why))
                cancelled = len(results) < len(paths)
                okl = [n for n, k, _ in results if k]
                badl = [(n, why) for n, k, why in results if not k]
                msg = ""
                if okl:
                    msg += tr("ثُبّت: ", "Installed: ") + "، ".join(okl)
                for n, why in badl:
                    if why == cancelled_reason:
                        continue
                    msg += ("\n" if msg else "") + \
                        tr(f"فشل {n}: ", f"Failed {n}: ") + why
                if cancelled:
                    msg += ("\n" if msg else "") + \
                        tr("أُلغي باقي القائمة بناءً على طلبك",
                           "rest of the list cancelled as requested")
                if rejected:
                    msg += ("\n" if msg else "") + \
                        tr(f"تم تجاهل {len(rejected)} ملف غير APK: ",
                           f"skipped {len(rejected)} non-APK file(s): ") + \
                        "، ".join(rejected)
                self.op_done.emit(title, msg)
            finally:
                # never leave an orphan dialog behind on any code path
                dlg.finish()

        self.run_job(job, blocking=True,
                     timeout=len(paths) * self.INSTALL_FILE_TIMEOUT + 60,
                     hint=tr("جارٍ التثبيت...", "Installing..."))

    def screenshot(self, target, model):
        def job():
            r = subprocess.run(
                ["adb", "-s", target, "exec-out", "screencap", "-p"],
                capture_output=True, timeout=20)
            path = os.path.join(
                desktop_dir(),
                f"awm_shot_{datetime.now():%Y%m%d_%H%M%S}.png")
            with open(path, "wb") as f:
                f.write(r.stdout or b"")
            self.op_done.emit(tr("لقطة شاشة", "Screenshot"),
                              f"{model} > {path}")
        self.run_job(job)

    def record_screen(self, target, model):
        def job():
            remote = "/sdcard/awm_rec.mp4"
            subprocess.run(
                ["adb", "-s", target, "shell",
                 f"screenrecord --time-limit {RECORD_SECONDS} {remote}"],
                capture_output=True, timeout=RECORD_SECONDS + 20)
            local = os.path.join(
                desktop_dir(),
                f"awm_record_{datetime.now():%Y%m%d_%H%M%S}.mp4")
            subprocess.run(
                ["adb", "-s", target, "pull", remote, local],
                capture_output=True, timeout=60)
            sh(["adb", "-s", target, "shell", f"rm -f {remote}"], 8)
            self.op_done.emit(tr("تسجيل شاشة", "Screen recording"),
                              f"{model} > {local}")
        notify(tr("بدأ التسجيل", "Recording started"),
               tr(f"مدة {RECORD_SECONDS} ثانية", f"{RECORD_SECONDS}s"))
        self.run_job(job)

    def rename_device(self, serial, current):
        name, ok = QInputDialog.getText(
            None, tr("تسمية الجهاز", "Rename device"),
            tr("اسم مستعار (اتركه فارغًا للحذف):",
               "Alias (empty to remove):"),
            text=current)
        aliases = S.get("aliases") or {}
        aliases = dict(aliases)
        if ok and name.strip():
            aliases[serial] = name.strip()
        elif ok:
            aliases.pop(serial, None)
        else:
            return
        S.set("aliases", aliases)
        S.save()
        self._sig = None
        self.rebuild_device_items()

    def _poll_loop(self):
        while True:
            try:
                self.state_ready.emit(collect_state())
            except Exception:
                pass
            time.sleep(max(3, int(S.get("poll_interval_sec"))))

    def update_check_loop(self):
        time.sleep(20)
        while True:
            if S.get("check_updates"):
                try:
                    url = (f"https://api.github.com/repos/{REPO}/releases/"
                           f"latest")
                    req = urllib.request.Request(url,
                                                 headers={"User-Agent":
                                                          "awm-tray"})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        tag = json.loads(resp.read().decode()).get(
                            "tag_name", "")
                    if tag and version_is_newer(tag.lstrip("v"),
                                                __version__):
                        notify(
                            tr("يتوفر تحديث جديد", "Update available"),
                            tr(f"الإصدار {tag} متاح على GitHub",
                               f"Version {tag} is on GitHub"))
                        break
                except Exception:
                    pass
            time.sleep(6 * 3600)

    def on_menu_hidden(self):
        if self.pending_rebuild:
            self.pending_rebuild = False
            self.rebuild_device_items()

    def apply_settings(self, lang_changed=False):
        if lang_changed:
            self.build_static()
            self._sig = None
        self.kick_refresh()

    def kick_refresh(self):
        threading.Thread(
            target=lambda: self.state_ready.emit(collect_state()),
            daemon=True).start()

    def on_state_ready(self, state):
        if self.busy:
            self.pending_rebuild = True
            return
        self.devices = state["devices"]
        self.suspended = {norm_serial(s) for s in state["suspended"]}
        self.view = state["view"]

        online = [r for r in self.view if r["online"]]
        wireless = [r for r in online if r["net_target"]]

        if not shutil.which("adb"):
            self.setIcon(make_icon(C_GRAY, "!"))
            self.setToolTip(tr("adb غير مثبت", "adb is not installed"))
        elif online:
            self.setIcon(make_icon(C_GREEN, str(len(online))))
            tip = "\n".join(
                f"{r['label']} - {r['target']}" +
                (f"  {r['battery']}%" if r["battery"] is not None else "")
                for r in online)
            self.setToolTip(
                tr(f"متصل ({len(wireless)} لاسلكي):\n{tip}",
                   f"Connected ({len(wireless)} wireless):\n{tip}"))
        else:
            self.setIcon(make_icon(C_RED, "0"))
            self.setToolTip(tr("لا توجد أجهزة متصلة", "No devices connected"))

        # Loss detection keys on the hardware serial, not on the transport
        # name: a phone whose wireless port rotates keeps the same serial and
        # must not be reported as disconnected.
        cur_online = {r["serial"] for r in wireless if r["serial"]}
        if self._prev_online is not None:
            by_serial = {r["serial"]: r for r in self.view if r["serial"]}
            for s in self._prev_online - cur_online:
                row = by_serial.get(s)
                if row is not None and row["suspended"]:
                    continue           # the user asked for this one to stay off
                threading.Thread(
                    target=self._loss_handler, daemon=True,
                    args=(s, row["label"] if row else s,
                          row["known_target"] if row else "")).start()
        self._prev_online = cur_online

        sig = view_signature(self.view, S.get("lang"))
        if sig == self._sig:
            return
        self._sig = sig
        if self.menu.isVisible():
            self.pending_rebuild = True
        else:
            self.rebuild_device_items()

    def _loss_handler(self, serial, model, target):
        choice = actionable_notify(
            tr(f"انقطع اتصال {model}", f"{model} disconnected"),
            tr("هل تريد إعادة الاتصال؟", "Reconnect now?"),
            {"reconnect": tr("إعادة الاتصال", "Reconnect"),
             "dismiss": tr("تجاهل", "Dismiss")})
        if choice == "reconnect" and target:
            self.reconnect_one(target, serial)

    def rebuild_device_items(self):
        """One menu entry per physical device, taken from the merged view."""
        for a in self.device_actions:
            self.menu.removeAction(a)
        self.device_actions = []

        aliases = S.get("aliases") or {}
        rows = self.view
        online, unready, offline = [], [], []
        for r in rows:
            if r["online"]:
                online.append(r)
            elif r["state"] in ("unauthorized", "offline"):
                unready.append(r)
            else:
                offline.append(r)

        self.header.setText(tr("الأجهزة", "Devices"))

        if not rows:
            a = self.menu.addAction(tr("لا توجد أجهزة", "No devices"))
            a.setEnabled(False)
            self._add(a)

        for r in online:
            bat = f"  {r['battery']}%" if r["battery"] is not None else ""
            sub = QMenu(f"{r['label']}{bat}  [{r['tag']}]", self.menu)
            a = self.menu.addMenu(sub)
            target, label = r["target"], r["label"]
            sub.addAction(tr("عرض الشاشة (scrcpy)", "Mirror screen (scrcpy)"))\
                .triggered.connect(lambda _, t=target: self.run_scrcpy(t))
            sub.addAction(tr("لقطة شاشة", "Screenshot")).triggered.connect(
                lambda _, t=target, m=label: self.screenshot(t, m))
            sub.addAction(tr(f"تسجيل شاشة ({RECORD_SECONDS} ثانية)",
                             f"Record screen ({RECORD_SECONDS}s)"))\
                .triggered.connect(
                lambda _, t=target, m=label: self.record_screen(t, m))
            sub.addAction(tr("معلومات الجهاز", "Device info"))\
                .triggered.connect(
                lambda _, t=target, m=label: self.show_info_safe(t, m))
            sub.addAction(tr("تسمية", "Rename")).triggered.connect(
                lambda _, s=r["serial"] or target:
                    self.rename_device(s, aliases.get(s, "")))
            if not r["net_target"] and r["usb_target"]:
                sub.addAction(tr("تحويل إلى اتصال لاسلكي",
                                 "Switch to wireless"))\
                    .triggered.connect(
                        lambda _, s=r["usb_target"]: self.usb_connect_flow(s))
            if r["net_target"]:
                sub.addAction(tr("فصل مؤقت", "Disconnect"))\
                    .triggered.connect(
                        lambda _, t=r["net_target"], s=r["serial"]:
                            self.drop_one(t, s))
            self._add(a)

        for r in offline:
            target = r["known_target"]
            state_txt = tr("موقوف بواسطتك", "paused by you") if r["suspended"] \
                else tr("غير متصل", "offline")
            sub = QMenu(f"{r['label']} - {state_txt} ({target})", self.menu)
            a = self.menu.addMenu(sub)
            sub.addAction(tr("إعادة الاتصال الآن", "Reconnect now"))\
                .triggered.connect(
                    lambda _, tt=target, ss=r["serial"]:
                        self.reconnect_one(tt, ss))
            sub.addAction(tr("حذف من المحفوظات", "Forget device"))\
                .triggered.connect(lambda _, tt=target: self.remove_saved(tt))
            self._add(a)

        for r in unready:
            explain = {
                "unauthorized": tr("غير مصرّح - اقبل النافذة على الهاتف",
                                   "unauthorized - accept the prompt on the "
                                   "phone"),
                "offline": tr("الاتصال معلّق - أعد التوصيل",
                              "transport offline - reconnect it"),
            }.get(r["state"], r["state"])
            a = self.menu.addAction(f"{r['label']} - {explain}")
            a.setEnabled(False)
            self._add(a)

    def show_info_safe(self, target, model):
        def job():
            raw = sh(["adb", "-s", target, "shell", INFO_CMD], 12)
            info = parse_device_info(raw)
            bat = (f"{info['battery']}%" if info["battery"] is not None
                   else "?")
            st = info["storage"]
            storage = (tr(f"مستعمل {st[0]} من {st[1]} ({st[2]}%)",
                          f"{st[0]} used of {st[1]} ({st[2]}%)")
                       if st else "?")
            alias = resolve_label(info["serial"], info["model"],
                                  S.get("aliases"))
            text = (
                f"{tr('الاسم', 'Name')}: {alias}\n"
                f"{tr('الموديل', 'Model')}: {info['model']}\n"
                f"{tr('الأندرويد', 'Android')}: {info['android']}\n"
                f"{tr('التسلسلي', 'Serial')}: {info['serial']}\n"
                f"{tr('البطارية', 'Battery')}: {bat}\n"
                f"{tr('التخزين', 'Storage')}: {storage}\n"
                f"IP: {', '.join(info['ips']) or '?'}\n"
                f"{tr('الاتصال', 'Connection')}: {target}")
            self.info_ready.emit(text)
        self.run_job(job)

    def _add(self, a):
        self.menu.insertAction(self.sep1, a)
        self.device_actions.append(a)

    def set_busy(self, busy, hint="", timeout=45):
        self.busy = busy
        self._busy_deadline = time.time() + timeout if busy else 0.0
        if busy:
            self.setIcon(make_icon(C_YELLOW, "..."))
            self.setToolTip(hint or tr("جارٍ العمل...",
                                       "Working..."))

    def on_busy_release(self, token):
        if token is not None and token is not self._busy_owner:
            return
        self.busy = False
        self._busy_deadline = 0.0
        self._busy_owner = None
        self.run_queued()

    def run_queued(self):
        q = self._queued
        self._queued = None
        if q is not None:
            self.run_job(q, blocking=True)

    def run_job(self, job, blocking=True, timeout=45, hint=""):
        """Ownership-token gate: expiry + queue + watchdog, never sticks."""
        token = object()
        if blocking and self.busy and self._busy_owner is not None \
                and time.time() < self._busy_deadline:
            self._queued = job
            notify(tr("انتظر", "Please wait"),
                   tr("سيُنفَّذ تلقائيًا بعد انتهاء العملية الحالية",
                      "Will run automatically after the current "
                      "operation"))
            return

        def runner():
            try:
                job()
            except Exception as e:
                self.op_done.emit(tr("خطأ", "Error"), str(e))
            finally:
                self.busy_release.emit(token)

        if blocking:
            self._busy_owner = token
            self.set_busy(True, hint=hint, timeout=timeout)
        threading.Thread(target=runner, daemon=True).start()

    def watchdog_tick(self):
        if self.busy and self._busy_deadline \
                and time.time() > self._busy_deadline:
            self.on_busy_release(self._busy_owner)

    def _attach(self, target, serial="", label=""):
        """Connect one target, verify it, and record the result.

        Shared by every reconnect path so verification and cache bookkeeping
        can never drift apart. Returns (ok, target, detail).
        """
        want = norm_serial(serial)
        candidates = []
        fresh = mdns_target_for_serial(mdns_entries(), want) if want else ""
        # A fresh mDNS advert beats a saved port: Android rotates the
        # wireless-debugging port on every reboot and Wi-Fi toggle.
        for t in (fresh, target):
            if t and t not in candidates:
                candidates.append(t)
        if not candidates:
            return False, target, tr("لا يوجد عنوان معروف لهذا الجهاز",
                                     "no known address for this device")
        last = ""
        for t in candidates:
            ok, got, reason = connect_and_verify(t, want)
            if ok:
                found = got or want or device_serial(t)
                suspend_del(found)
                save_cache_entry(found, t, label or self._label_for(t, found))
                return True, t, ""
            last = reason
        return False, candidates[0], last

    def _label_for(self, target, serial=""):
        for r in self.view:
            if target in (r["usb_target"], r["net_target"],
                          r["known_target"]) \
                    or (serial and r["serial"] == norm_serial(serial)):
                return r["label"]
        probe = probe_device(target, 6)
        return probe["model"] or serial or target

    def reconnect_all(self):
        def job():
            rows = [r for r in self.view if not r["online"]
                    and r["known_target"]]
            if not rows:
                self.op_done.emit(
                    tr("إعادة الاتصال", "Reconnect"),
                    tr("كل الأجهزة المعروفة متصلة بالفعل",
                       "every known device is already connected")
                    if self.view else
                    tr("لا توجد أجهزة محفوظة. وصّل الجهاز بـ USB أولًا.",
                       "No saved devices. Connect one over USB first."))
                return
            ok, fail = [], []
            for r in rows:
                good, target, reason = self._attach(
                    r["known_target"], r["serial"], r["label"])
                if good:
                    ok.append(f"{r['label']} ({target})")
                else:
                    fail.append(f"{r['label']}: {reason}")
            msg = ""
            if ok:
                msg += tr("تم الاتصال: ", "Connected: ") + "، ".join(ok)
            if fail:
                msg += ("\n" if msg else "") + \
                    tr("فشل: ", "Failed: ") + "\n".join(fail)
            self.op_done.emit(tr("إعادة الاتصال", "Reconnect"), msg)
        self.run_job(job, blocking=True, timeout=150,
                     hint=tr("جارٍ إعادة الاتصال...", "Reconnecting..."))

    def usb_connect_flow(self, only_serial=""):
        """Take USB-attached phones wireless without switching their
        Wireless-debugging toggle off."""
        def job():
            devs = [d for d in list_devices()
                    if d["usb"] and d["state"] == "device"
                    and (not only_serial or d["serial"] == only_serial)]
            if not devs:
                self.op_done.emit(
                    "USB", tr("لا يوجد جهاز موصول بـ USB (أو التخويل مرفوض)",
                              "No USB device attached (or not authorised)"))
                return
            results = []
            for d in devs:
                results.append(self._usb_to_wireless(d))
            self.op_done.emit(tr("توصيل عبر USB", "USB connect"),
                              "\n".join(results))
        self.run_job(job, blocking=True, timeout=180,
                     hint=tr("جارٍ التحويل إلى اتصال لاسلكي...",
                             "Switching to wireless..."))

    def _usb_to_wireless(self, dev):
        """Strategy-driven wireless handover for a single USB device."""
        serial = dev["serial"]
        probe = probe_device(serial)
        ident = probe["serial"] or norm_serial(serial)
        label = probe["model"] or dev["model"]
        strategy, target = plan_connect_strategy(
            probe["sdk"], probe["adb_wifi"],
            mdns_target_for_serial(mdns_entries(), ident))

        if strategy == STRATEGY_ENABLE_WIFI:
            enable_wireless_debugging(serial)
            target = wait_for_mdns_target(ident)
            strategy = STRATEGY_MDNS if target else STRATEGY_TCPIP
        elif strategy == STRATEGY_AWAIT_MDNS:
            target = wait_for_mdns_target(ident)
            strategy = STRATEGY_MDNS if target else STRATEGY_TCPIP

        if strategy == STRATEGY_MDNS:
            ok, used, _ = self._attach(target, ident, label)
            if ok:
                return tr(f"{label}: متصل لاسلكيًا عبر {used}",
                          f"{label}: wireless via {used}")
            # The advert exists but the link failed; fall through to the
            # legacy path rather than leaving the user stranded.

        return self._legacy_tcpip_handover(serial, ident, label,
                                          probe["adb_wifi"])

    def _legacy_tcpip_handover(self, serial, ident, label, wifi_was_on):
        """Last-resort path: `adb tcpip` restarts adbd on the phone.

        That restart is what switches Wireless debugging off on Android 11+,
        so the toggle is put back afterwards and the user is told what
        happened.
        """
        port = next_free_port()
        sh(["adb", "-s", serial, "tcpip", str(port)], 12)
        time.sleep(3)
        detail = ""
        for ip in phone_ips(serial):
            ok, used, reason = self._attach(f"{ip}:{port}", ident, label)
            if ok:
                if wifi_was_on:
                    enable_wireless_debugging(serial)
                return tr(f"{label}: متصل عبر {used} (منفذ ثابت)",
                          f"{label}: connected via {used} (fixed port)")
            detail = reason
        if wifi_was_on:
            enable_wireless_debugging(serial)
        return tr(f"{label}: فشل - {detail or 'لا يوجد عنوان IP على الواي فاي'}",
                  f"{label}: failed - {detail or 'no Wi-Fi address found'}")

    def disconnect_all(self):
        """Drop wireless links on the host side only; the phone keeps its
        Wireless-debugging toggle exactly as the user left it."""
        def job():
            dropped = 0
            for r in self.view:
                if r["online"] and r["net_target"]:
                    suspend_add(r["serial"] or device_serial(r["net_target"]))
                    dropped += 1
            sh(["adb", "disconnect"], 8)
            self.op_done.emit(
                tr("فصل الاتصالات", "Disconnect all"),
                tr(f"تم فصل {dropped} - لن تعود تلقائيًا حتى تعيدها من القائمة",
                   f"{dropped} disconnected - they stay offline until you "
                   f"reconnect them here"))
        self.run_job(job)

    def drop_one(self, target, serial=""):
        def job():
            ident = norm_serial(serial) or device_serial(target)
            label = self._label_for(target, ident)
            suspend_add(ident)
            sh(["adb", "disconnect", target], 8)
            notify(tr("فصل مؤقت", "Disconnected"),
                   tr(f"{label} لن يعود تلقائيًا - أعده من القائمة",
                      f"{label} stays offline until you reconnect it here"))
            self.kick_refresh()
        self.run_job(job)

    def reconnect_one(self, target, serial=""):
        def job():
            ident = norm_serial(serial) or device_serial(target)
            suspend_del(ident)
            ok, used, reason = self._attach(target, ident)
            title = tr("إعادة الاتصال", "Reconnect")
            if ok:
                self.op_done.emit(title, tr(f"تم: {used}", f"Done: {used}"))
            else:
                self.op_done.emit(title, tr(f"فشل {used} - {reason}",
                                            f"Failed {used} - {reason}"))
        self.run_job(job, blocking=False)

    def remove_saved(self, target):
        drop_from_cache(target)
        notify(tr("المحفوظات", "Saved devices"),
               tr(f"تم حذف {target}", f"Removed {target}"))
        self.kick_refresh()

    def run_scrcpy(self, target):
        def job():
            if launch_scrcpy(target, str(S.get("scrcpy_args") or "")):
                self.op_done.emit("scrcpy",
                                  tr("جارٍ عرض الشاشة...", "Mirroring..."))
        self.run_job(job)

    def doctor(self):
        adb_v = sh(["adb", "version"], 6).splitlines()
        adb_line = adb_v[0] if adb_v else tr("غير مثبت", "not installed")
        adb_path = shutil.which("adb") or "-"
        scrcpy_path = shutil.which("scrcpy") or tr("غير مثبت", "not installed")
        mdns_raw = sh(["adb", "mdns", "check"], 8).strip()
        mdns_ok = "mdns daemon version" in mdns_raw.lower()
        mdns = mdns_raw or tr("غير مدعوم", "unsupported")
        online = [r for r in self.view if r["online"]]
        lines = [
            f"{tr('النسخة', 'Version')} : v{__version__}",
            f"adb : {adb_line} [{adb_path}]",
            f"scrcpy : {scrcpy_path}",
            f"mDNS : {mdns}",
        ]
        if not mdns_ok:
            lines.append(
                tr("  تنبيه: بدون mDNS لن يعمل الاكتشاف التلقائي ولا اقتران "
                   "QR، وسيضطر البرنامج لاستخدام tcpip",
                   "  Note: without mDNS there is no auto-discovery or QR "
                   "pairing, and the tool must fall back to tcpip"))
        lines.append(f"{tr('شبكة الحاسب', 'This machine')} : {lan_info()}")
        if not IS_WINDOWS and not IS_MACOS and shutil.which("systemctl"):
            watch = sh(["systemctl", "--user", "is-active",
                        "adbwatch.service"], 6).strip()
            lines.append(
                f"{tr('خدمة المراقبة', 'Watch service')} : "
                f"{tr('تعمل', 'running') if watch == 'active' else watch}")
        lines.append(f"{tr('أجهزة معروفة', 'Known devices')} : "
                     f"{len(self.view)}")
        lines.append(f"{tr('متصلة الآن', 'Connected now')} : {len(online)}")
        for r in online:
            lines.append(f"  {r['label']} [{r['tag']}] {r['target']}")
        QMessageBox.information(None, tr("فحص النظام", "Doctor"),
                                "\n".join(lines))

    def on_op_done(self, title, msg):
        notify(title, msg)
        self.kick_refresh()

    def on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.Context):
            self.kick_refresh()


def lan_info():
    if IS_WINDOWS:
        out = sh(["ipconfig"], 6)
        lines = [l.strip() for l in out.splitlines() if "IPv4" in l]
        return " | ".join(lines) or "N/A"
    if IS_MACOS:
        out = sh(["ifconfig"], 6)
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return " ".join(ip for ip in ips if not ip.startswith("127.")) or "N/A"
    out = sh("ip -o -4 addr show scope global".split(), 6)
    return " ".join(out.split()) or "N/A"


def acquire_single_instance_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def QTimer_single(ms, fn):
    from PyQt5.QtCore import QTimer
    QTimer.singleShot(ms, fn)


TRAY_WAIT_INTERVAL_MS = 2000
TRAY_WARN_AFTER_TRIES = 30    # ~60 s before telling the user (keep trying)


def log(msg):
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [tray] {msg}\n")
    except OSError:
        pass


def main():
    lock_sock = acquire_single_instance_lock()
    if lock_sock is None:
        notify("ADB Wireless Manager",
               tr("يعمل بالفعل في الشريط.", "Already running in the tray."))
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("adb-wireless-manager")

    log(f"starting v{__version__} (pid={os.getpid()})")
    state = {"tray": None, "tries": 0, "warned": False}

    def start_tray():
        log(f"system tray ready after {state['tries']} wait cycle(s) - starting")
        tray = Tray()
        tray.show()
        state["tray"] = tray
        threading.Thread(target=tray.update_check_loop, daemon=True).start()
        if S.get("show_dropzone"):
            QTimer_single(500, tray.ensure_dropzone)
        notify("ADB Wireless Manager v" + __version__,
               tr("يعمل الآن - انقر الأيقونة للقائمة",
                  "Running - click the icon for the menu"))

    def wait_for_tray():
        if state["tray"] is not None:
            return
        state["tries"] += 1
        if QSystemTrayIcon.isSystemTrayAvailable():
            start_tray()
            return
        if not state["warned"] and state["tries"] >= TRAY_WARN_AFTER_TRIES:
            state["warned"] = True
            log("tray still unavailable - notifying user, keep polling")
            notify("ADB Wireless Manager",
                   tr("شريط النظام غير متاح بعد - سأواصل المحاولة.",
                      "System tray is not available yet - still trying."))
        QTimer_single(TRAY_WAIT_INTERVAL_MS, wait_for_tray)

    # At login the desktop shell may not have published its tray yet
    # (AppIndicator / StatusNotifier DBus service) - poll instead of dying.
    if QSystemTrayIcon.isSystemTrayAvailable():
        start_tray()
    else:
        log("tray not ready at startup - polling every "
            f"{TRAY_WAIT_INTERVAL_MS // 1000} s")
        QTimer_single(TRAY_WAIT_INTERVAL_MS, wait_for_tray)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
