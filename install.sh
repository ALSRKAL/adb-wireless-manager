#!/usr/bin/env bash
# ==============================================================================
#  ADB Wireless Manager - Linux/macOS installer
#  Repo: https://github.com/ALSRKAL/adb-wireless-manager
#  Usage:
#    ./install.sh                 install tray autostart only
#    ./install.sh --with-watch    also install systemd auto-reconnect service
#    ./install.sh --no-tray       skip tray autostart
# ==============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TRAY="$DIR/tray/adbtray.py"
CLI="$DIR/scripts/adbconnect.sh"
WITH_WATCH=false
WITH_TRAY=true

for arg in "$@"; do
    case "$arg" in
        --with-watch) WITH_WATCH=true ;;
        --no-tray)    WITH_TRAY=false ;;
        -h|--help)    sed -n '2,7p' "$0"; exit 0 ;;
    esac
done

ok()   { printf '  \033[0;32m✔\033[0m %s\n' "$1"; }
warn() { printf '  \033[1;33m!\033[0m %s\n' "$1"; }
fail() { printf '  \033[0;31m✘\033[0m %s\n' "$1"; }

echo "=== ADB Wireless Manager installer ==="

if command -v adb >/dev/null 2>&1; then
    ok "adb found: $(command -v adb)"
else
    fail "adb missing — install it: sudo apt install adb  (or download Android platform-tools)"
fi

# distro adb builds often ship without the mDNS daemon (breaks auto
# discovery / QR pairing). Auto-provision Google's official tools.
if command -v adb >/dev/null 2>&1; then
    if adb mdns check 2>/dev/null | grep -qi "enabled\|daemon version"; then
        ok "adb mDNS backend available"
    elif [[ ! -d "$HOME/.local/share/awm/platform-tools" ]]; then
        warn "system adb lacks mDNS — downloading official platform-tools…"
        case "$(uname -s)" in
            Darwin) PT_URL="https://dl.google.com/android/repository/platform-tools-latest-darwin.zip" ;;
            *)      PT_URL="https://dl.google.com/android/repository/platform-tools-latest-linux.zip" ;;
        esac
        mkdir -p "$HOME/.local/share/awm"
        TMPZ="$(mktemp -u).zip"
        if curl -fsSL -o "$TMPZ" "$PT_URL"; then
            python3 - "$TMPZ" "$HOME/.local/share/awm" <<'PY' 2>/dev/null || unzip -qo "$TMPZ" -d "$HOME/.local/share/awm"
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
            find "$HOME/.local/share/awm/platform-tools" -maxdepth 1 \
                -type f -exec chmod +x {} + 2>/dev/null
            ok "official platform-tools installed to ~/.local/share/awm/"
        else
            warn "download failed — QR/auto-discovery needs official platform-tools"
        fi
        rm -f "$TMPZ"
    fi
fi

if command -v python3 >/dev/null 2>&1; then
    ok "python3: $(python3 --version)"
else
    fail "python3 missing — install Python 3.9+"
fi

if python3 -c "import PyQt5" >/dev/null 2>&1; then
    ok "PyQt5 installed"
else
    warn "PyQt5 missing — trying pip..."
    if command -v pip3 >/dev/null 2>&1; then
        if pip3 install --user PyQt5 >/dev/null 2>&1; then
            ok "PyQt5 installed via pip"
        else
            warn "pip failed. Try: sudo apt install python3-pyqt5"
        fi
    else
        warn "pip3 missing. Try: sudo apt install python3-pyqt5"
    fi
fi

if command -v scrcpy >/dev/null 2>&1; then
    ok "scrcpy: $(command -v scrcpy)"
else
    warn "scrcpy not installed (optional, needed for screen mirroring): sudo apt install scrcpy"
fi

chmod +x "$CLI" "$TRAY" 2>/dev/null || true

XDG_AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
if $WITH_TRAY; then
    mkdir -p "$XDG_AUTOSTART"
    cat >"$XDG_AUTOSTART/adbtray.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=ADB Wireless Tray
Comment=ADB Wireless Manager tray icon and menu
Exec=$(command -v python3 || echo /usr/bin/python3) $TRAY
Terminal=false
Categories=Utility;Network;
Icon=phone
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=5
EOF
    ok "Tray autostart -> $XDG_AUTOSTART/adbtray.desktop"
fi

if $WITH_WATCH && command -v systemctl >/dev/null 2>&1; then
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$UNIT_DIR"
    cat >"$UNIT_DIR/adbwatch.service" <<EOF
[Unit]
Description=ADB Wireless Auto-Connect (watch mode)
After=network-online.target

[Service]
Type=simple
ExecStart=$(command -v bash || echo /bin/bash) $CLI watch
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    if systemctl --user enable --now adbwatch.service; then
        ok "Watch service enabled (systemctl --user status adbwatch)"
    else
        warn "Could not start adbwatch.service automatically"
    fi
elif $WITH_WATCH; then
    warn "systemd not available — skipping watch service"
fi

echo
ok "Done! CLI: $CLI connect | reconnect | doctor"
$WITH_TRAY && ok "Log out/in once (or run: python3 $TRAY &) to see the tray icon."
