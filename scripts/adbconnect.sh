#!/usr/bin/env bash
# ==============================================================================
#  ADB Wireless Manager - POSIX CLI
#  Commands: connect / reconnect / pair / watch / list / disconnect / doctor
#  Prefers Android 11+ wireless debugging over mDNS and falls back to classic
#  tcpip only when that is unavailable, because tcpip restarts adbd and
#  switches the phone's Wireless-debugging toggle off.
#  Requires: bash 4+, adb.  Optional: scrcpy, ss|netstat, ping
#  Repo    : https://github.com/ALSRKAL/adb-wireless-manager
# ==============================================================================
set -uo pipefail
IFS=$'\n\t'

readonly VERSION="14.0.1"
readonly SCRIPT_NAME="${0##*/}"

# ------------------------------------------------------------------------------
# 1) CENTRAL CONFIG  (single source of truth - override via config file or env)
#    Config file: ~/.config/adbconnect/config   (plain KEY=VALUE bash)
# ------------------------------------------------------------------------------
readonly XDG_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/adbconnect"
readonly XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/adbconnect"
_AWM_PT="$HOME/.local/share/awm/platform-tools"
[[ -d "$_AWM_PT" ]] && export PATH="$_AWM_PT:$PATH"
unset _AWM_PT
readonly CONFIG_FILE="$XDG_CONF/config"
readonly SETTINGS_FILE="$XDG_CONF/settings.json"
readonly CACHE_FILE="$XDG_DATA/devices.tsv"
readonly SUSPENDED_FILE="$XDG_DATA/suspended.tsv"
_LOCK_FILE="/tmp/adbconnect.$(id -u).lock"
readonly LOCK_FILE="$_LOCK_FILE"
readonly LOCK_WAIT=45        # seconds a manual command waits for a watch pass

START_PORT="${ADBC_START_PORT:-5555}"          # first port to try
MAX_RETRIES="${ADBC_MAX_RETRIES:-3}"           # connect attempts per IP
CONNECT_TIMEOUT="${ADBC_CONNECT_TIMEOUT:-6}"   # seconds per adb connect
SHELL_TIMEOUT="${ADBC_SHELL_TIMEOUT:-4}"       # seconds per adb shell probe
TCPIP_SETTLE="${ADBC_TCPIP_SETTLE:-3}"         # sleep after `adb tcpip`
MDNS_WAIT="${ADBC_MDNS_WAIT:-20}"              # seconds to await an mDNS advert
WIRELESS_SDK=30                                # Android 11: adb_wifi_enabled
AUTO_SCRCPY="${ADBC_AUTO_SCRCPY:-true}"        # launch scrcpy on success
SCRCPY_ARGS="${ADBC_SCRCPY_ARGS:-}"            # extra scrcpy flags
WATCH_INTERVAL="${ADBC_WATCH_INTERVAL:-20}"    # --watch poll seconds
PREFER_IFACES="${ADBC_PREFER_IFACES:-wlan,wifi,eth,ap}"
LOG_FILE="${ADBC_LOG_FILE:-/tmp/adbconnect.log}"
LOG_MAX_KB="${ADBC_LOG_MAX_KB:-512}"
LANG_CODE="${ADBC_LANG:-auto}"                 # ar | en | auto
VERBOSE=false
QUIET=false

# shellcheck source=/dev/null
[[ -r "$CONFIG_FILE" ]] && source "$CONFIG_FILE"

# overlay: unified GUI settings (settings.json) win over KEY=VALUE config
load_json_settings() {
    command -v python3 >/dev/null 2>&1 || return 0
    [[ -r "$SETTINGS_FILE" ]] || return 0
    local out
    out=$(python3 - "$SETTINGS_FILE" <<'PY' 2>/dev/null
import json, sys
mapping = {"watch_interval_sec": "WATCH_INTERVAL",
           "start_port": "START_PORT",
           "max_retries": "MAX_RETRIES",
           "connect_timeout_sec": "CONNECT_TIMEOUT",
           "auto_scrcpy": "AUTO_SCRCPY",
           "scrcpy_args": "SCRCPY_ARGS"}
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    raise SystemExit
for k, var in mapping.items():
    if k in d:
        v = d[k]
        if isinstance(v, bool):
            v = "true" if v else "false"
        print(f'{var}="{v}"')
PY
)
    [[ -n "$out" ]] && eval "$out"
}
load_json_settings

