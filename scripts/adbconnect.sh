#!/usr/bin/env bash
# ==============================================================================
#  Auto ADB WiFi Connector  v11.0
#  Wireless ADB manager: connect / reconnect / pair / watch / doctor
#  Supports classic tcpip (Android <11) and Wireless Debugging mDNS (Android 11+)
#  Requires: bash 4+, adb.  Optional: scrcpy, ss|netstat, ping, jq
#  Repo    : https://github.com/ALSRKAL/adb-wireless-manager
# ==============================================================================
set -uo pipefail
IFS=$'\n\t'

readonly VERSION="13.0"
readonly SCRIPT_NAME="${0##*/}"

# ------------------------------------------------------------------------------
# 1) CENTRAL CONFIG  (single source of truth — override via config file or env)
#    Config file: ~/.config/adbconnect/config   (plain KEY=VALUE bash)
# ------------------------------------------------------------------------------
readonly XDG_CONF="${XDG_CONFIG_HOME:-$HOME/.config}/adbconnect"
readonly XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/adbconnect"
readonly CONFIG_FILE="$XDG_CONF/config"
readonly SETTINGS_FILE="$XDG_CONF/settings.json"
readonly CACHE_FILE="$XDG_DATA/devices.tsv"
readonly SUSPENDED_FILE="$XDG_DATA/suspended.tsv"
_LOCK_FILE="/tmp/adbconnect.$(id -u).lock"
readonly LOCK_FILE="$_LOCK_FILE"

