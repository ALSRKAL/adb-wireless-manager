#!/usr/bin/env python3
"""ADB Wireless Manager - cross-platform system tray app (Windows/Linux/macOS).

Repo: https://github.com/ALSRKAL/adb-wireless-manager
"""
import json
import os
import re
import shutil
import socket
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
                             QInputDialog, QLabel, QLineEdit, QMainWindow,
                             QMenu, QMessageBox, QPushButton, QSpinBox,
                             QSystemTrayIcon, QVBoxLayout)

__version__ = "13.0.0"
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
    rows = []
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) >= 4:
                    rows.append((c[0], c[1] + ":" + c[2],
                                 c[3].replace("_", " ")))
    except OSError:
        pass
    return rows


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
    for line in out.splitlines():
        if "_adb-tls-connect" not in line:
            continue
        m = re.search(
            r"adb-([A-Za-z0-9]+)-[A-Za-z0-9]+\._adb-tls-connect"
            r".*?(\S+:\d+)\s*$", line.strip())
        if m and m.group(2) not in seen:
            seen.add(m.group(2))
            entries.append((m.group(1).upper(), m.group(2)))
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


def device_serial(target):
    out = sh(["adb", "-s", target, "shell",
              "getprop", "ro.serialno"], 6).strip()
    if out:
        return out.upper()
    for mserial, t in mdns_entries():
        if t == target:
            return mserial
    return ""


def device_battery(serial):
    out = sh(["adb", "-s", serial, "shell",
              "dumpsys battery | grep -E '^  level'"], 5)
    m = re.search(r"level:\s*(\d+)", out)
    return int(m.group(1)) if m else None


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
    a = aliases.get(serial) or aliases.get(serial.upper()) \
        or aliases.get(serial.lower())
    return a if a else model


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
    devices = list_devices()
    batteries = {}
    for d in devices:
        if d["state"] == "device":
            b = device_battery(d["serial"])
            if b is not None:
                batteries[d["serial"]] = b
    return {
        "devices": devices,
        "cached": cached_entries(),
        "mdns": mdns_entries(),
        "suspended": suspended_serials(),
        "batteries": batteries,
        "dark": system_is_dark(),
    }


class SettingsDialog(QDialog):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("الإعدادات — ADB Wireless", 
                               "Settings — ADB Wireless"))
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
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr("اقتران لاسلكي (أندرويد 11+)",
                               "Wireless pairing (Android 11+)"))
        self.setMinimumWidth(430)
        lay = QVBoxLayout(self)
        hint = QLabel(tr(
            "على الهاتف: خيارات المطوّر ← تصحيح لاسلكي ← الإقران برمز.\n"
            "أدخل عنوان الاقتران والرمز الظاهر على شاشة الهاتف.",
            "On phone: Developer options → Wireless debugging → Pair with "
            "code.\nEnter the pairing address and code shown on the phone."))
        hint.setWordWrap(True)
        lay.addWidget(hint)

        row1 = QHBoxLayout()
        self.addr = QLineEdit()
        self.addr.setPlaceholderText(tr("العنوان مثل 192.168.1.5:37843",
                                        "Address e.g. 192.168.1.5:37843"))
        btn_scan = QPushButton(tr("بحث 🔍", "Scan 🔍"))
        btn_scan.clicked.connect(self.scan)
        row1.addWidget(self.addr, 1)
        row1.addWidget(btn_scan)
        lay.addLayout(row1)

        row2 = QHBoxLayout()
        self.code = QLineEdit()
        self.code.setPlaceholderText(tr("رمز من 6 أرقام", "6-digit code"))
        row2.addWidget(self.code, 1)
        lay.addLayout(row2)

        row3 = QHBoxLayout()
        go = QPushButton(tr("اقتران واتصال", "Pair && Connect"))
        cancel = QPushButton(tr("إلغاء", "Cancel"))
        go.clicked.connect(self.pair)
        cancel.clicked.connect(self.reject)
        row3.addStretch(1)
        row3.addWidget(go)
        row3.addWidget(cancel)
        lay.addLayout(row3)

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
                notify(tr("تم الاقتران ✓", "Paired ✓"),
                       tr("يمكنك الاتصال الآن", "You can now connect"))
            else:
                notify(tr("فشل الاقتران ✗", "Pairing failed ✗"),
                       tr("تأكد من الرمز وأن النافذة مفتوحة",
                          "Check the code and keep the dialog open"))
            if _tray_ref is not None:
                _tray_ref.kick_refresh()
        threading.Thread(target=job, daemon=True).start()
        self.accept()