# ------------------------------------------------------------------------------
# 2) UI: colors + i18n (translation-first: no raw strings in logic)
# ------------------------------------------------------------------------------
if [[ -t 1 && "${NO_COLOR:-}" == "" ]]; then
    BOLD=$'\033[1m'; DIM=$'\033[2m'; RED=$'\033[0;31m'; GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'; BLUE=$'\033[0;36m'; NC=$'\033[0m'
else
    BOLD=''; DIM=''; RED=''; GREEN=''; YELLOW=''; BLUE=''; NC=''
fi

if [[ "$LANG_CODE" == "auto" ]]; then
    if [[ "${LANG:-}${LC_ALL:-}" == *ar* ]]; then
        LANG_CODE="ar"
    else
        LANG_CODE="en"
    fi
fi

declare -A T_AR=(
  [banner]="مدير ADB اللاسلكي"
  [need]="أداة مطلوبة غير مثبتة: %s"
  [locked]="عملية أخرى ما زالت تعمل بعد انتظار %s ثانية. أوقف خدمة المراقبة ثم أعد المحاولة: systemctl --user stop adbwatch.service"
  [no_usb]="لا توجد أجهزة USB متصلة (أو أن التخويل مرفوض)."
  [dev]="الجهاز: %s %s"
  [unauth]="غير مصرَّح (unauthorized) - اقبل نافذة التخويل على شاشة الهاتف."
  [offline_usb]="حالة الجهاز غير جاهزة: %s"
  [port]="المنفذ المخصص: %s"
  [tcpip]="تفعيل وضع TCP/IP..."
  [tcpip_fail]="فشل أمر tcpip: %s"
  [no_ip]="لا يوجد عنوان IP على شبكة WiFi. تحقق من اتصال الهاتف."
  [try]="محاولة %s/%s > %s"
  [unreachable]="العنوان %s لا يستجيب للشبكة (ping) - تأكد أن الجهازين على نفس الشبكة."
  [ok]="تم الاتصال والتحقق: %s"
  [zombie]="الاتصال قائم لكن الجهاز لا يستجيب للأوامر - إصلاح..."
  [dev_offline]="الاتصال معلّق في حالة offline - إصلاح..."
  [reset]="تنظيف الاتصال من جهة الحاسب..."
  [wrong_dev]="العنوان %s يخص جهازًا آخر (%s) - تم إلغاء الاتصال."
  [refused]="لا شيء يستمع على %s - التصحيح اللاسلكي مغلق أو المنفذ تغيّر."
  [no_answer]="لا استجابة من %s خلال المهلة - العنوان غير قابل للوصول."
  [wireless_on]="تفعيل التصحيح اللاسلكي على الجهاز..."
  [await_mdns]="بانتظار إعلان التصحيح اللاسلكي عبر mDNS..."
  [via_mdns]="اتصال عبر التصحيح اللاسلكي: %s"
  [legacy]="لا يوجد تصحيح لاسلكي متاح - استخدام tcpip (سيُعاد تشغيل adbd على الهاتف)."
  [wifi_restored]="تم إرجاع مفتاح التصحيح اللاسلكي إلى وضع التشغيل."
  [wifi_not_restored]="النظام أبقى التصحيح اللاسلكي مغلقًا - فعّله يدويًا من خيارات المطوّر."
  [wireless_denied]="النظام لم يسمح بتفعيل التصحيح اللاسلكي تلقائيًا - فعّله يدويًا من خيارات المطوّر."
  [failed_dev]="فشلت كل المحاولات لهذا الجهاز. راجع السجل: %s"
  [unplug]="يمكنك سحب كابل USB الآن - الاتصال اللاسلكي شغّال."
  [scrcpy_run]="Scrcpy يعمل بالفعل لهذا الجهاز."
  [scrcpy_go]="تشغيل Scrcpy..."
  [summary]="الخلاصة"
  [none_conn]="لا توجد اتصالات لاسلكية حالياً."
  [cache_empty]="لا توجد أجهزة محفوظة. وصّل الجهاز بـ USB ونفّذ السكربت مرة واحدة."
  [recon]="إعادة اتصال بالمحفوظ: %s (%s)"
  [mdns]="جهاز مقترن ظهر عبر mDNS: %s - جارٍ الاتصال..."
  [already]="متصل مسبقاً: %s"
  [disc]="تم فصل: %s"
  [pair_scan]="جارٍ البحث عن أجهزة الاقتران (mDNS)..."
  [pair_none]="لم يتم العثور على جهاز في وضع الاقتران. الهاتف: إعدادات المطوّر > تصحيح لاسلكي > الإقران برمز."
  [pair_found]="تم العثور على: %s"
  [pair_ask]="أدخل رمز الاقتران المكوّن من 6 أرقام: "
  [pair_ok]="تم الاقتران بنجاح مع %s"
  [pair_fail]="فشل الاقتران. تأكد من الرمز وأن نافذة الاقتران ما زالت مفتوحة."
  [watch]="مراقبة مستمرة كل %s ثانية (Ctrl+C للإيقاف)..."
  [watch_lost]="انقطع %s - إعادة اتصال..."
  [doctor]="تشخيص البيئة"
  [bye]="تم."
)
declare -A T_EN=(
  [banner]="Wireless ADB Manager"
  [need]="Missing required tool: %s"
  [locked]="Another operation is still running after waiting %ss. Stop the watch service and retry: systemctl --user stop adbwatch.service"
  [no_usb]="No USB devices detected (or authorization denied)."
  [dev]="Device: %s %s"
  [unauth]="Unauthorized - accept the RSA prompt on the phone screen."
  [offline_usb]="Device state not ready: %s"
  [port]="Assigned port: %s"
  [tcpip]="Enabling TCP/IP mode..."
  [tcpip_fail]="tcpip command failed: %s"
  [no_ip]="No WiFi IP address found. Check the phone's connection."
  [try]="Attempt %s/%s -> %s"
  [unreachable]="%s is not reachable (ping) - make sure both are on the same LAN."
  [ok]="Connected and verified: %s"
  [zombie]="Linked but the device answers no commands - healing..."
  [dev_offline]="Transport stuck offline - healing..."
  [reset]="Clearing the transport on the host side..."
  [wrong_dev]="%s belongs to a different device (%s) - connection dropped."
  [refused]="Nothing is listening on %s - wireless debugging is off or the port changed."
  [no_answer]="No answer from %s within the timeout - the address is unreachable."
  [wireless_on]="Turning wireless debugging on over the cable..."
  [await_mdns]="Waiting for the wireless-debugging mDNS advert..."
  [via_mdns]="Connected through wireless debugging: %s"
  [legacy]="No wireless debugging available - falling back to tcpip (this restarts adbd on the phone)."
  [wifi_restored]="Wireless debugging toggle switched back on."
  [wifi_not_restored]="The system left wireless debugging off - turn it back on in Developer options."
  [wireless_denied]="The system refused to enable wireless debugging - turn it on in Developer options."
  [failed_dev]="All attempts failed for this device. See log: %s"
  [unplug]="You can unplug the USB cable now - the wireless link is live."
  [scrcpy_run]="Scrcpy already running for this device."
  [scrcpy_go]="Launching scrcpy..."
  [summary]="Summary"
  [none_conn]="No wireless connections right now."
  [cache_empty]="No saved devices. Plug in USB and run once."
  [recon]="Reconnecting saved device: %s (%s)"
  [mdns]="Paired device discovered via mDNS: %s - connecting..."
  [already]="Already connected: %s"
  [disc]="Disconnected: %s"
  [pair_scan]="Scanning for pairing devices (mDNS)..."
  [pair_none]="No device in pairing mode. On phone: Developer options -> Wireless debugging -> Pair with code."
  [pair_found]="Found: %s"
  [pair_ask]="Enter the 6-digit pairing code: "
  [pair_ok]="Paired successfully with %s"
  [pair_fail]="Pairing failed. Check the code and keep the pairing dialog open."
  [watch]="Watching every %ss (Ctrl+C to stop)..."
  [watch_lost]="%s dropped - reconnecting..."
  [doctor]="Environment diagnostics"
  [bye]="Done."
)

t() { # t <key> [printf args...]
    local key="$1"; shift
    local fmt
    if [[ "$LANG_CODE" == "ar" ]]; then fmt="${T_AR[$key]-$key}"; else fmt="${T_EN[$key]-$key}"; fi
    # shellcheck disable=SC2059
    printf "$fmt" "$@"
}

# ------------------------------------------------------------------------------
# 3) LOGGING
# ------------------------------------------------------------------------------
init_log() {
    mkdir -p "$(dirname "$LOG_FILE")" "$XDG_DATA" "$XDG_CONF" 2>/dev/null
    if [[ -f "$LOG_FILE" ]]; then
        local kb=$(( $(wc -c <"$LOG_FILE" 2>/dev/null || echo 0) / 1024 ))
        (( kb > LOG_MAX_KB )) && mv -f "$LOG_FILE" "$LOG_FILE.1"
    fi
    printf '\n===== v%s | %s | %s =====\n' "$VERSION" "$(date '+%F %T')" "$*" >>"$LOG_FILE"
}

log() { # log <INFO|OK|WARN|ERR|DEBUG> <message>
    local lvl="$1"; shift
    local msg="$*" color="$NC" icon="[    ]"
    case "$lvl" in
        INFO)  color="$BLUE";   icon="[INFO]" ;;
        OK)    color="$GREEN";  icon="[ OK ]" ;;
        WARN)  color="$YELLOW"; icon="[WARN]" ;;
        ERR)   color="$RED";    icon="[FAIL]" ;;
        DEBUG) color="$DIM";    icon="[ .. ]" ;;
    esac
    printf '[%s] [%-5s] %s\n' "$(date '+%H:%M:%S')" "$lvl" "$msg" >>"$LOG_FILE"
    [[ "$lvl" == "DEBUG" && "$VERBOSE" != true ]] && return 0
    [[ "$QUIET" == true && "$lvl" != "ERR" ]] && return 0
    printf '   %s%s %s%s\n' "$color" "$icon" "$msg" "$NC"
}

