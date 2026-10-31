<#
.SYNOPSIS
  Removes StarterStopper shortcuts and stops the running app. Never requires admin.
.PARAMETER Force       Do not ask before stopping the running process.
.PARAMETER RemoveData  Also delete %APPDATA%\StarterStopper and %LOCALAPPDATA%\StarterStopper.
.PARAMETER DryRun      Print actions without doing them.
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$RemoveData,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$prefix = if ($DryRun) { '[dry-run] ' } else { '' }
function Say($m) { Write-Host "$prefix$m" }

# Shortcuts
foreach ($folder in @([Environment]::GetFolderPath('Startup'), [Environment]::GetFolderPath('Desktop'))) {
    $lnk = Join-Path $folder 'StarterStopper.lnk'
    if (Test-Path -LiteralPath $lnk) {
        Say "Removing shortcut: $lnk"
        if (-not $DryRun) { Remove-Item -LiteralPath $lnk -Force }
    } else { Say "No shortcut at $lnk" }
}

# Running process
$procs = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*StarterStopper.pyw*' })
if (-not $procs) { Say 'StarterStopper is not running.' }
foreach ($p in $procs) {
    Say "Found PID $($p.ProcessId): $($p.CommandLine)"
    $go = $Force
    if (-not $go -and -not $DryRun) {
        $ans = Read-Host "Stop PID $($p.ProcessId)? [y/N]"
        $go = $ans -match '^(y|yes)$'
    }
    if ($go) {
        Say "Stopping PID $($p.ProcessId)"
        if (-not $DryRun) { Stop-Process -Id $p.ProcessId -Force }
    } elseif (-not $DryRun) { Say "Left PID $($p.ProcessId) running." }
}

# Data
$dirs = @((Join-Path $env:APPDATA 'StarterStopper'), (Join-Path $env:LOCALAPPDATA 'StarterStopper'))
if ($RemoveData) {
    foreach ($d in $dirs) {
        if (Test-Path -LiteralPath $d) {
            Say "Removing data: $d"
            if (-not $DryRun) { Remove-Item -LiteralPath $d -Recurse -Force }
        }
    }
} else {
    Say "Leaving data in place (use -RemoveData to delete): $($dirs -join ', ')"
}
Say 'Done.'
