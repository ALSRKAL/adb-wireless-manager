#!/usr/bin/env bash
# ADB Wireless Manager - uninstaller (Linux)
set -uo pipefail

echo "=== Removing ADB Wireless Manager ==="
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/adbtray.desktop" && echo "  removed tray autostart"
systemctl --user disable --now adbwatch.service >/dev/null 2>&1 \
    && echo "  stopped watch service"
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/adbwatch.service"
systemctl --user daemon-reload 2>/dev/null
pkill -f "tray/adbtray.py" 2>/dev/null && echo "  tray app closed"
echo "Done. (Saved devices cache kept in ~/.local/share/adbconnect/)"
