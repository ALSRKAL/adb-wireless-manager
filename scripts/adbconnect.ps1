# ==============================================================================
#  ADB Wireless Manager - Windows CLI  v14.0.1
#  connect / reconnect / list / disconnect / pair / watch / doctor
#  Prefers Android 11+ wireless debugging over mDNS; the classic 'adb tcpip'
#  path is a fallback only, because it restarts adbd and switches the phone's
#  Wireless-debugging toggle off.
#  Requires: adb (platform-tools). Optional: scrcpy.
#  Repo    : https://github.com/ALSRKAL/adb-wireless-manager
# ==============================================================================
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('connect', 'reconnect', 'r', 'list', 'l', 'disconnect',
                 'pair', 'watch', 'doctor', 'help')]
    [string]$Command = 'connect',

    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$Args,

    [int]$Port = 5555,
    [int]$Retries = 3,
    [int]$WatchInterval = 20,
    [switch]$NoScrcpy
)

$ErrorActionPreference = 'SilentlyContinue'
$ptDir = Join-Path $env:LOCALAPPDATA 'AWM\platform-tools'
if (Test-Path $ptDir) { $env:PATH = "$ptDir;$env:PATH" }

$script:DataDir = Join-Path $env:APPDATA 'adbconnect'
$script:CacheFile = Join-Path $script:DataDir 'devices.tsv'
$script:SuspendFile = Join-Path $script:DataDir 'suspended.tsv'
$script:LogFile = Join-Path $env:TEMP 'adbconnect.log'

function Write-Log([string]$Level, [string]$Message) {
    $stamp = Get-Date -Format 'HH:mm:ss'
    Add-Content -Path $script:LogFile -Value "[$stamp][$Level] $Message"
    $color = @{ OK = 'Green'; WARN = 'Yellow'; ERR = 'Red';
                INFO = 'Cyan' }[$Level]; if (-not $color) { $color = 'Gray' }
    Write-Host "   [$Level] $Message" -ForegroundColor $color
}

function Test-Adb { return [bool](Get-Command adb -ErrorAction SilentlyContinue) }

function Get-DeviceList {
    $out = adb devices -l 2>$null
    $rows = @()
    foreach ($line in ($out | Select-Object -Skip 1)) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $p = $line -split '\s+'
        if ($p.Count -lt 2) { continue }
        $model = $p[0]
        foreach ($tok in ($p | Select-Object -Skip 2)) {
            if ($tok -like 'model:*') {
                $model = $tok.Substring(6).Replace('_', ' ')
            }
        }
        $isUsb = ($p[0] -notmatch ':' -and $p[0] -notmatch '^adb-')
        $rows += [pscustomobject]@{
            Serial = $p[0]; State = $p[1]; Model = $model; Usb = $isUsb
        }
    }
    return $rows
}

function Get-CachedTargets {
    $rows = @()
    if (Test-Path $script:CacheFile) {
        foreach ($line in Get-Content $script:CacheFile) {
            $c = $line -split "`t"
            if ($c.Count -ge 4) {
                $rows += , @("$($c[1]):$($c[2])",
                             $c[3].Replace('_', ' '),
                             $c[0].ToUpper())
            }
        }
    }
    return $rows
}

function Add-CacheEntry([string]$Serial, [string]$Ip, [int]$P,
                        [string]$Label) {
    # One row per device. Dropping rows that share the serial (case
    # insensitive) or the address stops a phone whose wireless port rotated
    # from being saved twice.
    New-Item -ItemType Directory -Force -Path $script:DataDir | Out-Null
    $key = $Serial.ToUpper()
    $kept = @()
    if (Test-Path $script:CacheFile) {
        foreach ($line in Get-Content $script:CacheFile) {
            $c = $line -split "`t"
            if ($c.Count -lt 4) { continue }
            if ($key -and $c[0].ToUpper() -eq $key) { continue }
            if ($c[1] -eq $Ip) { continue }
            $kept += $line
        }
    }
    $kept += "$key`t$Ip`t$P`t$Label`t$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    Set-Content -Path $script:CacheFile -Value $kept -Encoding UTF8
}

