# ==============================================================================
#  ADB Wireless Manager - Windows installer
#  Repo: https://github.com/ALSRKAL/adb-wireless-manager
#  Run with:  powershell -ExecutionPolicy Bypass -File install.ps1
# ==============================================================================
$ErrorActionPreference = 'Continue'
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path

function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  [!] $m"  -ForegroundColor Yellow }
function Write-Fail($m) { Write-Host "  [X] $m"  -ForegroundColor Red }

Write-Host "=== ADB Wireless Manager installer (Windows) ==="

$adb = Get-Command adb -ErrorAction SilentlyContinue
if ($adb) { Write-Ok "adb found: $($adb.Source)" }
else {
    Write-Fail "adb missing. Install it with:  winget install Google.PlatformTools"
    Write-Host "      then reopen the terminal and run this installer again."
}

$scrcpy = Get-Command scrcpy -ErrorAction SilentlyContinue
if ($scrcpy) { Write-Ok "scrcpy found: $($scrcpy.Source)" }
else { Write-Warn2 "scrcpy missing (optional, for screen mirroring): winget install scrcpy" }

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if ($python) { Write-Ok "python found: $($python.Source)" }
else {
    Write-Fail "python missing. Install from https://www.python.org/downloads/ (check 'Add to PATH')"
}

if ($python) {
    & python -c "import PyQt5" 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Ok "PyQt5 installed" }
    else {
        Write-Warn2 "Installing PyQt5 via pip..."
        & python -m pip install --user PyQt5
        if ($LASTEXITCODE -eq 0) { Write-Ok "PyQt5 installed" }
        else { Write-Warn2 "pip failed. Try manually: python -m pip install PyQt5" }
    }
}

$cli = Join-Path $Dir 'scripts\adbconnect.ps1'
Write-Ok "CLI ready: powershell -ExecutionPolicy Bypass -File `"$cli`" connect"

$startup = [Environment]::GetFolderPath('Startup')
$wscript = New-Object -ComObject WScript.Shell
$lnk = $wscript.CreateShortcut((Join-Path $startup 'ADB Wireless Tray.lnk'))
$pyw = Get-Command pythonw -ErrorAction SilentlyContinue
if (-not $pyw) {
    $guess = Join-Path (Split-Path -Parent $python.Source) 'pythonw.exe'
    if (Test-Path $guess) { $pyw = @{ Source = $guess } }
}
if ($pyw) {
    $lnk.TargetPath = $pyw.Source
    $lnk.Arguments  = "`"$Dir\tray\adbtray.py`""
    $lnk.WorkingDirectory = $Dir
    $lnk.Description = 'ADB Wireless Manager tray icon'
    $lnk.Save()
    Write-Ok "Tray autostart -> $startup\ADB Wireless Tray.lnk"
} else {
    Write-Warn2 "Could not find pythonw.exe - tray autostart skipped."
}

Write-Host ""
Write-Ok "Done! Re-login (or run the tray manually) to see the icon."