START_PORT="${ADBC_START_PORT:-5555}"          # first port to try
MAX_RETRIES="${ADBC_MAX_RETRIES:-3}"           # connect attempts per IP
CONNECT_TIMEOUT="${ADBC_CONNECT_TIMEOUT:-6}"   # seconds per adb connect
SHELL_TIMEOUT="${ADBC_SHELL_TIMEOUT:-4}"       # seconds per adb shell probe
TCPIP_SETTLE="${ADBC_TCPIP_SETTLE:-3}"         # sleep after `adb tcpip`
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
  [locked]="نسخة أخرى تعمل بالخلفية. لتوصيل جهاز جديد نفّذ: systemctl --user stop adbconnect-watch ثم أعد المحاولة."
  [no_usb]="لا توجد أجهزة USB متصلة (أو أن التخويل مرفوض)."
  [dev]="الجهاز: %s %s"
  [unauth]="غير مصرَّح (unauthorized) — اقبل نافذة التخويل على شاشة الهاتف."
  [offline_usb]="حالة الجهاز غير جاهزة: %s"
  [port]="المنفذ المخصص: %s"
  [tcpip]="تفعيل وضع TCP/IP..."
  [tcpip_fail]="فشل أمر tcpip: %s"
  [no_ip]="لا يوجد عنوان IP على شبكة WiFi. تحقق من اتصال الهاتف."
  [try]="محاولة %s/%s ← %s"
  [unreachable]="العنوان %s لا يستجيب للشبكة (ping) — تأكد أن الجهازين على نفس الشبكة."
  [ok]="تم الاتصال والتحقق: %s"
  [zombie]="اتصال وهمي (لا يستجيب للأوامر) — إصلاح عميق..."
  [dev_offline]="الجهاز offline — إصلاح عميق..."
  [reset]="إعادة تهيئة الاتصال (Hard Reset)..."
  [failed_dev]="فشلت كل المحاولات لهذا الجهاز. راجع السجل: %s"
  [unplug]="يمكنك سحب كابل USB الآن — الاتصال اللاسلكي شغّال."
  [scrcpy_run]="Scrcpy يعمل بالفعل لهذا الجهاز."
  [scrcpy_go]="تشغيل Scrcpy..."
  [summary]="الخلاصة"
  [none_conn]="لا توجد اتصالات لاسلكية حالياً."
  [cache_empty]="لا توجد أجهزة محفوظة. وصّل الجهاز بـ USB ونفّذ السكربت مرة واحدة."
  [recon]="إعادة اتصال بالمحفوظ: %s (%s)"
  [mdns]="جهاز مقترن ظهر عبر mDNS: %s — جارٍ الاتصال..."
  [already]="متصل مسبقاً: %s"
  [disc]="تم فصل: %s"
  [pair_scan]="جارٍ البحث عن أجهزة الاقتران (mDNS)..."
  [pair_none]="لم يتم العثور على جهاز في وضع الاقتران. الهاتف: إعدادات المطوّر ← تصحيح لاسلكي ← الإقران برمز."
  [pair_found]="تم العثور على: %s"
  [pair_ask]="أدخل رمز الاقتران المكوّن من 6 أرقام: "
  [pair_ok]="تم الاقتران بنجاح مع %s"
  [pair_fail]="فشل الاقتران. تأكد من الرمز وأن نافذة الاقتران ما زالت مفتوحة."
  [watch]="مراقبة مستمرة كل %s ثانية (Ctrl+C للإيقاف)..."
  [watch_lost]="انقطع %s — إعادة اتصال..."
  [doctor]="تشخيص البيئة"
  [bye]="تم."
)
declare -A T_EN=(
  [banner]="Wireless ADB Manager"
  [need]="Missing required tool: %s"
  [locked]="Another instance is running (auto-watch). For a new device: systemctl --user stop adbconnect-watch, then retry."
  [no_usb]="No USB devices detected (or authorization denied)."
  [dev]="Device: %s %s"
  [unauth]="Unauthorized — accept the RSA prompt on the phone screen."
  [offline_usb]="Device state not ready: %s"
  [port]="Assigned port: %s"
  [tcpip]="Enabling TCP/IP mode..."
  [tcpip_fail]="tcpip command failed: %s"
  [no_ip]="No WiFi IP address found. Check the phone's connection."
  [try]="Attempt %s/%s -> %s"
  [unreachable]="%s is not reachable (ping) — make sure both are on the same LAN."
  [ok]="Connected and verified: %s"
  [zombie]="Zombie connection (not responding) — hard reset..."
  [dev_offline]="Device offline — hard reset..."
  [reset]="Re-initializing connection (hard reset)..."
  [failed_dev]="All attempts failed for this device. See log: %s"
  [unplug]="You can unplug the USB cable now — the wireless link is live."
  [scrcpy_run]="Scrcpy already running for this device."
  [scrcpy_go]="Launching scrcpy..."
  [summary]="Summary"
  [none_conn]="No wireless connections right now."
  [cache_empty]="No saved devices. Plug in USB and run once."
  [recon]="Reconnecting saved device: %s (%s)"
  [mdns]="Paired device discovered via mDNS: %s — connecting..."
  [already]="Already connected: %s"
  [disc]="Disconnected: %s"
  [pair_scan]="Scanning for pairing devices (mDNS)..."
  [pair_none]="No device in pairing mode. On phone: Developer options -> Wireless debugging -> Pair with code."
  [pair_found]="Found: %s"
  [pair_ask]="Enter the 6-digit pairing code: "
  [pair_ok]="Paired successfully with %s"
  [pair_fail]="Pairing failed. Check the code and keep the pairing dialog open."
  [watch]="Watching every %ss (Ctrl+C to stop)..."
  [watch_lost]="%s dropped — reconnecting..."
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
    local msg="$*" color="$NC" icon=" "
    case "$lvl" in
        INFO)  color="$BLUE";   icon="•" ;;
        OK)    color="$GREEN";  icon="✔" ;;
        WARN)  color="$YELLOW"; icon="!" ;;
        ERR)   color="$RED";    icon="✘" ;;
        DEBUG) color="$DIM";    icon="·" ;;
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

run_to() { # run_to <seconds> <cmd...>  (portable timeout)
    local s="$1"; shift
    if   have timeout;  then timeout --foreground "$s" "$@"
    elif have gtimeout; then gtimeout --foreground "$s" "$@"
    else "$@"; fi
}

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
    adb devices 2>>"$LOG_FILE" | awk 'NR>1 && NF>=2 && $1 !~ /:|_adb-tls-/ {print $1"\t"$2}'
}

