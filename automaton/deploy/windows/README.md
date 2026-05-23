# Running automaton on Windows via NSSM

What you get: worker, scheduler, and UI as Windows services that
start on boot, restart if they crash, and log to a predictable
place under `%ProgramData%\automaton\logs\`.

This is the Windows equivalent of [`deploy/systemd/`](../systemd/)
for Linux and [`deploy/macos/`](../macos/) for macOS. File layout
follows Windows conventions:

| What | Where |
|---|---|
| DB | `%APPDATA%\automaton\automaton.db` |
| Env file | `%ProgramData%\automaton\automaton.env` |
| Logs | `%ProgramData%\automaton\logs\` |
| NSSM binary | `%ProgramData%\automaton\nssm\nssm.exe` |
| Service definitions | Windows Service Control Manager (`services.msc`) |

NSSM ([nssm.cc](https://nssm.cc/)) is the lightest path - a tiny
MIT-licensed exe that wraps an arbitrary command as a Windows service.
Built-in NT services would need `pywin32` + a service class, which is
strictly more moving parts for no real upside on a single-host
personal deployment.

## Install

1. Install Python 3.10+ from python.org (check "Add to PATH" during the
   installer wizard).
2. Install automaton:
   ```powershell
   git clone https://github.com/your-name/automaton
   cd automaton
   pip install -e .
   ```
3. Open a PowerShell as Administrator (right-click PowerShell ->
   *Run as administrator*) and run:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\deploy\windows\install.ps1
   ```

What the script does:

- Confirms Administrator + that `python` is on PATH + that `automaton`
  is installed.
- Creates `%ProgramData%\automaton\` (env file, NSSM binary, logs) and
  `%APPDATA%\automaton\` (DB).
- Downloads NSSM 2.24 to `%ProgramData%\automaton\nssm\` if not
  already present. ~700 KB; verifies architecture; one-shot download.
- Reads `%ProgramData%\automaton\automaton.env`, folds the key=value
  pairs into each service's environment.
- Registers `automaton-worker`, `automaton-scheduler`, `automaton-ui`
  as Auto-Start Windows services depending on EventLog.
- Sets `AppRestartDelay` to 10 s so a crashing service can't spin the
  CPU.
- Starts each service.

Re-run any time you edit the env file - the script tears down + reinstalls
each service so the env propagates.

## Verify

```powershell
Get-Service -Name automaton-*
# Status   Name                  DisplayName
# ------   ----                  -----------
# Running  automaton-scheduler   automaton-scheduler
# Running  automaton-ui          automaton-ui
# Running  automaton-worker      automaton-worker

# Tail recent logs:
Get-Content -Wait -Tail 20 "$env:ProgramData\automaton\logs\automaton-worker.out.log"

# Open the UI:
Start-Process http://127.0.0.1:8080/
```

If a service fails to start, Event Viewer ->
*Windows Logs* -> *Application* has NSSM's diagnostic plus the
captured stdout / stderr.

## Uninstall

```powershell
.\deploy\windows\uninstall.ps1            # remove services, keep DB
.\deploy\windows\uninstall.ps1 -Purge     # also delete %ProgramData% + DB
```

## Exposing the UI

The UI service binds to `127.0.0.1:8080` by default. To make it
reachable from your phone:

1. Generate a self-signed cert (`automaton tls init` - see the [TLS
   docs](../../README.md#tls)).
2. Edit the UI service's `Application` field via the NSSM GUI
   (`nssm.exe edit automaton-ui`) to add `--host 0.0.0.0 --tls-cert ...
   --tls-key ...`, OR just re-run `install.ps1` after editing the
   command at the top of the script.
3. Add the host to your Tailscale tailnet ([mesh deploy
   guide](../mesh/README.md)).

## Common gotchas

**"AV flags NSSM's download as suspicious."** Windows Defender
occasionally flags signed-but-uncommon binaries. Whitelist
`%ProgramData%\automaton\nssm\nssm.exe` if you trust your nssm.cc
download, or pre-stage NSSM and skip the download by placing it at
that path before running `install.ps1`.

**"`Set-ExecutionPolicy` complains about group policy."** Some
corporate-managed machines pin the execution policy. Run the script
via `powershell.exe -ExecutionPolicy Bypass -File install.ps1`
instead.

**"Service starts then immediately stops."** Almost always a Python
path issue or a missing dep. Check `automaton-worker.err.log` in
`%ProgramData%\automaton\logs\`. The most common cause is the
service running under LocalSystem (which has its own PATH) and not
finding `python.exe` even though the user's shell did. `install.ps1`
resolves the absolute path and passes it explicitly to avoid this -
re-run if you've moved Python.

**"SQLite database is locked."** The DB defaults to `%APPDATA%\automaton\`
which on Windows means *the user's roaming profile* (interpreted by
the service's user context). With LocalSystem, `%APPDATA%` resolves
to `C:\Windows\System32\config\systemprofile\AppData\Roaming\`. That's
fine, but don't put the DB on a network share - SQLite WAL doesn't
support SMB / CIFS. Keep it on a local NTFS volume.

**"Long paths fail."** Windows defaults to a 260-char path limit
unless long paths are enabled. The default install paths are well
under that, but if you've nested it inside a long workspace path,
turn on `LongPathsEnabled` in the registry first
(`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem`).

**"The UI hangs on certain pages from a phone."** SSE through Python's
stdlib `http.server` can be sensitive to proxies. Inside a tailnet
that's a non-issue; in front of an enterprise reverse proxy it can
need tuning. See the [scale doc](../../docs/scale.md) for the bail-out
plan (move SSE behind uvicorn).
