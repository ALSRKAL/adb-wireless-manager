# ==============================================================================
#  ADB Wireless Manager - Windows CLI  v12.0
#  connect / reconnect / list / disconnect / pair / watch / doctor
#  Requires: adb (platform-tools). Optional: scrcpy.
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

$script:DataDir = Join-Path $env:APPDATA 'adbconnect'
$script:CacheFile = Join-Path $script:DataDir 'devices.tsv'
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
                $rows += , @("$($c[1]):$($c[2])", $c[3].Replace('_', ' '))
            }
        }
    }
    return $rows
}

function Add-CacheEntry([string]$Serial, [string]$Ip, [int]$P,
                        [string]$Label) {
    New-Item -ItemType Directory -Force -Path $script:DataDir | Out-Null
    $kept = @()
    if (Test-Path $script:CacheFile) {
        foreach ($line in Get-Content $script:CacheFile) {
            $c = $line -split "`t"
            if ($c.Count -ge 4 -and "$($c[1]):$($c[2])" -ne "$Ip`:$P") {
                $kept += $line
            }
        }
    }
    $kept += "$Serial`t$Ip`t$P`t$Label`t$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
    Set-Content -Path $script:CacheFile -Value $kept -Encoding UTF8
}

function Get-MdnsTargets {
    $out = adb mdns services 2>$null
    $targets = @()
    foreach ($line in $out) {
        if ($line -match '_adb-tls-connect' -and $line -match '(\S+:\d+)\s*$') {
            $targets += $Matches[1]
        }
    }
    return $targets | Sort-Object -Unique
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

function Connect-Target([string]$Target, [int]$Attempts = 3) {
    for ($i = 1; $i -le $Attempts; $i++) {
        Write-Log 'INFO' "Attempt $i/$Attempts -> $Target"
        $null = adb connect $Target 2>&1
        Start-Sleep -Seconds 1
        if (Test-TargetConnected $Target) {
            Write-Log 'OK' "Connected and verified: $Target"
            return $true
        }
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
        Write-Log 'INFO' "Enabling TCP/IP mode on port $Port..."
        $null = adb -s $d.Serial tcpip $Port 2>&1
        Start-Sleep -Seconds 3
        $done = $false
        foreach ($ip in (Get-PhoneIps $d.Serial)) {
            $target = "$ip`:$Port"
            if (Connect-Target $target $Retries) {
                Add-CacheEntry $d.Serial $ip $Port $d.Model
                Invoke-Scrcpy $target $d.Model
                Write-Log 'OK' 'You can unplug the USB cable now.'
                $done = $true
                break
            }
        }
        if (-not $done) { Write-Log 'ERR' "All attempts failed for $($d.Model)" }
    }
}

function Invoke-Reconnect {
    $targets = @(Get-CachedTargets)
    $mdns = Get-MdnsTargets | Where-Object {
        $t = $_; -not ($targets | Where-Object { $_[0] -eq $t }) }
    foreach ($m in $mdns) { $targets += , @($m, 'mDNS') }
    if ($targets.Count -eq 0) {
        Write-Log 'WARN' 'No saved devices. Plug a device via USB and run once.'
        return
    }
    foreach ($entry in $targets) {
        $t = $entry[0]; $label = $entry[1]
        if (Test-TargetConnected $t) {
            Write-Log 'OK' "Already connected: $t [$label]"
            continue
        }
        Write-Log 'INFO' "Reconnecting saved device: $t ($label)"
        if (Connect-Target $t $Retries) { Invoke-Scrcpy $t $label }
        else { Write-Log 'ERR' "Failed: $t" }
    }
}

function Invoke-WatchLoop {
    Write-Log 'INFO' "Watching every ${WatchInterval}s (Ctrl+C to stop)..."
    while ($true) {
        Invoke-Reconnect
        Start-Sleep -Seconds $WatchInterval
    }
}

function Invoke-Disconnect([string]$What = 'all') {
    if ($What -eq 'all') { $null = adb disconnect 2>&1 }
    else { $null = adb disconnect $What 2>&1 }
    Write-Log 'OK' "Disconnected: $What"
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
    $null = adb pair $Target $Code 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Log 'OK' "Paired with $Target"
        $conn = Get-MdnsTargets | Select-Object -First 1
        if ($conn) { $null = Connect-Target $conn 2 }
    } else {
        Write-Log 'ERR' 'Pairing failed. Check the code and keep the dialog open.'
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
    Write-Log 'INFO' "mDNS : $(adb mdns check 2>$null)"
    Write-Log 'INFO' "Cache: $script:CacheFile ($(Get-CachedTargets).Count devices)"
    Write-Log 'INFO' "Log  : $script:LogFile"
    Show-List
}

function Show-Help {
    Write-Host @"
ADB Wireless Manager v12.0 (Windows)

Usage: .\scripts\adbconnect.ps1 [command] [-Port N] [-Retries N] [-NoScrcpy]

Commands:
  connect      (default) enable TCP/IP over USB, connect and verify
  reconnect|r  reconnect saved devices without a cable (cache + mDNS)
  list|l       show current adb devices
  disconnect   drop all wireless connections
  pair         Android 11+ wireless debugging pairing (mDNS discovery)
  watch        keep devices connected (auto-healing loop)
  doctor       environment diagnostics
  help         this screen
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