die() { log ERR "$*"; exit 1; }

# ------------------------------------------------------------------------------
# 4) LOW-LEVEL HELPERS
# ------------------------------------------------------------------------------
have() { command -v "$1" >/dev/null 2>&1; }

# Every adb invocation closes fd 9, the lock descriptor. An adb server that
# gets daemonized while fd 9 is inherited would hold the lock for as long as it
# lives and block every later command with "another operation is running".
run_to() { # run_to <seconds> <cmd...>  (portable timeout)
    local s="$1"; shift
    if   have timeout;  then timeout --foreground "$s" "$@" 9>&-
    elif have gtimeout; then gtimeout --foreground "$s" "$@" 9>&-
    else "$@" 9>&-; fi
}

adbc() { command adb "$@" 9>&-; }

adbq() { run_to "$SHELL_TIMEOUT" adb "$@" 2>>"$LOG_FILE"; }

port_busy() { # host-side only
    local p="$1"
    if   have ss;      then ss -Htuln 2>/dev/null    | grep -qE "[:.]${p}([[:space:]]|$)"
    elif have netstat; then netstat -tuln 2>/dev/null | grep -qE "[:.]${p}([[:space:]]|$)"
    else return 1; fi
}

next_free_port() {
    local p="$1"
    while port_busy "$p" && (( p < 5700 )); do ((p++)); done
    printf '%s' "$p"
}

reachable_host() { # network-level pre-check (avoids long adb hangs)
    local ip="$1"
    have ping || return 0
    run_to 3 ping -c1 -W1 "$ip" >/dev/null 2>&1
}

mdns_entries() { # "serial<TAB>ip:port" for each discovered paired device
    run_to 8 adb mdns services 2>/dev/null | awk '
        /_adb-tls-connect/ && !seen[$NF]++ {
            name=$1; s=""
            if (match(name, /adb-[A-Za-z0-9]+-/))
                s=toupper(substr(name, RSTART+4, RLENGTH-5))
            print s "\t" $NF
        }'
}

# ------------------------------------------------------------------------------
# 5) DEVICE LAYER
# ------------------------------------------------------------------------------
usb_serials() {
    adbc devices 2>>"$LOG_FILE" | awk 'NR>1 && NF>=2 && $1 !~ /:|_adb-tls-/ {print $1"\t"$2}'
}

resolve_label() { # target fallback -> the device's real model when it answers
    local target="$1" fallback="${2:-}" model
    model=$(adbq -s "$target" shell getprop ro.product.model 2>/dev/null \
        | tr -d '\r\n' | tr '_' ' ')
    if [[ -n "$model" ]]; then printf '%s' "$model"
    else printf '%s' "$fallback"; fi
}

device_label() { # model / product for nicer output
    local s="$1" label
    label=$(adbc devices -l 2>>"$LOG_FILE" | awk -v s="$s" '$1==s{for(i=1;i<=NF;i++) if($i ~ /^model:/){sub("model:","",$i); print $i; exit}}')
    printf '%s' "${label:-unknown}"
}

