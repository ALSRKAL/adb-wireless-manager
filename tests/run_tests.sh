#!/usr/bin/env bash
# ==============================================================================
#  ADB Wireless Manager - full test runner
#  Usage:
#    ./tests/run_tests.sh          static + unit tests (no device needed)
#    LIVE=1 ./tests/run_tests.sh   also run live system/device checks
#
#  Runs the same checks CI does, so a push cannot fail on something that was
#  reproducible here.
# ==============================================================================
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || {
    printf 'cannot enter the project directory\n' >&2
    exit 1
}

UNIT_LOG="${TMPDIR:-/tmp}/awm_unittest.log"

# every shell file CI lints, kept in one place
SHELL_FILES=(scripts/adbconnect.sh install.sh uninstall.sh tests/run_tests.sh)

pass=0; fail=0
ok()   { printf '  \033[0;32m[ PASS ]\033[0m %s\n' "$1"; pass=$((pass+1)); }
bad()  { printf '  \033[0;31m[ FAIL ]\033[0m %s\n' "$1"; fail=$((fail+1)); }
skip() { printf '  \033[2m[ SKIP ]\033[0m %s\n' "$1"; }
title(){ printf '\n\033[1m-- %s --\033[0m\n' "$1"; }

title "Shell syntax"
for f in "${SHELL_FILES[@]}"; do
    if bash -n "$f"; then ok "bash syntax: $f"; else bad "bash syntax: $f"; fi
done

title "Shellcheck"
if command -v shellcheck >/dev/null 2>&1; then
    if shellcheck "${SHELL_FILES[@]}"; then
        ok "shellcheck: ${#SHELL_FILES[@]} files clean"
    else
        bad "shellcheck reported findings (CI treats these as failures)"
    fi
else
    skip "shellcheck not installed - CI will still run it"
fi

title "Python"
for f in tray/adbtray.py tests/test_core.py; do
    if python3 -m py_compile "$f"; then ok "compile: $f"; else bad "compile: $f"; fi
done

title "PowerShell"
if command -v pwsh >/dev/null 2>&1; then
    for f in scripts/adbconnect.ps1 install.ps1; do
        if pwsh -NoProfile -Command \
            "\$null = [scriptblock]::Create((Get-Content -Raw $f))" \
            >/dev/null 2>&1
        then ok "parse: $f"
        else bad "parse: $f"; fi
    done
else
    skip "pwsh not installed - CI will still run it"
fi

title "Unit tests (core logic)"
if python3 tests/test_core.py >"$UNIT_LOG" 2>&1; then
    ok "unittest: $(grep -c '^ok\| ok$' "$UNIT_LOG" 2>/dev/null || echo 'all') tests passed"
    tail -3 "$UNIT_LOG" | sed 's/^/      /'
else
    bad "unittest - see $UNIT_LOG"
    tail -15 "$UNIT_LOG"
fi

if [[ "${LIVE:-0}" == "1" ]]; then
    title "Live environment checks"
    if command -v adb >/dev/null 2>&1; then ok "adb installed"
    else bad "adb missing"; fi
    if command -v scrcpy >/dev/null 2>&1; then ok "scrcpy installed"
    else skip "scrcpy not installed (optional)"; fi
    if python3 -c "import PyQt5" >/dev/null 2>&1; then ok "PyQt5 importable"
    else bad "PyQt5 missing"; fi
    if command -v adb >/dev/null 2>&1; then
        adb start-server >/dev/null 2>&1
        ok "adb server responsive ($(adb devices \
            | grep -cE 'device|offline|unauthorized') entries)"
        if adb mdns check 2>/dev/null | grep -qi 'mdns daemon version'; then
            ok "adb has an mDNS backend"
        else
            skip "adb lacks mDNS - auto-discovery and QR pairing unavailable"
        fi
    fi
    if pgrep -f "[t]ray/adbtray.py$" >/dev/null; then ok "tray app running"
    else skip "tray not currently running"; fi
    if command -v systemctl >/dev/null 2>&1; then
        if [[ "$(systemctl --user is-active adbwatch.service)" == "active" ]]
        then ok "watch service active"
        else skip "watch service not active"; fi
    fi
fi

printf '\n\033[1mResult: %d passed, %d failed\033[0m\n' "$pass" "$fail"
[[ $fail -eq 0 ]]
