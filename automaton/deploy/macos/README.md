# Running automaton on macOS via launchd

What you get: worker, scheduler, and UI processes that start on login,
restart if they crash, and log to a predictable place under
`~/Library/Logs/automaton/`.

This is the macOS equivalent of [`deploy/systemd/`](../systemd/) for
Linux. The file layout follows Apple conventions:

| What | Where |
|---|---|
| DB + env file | `~/Library/Application Support/automaton/` |
| Logs | `~/Library/Logs/automaton/` |
| launchd agents | `~/Library/LaunchAgents/com.automaton.*.plist` |

Per-user only - this guide doesn't cover system-wide installs (which
would use `/Library/LaunchDaemons/` and a dedicated service user).
Personal infra fits in user-scope; production-style setups should run
on Linux.

## Option A: install from a clone

```bash
cd automaton
pip install -e .
bash deploy/macos/install.sh
```

`install.sh`:

- Substitutes `@PREFIX@` and `@HOME@` into the shipped plists.
- Copies them to `~/Library/LaunchAgents/`.
- Creates `~/Library/Application Support/automaton/automaton.env` if
  it doesn't exist (mode 600).
- Folds the env file's values into each plist's
  `EnvironmentVariables` dict so launchd actually sees them.
- `launchctl bootstrap gui/$(id -u)` each plist and `launchctl enable`
  them so they survive logout.

Re-run it any time you edit the env file - the script tears down the
existing agents and reinstalls with the refreshed env.

## Option B: install via Homebrew

A formula template is in [`automaton.rb`](./automaton.rb). To use it:

1. Create your own tap: `gh repo create your-name/homebrew-automaton`.
2. Copy `automaton.rb` into the tap.
3. Replace the placeholder SHA-256s with real ones from
   `brew create --python <package_url>`.
4. `brew tap your-name/automaton` then
   `brew install your-name/automaton/automaton`.
5. Follow the caveats: `bash $(brew --prefix)/opt/automaton/libexec/macos/install.sh`.

The plan ([Phase 2 of `PLATFORM-EXPANSION-PLAN.md`](../../PLATFORM-EXPANSION-PLAN.md))
deliberately doesn't ship a `.pkg` or notarize anything. Homebrew is
the right install vector for a CLI/server tool on macOS - fighting
Gatekeeper for a launchd-managed CLI is wasted effort.

## Verify it's running

```bash
launchctl print "gui/$(id -u)/com.automaton.worker"   | head -30
launchctl print "gui/$(id -u)/com.automaton.scheduler" | head -30
launchctl print "gui/$(id -u)/com.automaton.ui"        | head -30

# Tail recent logs:
tail -F ~/Library/Logs/automaton/worker.{out,err}.log

# Hit the UI:
open http://127.0.0.1:8080/
```

If something failed to start, the structured `automaton.log` plus the
plain `*.out.log` / `*.err.log` sidecars usually have what you need.

## Updating the env after install

```bash
${EDITOR:-vim} ~/Library/Application\ Support/automaton/automaton.env
bash deploy/macos/install.sh        # re-run to refresh
```

Don't `launchctl unload` + `load` by hand - the script does the right
sequence (`bootout` then `bootstrap`) and re-folds the env values into
the plists.

## Uninstalling

```bash
bash deploy/macos/uninstall.sh
```

Removes the LaunchAgents plists, leaves the DB + env file + logs alone
so you can revert without losing run history. `rm -rf
~/Library/Application\ Support/automaton ~/Library/Logs/automaton` to
fully wipe.

## Exposing the UI

The UI plist binds to `127.0.0.1:8080` by default. To reach it from a
phone or another host:

1. Generate a self-signed cert: `automaton tls init --hostname
   automaton.your-tailnet.ts.net`.
2. Edit the UI plist's `ProgramArguments` to add the `host` 0.0.0.0
   flag, plus the TLS cert and key paths.
3. Re-run `install.sh` to push the change.

Combined with the [mesh deploy guide](../mesh/README.md), this gives
you a phone-friendly dashboard over Tailscale without exposing
anything publicly.

## Common gotchas

**"`launchctl bootstrap` reports `Bootstrap failed: 5: Input/output
error`."** Usually means a previous instance is still around. Try
`launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.automaton.worker.plist`
and re-run install.

**"My env changes aren't being picked up."** launchd snapshots
`EnvironmentVariables` at bootstrap time. Editing the env file alone
doesn't reload it - you have to re-run `install.sh`.

**"After reboot, the agents aren't running."** Make sure you ran
`launchctl enable` (the install script does). Without it, the agent
goes away when you `bootout` and won't come back at login.

**"I want to run worker + scheduler but not the UI."** Just
`launchctl disable "gui/$(id -u)/com.automaton.ui"` and `bootout` it.
The other two work fine on their own.
