<#
.SYNOPSIS
  Installs StarterStopper for the current user (no admin required).
.DESCRIPTION
  Finds pythonw.exe, optionally installs dependencies, creates a Startup-folder
  shortcut (and optionally a Desktop one) and the %APPDATA%\StarterStopper folder.
  Idempotent: safe to re-run. Does not create config.json.
.PARAMETER Python       Path to python.exe or pythonw.exe to use.
.PARAMETER InstallDeps  Run: python -m pip install --user -r requirements.txt
.PARAMETER NoStartup    Skip the Startup-folder shortcut.
.PARAMETER DesktopShortcut  Also create a Desktop shortcut.
.PARAMETER Launch       Start the app now.
.PARAMETER DryRun       Print actions without doing them.
#>
[CmdletBinding()]
param(
    [string]$Python,
    [switch]$InstallDeps,
    [switch]$NoStartup,
    [switch]$DesktopShortcut,
    [switch]$Launch,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

$repo   = $PSScriptRoot
$pyw    = Join-Path $repo 'StarterStopper.pyw'
$icon   = Join-Path $repo 'assets\starterstopper.ico'
$reqs   = Join-Path $repo 'requirements.txt'
$cfgDir = Join-Path $env:APPDATA 'StarterStopper'
$prefix = if ($DryRun) { '[dry-run] ' } else { '' }

function Say($m) { Write-Host "$prefix$m" }

function Test-PythonOk($exe) {
    # Returns version string if >= 3.9, otherwise $null
    try {
        $v = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $v) { return $null }
        $p = ([string]$v).Trim().Split('.')
        if ([int]$p[0] -gt 3 -or ([int]$p[0] -eq 3 -and [int]$p[1] -ge 9)) { return ([string]$v).Trim() }
    } catch { }
    return $null
}

# --- Locate python / pythonw ---------------------------------------------
if ($Python) {
    if (-not (Test-Path -LiteralPath $Python)) { throw "-Python path not found: $Python" }
    $dir = Split-Path -Parent (Resolve-Path -LiteralPath $Python).Path
} else {
    $cmd = Get-Command python -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' } |
        Select-Object -First 1
    if (-not $cmd) {
        throw "No usable python.exe on PATH (the Microsoft Store stub is ignored). Install Python 3.9+ from python.org or pass -Python <path>."
    }
    $dir = Split-Path -Parent $cmd.Source
}
$pythonExe  = Join-Path $dir 'python.exe'
$pythonwExe = Join-Path $dir 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonwExe)) { throw "pythonw.exe not found beside $pythonExe" }
if (-not (Test-Path -LiteralPath $pythonExe))  { throw "python.exe not found in $dir" }
$ver = Test-PythonOk $pythonExe
if (-not $ver) { throw "Python at $pythonExe is missing or older than 3.9." }
Say "Using Python $ver : $pythonwExe"

if (-not (Test-Path -LiteralPath $pyw)) {
    if ($DryRun) { Write-Warning "StarterStopper.pyw not found at $pyw (continuing, dry run)." }
    else { throw "StarterStopper.pyw not found at $pyw" }
}

# --- Dependencies ---------------------------------------------------------
if ($InstallDeps) {
    Say "pip install --user -r $reqs"
    if (-not $DryRun) {
        & $pythonExe -m pip install --user -r $reqs
        if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)." }
    }
}

# --- Shortcuts -------------------------------------------------------------
function New-AppShortcut($folder) {
    $lnk = Join-Path $folder 'StarterStopper.lnk'
    $iconLoc = if (Test-Path -LiteralPath $icon) { $icon } else { "$pythonwExe,0" }
    $verb = if (Test-Path -LiteralPath $lnk) { 'Updating' } else { 'Creating' }
    Say "$verb shortcut: $lnk"
    Say "    target : $pythonwExe"
    Say "    args   : `"$pyw`""
    Say "    start in: $repo"
    Say "    icon   : $iconLoc"
    if ($DryRun) { return }
    $sh = New-Object -ComObject WScript.Shell
    $s = $sh.CreateShortcut($lnk)
    $s.TargetPath = $pythonwExe
    $s.Arguments = "`"$pyw`""
    $s.WorkingDirectory = $repo
    $s.IconLocation = $iconLoc
    $s.Description = 'StarterStopper tray app'
    $s.Save()
}

if ($NoStartup) { Say 'Skipping Startup shortcut (-NoStartup).' }
else { New-AppShortcut ([Environment]::GetFolderPath('Startup')) }
if ($DesktopShortcut) { New-AppShortcut ([Environment]::GetFolderPath('Desktop')) }

# --- Config folder (no config.json is created) ----------------------------
if (Test-Path -LiteralPath $cfgDir) { Say "Config folder exists: $cfgDir" }
else {
    Say "Creating config folder: $cfgDir"
    if (-not $DryRun) { New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null }
}
Say 'config.json is not created; use Preferences in the app to load the example.'

# --- Launch ---------------------------------------------------------------
if ($Launch) {
    Say "Launching: $pythonwExe `"$pyw`""
    if (-not $DryRun) {
        Start-Process -FilePath $pythonwExe -ArgumentList "`"$pyw`"" -WorkingDirectory $repo
    }
}
Say 'Done.'