function Get-Suspended {
    $list = @()
    if (Test-Path $script:SuspendFile) {
        foreach ($line in Get-Content $script:SuspendFile) {
            $s = ($line -split "`t")[0]
            if ($s) { $list += $s.ToUpper() }
        }
    }
    return $list
}

function Suspend-Device([string]$Serial) {
    if (-not $Serial) { return }
    New-Item -ItemType Directory -Force -Path $script:DataDir | Out-Null
    $keep = @(Get-Suspended | Where-Object { $_ -ne $Serial.ToUpper() })
    $keep += $Serial.ToUpper()
    Set-Content -Path $script:SuspendFile -Value $keep -Encoding UTF8
}

function Resume-Device([string]$Serial) {
    if (-not $Serial -or -not (Test-Path $script:SuspendFile)) { return }
    $keep = @(Get-Suspended | Where-Object { $_ -ne $Serial.ToUpper() })
    Set-Content -Path $script:SuspendFile -Value $keep -Encoding UTF8
}

function Get-MdnsEntries {
    $out = adb mdns services 2>$null
    $rows = @()
    foreach ($line in $out) {
        if ($line -match 'adb-([A-Za-z0-9]+)-[A-Za-z0-9]+\._adb-tls-connect' -and
            $line -match '(\S+:\d+)\s*$') {
            $rows += , @($Matches[1].ToUpper(), $Matches[2])
        }
    }
    return $rows
}

function Get-MdnsTargets {
    return (Get-MdnsEntries | ForEach-Object { $_[1] }) |
        Sort-Object -Unique
}

function Get-PhoneIps([string]$Serial) {
    $out = adb -s $Serial shell ip -o -4 addr show scope global 2>$null
    $wlan = @(); $other = @()
    foreach ($line in $out) {
        if ($line -match 'rmnet|ccmni|tun') { continue }
        if ($line -match 'inet\s+(\d+\.\d+\.\d+\.\d+)/') {
            $ip = $Matches[1]
            if ($wlan -notcontains $ip -and $other -notcontains $ip) {
                if ($line -match 'wlan') { $wlan += $ip } else { $other += $ip }
            }
        }
    }
    return ($wlan + $other)
}

function Test-TargetConnected([string]$Target) {
    $hit = Get-DeviceList | Where-Object {
        $_.Serial -eq $Target -and $_.State -eq 'device' }
    return [bool]$hit
}

function Get-TargetSerial([string]$Target) {
    $out = adb -s $Target shell getprop ro.serialno 2>$null
    return ("$out").Trim().ToUpper()
}

function Get-DeviceSdk([string]$Target) {
    $out = adb -s $Target shell getprop ro.build.version.sdk 2>$null
    $digits = ("$out") -replace '\D', ''
    if ($digits) { return [int]$digits }
    return 0
}

function Test-WirelessDebugging([string]$Target) {
    $out = adb -s $Target shell settings get global adb_wifi_enabled 2>$null
    return (("$out").Trim() -eq '1')
}

function Enable-WirelessDebugging([string]$Target) {
    # The adb shell user holds WRITE_SECURE_SETTINGS, so this also restores a
    # toggle that an earlier adbd restart switched off.
    $null = adb -s $Target shell settings put global adb_wifi_enabled 1 2>$null
}

function Get-MdnsTargetForSerial([string]$Serial) {
    if (-not $Serial) { return '' }
    $key = $Serial.ToUpper()
    foreach ($m in (Get-MdnsEntries)) {
        if ($m[0] -eq $key) { return $m[1] }
    }
    return ''
}

function Wait-MdnsTarget([string]$Serial, [int]$Seconds = 20) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        $hit = Get-MdnsTargetForSerial $Serial
        if ($hit) { return $hit }
        Start-Sleep -Seconds 2
    }
    return ''
}

function Repair-Transport([string]$Target) {
    # Host side only: 'adb usb' / 'adb tcpip' would restart adbd on the phone
    # and drop its wireless-debugging session.
    $null = adb disconnect $Target 2>&1
    $null = adb reconnect offline 2>&1
}

