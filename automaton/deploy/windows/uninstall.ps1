<#
.SYNOPSIS
  Remove the three automaton services. Preserves DB + env file by default.

.PARAMETER AppDir
  Where NSSM and the env file live. Default: %ProgramData%\automaton.

.PARAMETER Purge
  Also delete %ProgramData%\automaton (including nssm, env, logs) AND
  %APPDATA%\automaton (the DB). Use with care.

.EXAMPLE
  # Stop and remove services, keep data:
  PS> .\uninstall.ps1
  # Remove everything:
  PS> .\uninstall.ps1 -Purge
#>
[CmdletBinding()]
param(
    [string]$AppDir = (Join-Path $env:ProgramData "automaton"),
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
$ServiceNames = @("automaton-worker", "automaton-scheduler", "automaton-ui")
$NssmExe = Join-Path $AppDir "nssm\nssm.exe"

if (-not ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent() `
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "uninstall.ps1 must run as Administrator."
}

foreach ($name in $ServiceNames) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host "  not installed:  $name"
        continue
    }
    if ($svc.Status -eq "Running") {
        Stop-Service -Name $name -Force
        Start-Sleep -Seconds 1
    }
    if (Test-Path $NssmExe) {
        & $NssmExe remove $name confirm | Out-Null
    } else {
        # Fall back to sc.exe so we can still uninstall if nssm is gone.
        sc.exe delete $name | Out-Null
    }
    Write-Host "  removed:  $name"
}

if ($Purge) {
    if (Test-Path $AppDir) {
        Remove-Item -Recurse -Force $AppDir
        Write-Host "  purged:  $AppDir"
    }
    $DbDir = Join-Path $env:APPDATA "automaton"
    if (Test-Path $DbDir) {
        Remove-Item -Recurse -Force $DbDir
        Write-Host "  purged:  $DbDir"
    }
} else {
    Write-Host ""
    Write-Host "  state preserved:"
    Write-Host "    $AppDir  (nssm, env file, logs)"
    Write-Host ("    {0}\automaton  (DB)" -f $env:APPDATA)
    Write-Host "  re-run with -Purge to delete those."
}