state_of() { # exact adb state string for a serial/target
    adbc devices 2>>"$LOG_FILE" | awk -v s="$1" '$1==s{print $2; exit}'
}

is_ready() { # adb lists the target as usable (says nothing about responsiveness)
    [[ "$(state_of "$1")" == "device" ]]
}

is_live() { # listed as usable AND actually answering commands
    is_ready "$1" && alive "$1"
}

alive() { # real command round-trip, not just list presence
    [[ "$(adbq -s "$1" shell echo ok 2>/dev/null | tr -d '\r\n')" == "ok" ]]
}

device_sdk() { # target -> API level, empty when unknown
    adbq -s "$1" shell getprop ro.build.version.sdk 2>/dev/null \
        | tr -dc '0-9'
}

wireless_debugging_on() { # target -> 0 when the Android 11+ toggle is on
    [[ "$(adbq -s "$1" shell settings get global adb_wifi_enabled \
        2>/dev/null | tr -d '\r\n')" == "1" ]]
}

enable_wireless_debugging() { # target -> 0 only when the toggle really went on
    # Best effort, and the result is read back rather than assumed. `settings
    # put` exits 0 even when the framework discards the write, and several
    # vendors (Samsung among them) do exactly that: the toggle is meant to be
    # user-controlled and Android offers no sanctioned adb command for it.
    adbq -s "$1" shell settings put global adb_wifi_enabled 1 >/dev/null 2>&1
    sleep 2
    wireless_debugging_on "$1"
}

restore_wireless_debugging() { # target was_on
    [[ "${2:-false}" == true ]] || return 0
    if enable_wireless_debugging "$1"; then
        log OK "$(t wifi_restored)"
    else
        log WARN "$(t wifi_not_restored)"
    fi
}

mdns_target_for_serial() { # serial -> ip:port advertised for that serial
    local want
    want=$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')
    [[ -z "$want" ]] && return 0
    mdns_entries | awk -F'\t' -v s="$want" '$1==s {print $2; exit}'
}

wait_for_mdns_target() { # serial [seconds] -> ip:port once advertised
    local serial="$1" limit="${2:-$MDNS_WAIT}" waited=0 hit
    while (( waited < limit )); do
        hit=$(mdns_target_for_serial "$serial")
        [[ -n "$hit" ]] && { printf '%s' "$hit"; return 0; }
        sleep 2; waited=$(( waited + 2 ))
    done
    return 1
}

device_ips() { # ordered: preferred interfaces first
    local serial="$1" raw pref out=""
    raw=$(adbq -s "$serial" shell ip -o -4 addr show scope global 2>/dev/null | tr -d '\r')
    [[ -z "$raw" ]] && return 0
    IFS=',' read -ra pref <<< "$PREFER_IFACES"
    local p line ip
    for p in "${pref[@]}"; do
        while IFS= read -r line; do
            [[ "$line" == *"$p"* ]] || continue
            ip=$(awk '{print $4}' <<< "$line" | cut -d/ -f1)
            [[ -n "$ip" && "$out" != *"$ip"* ]] && out+="$ip"$'\n'
        done <<< "$raw"
    done
    while IFS= read -r line; do            # fallback: anything else, skip mobile data
        [[ "$line" == *rmnet* || "$line" == *"ccmni"* || "$line" == *"tun"* ]] && continue
        ip=$(awk '{print $4}' <<< "$line" | cut -d/ -f1)
        [[ -n "$ip" && "$out" != *"$ip"* ]] && out+="$ip"$'\n'
    done <<< "$raw"
    printf '%s' "$out"
}

# ------------------------------------------------------------------------------
# 6) CACHE (remember devices -> reconnect later without a cable)
# ------------------------------------------------------------------------------
cache_put() { # serial ip port label
    # One row per device. Matching is case-insensitive on the serial and also
    # drops any row holding the same address, so a phone whose wireless port
    # rotated replaces its old row instead of adding a duplicate.
    local serial="$1" ip="$2" port="$3" label="$4" tmp
    serial=$(printf '%s' "$serial" | tr '[:lower:]' '[:upper:]')
    mkdir -p "$XDG_DATA"; touch "$CACHE_FILE"
    tmp=$(mktemp) || return 0
    awk -F'\t' -v s="$serial" -v h="$ip" '
        toupper($1) != s && $2 != h' "$CACHE_FILE" >"$tmp" 2>/dev/null
    printf '%s\t%s\t%s\t%s\t%s\n' "$serial" "$ip" "$port" "$label" "$(date +%s)" >>"$tmp"
    mv -f "$tmp" "$CACHE_FILE"
}

cache_rows() { [[ -s "$CACHE_FILE" ]] && cat "$CACHE_FILE"; }

# ------------------------------------------------------------------------------
# 6b) SUSPEND LIST (devices the user disconnected on purpose stay offline)
#     Columns: serial<TAB>timestamp
# ------------------------------------------------------------------------------
suspended_serials() {
    [[ -r "$SUSPENDED_FILE" ]] && cut -f1 "$SUSPENDED_FILE" | grep -v '^$'
    return 0
}

is_suspended() { # case-insensitive: cache and adb disagree on serial casing
    local want
    want=$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')
    [[ -z "$want" ]] && return 1
    suspended_serials | tr '[:lower:]' '[:upper:]' | grep -qxFe "$want"
}

suspend_add() { # serial
    local serial="$1"
    [[ -z "$serial" ]] && return 0
    mkdir -p "$XDG_DATA"; touch "$SUSPENDED_FILE"
    awk -F'\t' 'tolower($1)!=tolower(s)' s="$serial" \
        "$SUSPENDED_FILE" >"$SUSPENDED_FILE.tmp" 2>/dev/null || true
    mv -f "$SUSPENDED_FILE.tmp" "$SUSPENDED_FILE"
    printf '%s\t%s\n' "$serial" "$(date +%s)" >>"$SUSPENDED_FILE"
}

