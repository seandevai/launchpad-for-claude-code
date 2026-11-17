# Launchpad for Claude Code - Windows installer
# Installs the Python dependency and creates Desktop / Start menu shortcuts.
# Usage: install.bat            (default hotkey Ctrl+Alt+L)
#        install.bat -Hotkey "Ctrl+Alt+C"
#        install.bat -NoDesktopShortcut
param(
    [string]$Hotkey = "Ctrl+Alt+L",
    [switch]$NoDesktopShortcut
)
$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$app = Join-Path $dir "launchpad.py"
$icon = Join-Path $dir "assets\icon.ico"
$dataDir = Join-Path $env:USERPROFILE ".launchpad-cc"
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
Start-Transcript -Path (Join-Path $dataDir "install.log") -Force | Out-Null

function Ok($m)   { Write-Host "[OK]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[WARN] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "[FAIL] $m" -ForegroundColor Red }

Write-Host "Launchpad for Claude Code - installer`n"

# 1. Python 3.9+ (ignores the Microsoft Store stub, which cannot run scripts)
$python = $null
$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@("py", "-3") }
if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@("python") }
foreach ($c in $candidates) {
    $exe = $c[0]; $pre = @(); if ($c.Count -gt 1) { $pre = $c[1..($c.Count-1)] }
    $out = & $exe @pre -c "import sys;print(sys.executable);print(sys.version_info>=(3,9))" 2>$null
    if ($LASTEXITCODE -eq 0 -and $out -and $out[1] -eq "True" -and $out[0] -notmatch "WindowsApps") { $python = $out[0].Trim(); break }
}
if (-not $python) {
    Fail "Python 3.9 or newer not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH') and run this installer again."
    Stop-Transcript | Out-Null; exit 1
}
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Ok "Python: $python"

# 2. Python dependency (pinned range in requirements.txt)
Write-Host "Installing pywebview..."
& $python -m pip install --disable-pip-version-check -q -r (Join-Path $dir "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    Warn "System-wide install failed, retrying for the current user only"
    & $python -m pip install --disable-pip-version-check -q --user -r (Join-Path $dir "requirements.txt")
}
& $python -c "import webview" 2>$null
if ($LASTEXITCODE -eq 0) { Ok "pywebview installed" } else { Fail "pywebview could not be imported (see the pip output above)"; Stop-Transcript | Out-Null; exit 1 }

# 3. Optional tools
if (Get-Command claude -ErrorAction SilentlyContinue) { Ok "Claude Code CLI found" }
else { Warn "Claude Code CLI ('claude') not on PATH: 'Open in terminal' will not work until you install it" }
if (Get-Command npx -ErrorAction SilentlyContinue) { Ok "Node.js / npx found (token usage panel enabled)" }
else { Warn "npx not found: install Node.js to enable the token usage panel" }

# 4. Shortcuts
$ws = New-Object -ComObject WScript.Shell
$wsHotkey = ($Hotkey -replace '\s', '').ToUpper()
function New-Shortcut($path, $withHotkey) {
    $s = $ws.CreateShortcut($path)
    $s.TargetPath = $pythonw
    $s.Arguments = "`"$app`""
    $s.WorkingDirectory = $dir
    $s.IconLocation = "$icon,0"
    $s.Description = "Launchpad for Claude Code"
    if ($withHotkey -and $wsHotkey) { $s.Hotkey = $wsHotkey }
    $s.Save()
}
$startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Launchpad for Claude Code.lnk"
$desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "Launchpad for Claude Code.lnk"
if ($NoDesktopShortcut) {
    New-Shortcut $startMenu $true
    Ok "Start menu shortcut created (hotkey $Hotkey)"
} else {
    New-Shortcut $startMenu $false
    New-Shortcut $desktop $true
    Ok "Start menu and Desktop shortcuts created (hotkey $Hotkey)"
}

# Replace the shortcut of the pre-release version ("Claude Launcher"), if present
$legacy = Join-Path ([Environment]::GetFolderPath("Desktop")) "Claude Launcher.lnk"
if (Test-Path $legacy) {
    $l = $ws.CreateShortcut($legacy)
    if ($l.Arguments -match "claude_launcher\.py") { Remove-Item $legacy; Ok "Removed the old 'Claude Launcher' shortcut" }
}

@{ hotkey = $Hotkey; app = $app; installed = (Get-Date).ToString("s") } | ConvertTo-Json | Set-Content -Encoding UTF8 (Join-Path $dataDir "install.json")

Write-Host "`nDone. Open Launchpad from the Desktop, the Start menu or with $Hotkey."
Stop-Transcript | Out-Null
