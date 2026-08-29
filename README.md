# ADB Wireless Manager

[![CI](https://github.com/ALSRKAL/adb-wireless-manager/actions/workflows/ci.yml/badge.svg)](https://github.com/ALSRKAL/adb-wireless-manager/actions/workflows/ci.yml)
[![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-blue)](#installation)
[![Python](https://img.shields.io/badge/python-3.9%2B-green)](requirements.txt)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)

[التوثيق بالعربية](README.ar.md)

Connect to Android devices over Wi-Fi from a system-tray menu or the command
line. Handles several devices at once, keeps links alive across network drops
and reboots, and mirrors screens through scrcpy.

## How it connects

Android 11 introduced Wireless debugging, a TLS service the phone advertises
over mDNS on a port that changes after every reboot and Wi-Fi toggle. The older
approach, `adb tcpip <port>`, restarts `adbd` on the phone. That restart tears
down the Wireless-debugging session and leaves the developer-options toggle
switched off.

This tool therefore picks its path per device:

| Device state | Path taken | Effect on the phone |
|--------------|------------|---------------------|
| Android 11+, already advertising over mDNS | connect to the advertised port | nothing is changed |
| Android 11+, toggle off | set `adb_wifi_enabled`, wait for the advert | toggle switched on |
| Android 11+, toggle on but no advert yet | wait for the advert | nothing is changed |
| Android 10 or older, or no mDNS support | `adb tcpip` fallback | `adbd` restarts, toggle restored afterwards |

Disconnecting is always host-side only: `adb disconnect`, never `adb usb` and
never `adb tcpip`. Your Wireless-debugging toggle stays exactly as you left it.

Every connection is verified before it is reported as successful: the tool runs
a real command round-trip and compares the hardware serial against the device
it was asked to reach, so a phone that has inherited another one's DHCP address
is refused instead of silently adopted.

## Features

| Feature | Tray | CLI |
|---------|:----:|:---:|
| Wireless handover from a USB cable, with verification | yes | yes |
| Reconnect known devices without a cable (mDNS + saved ports) | yes | yes |
| Watch mode: healing loop for dropped links | systemd | `watch` |
| Several devices connected at the same time | yes | yes |
| One menu row per physical device, whatever it is connected through | yes | - |
| Manual pause: a device you disconnect stays offline until you reconnect it | yes | yes |
| Wireless pairing wizard for Android 11+ with a readiness checklist | yes | yes |
| Screen mirroring per device through scrcpy | yes | - |
| Screenshot and 30-second screen recording, saved to the desktop | yes | - |
| APK installer with a drop zone, per-file progress and a working cancel | yes | - |
| Device info: battery, storage, Android version, addresses | yes | - |
| Device aliases | yes | - |
| Environment doctor: adb, scrcpy, mDNS, network, watch service | yes | yes |
| Arabic and English interface | yes | yes |
| Update check against GitHub releases | yes | - |

## Requirements

| Requirement | Required | Notes |
|-------------|:--------:|-------|
| Android device with USB debugging | yes | Developer options, USB debugging |
| `adb` (Android platform-tools) | yes | Windows: `winget install Google.PlatformTools`; Debian/Ubuntu: `sudo apt install adb`; macOS: `brew install android-platform-tools` |
| Python 3.9+ with PyQt5 | for the tray | CLI-only users can skip it |
| `adb` built with mDNS | recommended | Needed for auto-discovery and QR pairing. Distribution builds often lack it; the installer then fetches Google's platform-tools into `~/.local/share/awm/`. Check with `adb mdns check` |
| `scrcpy` | optional | Screen mirroring |
| GNOME on Linux | optional | Tray icons need AppIndicators: `gnome-extensions enable ubuntu-appindicators@ubuntu.com` |

## Installation

Linux and macOS:

```bash
git clone https://github.com/ALSRKAL/adb-wireless-manager.git
cd adb-wireless-manager

./install.sh                 # tray autostart only
./install.sh --with-watch    # tray plus the background reconnect service
```

Windows:

```powershell
git clone https://github.com/ALSRKAL/adb-wireless-manager.git
cd adb-wireless-manager
powershell -ExecutionPolicy Bypass -File install.ps1
```

The installer checks adb, scrcpy and PyQt5, registers the tray to start with
your session, and with `--with-watch` creates a systemd user service that keeps
devices connected in the background.

## Usage

### First time with a device

The cable is needed once, to authorise this computer.

1. Connect over USB and accept the debugging prompt on the phone.
2. Tray menu, `Connect a device over USB`. Or from the command line:

```bash
./scripts/adbconnect.sh connect                    # Linux, macOS
powershell -File scripts\adbconnect.ps1 connect    # Windows
```

3. Unplug. The device is saved.

On Android 11 and newer you can skip the cable entirely: use
`Wireless pairing (no cable)` in the tray, or `adbconnect.sh pair`.

### After that

From the tray, click the icon. Every known device appears once, with its
current transport. Use `Reconnect all`, or open a single offline device and
choose `Reconnect now`.

From the command line:

```bash
./scripts/adbconnect.sh reconnect    # mDNS discovery plus saved ports
./scripts/adbconnect.sh watch        # healing loop
./scripts/adbconnect.sh list         # current adb devices
./scripts/adbconnect.sh disconnect   # drop wireless links on this machine
./scripts/adbconnect.sh doctor       # environment diagnostics
./scripts/adbconnect.sh pair         # Android 11+ wireless pairing
```

### What survives what

| Event | Result |
|-------|--------|
| Network drops and returns | The watch service or the tray restores the link |
| Computer reboots | The autostarted tray and watch service reconnect at login |
| Phone reboots | Its wireless port changes; mDNS discovery finds the new one |
| You disconnect a device yourself | It stays offline until you reconnect it; the healing loop skips it |
| You unpair on the phone | Run `pair`, or connect the cable once more |

## Configuration

The tray and the CLI share `~/.config/adbconnect/settings.json`, written by the
tray's settings window. The bash CLI also reads `KEY=VALUE` overrides from
`~/.config/adbconnect/config`:

| Key | Default | Meaning |
|-----|---------|---------|
| `START_PORT` | 5555 | First port for the tcpip fallback |
| `MAX_RETRIES` | 3 | Connection attempts per address |
| `CONNECT_TIMEOUT` | 6 | Seconds per `adb connect` |
| `MDNS_WAIT` | 20 | Seconds to wait for an mDNS advert |
| `WATCH_INTERVAL` | 20 | Seconds between healing passes |
| `AUTO_SCRCPY` | true | Launch scrcpy after a successful connect |
| `SCRCPY_ARGS` | empty | Extra scrcpy flags |
| `PREFER_IFACES` | wlan,wifi,eth,ap | Interface order when picking an address |
| `LANG_CODE` | auto | `ar`, `en` or `auto` |

Saved devices live in `~/.local/share/adbconnect/devices.tsv`, or
`%APPDATA%\adbconnect` on Windows. One row per device, keyed on the hardware
serial.

## Project layout

```
adb-wireless-manager/
  scripts/
    adbconnect.sh      POSIX CLI (Linux, macOS, BSD)
    adbconnect.ps1     Windows CLI (PowerShell 5+)
  tray/
    adbtray.py         Cross-platform Qt tray application
  tests/
    test_core.py       Unit tests, no device required
    run_tests.sh       Static analysis plus unit tests
  install.sh           Linux and macOS installer
  uninstall.sh         Linux uninstaller
  install.ps1          Windows installer
  requirements.txt     Python dependencies
```

## Tests

```bash
./tests/run_tests.sh          # static analysis plus unit tests
LIVE=1 ./tests/run_tests.sh   # also runs live adb, service and tray checks
```

The unit tests cover the connection strategy, the device-identity merge that
keeps duplicate rows out of the menu, cache de-duplication, output parsers and
headless GUI construction. They also guard two rules at source level: no code
path may call `adb usb`, and no shipped file may contain decorative glyphs.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `unauthorized` | Unlock the phone and accept the prompt |
| Wireless debugging switches itself off | Fixed in 14.0.0. The tool no longer restarts `adbd` on Android 11+, and restores the toggle when the tcpip fallback is unavoidable |
| The same phone appeared twice in the menu | Fixed in 14.0.0. Rows are merged on the hardware serial; old duplicated cache files are repaired on read |
| Reconnect fails right after a phone reboot | The wireless port changed. `reconnect` picks up the new one over mDNS, provided your adb has mDNS support |
| QR pairing or auto-discovery never sees the phone | Your adb build lacks mDNS. Verify with `adb mdns check`; the installer can fetch Google's platform-tools |
| Tray icon missing on GNOME | Enable AppIndicators, then log out and back in |
| Port already in use | Ports are probed upward from 5555 to 5699 |
| scrcpy window never opens | Install scrcpy, or set `AUTO_SCRCPY=false` |

## Contributing

Run `./tests/run_tests.sh` before opening a pull request.

## License

[MIT](LICENSE), Mohammed Alsrkal ([@ALSRKAL](https://github.com/ALSRKAL))