device_label() { # model / product for nicer output
    local s="$1" label
    label=$(adb devices -l 2>>"$LOG_FILE" | awk -v s="$s" '$1==s{for(i=1;i<=NF;i++) if($i ~ /^model:/){sub("model:","",$i); print $i; exit}}')
    printf '%s' "${label:-unknown}"
}

state_of() { # exact adb state string for a serial/target
    adb devices 2>>"$LOG_FILE" | awk -v s="$1" '$1==s{print $2; exit}'
}

is_ready() { [[ "$(state_of "$1")" == "device" ]]; }

alive() { # real command round-trip, not just list presence
    [[ "$(adbq -s "$1" shell echo ok 2>/dev/null | tr -d '\r\n')" == "ok" ]]
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
    local serial="$1" ip="$2" port="$3" label="$4" tmp
    mkdir -p "$XDG_DATA"; touch "$CACHE_FILE"
    tmp=$(mktemp) || return 0
    awk -F'\t' -v s="$serial" '$1!=s' "$CACHE_FILE" >"$tmp" 2>/dev/null
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

is_suspended() {
    suspended_serials | grep -qxFe "$1"
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
hard_reset() { # serial ip port
    local serial="$1" ip="$2" port="$3"
    log WARN "$(t reset)"
    adb disconnect "$ip:$port" >>"$LOG_FILE" 2>&1
    if [[ -n "$serial" ]] && is_ready "$serial"; then
        adbq -s "$serial" usb >/dev/null 2>&1; sleep 2
        adbq -s "$serial" tcpip "$port" >/dev/null 2>&1; sleep "$TCPIP_SETTLE"
    fi
}

connect_target() { # serial ip port  -> 0 on verified success
    local serial="$1" ip="$2" port="$3" attempt out
    local target="$ip:$port"
    if ! reachable_host "$ip"; then
        log WARN "$(t unreachable "$ip")"
    fi
    for ((attempt = 1; attempt <= MAX_RETRIES; attempt++)); do
        log INFO "$(t try "$attempt" "$MAX_RETRIES" "$target")"
        out=$(run_to "$CONNECT_TIMEOUT" adb connect "$target" 2>&1)
        log DEBUG "adb connect: ${out//$'\n'/ }"
        sleep 1
        case "$(state_of "$target")" in
            device)
                if alive "$target"; then
                    log OK "$(t ok "$target")"
                    return 0
                fi
                log WARN "$(t zombie)"
                hard_reset "$serial" "$ip" "$port"
                ;;
            offline)
                log WARN "$(t dev_offline)"
                hard_reset "$serial" "$ip" "$port"
                ;;
            *)
                adb disconnect "$target" >/dev/null 2>&1
                sleep $(( attempt ))     # linear backoff
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
action_connect() { # main flow over USB devices
    local rows serial state label port ips ip ok out total=0 done_n=0
    rows=$(usb_serials)
    [[ -z "$rows" ]] && { log WARN "$(t no_usb)"; return 1; }

    local next_port; next_port=$(next_free_port "$START_PORT")

    while IFS=$'\t' read -r serial state; do
        [[ -z "$serial" ]] && continue
        ((total++))
        label=$(device_label "$serial")
        printf '\n%s%s%s\n' "$BOLD" "$(t dev "$serial" "[$label]")" "$NC"

        if [[ "$state" == "unauthorized" ]]; then log ERR "$(t unauth)"; continue; fi
        if [[ "$state" != "device" ]]; then log ERR "$(t offline_usb "$state")"; continue; fi

        port="$next_port"; next_port=$(next_free_port $((port + 1)))
        log INFO "$(t port "$port")"

        log INFO "$(t tcpip)"
        if ! out=$(run_to "$CONNECT_TIMEOUT" adb -s "$serial" tcpip "$port" 2>&1); then
            log WARN "$(t tcpip_fail "${out//$'\n'/ }")"
        fi
        log DEBUG "tcpip: ${out//$'\n'/ }"
        sleep "$TCPIP_SETTLE"

        ips=$(device_ips "$serial")
        [[ -z "$ips" ]] && { log ERR "$(t no_ip)"; continue; }

        ok=false
        while IFS= read -r ip; do
            [[ -z "$ip" ]] && continue
            if connect_target "$serial" "$ip" "$port"; then
                ok=true
                cache_put "$serial" "$ip" "$port" "$label"
                launch_scrcpy "$ip:$port" "$label"
                break
            fi
        done <<< "$ips"

        if [[ "$ok" == true ]]; then
            ((done_n++))
            log INFO "$(t unplug)"
        else
            log ERR "$(t failed_dev "$LOG_FILE")"
        fi
    done <<< "$rows"

    (( done_n > 0 )) && return 0 || return 1
}

action_reconnect() { # explicit user action: overrides and clears suspensions
    local rows serial ip port label ts ok=false target t mserial
    rows=$(cache_rows)
    [[ -z "$rows" ]] && log WARN "$(t cache_empty)"
    while IFS=$'\t' read -r serial ip port label ts; do
        [[ -z "${ip:-}" ]] && continue
        target="$ip:$port"
        if [[ "$(state_of "$target")" == "device" ]] && alive "$target"; then
            log OK "$(t already "$target [$label]")"; ok=true
            suspend_del "$serial"
            continue
        fi
        log INFO "$(t recon "$target" "$label")"
        if connect_target "" "$ip" "$port"; then
            ok=true; launch_scrcpy "$target" "$label"
            suspend_del "$serial"
        fi
    done <<< "$rows"
    # catches paired devices whose wireless port changed after reboot / wifi toggle
    while IFS=$'\t' read -r mserial t; do
        [[ -z "$t" ]] && continue
        if [[ "$(state_of "$t")" == "device" ]]; then
            log OK "$(t already "$t")"; ok=true
            [[ -n "$mserial" ]] && suspend_del "$mserial"
            continue
        fi
        log INFO "$(t mdns "$t")"
        if connect_target "" "${t%:*}" "${t##*:}"; then
            ok=true; launch_scrcpy "$t" "$(device_label "$t")"
            [[ -n "$mserial" ]] && suspend_del "$mserial"
        fi
    done < <(mdns_entries)
    [[ "$ok" == true ]]
}

action_list() {
    printf '\n%s%s%s\n' "$BOLD" "$(t summary)" "$NC"
    local out
    out=$(adb devices -l 2>/dev/null | awk 'NR>1 && NF>=2 {print}')
    [[ -z "$out" ]] && { log INFO "$(t none_conn)"; return 0; }
    printf '%s\n' "$out" | sed 's/^/   /'
}

action_disconnect() { # suspends affected devices until the user reconnects
    local what="${1:-all}" target serial
    if [[ "$what" == "all" ]]; then
        while IFS= read -r target; do
            [[ -z "$target" ]] && continue
            serial=$(adbq -s "$target" shell getprop ro.serialno 2>/dev/null \
                | tr -d '\r\n')
            [[ -n "$serial" ]] && suspend_add "$serial"
        done < <(adb devices 2>/dev/null \
            | awk 'NR>1 && $2=="device" && $1 ~ /:/ {print $1}')
        adb disconnect >/dev/null 2>&1
        log OK "$(t disc "all")"
    else
        serial=$(adbq -s "$what" shell getprop ro.serialno 2>/dev/null \
            | tr -d '\r\n')
        [[ -n "$serial" ]] && suspend_add "$serial"
        adb disconnect "$what" >/dev/null 2>&1
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
        # after pairing, the connect service uses a different port; discover it
        local conn
        conn=$(run_to 8 adb mdns services 2>/dev/null | awk '/_adb-tls-connect/ {print $NF; exit}')
        [[ -n "$conn" ]] && connect_target "" "${conn%:*}" "${conn##*:}"
        return 0
    fi
    log ERR "$(t pair_fail)"; return 1
}

action_watch() { # automatic healing: NEVER touches user-suspended devices
    log INFO "$(t watch "$WATCH_INTERVAL")"
    local serial ip port label ts target t mserial
    while true; do
        # shellcheck disable=SC2034
        while IFS=$'\t' read -r serial ip port label ts; do
            [[ -z "${ip:-}" ]] && continue
            if is_suspended "$serial"; then continue; fi
            target="$ip:$port"
            if ! { [[ "$(state_of "$target")" == "device" ]] && alive "$target"; }; then
                log WARN "$(t watch_lost "$target")"
                connect_target "" "$ip" "$port" && launch_scrcpy "$target" "$label"
            fi
        done <<< "$(cache_rows)"
        # pick up paired devices whose port changed (reboot / wifi toggle)
        while IFS=$'\t' read -r mserial t; do
            [[ -z "$t" ]] && continue
            [[ -n "$mserial" ]] && is_suspended "$mserial" && continue
            [[ "$(state_of "$t")" == "device" ]] && continue
            log INFO "$(t mdns "$t")"
            connect_target "" "${t%:*}" "${t##*:}" \
                && launch_scrcpy "$t" "$(device_label "$t")"
        done < <(mdns_entries)
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
    log INFO "adb: $(adb version 2>/dev/null | head -1)"
    log INFO "mdns : $(run_to 6 adb mdns check 2>&1 | tr -d '\r' | head -1)"
    log INFO "config: $CONFIG_FILE $( [[ -r $CONFIG_FILE ]] && echo '(loaded)' || echo '(defaults)')"
    log INFO "cache : $CACHE_FILE ($(cache_rows | grep -c . || true) devices)"
    log INFO "log   : $LOG_FILE"
    log INFO "lan   : $(ip -o -4 addr show scope global 2>/dev/null | awk '{printf "%s(%s) ", $2, $4}')"
    action_list
}

usage() {
cat <<EOF
${BOLD}Auto ADB WiFi Connector v$VERSION${NC}

Usage: $SCRIPT_NAME [command] [options]

Commands:
  connect            (default) enable TCP/IP over USB, connect and verify
  reconnect, r       reconnect saved devices without a cable (cache + mDNS)
  pair [host:port] [code]
                     Android 11+ wireless debugging pairing (auto mDNS discovery)
  watch, w           keep saved devices connected (auto healing loop)
  list, l            show current adb devices
  disconnect [t|all] drop wireless connections
  doctor             environment + config diagnostics
  help               this screen

Options:
  -p, --port N       start port (default $START_PORT)
  -r, --retries N    attempts per IP (default $MAX_RETRIES)
  -s, --scrcpy       force scrcpy launch
  -S, --no-scrcpy    disable scrcpy
  -v, --verbose      debug output
  -q, --quiet        errors only
      --lang ar|en   force language
      --version

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
# adbconnect config — single source of truth. Uncomment to override.
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

# single instance guard: only for commands racing with the watch loop.
# read-only / independent commands (list, doctor, disconnect) run lock-free
case "$CMD" in
    list|l|disconnect|doctor|help) LOCK_REQUIRED=false ;;
    *)                             LOCK_REQUIRED=true ;;
esac

if $LOCK_REQUIRED; then
    # start adb server BEFORE taking the lock (daemonized server would
    # otherwise inherit fd 9 and hold the lock forever)
    adb start-server >>"$LOG_FILE" 2>&1
    exec 9>"$LOCK_FILE"
    if ! flock -n 9 2>/dev/null; then die "$(t locked)"; fi
fi

# shellcheck disable=SC2317
cleanup() { printf '%s' "$NC"; }
trap cleanup EXIT
trap 'printf "\n"; log WARN "interrupted"; exit 130' INT TERM

if [[ "$QUIET" != true ]]; then
    printf '%s══════════════════════════════════════════════%s\n' "$BLUE" "$NC"
    printf '%s  📡 %s  v%s%s\n' "$BOLD" "$(t banner)" "$VERSION" "$NC"
    printf '%s══════════════════════════════════════════════%s\n' "$BLUE" "$NC"
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