function Connect-Target([string]$Target, [int]$Attempts = 3,
                        [string]$ExpectSerial = '') {
    $want = $ExpectSerial.ToUpper()
    for ($i = 1; $i -le $Attempts; $i++) {
        Write-Log 'INFO' "Attempt $i/$Attempts -> $Target"
        $null = adb connect $Target 2>&1
        Start-Sleep -Seconds 1
        if (Test-TargetConnected $Target) {
            $got = Get-TargetSerial $Target
            if ($want -and $got -and $want -ne $got) {
                # A different phone inherited this address; do not adopt it.
                $null = adb disconnect $Target 2>&1
                Write-Log 'ERR' "$Target belongs to a different device ($got)"
                return $false
            }
            Write-Log 'OK' "Connected and verified: $Target"
            return $true
        }
        Repair-Transport $Target
    }
    return $false
}

function Invoke-Scrcpy([string]$Target, [string]$Label) {
    if ($NoScrcpy) { return }
    if (-not (Get-Command scrcpy -ErrorAction SilentlyContinue)) { return }
    $running = Get-CimInstance Win32_Process -Filter "Name='scrcpy.exe'" |
        Where-Object { $_.CommandLine -match [regex]::Escape("-s $Target") }
    if ($running) { Write-Log 'INFO' "scrcpy already running for $Target"; return }
    Write-Log 'INFO' "Launching scrcpy..."
    Start-Process -FilePath 'scrcpy' -ArgumentList "-s", $Target |
        Out-Null
}

function Invoke-ConnectUsb {
    $usb = Get-DeviceList | Where-Object { $_.Usb -and $_.State -eq 'device' }
    if (-not $usb) {
        Write-Log 'WARN' 'No USB devices detected (or authorization denied).'
        return
    }
    foreach ($d in $usb) {
        Write-Host "`nDevice: $($d.Serial) [$($d.Model)]"
        $ident = Get-TargetSerial $d.Serial
        if (-not $ident) { $ident = $d.Serial.ToUpper() }
        $sdk = Get-DeviceSdk $d.Serial
        $done = $false

        # Preferred path on Android 11+: connect to the wireless-debugging
        # service the phone already advertises. No adbd restart, so the
        # Wireless-debugging toggle stays on.
        if ($sdk -ge 30) {
            $target = Get-MdnsTargetForSerial $ident
            if (-not $target) {
                if (-not (Test-WirelessDebugging $d.Serial)) {
                    Write-Log 'INFO' 'Turning wireless debugging on over the cable...'
                    Enable-WirelessDebugging $d.Serial
                }
                Write-Log 'INFO' 'Waiting for the wireless-debugging mDNS advert...'
                $target = Wait-MdnsTarget $ident 20
            }
            if ($target -and (Connect-Target $target $Retries $ident)) {
                $parts = $target -split ':'
                Add-CacheEntry $ident $parts[0] ([int]$parts[1]) $d.Model
                Resume-Device $ident
                Invoke-Scrcpy $target $d.Model
                Write-Log 'OK' 'You can unplug the USB cable now.'
                $done = $true
            }
        }

        if (-not $done) {
            # Fallback. This restarts adbd, which is what switches the
            # Android 11+ wireless-debugging toggle off, so it is restored
            # afterwards when the user had it on.
            $wifiWasOn = Test-WirelessDebugging $d.Serial
            Write-Log 'WARN' 'No wireless debugging available - falling back to tcpip.'
            $null = adb -s $d.Serial tcpip $Port 2>&1
            Start-Sleep -Seconds 3
            foreach ($ip in (Get-PhoneIps $d.Serial)) {
                $target = "$ip`:$Port"
                if (Connect-Target $target $Retries $ident) {
                    Add-CacheEntry $ident $ip $Port $d.Model
                    Resume-Device $ident
                    Invoke-Scrcpy $target $d.Model
                    Write-Log 'OK' 'You can unplug the USB cable now.'
                    $done = $true
                    break
                }
            }
            if ($wifiWasOn) {
                Enable-WirelessDebugging $d.Serial
                Write-Log 'INFO' 'Wireless debugging toggle switched back on.'
            }
        }

        if (-not $done) { Write-Log 'ERR' "All attempts failed for $($d.Model)" }
    }
}

