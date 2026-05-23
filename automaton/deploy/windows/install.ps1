<#
.SYNOPSIS
  Install automaton as three Windows services (worker, scheduler, ui) via NSSM.

.DESCRIPTION
  - Downloads NSSM (https://nssm.cc/) into %ProgramData%\automaton\nssm\ if it
    isn't already there. NSSM is MIT-licensed and small (~700 KB).
  - Registers automaton-worker, automaton-scheduler, automaton-ui as Windows
    services running under the LocalSystem account by default.
  - Reads %ProgramData%\automaton\automaton.env and applies each KEY=VALUE to
    the service's environment.
  - Sets AppRestartDelay so a crashing service can't spin the CPU.

.PARAMETER PythonExe
  Path to the python.exe that has the `automaton` package installed.
  Default: the `python` on PATH.

.PARAMETER AppDir
  Where the env file and per-service config snippets live.
  Default: $env:ProgramData\automaton.

.EXAMPLE
  # As Administrator:
  PS> Set-ExecutionPolicy -Scope Process Bypass
  PS> .\install.ps1
#>
[CmdletBinding()]
param(
    [string]$PythonExe = "python",
    [string]$AppDir = (Join-Path $env:ProgramData "automaton"),
    [string]$NssmVersion = "2.24"
)

$ErrorActionPreference = "Stop"
$ServiceNames = @("automaton-worker", "automaton-scheduler", "automaton-ui")
$ServiceCmds  = @{
    "automaton-worker"     = @("-m", "automaton", "worker")
    "automaton-scheduler"  = @("-m", "automaton", "scheduler")
    "automaton-ui"         = @("-m", "automaton", "serve", "--host", "127.0.0.1", "--port", "8080")
}

# --- preflight ---------------------------------------------------------
if (-not ([Security.Principal.WindowsPrincipal] `
          [Security.Principal.WindowsIdentity]::GetCurrent() `
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "install.ps1 must run as Administrator (right-click PowerShell -> Run as administrator)."
}

# Locate python.exe so the service ProgramArguments have an absolute path.
$pythonResolved = (Get-Command $PythonExe -ErrorAction SilentlyContinue).Source
if (-not $pythonResolved) {
    throw "could not find $PythonExe on PATH. Install Python 3.10+ first, then re-run."
}
Write-Host "  python: $pythonResolved"

# Verify automaton is importable.
& $pythonResolved -c "import automaton" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "the python at $pythonResolved doesn't have the 'automaton' package. Run 'pip install -e .' first."
}

# Make app directories.
$LogDir = Join-Path $AppDir "logs"
$DbDir  = Join-Path $env:APPDATA "automaton"
foreach ($d in @($AppDir, $LogDir, $DbDir)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Write-Host "  app dir:  $AppDir"
Write-Host "  log dir:  $LogDir"
Write-Host "  db dir:   $DbDir"

# Env file template on first run.
$EnvFile = Join-Path $AppDir "automaton.env"
if (-not (Test-Path $EnvFile)) {
    $template = Get-Content (Join-Path $PSScriptRoot "automaton.env.example") -Raw
    Set-Content -Path $EnvFile -Value $template -Encoding UTF8
    Write-Host "  created env file:  $EnvFile  (edit before relying on services)"
}

# --- fetch NSSM -------------------------------------------------------
$NssmDir = Join-Path $AppDir "nssm"
$NssmExe = Join-Path $NssmDir "nssm.exe"
if (-not (Test-Path $NssmExe)) {
    Write-Host "  downloading NSSM $NssmVersion..."
    $tmp = New-TemporaryFile
    $zip = "$tmp.zip"
    Invoke-WebRequest "https://nssm.cc/release/nssm-$NssmVersion.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath "$tmp.dir" -Force
    $arch = if ([Environment]::Is64BitOperatingSystem) { "win64" } else { "win32" }
    $src = Join-Path "$tmp.dir" "nssm-$NssmVersion\$arch\nssm.exe"
    New-Item -ItemType Directory -Force -Path $NssmDir | Out-Null
    Copy-Item $src -Destination $NssmExe -Force
    Remove-Item -Recurse -Force $tmp, $zip, "$tmp.dir"
}
Write-Host "  nssm:     $NssmExe"

# --- read env file into a hashtable ----------------------------------
$envMap = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $k, $v = $line.Split("=", 2)
            $envMap[$k.Trim()] = $v.Trim().Trim('"').Trim("'")
        }
    }
}

# --- install / refresh each service ----------------------------------
function Set-Service-If-Present($name, $action) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if ($svc -and $svc.Status -eq "Running") {
        & $NssmExe stop $name confirm | Out-Null
    }
}

foreach ($name in $ServiceNames) {
    Set-Service-If-Present $name 'stop'
    # Remove if already registered, so we can re-install cleanly.
    & $NssmExe status $name 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        & $NssmExe remove $name confirm | Out-Null
    }

    $args = @($name, $pythonResolved) + $ServiceCmds[$name]
    & $NssmExe install @args | Out-Null
    & $NssmExe set $name AppDirectory $DbDir | Out-Null
    & $NssmExe set $name AppStdout (Join-Path $LogDir "$name.out.log") | Out-Null
    & $NssmExe set $name AppStderr (Join-Path $LogDir "$name.err.log") | Out-Null
    & $NssmExe set $name AppRotateFiles 1 | Out-Null
    & $NssmExe set $name AppRotateBytes 10485760 | Out-Null
    & $NssmExe set $name Start SERVICE_AUTO_START | Out-Null
    & $NssmExe set $name AppRestartDelay 10000 | Out-Null
    & $NssmExe set $name AppStopMethodConsole 30000 | Out-Null
    & $NssmExe set $name DependOnService EventLog | Out-Null

    # Push the env file's vars onto the service. NSSM's AppEnvironmentExtra
    # takes one "KEY=VALUE" string per :argument: that's NUL-terminated.
    if ($envMap.Count -gt 0) {
        $envArgs = $envMap.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }
        & $NssmExe set $name AppEnvironmentExtra @envArgs | Out-Null
    }

    # Default to per-service AUTOMATON_DB so it's unambiguous.
    & $NssmExe set $name AppEnvironmentExtra "+AUTOMATON_DB=$DbDir\automaton.db" | Out-Null

    Start-Service -Name $name
    Write-Host "  installed + started:  $name"
}

Write-Host ""
Write-Host "  done. status check:"
foreach ($name in $ServiceNames) {
    $svc = Get-Service -Name $name
    Write-Host ("    {0,-22}  {1}" -f $name, $svc.Status)
}
Write-Host ""
Write-Host "  edit the env file and re-run install.ps1 to refresh:"
Write-Host "    notepad $EnvFile"
Write-Host "  UI:  http://127.0.0.1:8080/"
Write-Host "  logs:  $LogDir\*.out.log"
