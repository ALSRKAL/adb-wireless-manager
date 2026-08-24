#!/usr/bin/env python3
"""ADB Wireless Manager - cross-platform system tray app (Windows/Linux/macOS)."""
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

from PyQt5.QtCore import QObject, QRect, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

__version__ = "12.0.0"

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

C_GREEN = "#2ecc71"
C_YELLOW = "#f1c40f"
C_RED = "#e74c3c"
C_GRAY = "#95a5a6"

POLL_MS = 8000
SINGLE_INSTANCE_PORT = 48765


def data_dir():
    if IS_WINDOWS:
        base = os.environ.get("APPDATA", os.path.expanduser("~"))
        return os.path.join(base, "adbconnect")
    if IS_MACOS:
        return os.path.expanduser("~/Library/Application Support/adbconnect")
    base = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
    return os.path.join(base, "adbconnect")


CACHE_FILE = os.path.join(data_dir(), "devices.tsv")


def log_file():
    if IS_WINDOWS:
        return os.path.join(tempfile.gettempdir(), "adbconnect.log")
    return "/tmp/adbconnect.log"


LOG_FILE = log_file()

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
        _tray_ref.showMessage(title, body,
                              QSystemTrayIcon.Information, 6000)


def list_devices():
    out = sh("adb devices -l".split())
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


def cached_targets():
    rows = []
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            for line in f:
                c = line.rstrip("\n").split("\t")
                if len(c) >= 4:
                    rows.append((c[1] + ":" + c[2], c[3].replace("_", " ")))
    except OSError:
        pass
    return rows


def mdns_targets():
    out = sh(["adb", "mdns", "services"], 10)
    tg = []
    for line in out.splitlines():
        if "_adb-tls-connect" not in line:
            continue
        m = re.search(r"(\S+:\d+)\s*$", line.strip())
        if m:
            tg.append(m.group(1))
    return sorted(set(tg))


def port_busy(p):
    if IS_WINDOWS:
        out = sh(["netstat", "-an"], 6)
        return bool(re.search(rf":{p}\s.*LISTENING", out))
    if IS_MACOS or not shutil.which("ss"):
        out = sh(["netstat", "-an"], 6)
        return bool(re.search(rf"[:.]{p}\s", out))
    out = sh(["ss", "-Htuln"], 5)
    return bool(re.search(rf"[:.]{p}(\s|$)", out))


def next_free_port(start=5555):
    p = start
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


def scrcpy_running(target):
    if IS_WINDOWS:
        out = sh(["powershell", "-NoProfile", "-Command",
                  "Get-CimInstance Win32_Process -Filter "
                  "\"Name='scrcpy.exe'\" | Select-Object -ExpandProperty "
                  "CommandLine"], 8)
        return f"-s {target}" in out
    out = sh(["pgrep", "-af", "scrcpy"], 5)
    return f"-s {target}" in out