suspend_del() { # serial
    local serial="$1"
    [[ -r "$SUSPENDED_FILE" ]] || return 0
    awk -F'\t' 'tolower($1)!=tolower(s)' s="$serial" \
        "$SUSPENDED_FILE" >"$SUSPENDED_FILE.tmp" 2>/dev/null || true
    mv -f "$SUSPENDED_FILE.tmp" "$SUSPENDED_FILE"
}

# ------------------------------------------------------------------------------
# 7) CONNECTION LOGIC
# ------------------------------------------------------------------------------
heal_transport() { # ip port
    # Clears a half-open transport on the HOST only. `adb usb` / `adb tcpip`
    # restart adbd on the phone, which on Android 11+ tears down the
    # Wireless-debugging session and leaves the toggle switched off, so
    # neither belongs in a healing path.
    local ip="$1" port="$2"
    log WARN "$(t reset)"
    adbc disconnect "$ip:$port" >>"$LOG_FILE" 2>&1
    run_to 8 adb reconnect offline >>"$LOG_FILE" 2>&1
}

is_hard_connect_failure() { # adb connect output -> 0 when retrying is pointless
    local out
    out=$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')
    [[ "$out" == *refused* || "$out" == *"no route to host"* \
       || "$out" == *"network is unreachable"* \
       || "$out" == *"host is unreachable"* \
       || "$out" == *"unknown host"* ]]
}

serial_of_target() { # target -> hardware serial (uppercase) or empty
    adbq -s "$1" shell getprop ro.serialno 2>/dev/null \
        | tr -d '\r\n' | tr '[:lower:]' '[:upper:]'
}

connect_target() { # expected_serial ip port -> 0 on verified success
    local want ip="$2" port="$3" attempt out got tries="$MAX_RETRIES"
    want=$(printf '%s' "${1:-}" | tr '[:lower:]' '[:upper:]')
    local target="$ip:$port"
    if ! reachable_host "$ip"; then
        # ICMP may simply be filtered, so this is not fatal - but spending the
        # full retry budget on a host that does not answer at all is what makes
        # a healing pass drag on for a minute.
        log WARN "$(t unreachable "$ip")"
        tries=1
    fi
    for ((attempt = 1; attempt <= tries; attempt++)); do
        log INFO "$(t try "$attempt" "$tries" "$target")"
        out=$(run_to "$CONNECT_TIMEOUT" adb connect "$target" 2>&1)
        log DEBUG "adb connect: ${out//$'\n'/ }"
        sleep 1
        case "$(state_of "$target")" in
            device)
                if alive "$target"; then
                    got=$(serial_of_target "$target")
                    # identity check: refuse a phone that merely inherited
                    # this address from the one we were looking for
                    if [[ -n "$want" && -n "$got" && "$want" != "$got" ]]; then
                        adbc disconnect "$target" >/dev/null 2>&1
                        log ERR "$(t wrong_dev "$target" "$got")"
                        return 1
                    fi
                    log OK "$(t ok "$target")"
                    return 0
                fi
                log WARN "$(t zombie)"
                heal_transport "$ip" "$port"
                ;;
            offline)
                log WARN "$(t dev_offline)"
                heal_transport "$ip" "$port"
                ;;
            *)
                adbc disconnect "$target" >/dev/null 2>&1
                # Retrying a refused or unroutable address cannot help: a
                # stale mDNS advert or a wireless toggle that went off needs
                # the NEXT candidate address, not another attempt at this one.
                if is_hard_connect_failure "$out"; then
                    log WARN "$(t refused "$target")"
                    return 1
                fi
                if [[ -z "${out// }" ]]; then
                    # killed by our own timeout before printing: that is what
                    # an unroutable address looks like from here
                    log WARN "$(t no_answer "$target")"
                    return 1
                fi
                (( attempt < tries )) && sleep $(( attempt ))  # linear backoff
                ;;
        esac
    done
    return 1
}

launch_scrcpy() { # target label
    [[ "$AUTO_SCRCPY" == true ]] || return 0
    have scrcpy || return 0
    local target="$1" label="$2"
    if pgrep -f -- "scrcpy .*-s $target" >/dev/null 2>&1; then
        log INFO "$(t scrcpy_run)"; return 0
    fi
    log INFO "$(t scrcpy_go)"
    # shellcheck disable=SC2086
    nohup scrcpy -s "$target" --window-title "$label ($target)" $SCRCPY_ARGS \
        >>"$LOG_FILE" 2>&1 9>&- & disown
}

# ------------------------------------------------------------------------------
# 8) ACTIONS
# ------------------------------------------------------------------------------
wireless_via_mdns() { # usb_serial ident label -> 0 when connected
    # Preferred path on Android 11+: the phone already advertises a TLS
    # wireless-debugging port, so we connect to it and never restart adbd.
    local usb="$1" ident="$2" label="$3" target
    target=$(mdns_target_for_serial "$ident")
    if [[ -z "$target" ]]; then
        if ! wireless_debugging_on "$usb"; then
            log INFO "$(t wireless_on)"
            if ! enable_wireless_debugging "$usb"; then
                # The vendor discarded the write; the user has to flip the
                # toggle by hand. Say so instead of degrading silently.
                log WARN "$(t wireless_denied)"
                return 1
            fi
        fi
        log INFO "$(t await_mdns)"
        target=$(wait_for_mdns_target "$ident") || return 1
    fi
    if connect_target "$ident" "${target%:*}" "${target##*:}"; then
        log OK "$(t via_mdns "$target")"
        cache_put "$ident" "${target%:*}" "${target##*:}" "$label"
        suspend_del "$ident"
        launch_scrcpy "$target" "$label"
        return 0
    fi
    return 1
}

