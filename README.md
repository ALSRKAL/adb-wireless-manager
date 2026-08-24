# ADB Wireless Manager

[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)](#installation)
[![Python](https://img.shields.io/badge/python-3.9%2B-green)](requirements.txt)
[![Tests](https://img.shields.io/badge/tests-15%2F15%20passing-brightgreen)](tests/run_tests.sh)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

**النسخة العربية من التوثيق | [Arabic documentation](README.ar.md)**

Cut the cable. Connect to your Android devices over Wi-Fi with one click —
from a system-tray menu or the command line. Supports **multiple phones at
once**, survives network drops, laptop reboots, and even phone reboots
(automatic mDNS re-discovery), and mirrors screens with scrcpy.

---

## ✨ Features

| | Feature | Tray (GUI) | CLI |
|--|---------|:---:|:---:|
| 📡 | Wireless connect over USB (one-time setup) | ✅ | ✅ |
| 🔁 | Auto-reconnect saved devices (cache + mDNS) | ✅ | ✅ |
| 👀 | Watch mode — self-healing connection loop | systemd | `watch` |
| 📱 | Multi-device support (connect many phones together) | ✅ | ✅ |
| 🖥️ | Screen mirroring via scrcpy per device | ✅ | — |
| 📴 | Offline devices stay visible with a per-device reconnect button | ✅ | — |
| 🚫 | Manual suspend: a device you disconnect stays offline until *you* reconnect it (watch/auto-heal skips it) | ✅ | ✅ |
| 🩺 | Environment doctor (adb / scrcpy / mDNS / network) | ✅ | ✅ |
| 🟢 | Live tray status icon (green = connected, red = none, yellow = busy) | ✅ | — |

## 🗂 Project layout

```
adb-wireless-manager/
├── scripts/
│   ├── adbconnect.sh        POSIX CLI (Linux/macOS/BSD)
│   └── adbconnect.ps1       Windows CLI (PowerShell 5+)
├── tray/
│   └── adbtray.py           Cross-platform Qt tray app
├── tests/
│   ├── test_core.py         Unit tests (15 cases, no device needed)
│   └── run_tests.sh         Full test runner (LIVE=1 for live checks)
├── install.sh               Linux/macOS installer (--with-watch)
├── uninstall.sh             Linux uninstaller
├── install.ps1              Windows installer
└── requirements.txt         Python requirements
```

## 📋 Prerequisites

| Requirement | Mandatory | Notes |
|-------------|:---------:|-------|
| Android device with USB debugging | ✅ | Developer options → USB debugging |
| `adb` (Android platform-tools) | ✅ | Windows: `winget install Google.PlatformTools` · Debian/Ubuntu: `sudo apt install adb` · macOS: `brew install android-platform-tools` |
| Python **3.9+** + **PyQt5** | ✅ for tray | CLI-only users can skip this |
| `scrcpy` | optional | Screen mirroring · `winget install scrcpy` / `sudo apt install scrcpy` / `brew install scrcpy` |
| GNOME shell (Linux) | optional | Tray icons need *Ubuntu AppIndicators* enabled: `gnome-extensions enable ubuntu-appindicators@ubuntu.com` |

Python dependencies (`pip install -r requirements.txt`):

```
PyQt5>=5.15
```

## 🚀 Installation

### Linux / macOS

```bash
git clone https://github.com/ALSRKAL/adb-wireless-manager.git
cd adb-wireless-manager

./install.sh                 # tray autostart only
./install.sh --with-watch    # tray + background auto-reconnect service
```

### Windows (PowerShell)

```powershell
git clone https://github.com/ALSRKAL/adb-wireless-manager.git
cd adb-wireless-manager
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer verifies adb/scrcpy/PyQt5, registers the tray to start with your
session, and (with `--with-watch`) creates a systemd user service that keeps
your phones connected in the background.

## 🕹 Usage

### First time with a new phone (needs the cable once)

1. Plug the phone in via USB and accept the debugging prompt.
2. Click the tray icon → **🔌 Connect USB device** — or run:

```bash
# Linux / macOS                    # Windows (PowerShell)
./scripts/adbconnect.sh connect    powershell -File scripts\adbconnect.ps1 connect
```

3. Unplug the cable. Done — the phone is remembered forever.

### Every day after that — no cable

- **Tray:** click the icon; every saved phone appears. Use
  **🔄 Reconnect all**, or expand a specific 📴 offline phone and hit
  **Reconnect now**. Click a connected 📱 phone → **Mirror screen (scrcpy)**.
- **CLI:**

```bash
./scripts/adbconnect.sh reconnect   # cache + mDNS auto-discovery
./scripts/adbconnect.sh watch       # keep healing the connection
./scripts/adbconnect.sh list        # show devices
./scripts/adbconnect.sh disconnect  # drop all wireless links
./scripts/adbconnect.sh doctor      # full environment diagnostics
./scripts/adbconnect.sh pair        # Android 11+ wireless pairing
```

```powershell
powershell -File scripts\adbconnect.ps1 reconnect
powershell -File scripts\adbconnect.ps1 doctor
```

### How permanent is it?

| Event | What happens |
|-------|--------------|
| Network drops & returns | Watch service / tray restores the link automatically |
| Laptop reboots | Autostarted tray (+ watch service) reconnect on login |
| Phone reboots | Its wireless port changes → mDNS discovery finds it again |
| You unpair on the phone | Run `pair` (or plug USB once more) to re-pair |

## ⚙️ Configuration

The bash CLI reads overrides from `~/.config/adbconnect/config`
(`KEY=VALUE`, e.g. `AUTO_SCRCPY=false`, `START_PORT=5555`,
`WATCH_INTERVAL=20`). Saved devices live in
`~/.local/share/adbconnect/devices.tsv` (`%APPDATA%\adbconnect` on Windows).

## 🧪 Tests

```bash
./tests/run_tests.sh          # static analysis + 15 unit tests
LIVE=1 ./tests/run_tests.sh   # + live adb/service/tray checks
```

## 🛠 Troubleshooting

| Symptom | Fix |
|---------|-----|
| "unauthorized" | Unlock the phone and accept the RSA prompt |
| Tray icon missing (GNOME) | Enable AppIndicators (see prerequisites), re-login |
| Reconnect fails right after phone reboot | Keep the pairing dialog flow: `pair` command, or plug USB once |
| Port already in use | The tool auto-increments ports (5555…5699) |
| scrcpy window never opens | Install scrcpy or disable auto-launch (`AUTO_SCRCPY=false`) |

## 🤝 Contributing

PRs are welcome! Please run `./tests/run_tests.sh` before submitting.

## 📄 License

[MIT](LICENSE) © Mohammed Alsrkal ([@ALSRKAL](https://github.com/ALSRKAL))