def launch_scrcpy(target):
    if scrcpy_running(target):
        notify("Scrcpy is already running", target)
        return False
    flags = 0
    if IS_WINDOWS:
        flags = 0x00000008
    subprocess.Popen(
        ["scrcpy", "-s", target],
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
        notify("Error", f"Cannot open: {path}")


def lan_info():
    if IS_WINDOWS:
        out = sh(["ipconfig"], 6)
        lines = [l.strip() for l in out.splitlines()
                 if "IPv4" in l or "IPv4" in l.upper()]
        return " | ".join(lines) if lines else "N/A"
    if IS_MACOS:
        out = sh(["ifconfig"], 6)
        ips = re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return " ".join(ip for ip in ips if not ip.startswith("127.")) or "N/A"
    out = sh("ip -o -4 addr show scope global".split(), 6)
    return " ".join(out.split()) or "N/A"


class Tray(QSystemTrayIcon):
    op_done = pyqtSignal(str, str)
    op_refresh = pyqtSignal()

    def __init__(self):
        super().__init__()
        global _tray_ref
        _tray_ref = self
        self.busy = False
        self.devices = []
        self.op_done.connect(self.on_op_done)
        self.op_refresh.connect(self.refresh)

        self.menu = QMenu()
        self.header = self.menu.addAction("الأجهزة المتصلة | Connected Devices")
        self.header.setEnabled(False)

        self.device_actions = []
        self.sep1 = self.menu.addSeparator()

        act_recon = self.menu.addAction("🔄  إعادة اتصال الكل | Reconnect all")
        act_recon.triggered.connect(self.reconnect_all)
        act_usb = self.menu.addAction("🔌  توصيل جهاز عبر USB | Connect USB device")
        act_usb.triggered.connect(self.usb_connect_flow)
        act_disc = self.menu.addAction("✂️  فصل الكل | Disconnect all")
        act_disc.triggered.connect(self.disconnect_all)

        self.sep2 = self.menu.addSeparator()
        act_doc = self.menu.addAction("🩺  فحص النظام | Doctor")
        act_doc.triggered.connect(self.doctor)
        act_log = self.menu.addAction("📄  السجل | Open log")
        act_log.triggered.connect(lambda: open_in_file_manager(LOG_FILE))
        self.menu.addSeparator()
        act_quit = self.menu.addAction("❌  إنهاء | Quit")
        act_quit.triggered.connect(QApplication.quit)

        self.setContextMenu(self.menu)
        self.activated.connect(self.on_activated)
        self.setIcon(self.make_icon(C_GRAY, "!"))
        self.setToolTip("ADB Wireless: scanning...")

        self.timer = QTimer()
        self.timer.timeout.connect(self.refresh)
        self.timer.start(POLL_MS)
        QTimer.singleShot(300, self.refresh)

    def make_icon(self, color, label=""):
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setBrush(QColor(color))
        p.setPen(Qt.NoPen)
        p.drawEllipse(6, 6, 52, 52)
        if label:
            f = QFont()
            f.setBold(True)
            f.setPixelSize(30 if len(label) <= 2 else 22)
            p.setFont(f)
            p.setPen(QPen(QColor("white")))
            p.drawText(QRect(0, 0, 64, 64), Qt.AlignCenter, str(label))
        p.end()
        return QIcon(pm)

    def on_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.Context):
            self.refresh()

    def refresh(self):
        if self.busy:
            return
        self.devices = list_devices()
        online = [d for d in self.devices if d["state"] == "device"]
        wireless = [d for d in online if not d["usb"]]
        if not shutil.which("adb"):
            self.setIcon(self.make_icon(C_GRAY, "!"))
            self.setToolTip("adb not found!")
        elif online:
            self.setIcon(self.make_icon(C_GREEN, str(len(online))))
            tip = "\n".join(f"{d['model']} — {d['serial']}" for d in online)
            self.setToolTip(f"Connected ({len(wireless)} wireless):\n{tip}")
        else:
            self.setIcon(self.make_icon(C_RED, "0"))
            self.setToolTip("No devices connected")
        self.rebuild_device_items()

    def rebuild_device_items(self):
        for a in self.device_actions:
            self.menu.removeAction(a)
        self.device_actions = []

        online = [d for d in self.devices if d["state"] == "device"]
        bad = [d for d in self.devices if d["state"] != "device"]
        connected = {d["serial"] for d in self.devices}
        offline = [(t, lbl) for t, lbl in cached_targets()
                   if t not in connected]
        for t in mdns_targets():
            if t not in connected and t not in {x for x, _ in offline}:
                offline.append((t, "mDNS"))

        if not online and not bad and not offline:
            a = self.menu.addAction("لا توجد أجهزة | No devices")
            a.setEnabled(False)
            self._add(a)

        for d in online:
            tag = "USB" if d["usb"] else "WiFi"
            sub = QMenu(f"📱 {d['model']}  [{tag}]", self.menu)
            a = self.menu.addMenu(sub)
            s1 = sub.addAction("🖥️  عرض الشاشة (scrcpy) | Mirror screen")
            s1.triggered.connect(lambda _, t=d["serial"], m=d["model"]:
                                 self.run_scrcpy(t, m))
            s2 = sub.addAction("✂️  فصل مؤقت | Disconnect")
            s2.triggered.connect(lambda _, t=d["serial"]: self.drop_one(t))
            self._add(a)

        for t, lbl in offline:
            sub = QMenu(f"📴 {lbl} — غير متصل ({t})", self.menu)
            a = self.menu.addMenu(sub)
            r1 = sub.addAction("🔄  إعادة الاتصال الآن | Reconnect now")
            r1.triggered.connect(lambda _, tt=t: self.reconnect_one(tt))
            r2 = sub.addAction("🗑️  حذف من المحفوظات | Forget device")
            r2.triggered.connect(lambda _, tt=t: self.remove_saved(tt))
            self._add(a)

        for d in bad:
            state_ar = {
                "unauthorized": "غير مصرّح — اقبل النافذة على الهاتف",
                "offline": "غير متصل",
            }.get(d["state"], d["state"])
            a = self.menu.addAction(
                f"⚠️ {d['model']} — {state_ar} ({d['state']})")
            a.setEnabled(False)
            self._add(a)

    def _add(self, a):
        self.menu.insertAction(self.sep1, a)
        self.device_actions.append(a)

    def set_busy(self, busy, hint=""):
        self.busy = busy
        if busy:
            self.setIcon(self.make_icon(C_YELLOW, "…"))
            self.setToolTip(hint or "Working...")

    def run_job(self, job):
        if self.busy:
            notify("Please wait", "An operation is already running")
            return
        threading.Thread(target=job, daemon=True).start()

    def reconnect_all(self):
        def job():
            self.op_refresh.emit()
            connected_now = {d["serial"] for d in list_devices()
                             if d["state"] == "device"}
            targets = cached_targets()
            known = {t for t, _ in targets}
            for t in mdns_targets():
                if t not in known:
                    targets.append((t, "mDNS"))
            ok, fail = [], []
            for t, label in targets:
                if t in connected_now:
                    ok.append(label)
                    continue
                good = False
                for _ in range(2):
                    sh(["adb", "connect", t], 10)
                    time.sleep(1)
                    if t in {d["serial"] for d in list_devices()
                             if d["state"] == "device"}:
                        good = True
                        break
                (ok if good else fail).append(f"{label} ({t})")
            msg = ""
            if ok:
                msg += "تم الاتصال: " + "، ".join(ok)
            if fail:
                msg += ("\n" if msg else "") + "فشل: " + "، ".join(fail)
            if not msg:
                msg = "لا توجد أجهزة محفوظة. وصّل الجهاز بـ USB أولًا."
            self.op_done.emit("إعادة الاتصال", msg)
            self.op_refresh.emit()
        self.set_busy(True, "Reconnecting...")
        self.run_job(job)

    def usb_connect_flow(self):
        def job():
            devs = [d for d in list_devices()
                    if d["usb"] and d["state"] == "device"]
            if not devs:
                self.op_done.emit(
                    "USB", "لا يوجد جهاز موصول بـ USB (أو التخويل مرفوض)")
                self.op_refresh.emit()
                return
            port = next_free_port()
            results = []
            for d in devs:
                serial = d["serial"]
                sh(["adb", "-s", serial, "tcpip", str(port)], 12)
                time.sleep(3)
                done = False
                for ip in phone_ips(serial):
                    target = f"{ip}:{port}"
                    sh(["adb", "connect", target], 10)
                    states = {x["serial"]: x["state"] for x in list_devices()}
                    if states.get(target) == "device":
                        results.append(f"{d['model']} → {target}")
                        done = True
                        break
                if not done:
                    results.append(f"{d['model']} → فشل")
            self.op_done.emit("توصيل USB",
                              "\n".join(results) if results else "فشل")
            self.op_refresh.emit()
        self.set_busy(True, "Connecting via USB...")
        self.run_job(job)

    def disconnect_all(self):
        def job():
            sh(["adb", "disconnect"], 8)
            self.op_done.emit("فصل الاتصالات", "تم فصل جميع الاتصالات اللاسلكية")
            self.op_refresh.emit()
        self.run_job(job)

    def drop_one(self, target):
        def job():
            sh(["adb", "disconnect", target], 8)
            self.op_refresh.emit()
        self.run_job(job)

    def reconnect_one(self, target):
        def job():
            good = False
            for _ in range(3):
                sh(["adb", "connect", target], 10)
                time.sleep(1)
                states = {d["serial"] for d in list_devices()
                          if d["state"] == "device"}
                if target in states:
                    good = True
                    break
            if good:
                self.op_done.emit("إعادة الاتصال", f"تم: {target}")
            else:
                self.op_done.emit(
                    "إعادة الاتصال",
                    f"فشل {target} — تأكد أن الهاتف والشبكة يعملان")
            self.op_refresh.emit()
        self.set_busy(True, f"Reconnecting: {target}")
        self.run_job(job)

    def remove_saved(self, target):
        drop_from_cache(target)
        notify("المحفوظات", f"تم حذف {target} من القائمة")
        self.op_refresh.emit()

    def run_scrcpy(self, target, model):
        def job():
            if launch_scrcpy(target):
                self.op_done.emit("Scrcpy", f"جارٍ عرض شاشة {model}")
        self.run_job(job)

    def doctor(self):
        adb_v = sh(["adb", "version"], 6).splitlines()
        adb_line = adb_v[0] if adb_v else "NOT FOUND!"
        adb_path = shutil.which("adb") or "-"
        scrcpy_path = shutil.which("scrcpy") or "not installed"
        mdns = sh(["adb", "mdns", "check"], 8).strip() or "unsupported"
        n_cache = len(cached_targets())
        n_dev = len([d for d in self.devices if d["state"] == "device"])
        watch_line = ""
        if not IS_WINDOWS and not IS_MACOS and shutil.which("systemctl"):
            watch = sh(["systemctl", "--user", "is-active",
                        "adbwatch.service"], 6).strip()
            state = "running ✓" if watch == "active" else f"{watch}"
            watch_line = f"Watch service : {state}\n"
        text = (
            f"ADB           : {adb_line}\n"
            f"adb path      : {adb_path}\n"
            f"scrcpy        : {scrcpy_path}\n"
            f"mDNS          : {mdns}\n"
            f"This machine  : {lan_info()}\n"
            f"{watch_line}"
            f"Devices       : {n_dev} connected\n"
            f"Saved devices : {n_cache}\n"
            f"Cache file    : {CACHE_FILE}\n"
            f"Log file      : {LOG_FILE}"
        )
        QMessageBox.information(None, "ADB Wireless Manager — Doctor", text)

    def on_op_done(self, title, msg):
        self.set_busy(False)
        notify(title, msg)


def acquire_single_instance_lock():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def main():
    lock_sock = acquire_single_instance_lock()
    if lock_sock is None:
        notify("ADB Wireless Manager", "Already running in the tray.")
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("adb-wireless-manager")

    if not QSystemTrayIcon.isSystemTrayAvailable():
        notify("ADB Wireless Manager",
               "System tray is not available on this desktop session.")
        return 1

    tray = Tray()
    tray.show()
    notify("ADB Wireless Manager", "يعمل الآن — انقر الأيقونة للقائمة")
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