wireless_via_tcpip() { # usb_serial ident label port -> 0 when connected
    # Last resort. `adb tcpip` restarts adbd, which is what switches the
    # Android 11+ wireless-debugging toggle off, so the toggle is restored
    # afterwards if the user had it on.
    local usb="$1" ident="$2" label="$3" port="$4" ips ip out
    local wifi_was_on=false
    wireless_debugging_on "$usb" && wifi_was_on=true
    log WARN "$(t legacy)"
    log INFO "$(t port "$port")"
    log INFO "$(t tcpip)"
    if ! out=$(run_to "$CONNECT_TIMEOUT" adb -s "$usb" tcpip "$port" 2>&1); then
        log WARN "$(t tcpip_fail "${out//$'\n'/ }")"
    fi
    log DEBUG "tcpip: ${out//$'\n'/ }"
    sleep "$TCPIP_SETTLE"

    ips=$(device_ips "$usb")
    if [[ -z "$ips" ]]; then
        log ERR "$(t no_ip)"
        restore_wireless_debugging "$usb" "$wifi_was_on"
        return 1
    fi
    while IFS= read -r ip; do
        [[ -z "$ip" ]] && continue
        if connect_target "$ident" "$ip" "$port"; then
            cache_put "$ident" "$ip" "$port" "$label"
            suspend_del "$ident"
            launch_scrcpy "$ip:$port" "$label"
            restore_wireless_debugging "$usb" "$wifi_was_on"
            return 0
        fi
    done <<< "$ips"
    restore_wireless_debugging "$usb" "$wifi_was_on"
    return 1
}

action_connect() { # main flow over USB devices
    local rows serial state label port ident sdk ok done_n=0
    rows=$(usb_serials)
    [[ -z "$rows" ]] && { log WARN "$(t no_usb)"; return 1; }

    local next_port; next_port=$(next_free_port "$START_PORT")

    while IFS=$'\t' read -r serial state; do
        [[ -z "$serial" ]] && continue
        label=$(device_label "$serial")
        printf '\n%s%s%s\n' "$BOLD" "$(t dev "$serial" "[$label]")" "$NC"

        if [[ "$state" == "unauthorized" ]]; then log ERR "$(t unauth)"; continue; fi
        if [[ "$state" != "device" ]]; then log ERR "$(t offline_usb "$state")"; continue; fi

        ident=$(serial_of_target "$serial")
        [[ -z "$ident" ]] && ident=$(printf '%s' "$serial" | tr '[:lower:]' '[:upper:]')
        sdk=$(device_sdk "$serial")

        ok=false
        if [[ -n "$sdk" ]] && (( sdk >= WIRELESS_SDK )); then
            wireless_via_mdns "$serial" "$ident" "$label" && ok=true
        fi
        if [[ "$ok" != true ]]; then
            port="$next_port"; next_port=$(next_free_port $((port + 1)))
            wireless_via_tcpip "$serial" "$ident" "$label" "$port" && ok=true
        fi

        if [[ "$ok" == true ]]; then
            ((done_n++))
            log INFO "$(t unplug)"
        else
            log ERR "$(t failed_dev "$LOG_FILE")"
        fi
    done <<< "$rows"

    (( done_n > 0 )) && return 0 || return 1
}

known_devices() {
    # One line per device: serial<TAB>target<TAB>label
    # A fresh mDNS advert wins over the saved port, because Android rotates
    # the wireless-debugging port on every reboot and Wi-Fi toggle. Merging
    # on the serial here is what keeps the same phone from being visited
    # twice in one pass.
    { mdns_entries | awk -F'\t' '$1!="" {print $1"\t"$2"\tmDNS\t1"}'
      cache_rows  | awk -F'\t' 'NF>=4 {print toupper($1)"\t"$2":"$3"\t"$4"\t2"}'
    } | sort -t$'\t' -k1,1 -k4,4n \
      | awk -F'\t' '{ key = ($1 != "" ? $1 : $2) }
                    !seen[key]++ { print $1"\t"$2"\t"$3 }'
}

candidate_targets() { # serial target -> one address per line, best first
    # A fresh mDNS advert beats a saved port, because Android rotates the
    # wireless-debugging port on every reboot and Wi-Fi toggle.
    #
    # An mDNS advert also outlives the service it describes, so a refused
    # advert does not mean the phone is gone: a device taken wireless through
    # the legacy path still listens on the fixed port. Every host we know
    # about therefore gets the fixed port tried too before we give up.
    local serial="$1" target="$2" fresh cand host
    fresh=$(mdns_target_for_serial "$serial")
    { for cand in "$fresh" "$target"; do
          [[ -n "$cand" ]] && printf '%s\n' "$cand"
      done
      for cand in "$fresh" "$target"; do
          host="${cand%:*}"
          [[ -n "$host" ]] && printf '%s:%s\n' "$host" "$START_PORT"
      done
    } | awk 'NF && !seen[$0]++'
}

attach_known() { # serial target label -> 0 when connected and verified
    local serial="$1" target="$2" label="$3" cand
    if is_live "$target"; then
        log OK "$(t already "$target [$label]")"
        suspend_del "$serial"
        return 0
    fi
    while IFS= read -r cand; do
        [[ -z "$cand" ]] && continue
        log INFO "$(t recon "$cand" "$label")"
        if connect_target "$serial" "${cand%:*}" "${cand##*:}"; then
            suspend_del "$serial"
            # Now that the device answers, store its real model instead of
            # whatever placeholder the discovery source supplied.
            label=$(resolve_label "$cand" "$label")
            cache_put "$serial" "${cand%:*}" "${cand##*:}" "$label"
            launch_scrcpy "$cand" "$label"
            return 0
        fi
    done <<< "$(candidate_targets "$serial" "$target")"
    return 1
}

action_reconnect() { # explicit user action: overrides and clears suspensions
    local serial target label rows ok=false
    rows=$(known_devices)
    [[ -z "$rows" ]] && { log WARN "$(t cache_empty)"; return 1; }
    while IFS=$'\t' read -r serial target label; do
        [[ -z "${target:-}" ]] && continue
        attach_known "$serial" "$target" "$label" && ok=true
    done <<< "$rows"
    [[ "$ok" == true ]]
}

action_list() {
    printf '\n%s%s%s\n' "$BOLD" "$(t summary)" "$NC"
    local out
    out=$(adbc devices -l 2>/dev/null | awk 'NR>1 && NF>=2 {print}')
    [[ -z "$out" ]] && { log INFO "$(t none_conn)"; return 0; }
    printf '%s\n' "$out" | sed 's/^/   /'
}

action_disconnect() { # suspends affected devices until the user reconnects
    # Host-side only. The phone keeps its Wireless-debugging toggle exactly
    # as the user left it: no `adb usb`, no `adb tcpip`, no adbd restart.
    local what="${1:-all}" target serial
    if [[ "$what" == "all" ]]; then
        while IFS= read -r target; do
            [[ -z "$target" ]] && continue
            serial=$(serial_of_target "$target")
            [[ -n "$serial" ]] && suspend_add "$serial"
        done < <(adbc devices 2>/dev/null \
            | awk 'NR>1 && $2=="device" && $1 ~ /:/ {print $1}')
        adbc disconnect >/dev/null 2>&1
        log OK "$(t disc "all")"
    else
        serial=$(serial_of_target "$what")
        [[ -n "$serial" ]] && suspend_add "$serial"
        adbc disconnect "$what" >/dev/null 2>&1
        log OK "$(t disc "$what")"
    fi
}

action_pair() { # Android 11+ wireless debugging
    local target="${1:-}" code="${2:-}" svc
    if [[ -z "$target" ]]; then
        log INFO "$(t pair_scan)"
        svc=$(run_to 8 adb mdns services 2>/dev/null | awk '/_adb-tls-pairing/ {print $NF; exit}')
        [[ -z "$svc" ]] && { log ERR "$(t pair_none)"; return 1; }
        target="$svc"
        log OK "$(t pair_found "$target")"
    fi
    if [[ -z "$code" ]]; then
        printf '   %s' "$(t pair_ask)"; read -r code
    fi
    [[ "$code" =~ ^[0-9]{6}$ ]] || { log ERR "$(t pair_fail)"; return 1; }
    if run_to 25 adb pair "$target" "$code" >>"$LOG_FILE" 2>&1; then
        log OK "$(t pair_ok "$target")"
        # The connect service runs on a different port than the pairing one
        # and is published a moment later, so give the advert time to appear.
        local conn waited=0 ident label
        log INFO "$(t await_mdns)"
        while (( waited < MDNS_WAIT )); do
            conn=$(mdns_entries | awk -F'\t' 'NR==1 {print $2}')
            [[ -n "${conn:-}" ]] && break
            sleep 2; waited=$(( waited + 2 ))
        done
        [[ -z "${conn:-}" ]] && { log ERR "$(t pair_none)"; return 1; }
        if connect_target "" "${conn%:*}" "${conn##*:}"; then
            ident=$(serial_of_target "$conn")
            label=$(device_label "$conn")
            [[ -n "$ident" ]] && cache_put "$ident" "${conn%:*}" \
                "${conn##*:}" "$label"
            launch_scrcpy "$conn" "$label"
            return 0
        fi
        return 1
    fi
    log ERR "$(t pair_fail)"; return 1
}

heal_one() { # serial target label - restore one dropped link, quietly
    local serial="$1" target="$2" label="$3" cand
    is_live "$target" && return 0
    log WARN "$(t watch_lost "$target")"
    # scrcpy stays on-demand here: popping a mirror window out of a background
    # loop is intrusive.
    while IFS= read -r cand; do
        [[ -z "$cand" ]] && continue
        if connect_target "$serial" "${cand%:*}" "${cand##*:}"; then
            cache_put "$serial" "${cand%:*}" "${cand##*:}" \
                "$(resolve_label "$cand" "$label")"
            return 0
        fi
    done <<< "$(candidate_targets "$serial" "$target")"
    return 1
}

watch_pass() { # one healing sweep; NEVER touches user-suspended devices
    local serial target label
    while IFS=$'\t' read -r serial target label; do
        [[ -z "${target:-}" ]] && continue
        is_suspended "$serial" && continue
        # The lock is taken per device, not per pass. A manual command then
        # waits for one device's worth of work at most, instead of for a whole
        # sweep that can outlast the poll interval.
        ( flock -w "$LOCK_WAIT" 9 || exit 0
          heal_one "$serial" "$target" "$label" ) 9>"$LOCK_FILE"
    done <<< "$(known_devices)"
}

action_watch() {
    log INFO "$(t watch "$WATCH_INTERVAL")"
    while true; do
        watch_pass
        sleep "$WATCH_INTERVAL"
    done
}

action_doctor() {
    printf '\n%s%s%s\n' "$BOLD" "$(t doctor)" "$NC"
    local tool
    for tool in adb scrcpy ss ping timeout jq; do
        if have "$tool"; then log OK "$tool -> $(command -v "$tool")"
        else log WARN "$tool -> missing"; fi
    done
    log INFO "adb: $(adbc version 2>/dev/null | head -1)"
    log INFO "mdns : $(run_to 6 adb mdns check 2>&1 | tr -d '\r' | head -1)"
    log INFO "config: $CONFIG_FILE $( [[ -r $CONFIG_FILE ]] && echo '(loaded)' || echo '(defaults)')"
    log INFO "cache : $CACHE_FILE ($(cache_rows | grep -c . || true) devices)"
    log INFO "known : $(known_devices | grep -c . || true) unique devices"
    log INFO "log   : $LOG_FILE"
    log INFO "lan   : $(ip -o -4 addr show scope global 2>/dev/null | awk '{printf "%s(%s) ", $2, $4}')"
    action_list
}

usage() {
cat <<EOF
${BOLD}ADB Wireless Manager v$VERSION${NC}

Usage: $SCRIPT_NAME [command] [options]

Commands:
  connect            (default) take USB-attached devices wireless and verify
  reconnect, r       reconnect known devices without a cable (mDNS + cache)
  pair [host:port] [code]
                     Android 11+ wireless pairing (auto mDNS discovery)
  watch, w           keep known devices connected (healing loop)
  list, l            show current adb devices
  disconnect [t|all] drop wireless links on this machine
  doctor             environment + config diagnostics
  help               this screen

Options:
  -p, --port N       start port for the tcpip fallback (default $START_PORT)
  -r, --retries N    attempts per address (default $MAX_RETRIES)
  -s, --scrcpy       force scrcpy launch
  -S, --no-scrcpy    disable scrcpy
  -v, --verbose      debug output
  -q, --quiet        errors only
      --lang ar|en   force language
      --version

On Android 11 and newer, connect uses the phone's wireless-debugging service
discovered over mDNS. The classic 'adb tcpip' path is only used when that is
unavailable, because it restarts adbd and switches the phone's
Wireless-debugging toggle off.

Config file: $CONFIG_FILE  (KEY=VALUE, e.g. AUTO_SCRCPY=false)
Log file   : $LOG_FILE
EOF
}

# ------------------------------------------------------------------------------
# 9) ARG PARSING
# ------------------------------------------------------------------------------
CMD=""
ARGS=()
while (($#)); do
    case "$1" in
        connect|reconnect|r|pair|watch|w|list|l|disconnect|doctor|help)
            [[ -z "$CMD" ]] && CMD="$1" || ARGS+=("$1") ;;
        -p|--port)      START_PORT="${2:?}"; shift ;;
        -r|--retries)   MAX_RETRIES="${2:?}"; shift ;;
        -s|--scrcpy)    AUTO_SCRCPY=true ;;
        -S|--no-scrcpy) AUTO_SCRCPY=false ;;
        -v|--verbose)   VERBOSE=true ;;
        -q|--quiet)     QUIET=true ;;
        --lang)         LANG_CODE="${2:?}"; shift ;;
        --version)      printf '%s\n' "$VERSION"; exit 0 ;;
        -h|--help)      CMD="help" ;;
        *)              ARGS+=("$1") ;;
    esac
    shift