function Get-KnownDevices {
    # One entry per device: @(serial, target, label). A fresh mDNS advert wins
    # over the saved port, since Android rotates the wireless-debugging port
    # on every reboot and Wi-Fi toggle. Merging on the serial here is what
    # keeps the same phone from being visited twice in one pass.
    $seen = @{}
    $rows = @()
    foreach ($m in (Get-MdnsEntries)) {
        $key = $m[0]
        if ($key -and -not $seen.ContainsKey($key)) {
            $seen[$key] = $true
            $rows += , @($key, $m[1], 'mDNS')
        }
    }
    foreach ($entry in (Get-CachedTargets)) {
        $key = $entry[2]
        if (-not $key) { $key = $entry[0] }
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $rows += , @($entry[2], $entry[0], $entry[1])
    }
    return $rows
}

function Invoke-Reconnect {
    [CmdletBinding()]
    param([switch]$Auto)

    $rows = @(Get-KnownDevices)
    if ($rows.Count -eq 0) {
        Write-Log 'WARN' 'No saved devices. Plug a device via USB and run once.'
        return
    }
    $suspended = @(Get-Suspended)
    foreach ($entry in $rows) {
        $serial = $entry[0]
        $target = $entry[1]
        $label = $entry[2]
        if ($Auto -and $serial -and $suspended -contains $serial.ToUpper()) {
            continue
        }
        if ((Test-TargetConnected $target)) {
            Write-Log 'OK' "Already connected: $target [$label]"
            Resume-Device $serial
            continue
        }
        $candidates = @()
        $fresh = Get-MdnsTargetForSerial $serial
        foreach ($c in @($fresh, $target)) {
            if ($c -and $candidates -notcontains $c) { $candidates += $c }
        }
        $done = $false
        foreach ($c in $candidates) {
            Write-Log 'INFO' "Reconnecting: $c ($label)"
            if (Connect-Target $c $Retries $serial) {
                $parts = $c -split ':'
                Add-CacheEntry $serial $parts[0] ([int]$parts[1]) $label
                Resume-Device $serial
                Invoke-Scrcpy $c $label
                $done = $true
                break
            }
        }
        if (-not $done) { Write-Log 'ERR' "Failed: $target" }
    }
}

function Invoke-WatchLoop {
    Write-Log 'INFO' "Watching every ${WatchInterval}s (Ctrl+C to stop)..."
    while ($true) {
        Invoke-Reconnect -Auto
        Start-Sleep -Seconds $WatchInterval
    }
}

function Invoke-Disconnect([string]$What = 'all') {
    # Host side only: the phone keeps its Wireless-debugging toggle as-is.
    foreach ($d in (Get-DeviceList)) {
        if ($d.State -eq 'device' -and -not $d.Usb) {
            Suspend-Device (Get-TargetSerial $d.Serial)
        }
    }
    if ($What -eq 'all') { $null = adb disconnect 2>&1 }
    else { $null = adb disconnect $What 2>&1 }
    Write-Log 'OK' "Disconnected: $What (stays off until you reconnect)"
}

function Show-List {
    Write-Host "`n=== Connected devices ==="
    adb devices -l 2>$null | Select-Object -Skip 1 | ForEach-Object {
        if (-not [string]::IsNullOrWhiteSpace($_)) { Write-Host "   $_" }
    }
}