class DropZone(QMainWindow):
    def __init__(self, tray):
        super().__init__()
        self.tray = tray
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint
                            | Qt.WindowStaysOnTopHint)
        self.setAcceptDrops(True)
        lbl = QLabel("📦\n" + tr("أفلت APK هنا", "Drop APK here"))
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(
            "background:#17202a;color:#ecf0f1;border:3px dashed #3498db;"
            "border-radius:16px;font-size:15px;font-weight:bold;")
        lblFixedSize = 150
        lbl.setFixedHeight(lblFixedSize)
        self.setFixedSize(lblFixedSize, lblFixedSize)
        self.setCentralWidget(lbl)
        geo = S.get("dropzone_geometry")
        placed = False
        if isinstance(geo, list) and len(geo) == 2:
            x, y = geo
            screen = QApplication.primaryScreen().availableGeometry()
            if screen.contains(x + lblFixedSize, y + lblFixedSize):
                self.move(x, y)
                placed = True
        if not placed:
            g = QApplication.primaryScreen().availableGeometry()
            self.move(g.right() - self.width() - 30,
                      g.bottom() - self.height() - 40)

    def dragEnterEvent(self, e):
        if any(is_apk(u.toLocalFile()) for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [u.toLocalFile() for u in e.mimeData().urls()
                 if is_apk(u.toLocalFile())]
        if paths:
            self.tray.install_flow(paths)


class Tray(QSystemTrayIcon):
    op_done = pyqtSignal(str, str)
    op_refresh = pyqtSignal()
    state_ready = pyqtSignal(object)
    info_ready = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        global _tray_ref
        _tray_ref = self
        self.busy = False
        self.devices = []
        self.cached = []
        self.mdns = []
        self.suspended = set()
        self.batteries = {}
        self._sig = None
        self.pending_rebuild = False
        self._prev_online = None
        self._dropzone = None
        self.op_done.connect(self.on_op_done)
        self.op_refresh.connect(self.kick_refresh)
        self.state_ready.connect(self.on_state_ready)
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

    def build_static(self):
        self.menu.clear()
        self.device_actions = []
        self.header = self.menu.addAction("")
        self.header.setEnabled(False)
        self.sep1 = self.menu.addSeparator()
        act_recon = self.menu.addAction(tr("🔄  إعادة اتصال الكل",
                                           "🔄  Reconnect all"))
        act_recon.triggered.connect(self.reconnect_all)
        act_usb = self.menu.addAction(tr("🔌  توصيل جهاز عبر USB",
                                         "🔌  Connect USB device"))
        act_usb.triggered.connect(self.usb_connect_flow)
        act_pair = self.menu.addAction(tr("🔗  اقتران لاسلكي (بدون كابل)",
                                          "🔗  Wireless pairing (no cable)"))
        act_pair.triggered.connect(lambda: PairDialog().exec_())
        act_apk = self.menu.addAction(tr("📦  تثبيت ملفات APK…",
                                         "📦  Install APK files…"))
        act_apk.triggered.connect(lambda: self.install_flow(None))
        act_zone = self.menu.addAction(tr("📥  منطقة إفلات APK",
                                          "📥  Toggle APK drop-zone"))
        act_zone.triggered.connect(self.toggle_dropzone)
        act_disc = self.menu.addAction(tr("✂️  فصل الكل", "✂️  Disconnect all"))
        act_disc.triggered.connect(self.disconnect_all)
        self.sep2 = self.menu.addSeparator()
        act_doc = self.menu.addAction(tr("🩺  فحص النظام", "🩺  Doctor"))
        act_doc.triggered.connect(self.doctor)
        act_set = self.menu.addAction(tr("⚙️  الإعدادات", "⚙️  Settings"))
        act_set.triggered.connect(lambda: SettingsDialog().exec_())
        act_log = self.menu.addAction(tr("📄  السجل", "📄  Open log"))
        act_log.triggered.connect(lambda: open_in_file_manager(LOG_FILE))
        self.menu.addSeparator()
        act_quit = self.menu.addAction(tr("❌  إنهاء", "❌  Quit"))
        act_quit.triggered.connect(QApplication.quit)

    def ensure_dropzone(self):
        if self._dropzone is None:
            self._dropzone = DropZone(self)
            self._dropzone.show()

    def toggle_dropzone(self):
        if self._dropzone is not None and self._dropzone.isVisible():
            g = self._dropzone.geometry()
            S.set("dropzone_geometry", [g.x(), g.y()])
            S.save()
            self._dropzone.hide()
        else:
            self.ensure_dropzone()
            self._dropzone.show()

    def install_flow(self, paths):
        if not paths:
            paths, _ = QFileDialog.getOpenFileNames(
                None, tr("اختر ملفات APK", "Choose APK files"),
                os.path.expanduser("~"), "APK (*.apk)")
        if not paths:
            return
        online = [d for d in self.devices if d["state"] == "device"]
        if not online:
            notify(tr("لا أجهزة", "No devices"),
                   tr("وصّل جهازًا أولًا", "Connect a device first"))
            return
        if len(online) == 1:
            self._do_install(paths, online[0])
            return
        menu = QMenu()
        for d in online:
            menu.addAction(
                f"📱 {resolve_label(d['serial'], d['model'], S.get('aliases'))}"
            ).triggered.connect(lambda _, dd=d: self._do_install(paths, dd))
        menu.exec_(QCursor.pos())

    def _do_install(self, paths, dev):
        def job():
            results = []
            for p in paths:
                r = subprocess.run(
                    ["adb", "-s", dev["serial"], "install", "-r", p],
                    capture_output=True, text=True, timeout=180)
                okflag = "Success" in (r.stdout or "")
                results.append((os.path.basename(p), okflag))
            okl = [n for n, k in results if k]
            badl = [n for n, k in results if not k]
            msg = ""
            if okl:
                msg += tr("ثُبّت: ", "Installed: ") + "، ".join(okl)
            if badl:
                msg += ("\n" if msg else "") + \
                    tr("فشل: ", "Failed: ") + "، ".join(badl)
            self.op_done.emit(tr("تثبيت APK", "APK install"), msg)
        self.set_busy(True, tr("جارٍ التثبيت...", "Installing..."))
        threading.Thread(target=job, daemon=True).start()

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
                              f"{model} → {path}")
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
                              f"{model} → {local}")
        notify(tr("تسجيل بدأ ⏺", "Recording started ⏺"),
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
        self.kick_refresh()

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
                            tr("يتوفر تحديث جديد 🚀", "Update available 🚀"),
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
        self.cached = state["cached"]
        self.mdns = state["mdns"]
        self.suspended = {s.upper() for s in state["suspended"]}
        self.batteries = state["batteries"]

        online = [d for d in self.devices if d["state"] == "device"]
        wireless = [d for d in online if not d["usb"]]
        aliases = S.get("aliases")

        if not shutil.which("adb"):
            self.setIcon(make_icon(C_GRAY, "!"))
            self.setToolTip(tr("adb غير مثبت!", "adb not found!"))
        elif online:
            self.setIcon(make_icon(C_GREEN, str(len(online))))
            tip = "\n".join(
                f"{resolve_label(d['serial'], d['model'], aliases)} — "
                f"{d['serial']}" +
                (f" 🔋{self.batteries[d['serial']]}%"
                 if d["serial"] in self.batteries else "")
                for d in online)
            self.setToolTip(
                tr(f"متصل ({len(wireless)} لاسلكي):\n{tip}",
                   f"Connected ({len(wireless)} wireless):\n{tip}"))
        else:
            self.setIcon(make_icon(C_RED, "0"))
            self.setToolTip(tr("لا توجد أجهزة متصلة", "No devices connected"))

        cur_online = {d["serial"] for d in wireless}
        if self._prev_online is not None:
            lost = self._prev_online - cur_online
            if lost:
                models = {d["serial"]: resolve_label(
                    d["serial"], d["model"], aliases)
                    for d in self.devices}
                targets_by_serial = {s: t for s, t, _ in self.cached}
                for s in lost:
                    threading.Thread(
                        target=self._loss_handler, daemon=True,
                        args=(s, models.get(s, s),
                              targets_by_serial.get(s))).start()
        self._prev_online = cur_online

        sig = repr((
            sorted((d["serial"], d["state"]) for d in self.devices),
            sorted((s, t) for s, t, _ in self.cached),
            sorted(self.mdns),
            sorted(self.suspended),
            sorted(self.batteries.items()),
            S.get("lang"),
        ))
        if sig == self._sig:
            return
        self._sig = sig
        if self.menu.isVisible():
            self.pending_rebuild = True
        else:
            self.rebuild_device_items()

    def _loss_handler(self, serial, model, target):
        choice = actionable_notify(
            tr(f"انقطع اتصال {model} ⚠️", f"{model} disconnected ⚠️"),
            tr("هل تريد إعادة الاتصال؟", "Reconnect now?"),
            {"reconnect": tr("أعد الاتصال", "Reconnect"),
             "dismiss": tr("تجاهل", "Dismiss")})
        if choice == "reconnect" and target:
            self.reconnect_one(target, serial)

    def rebuild_device_items(self):
        for a in self.device_actions:
            self.menu.removeAction(a)
        self.device_actions = []

        aliases = S.get("aliases")
        online = [d for d in self.devices if d["state"] == "device"]
        bad = [d for d in self.devices if d["state"] != "device"]
        connected = {d["serial"] for d in self.devices}
        offline = [(s, t, lbl) for s, t, lbl in self.cached
                   if t not in connected]
        for mserial, t in self.mdns:
            if t not in connected and t not in {x for _, x, _ in offline}:
                offline.append((mserial, t, "mDNS"))

        self.header.setText(tr("الأجهزة المتصلة", "Connected Devices"))

        if not online and not bad and not offline:
            a = self.menu.addAction(tr("لا توجد أجهزة", "No devices"))
            a.setEnabled(False)
            self._add(a)

        for d in online:
            tag = "USB" if d["usb"] else "WiFi"
            bat = self.batteries.get(d["serial"])
            battxt = f" 🔋{bat}%" if bat is not None else ""
            sub = QMenu(f"📱 {resolve_label(d['serial'], d['model'], aliases)}"
                        f"{battxt}  [{tag}]", self.menu)
            a = self.menu.addMenu(sub)
            sub.addAction(tr("🖥️  عرض الشاشة (scrcpy)",
                             "🖥️  Mirror screen")).triggered.connect(
                lambda _, t=d["serial"]: self.run_scrcpy(t))
            sub.addAction(tr("📸  لقطة شاشة", "📸  Screenshot"))\
                .triggered.connect(
                lambda _, t=d["serial"], m=d["model"]: self.screenshot(t, m))
            sub.addAction(tr("⏺️  تسجيل شاشة (30ث)",
                             "⏺️  Record screen (30s)")).triggered.connect(
                lambda _, t=d["serial"], m=d["model"]:
                    self.record_screen(t, m))
            sub.addAction(tr("ℹ️  معلومات الجهاز", "ℹ️  Device info"))\
                .triggered.connect(
                lambda _, t=d["serial"], m=d["model"]: self.show_info_safe(
                    t, m))
            sub.addAction(tr("✏️  تسمية", "✏️  Rename")).triggered.connect(
                lambda _, s=d["serial"]:
                    self.rename_device(s, aliases.get(s, "")))
            sub.addAction(tr("✂️  فصل مؤقت", "✂️  Disconnect"))\
                .triggered.connect(lambda _, t=d["serial"]: self.drop_one(t))
            self._add(a)

        for sserial, t, lbl in offline:
            if sserial.upper() in self.suspended:
                sub = QMenu(f"🚫 {lbl} — "
                            f"{tr('موقوف بواسطتك', 'paused by you')} ({t})",
                            self.menu)
            else:
                sub = QMenu(f"📴 {lbl} — "
                            f"{tr('غير متصل', 'offline')} ({t})", self.menu)
            a = self.menu.addMenu(sub)
            sub.addAction(tr("🔄  إعادة الاتصال الآن", "🔄  Reconnect now"))\
                .triggered.connect(lambda _, tt=t, ss=sserial:
                                   self.reconnect_one(tt, ss))
            sub.addAction(tr("🗑️  حذف من المحفوظات", "🗑️  Forget device"))\
                .triggered.connect(lambda _, tt=t: self.remove_saved(tt))
            self._add(a)

        for d in bad:
            state_ar = {
                "unauthorized": tr("غير مصرّح — اقبل النافذة على الهاتف",
                                   "unauthorized — accept prompt on phone"),
                "offline": tr("غير متصل", "offline"),
            }.get(d["state"], d["state"])
            a = self.menu.addAction(
                f"⚠️ {d['model']} — {state_ar} ({d['state']})")
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
                f"🔋 {tr('البطارية', 'Battery')}: {bat}\n"
                f"💾 {tr('التخزين', 'Storage')}: {storage}\n"
                f"🌐 IP: {', '.join(info['ips']) or '?'}\n"
                f"{tr('الاتصال', 'Connection')}: {target}")
            self.info_ready.emit(text)
        self.run_job(job)

    def _add(self, a):
        self.menu.insertAction(self.sep1, a)
        self.device_actions.append(a)

    def set_busy(self, busy, hint=""):
        self.busy = busy
        if busy:
            self.setIcon(make_icon(C_YELLOW, "…"))
            self.setToolTip(hint or tr("جارٍ العمل...",
                                       "Working..."))

    def run_job(self, job):
        if self.busy:
            notify(tr("انتظر", "Please wait"),
                   tr("توجد عملية جارية حاليًا",
                      "An operation is already running"))
            return

        def runner():
            try:
                job()
            except Exception as e:
                self.op_done.emit(tr("خطأ", "Error"), str(e))

        threading.Thread(target=runner, daemon=True).start()

    def reconnect_all(self):
        def job():
            connected_now = {d["serial"] for d in list_devices()
                             if d["state"] == "device"}
            targets = [(s, t, lbl) for s, t, lbl in cached_entries()]
            known = {t for _, t, _ in targets}
            for mserial, t in mdns_entries():
                if t not in known:
                    targets.append((mserial, t, "mDNS"))
            ok, fail = [], []
            for serial, t, label in targets:
                if t in connected_now:
                    ok.append(label)
                    suspend_del(serial)
                    continue
                good = False
                retries = max(1, int(S.get("max_retries")))
                for _ in range(retries):
                    sh(["adb", "connect", t], 10)
                    time.sleep(1)
                    if t in {d["serial"] for d in list_devices()
                             if d["state"] == "device"}:
                        good = True
                        break
                if good:
                    suspend_del(serial)
                    ok.append(f"{label} ({t})")
                else:
                    fail.append(f"{label} ({t})")
            msg = ""
            if ok:
                msg += tr("تم الاتصال: ", "Connected: ") + "، ".join(ok)
            if fail:
                msg += ("\n" if msg else "") + \
                    tr("فشل: ", "Failed: ") + "، ".join(fail)
            if not msg:
                msg = tr("لا توجد أجهزة محفوظة. وصّل الجهاز بـ USB أولًا.",
                         "No saved devices. Plug one via USB first.")
            self.op_done.emit(tr("إعادة الاتصال", "Reconnect"), msg)
        self.set_busy(True, tr("جارٍ إعادة الاتصال...", "Reconnecting..."))
        self.run_job(job)

    def usb_connect_flow(self):
        def job():
            devs = [d for d in list_devices()
                    if d["usb"] and d["state"] == "device"]
            if not devs:
                self.op_done.emit(
                    "USB", tr("لا يوجد جهاز موصول بـ USB (أو التخويل مرفوض)",
                              "No USB device attached (or unauthorized)"))
                return
            port = next_free_port()
            results = []
            for d in devs:
                serial = d["serial"]
                sh(["adb", "-s", serial, "tcpip", str(port)], 12)
                time.sleep(3)
                done = False
                for ip in phone_ips(serial):
                    tgt = f"{ip}:{port}"
                    sh(["adb", "connect", tgt], 10)
                    states = {x["serial"]: x["state"] for x in list_devices()}
                    if states.get(tgt) == "device":
                        results.append(f"{d['model']} → {tgt}")
                        done = True
                        break
                if not done:
                    results.append(f"{d['model']} → {tr('فشل', 'failed')}")
            self.op_done.emit(tr("توصيل USB", "USB connect"),
                              "\n".join(results))
        self.set_busy(True, tr("جارٍ توصيل الجهاز عبر USB...",
                               "Connecting via USB..."))
        self.run_job(job)

    def disconnect_all(self):
        def job():
            for d in list_devices():
                if d["state"] == "device" and not d["usb"]:
                    suspend_add(device_serial(d["serial"]))
            sh(["adb", "disconnect"], 8)
            self.op_done.emit(
                tr("فصل الاتصالات", "Disconnect all"),
                tr("لن يعودوا تلقائيًا حتى تعيدوا من القائمة",
                   "They stay offline until you reconnect them here"))
        self.run_job(job)

    def drop_one(self, target):
        def job():
            serial = device_serial(target)
            suspend_add(serial)
            sh(["adb", "disconnect", target], 8)
            name = next((resolve_label(d["serial"], d["model"],
                                       S.get("aliases"))
                         for d in list_devices() if d["serial"] == target),
                        target)
            notify(tr("فصل مؤقت", "Disconnected"),
                   tr(f"{name} لن يعود تلقائيًا — أعده من القائمة 🚫",
                      f"{name} stays offline until you reconnect 🚫"))
            self.kick_refresh()
        self.run_job(job)

    def reconnect_one(self, target, serial=""):
        def job():
            good = False
            retries = max(1, int(S.get("max_retries")))
            for _ in range(retries):
                sh(["adb", "connect", target], 10)
                time.sleep(1)
                states = {d["serial"] for d in list_devices()
                          if d["state"] == "device"}
                if target in states:
                    good = True
                    break
            if good:
                suspend_del(serial)
                self.op_done.emit(tr("إعادة الاتصال", "Reconnect"),
                                  f"{tr('تم', 'Done')}: {target}")
            else:
                self.op_done.emit(
                    tr("إعادة الاتصال", "Reconnect"),
                    tr(f"فشل {target} — تأكد أن الهاتف والشبكة يعملان",
                       f"Failed {target} — check phone & network"))
        self.set_busy(True, f"{tr('إعادة اتصال', 'Reconnecting')}: {target}")
        self.run_job(job)

    def remove_saved(self, target):
        drop_from_cache(target)
        notify(tr("المحفوظات", "Saved devices"),
               tr(f"تم حذف {target}", f"Removed {target}"))
        self.kick_refresh()

    def run_scrcpy(self, target):
        def job():
            if launch_scrcpy(target, str(S.get("scrcpy_args") or "")):
                self.op_done.emit("scrcpy",
                                  tr("جارٍ عرض الشاشة…", "Mirroring…"))
        self.run_job(job)

    def doctor(self):
        adb_v = sh(["adb", "version"], 6).splitlines()
        adb_line = adb_v[0] if adb_v else "NOT FOUND!"
        adb_path = shutil.which("adb") or "-"
        scrcpy_path = shutil.which("scrcpy") or tr("غير مثبت", "missing")
        mdns = sh(["adb", "mdns", "check"], 8).strip() or "unsupported"
        n_cache = len(cached_entries())
        n_dev = len([d for d in self.devices if d["state"] == "device"])
        watch_line = ""
        if not IS_WINDOWS and not IS_MACOS and shutil.which("systemctl"):
            watch = sh(["systemctl", "--user", "is-active",
                        "adbwatch.service"], 6).strip()
            state = tr("يعمل ✓", "running ✓") if watch == "active" \
                else watch
            watch_line = (f"{tr('المراقبة', 'Watch')} : {state}\n")
        text = (
            f"ADB : {adb_line} [{adb_path}]\n"
            f"scrcpy : {scrcpy_path}\n"
            f"mDNS : {mdns}\n"
            f"{tr('شبكة الحاسب', 'This machine')} : {lan_info()}\n"
            f"{watch_line}"
            f"{tr('الأجهزة المتصلة', 'Devices')} : {n_dev}\n"
            f"{tr('المحفوظة', 'Saved')} : {n_cache}\n"
            f"{tr('النسخة', 'Version')} : v{__version__}")
        QMessageBox.information(None,
                                tr("فحص النظام", "Doctor"), text)

    def on_op_done(self, title, msg):
        self.set_busy(False)
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


def main():
    lock_sock = acquire_single_instance_lock()
    if lock_sock is None:
        notify("ADB Wireless Manager",
               tr("يعمل بالفعل في الشريط.", "Already running in the tray."))
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("adb-wireless-manager")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        notify("ADB Wireless Manager",
               tr("شريط النظام غير متاح في هذه الجلسة.",
                  "System tray is not available on this desktop session."))
        return 1

    tray = Tray()
    tray.show()
    threading.Thread(target=tray.update_check_loop, daemon=True).start()
    if S.get("show_dropzone"):
        QTimer_single(500, tray.ensure_dropzone)
    notify("ADB Wireless Manager v" + __version__,
           tr("يعمل الآن — انقر الأيقونة للقائمة",
              "Running — click the icon for the menu"))
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