done
CMD="${CMD:-connect}"
[[ "$CMD" == "help" ]] && { usage; exit 0; }

# ------------------------------------------------------------------------------
# 10) BOOTSTRAP
# ------------------------------------------------------------------------------
init_log "$CMD ${ARGS[*]:-}"
have adb || die "$(t need adb)"

# seed a documented config template once (never overwrite user edits)
if [[ ! -e "$CONFIG_FILE" ]]; then
    cat >"$CONFIG_FILE" <<'TPL' 2>/dev/null
# adbconnect config - single source of truth. Uncomment to override.
#START_PORT=5555
#MAX_RETRIES=3
#CONNECT_TIMEOUT=6
#SHELL_TIMEOUT=4
#TCPIP_SETTLE=3
#AUTO_SCRCPY=true
#SCRCPY_ARGS="--video-bit-rate 8M --max-fps 60 --turn-screen-off"
#WATCH_INTERVAL=20
#PREFER_IFACES="wlan,wifi,eth,ap"
#LANG_CODE=auto      # ar | en | auto
#LOG_FILE=/tmp/adbconnect.log
TPL
fi

# Mutual exclusion between connecting commands. The watch loop takes the lock
# per pass (see action_watch) rather than for its whole lifetime, so a manual
# `connect` or `reconnect` only ever waits for the current pass to finish
# instead of being refused outright.
case "$CMD" in
    list|l|disconnect|doctor|help|watch|w) LOCK_REQUIRED=false ;;
    *)                                     LOCK_REQUIRED=true ;;