function Invoke-Pair([string]$Target, [string]$Code) {
    if (-not $Target) {
        Write-Log 'INFO' 'Scanning for pairing services (mDNS)...'
        $out = adb mdns services 2>$null
        foreach ($line in $out) {
            if ($line -match '_adb-tls-pairing' -and
                $line -match '(\S+:\d+)\s*$') { $Target = $Matches[1]; break }
        }
        if (-not $Target) {
            Write-Log 'ERR' 'No pairing service found. Open the pairing dialog on the phone.'
            return
        }
        Write-Log 'OK' "Found: $Target"
    }
    if (-not $Code) { $Code = Read-Host 'Enter the 6-digit pairing code' }
    if ($Code -notmatch '^\d{6}$') {
        Write-Log 'ERR' 'The pairing code must be exactly 6 digits.'
        return
    }
    $null = adb pair $Target $Code 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Log 'ERR' 'Pairing failed. Check the code and keep the dialog open.'
        return
    }
    Write-Log 'OK' "Paired with $Target"
    # The connect service uses a different port than pairing and is published
    # a moment later, so wait for the advert instead of racing it.
    Write-Log 'INFO' 'Waiting for the wireless-debugging mDNS advert...'
    $conn = ''
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and -not $conn) {
        $conn = Get-MdnsTargets | Select-Object -First 1
        if (-not $conn) { Start-Sleep -Seconds 2 }
    }
    if (-not $conn) {
        Write-Log 'ERR' 'Paired, but the phone never advertised a connect service.'
        return
    }
    if (Connect-Target $conn $Retries) {
        $serial = Get-TargetSerial $conn
        $model = (Get-DeviceList | Where-Object { $_.Serial -eq $conn } |
            Select-Object -First 1).Model
        if (-not $model) { $model = $serial }
        $parts = $conn -split ':'
        if ($serial) {
            Add-CacheEntry $serial $parts[0] ([int]$parts[1]) $model
        }
        Invoke-Scrcpy $conn $model
    }
}

function Invoke-Doctor {
    Write-Host "`n=== Environment diagnostics ==="
    $tools = 'adb', 'scrcpy', 'python'
    foreach ($t in $tools) {
        $cmd = Get-Command $t -ErrorAction SilentlyContinue
        if ($cmd) { Write-Log 'OK' "$t -> $($cmd.Source)" }
        else { Write-Log 'WARN' "$t -> missing" }
    }
    $ver = adb version 2>$null | Select-Object -First 1
    Write-Log 'INFO' "$ver"
    $mdns = (adb mdns check 2>$null | Out-String).Trim()
    Write-Log 'INFO' "mDNS : $mdns"
    if ($mdns -notmatch 'mdns daemon version') {
        Write-Log 'WARN' ('Without mDNS there is no auto-discovery or QR ' +
                          'pairing, and the tool must fall back to tcpip.')
    }
    Write-Log 'INFO' "Cache: $script:CacheFile ($(@(Get-CachedTargets).Count) rows)"
    Write-Log 'INFO' "Known: $(@(Get-KnownDevices).Count) unique devices"
    Write-Log 'INFO' "Log  : $script:LogFile"
    Show-List
}

function Show-Help {
    Write-Host @"
ADB Wireless Manager v14.0.1 (Windows)

Usage: .\scripts\adbconnect.ps1 [command] [-Port N] [-Retries N] [-NoScrcpy]

Commands:
  connect      (default) take USB-attached devices wireless and verify
  reconnect|r  reconnect known devices without a cable (mDNS + cache)
  list|l       show current adb devices
  disconnect   drop wireless links on this machine
  pair         Android 11+ wireless pairing (mDNS discovery)
  watch        keep known devices connected (healing loop)
  doctor       environment diagnostics
  help         this screen

On Android 11 and newer, connect uses the phone's wireless-debugging service
discovered over mDNS. The classic 'adb tcpip' path is only used when that is
unavailable, because it restarts adbd and switches the phone's
Wireless-debugging toggle off.
"@
}

if (-not (Test-Adb)) {
    Write-Log 'ERR' 'adb not found. Install Android platform-tools first.'
    exit 1
}
New-Item -ItemType Directory -Force -Path $script:DataDir | Out-Null

switch ($Command) {
    'connect'    { Invoke-ConnectUsb; Show-List }
    'reconnect'  { Invoke-Reconnect; Show-List }
    'r'          { Invoke-Reconnect; Show-List }
    'list'       { Show-List }
    'l'          { Show-List }
    'disconnect' { Invoke-Disconnect ($Args | Select-Object -First 1) }
    'pair'       { Invoke-Pair ($Args | Select-Object -First 1)
                   ($Args | Select-Object -Skip 1 -First 1) }
    'watch'      { Invoke-WatchLoop }
    'doctor'     { Invoke-Doctor }
    'help'       { Show-Help }
}
