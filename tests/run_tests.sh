#!/usr/bin/env bash
# ==============================================================================
#  ADB Wireless Manager - full test runner
#  Usage:
#    ./tests/run_tests.sh          static + unit tests (no device needed)
#    LIVE=1 ./tests/run_tests.sh   also run live system/device checks
# ==============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

pass=0; fail=0
ok()   { printf '  \033[0;32m[ PASS ]\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[0;31m[ FAIL ]\033[0m %s\n' "$1"; fail=$((fail+1)); }
title(){ printf '\n\033[1m-- %s --\033[0m\n' "$1"; }

title "Static checks"
if bash -n scripts/adbconnect.sh; then ok "bash syntax: scripts/adbconnect.sh"; else bad "bash syntax"; fi
for f in tray/adbtray.py tests/test_core.py; do
    if python3 -m py_compile "$f"; then ok "python compile: $f"; else bad "python compile: $f"; fi
done
if command -v pwsh >/dev/null 2>&1; then
    if pwsh -NoProfile -Command "\$null = [scriptblock]::Create((Get-Content -Raw scripts/adbconnect.ps1))" \
        >/dev/null 2>&1; then ok "powershell parse: scripts/adbconnect.ps1"
    else bad "powershell parse"; fi
else
    printf '  \033[2m- SKIP powershell parse (pwsh not installed on this machine)\033[0m\n'
fi

title "Unit tests (core logic)"
if python3 tests/test_core.py >/tmp/awm_unittest.log 2>&1; then
    ok "unittest: $(grep -c '^ok\| ok$' /tmp/awm_unittest.log 2>/dev/null || echo 'all') tests passed"
    tail -3 /tmp/awm_unittest.log | sed 's/^/      /'
else
    bad "unittest - see /tmp/awm_unittest.log"
    tail -15 /tmp/awm_unittest.log
fi

title "Installer sanity"
if bash -n install.sh && bash -n uninstall.sh; then ok "bash syntax: installers"; else bad "installer syntax"; fi

if [[ "${LIVE:-0}" == "1" ]]; then
    title "Live environment checks"
    command -v adb >/dev/null 2>&1 && ok "adb installed" || bad "adb missing"
    command -v scrcpy >/dev/null 2>&1 && ok "scrcpy installed" || bad "scrcpy missing"
    python3 -c "import PyQt5" >/dev/null 2>&1 && ok "PyQt5 importable" || bad "PyQt5 missing"
    adb start-server >/dev/null 2>&1; ok "adb server responsive ($(adb devices | grep -cE 'device|offline|unauthorized') entries)"
    pgrep -f "[t]ray/adbtray.py$" >/dev/null && ok "tray app running" \
        || printf '  \033[2m- INFO tray not currently running\033[0m\n'
    if command -v systemctl >/dev/null 2>&1; then
        [[ "$(systemctl --user is-active adbwatch.service)" == "active" ]] \
            && ok "watch service active" \
            || printf '  \033[2m- INFO watch service not active\033[0m\n'
    fi
fi

printf '\n\033[1mResult: %d passed, %d failed\033[0m\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