esac

# start adb server BEFORE taking the lock (a daemonized server would otherwise
# inherit fd 9 and hold the lock forever)
adbc start-server >>"$LOG_FILE" 2>&1

if $LOCK_REQUIRED; then
    exec 9>"$LOCK_FILE"
    if ! flock -w "$LOCK_WAIT" 9 2>/dev/null; then
        die "$(t locked "$LOCK_WAIT")"
    fi
fi

# shellcheck disable=SC2317
cleanup() { printf '%s' "$NC"; }
trap cleanup EXIT
trap 'printf "\n"; log WARN "interrupted"; exit 130' INT TERM

if [[ "$QUIET" != true ]]; then
    printf '%s==============================================%s\n' "$BLUE" "$NC"
    printf '%s  %s  v%s%s\n' "$BOLD" "$(t banner)" "$VERSION" "$NC"
    printf '%s==============================================%s\n' "$BLUE" "$NC"
fi

RC=0
case "$CMD" in
    connect)      action_connect || RC=$?; action_list ;;
    reconnect|r)  action_reconnect || RC=$?; action_list ;;
    pair)         action_pair "${ARGS[0]:-}" "${ARGS[1]:-}" || RC=$? ;;
    watch|w)      action_watch ;;
    list|l)       action_list ;;
    disconnect)   action_disconnect "${ARGS[0]:-all}" ;;
    doctor)       action_doctor ;;
    *)            usage; RC=1 ;;
esac

[[ "$QUIET" != true ]] && printf '\n%s%s%s\n' "$DIM" "$(t bye)" "$NC"
exit "$RC"
