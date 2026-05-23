# Platform expansion plan

Goal: take what currently works on Linux as a single-machine personal automation engine, and make it work as personal infrastructure spanning Linux server, macOS host, Windows host, iOS client, Android client. Honest assessment in `README.md` and the prior audit said Linux is solid; this plan covers what it takes to close the gap on everything else.

This is written for one person (or a small team) doing the work over weeks-to-months, not for a funded engineering organization. Where the right call is "use something off the shelf instead," I say so.

The plan has fifteen actionable phases plus two speculative ones. Phases 1-4 close the host-platform gap (Linux is done; macOS, Windows, TLS aren't). Phases 5-13 are the operational-maturity work — remote access, secrets, notifications, backup, migrations, time correctness, mobile-friendly web UI, a templates library, and load tests. Phases 14-15 add the native mobile clients. Phases 16-17 are "real infrastructure" territory — only do them if the scope grows past one person's tools.

---

## Phase 0: where we are now

A single-machine Linux deployment is real. 68 tests pass. systemd units, bearer-token API, signed webhooks, signals, retries, plugins, backups, pruning, cancel, validation are all done. The state store is one SQLite file. The HTTP API is bound to localhost by default.

What this plan adds, in priority order:

- A1: Path / process portability so the engine runs unchanged on macOS and Windows.
- A2: macOS host support (launchd, brew install).
- A3: Windows host support (Service Manager wrapper, installer).
- A4: TLS on the UI server so you don't have to put a reverse proxy in front.
- A5: Remote access across devices via mesh networking (Tailscale / Headscale).
- A6: Secrets management via OS keychains (no plaintext env files).
- A7: Notifications & alerting on run failures and timeouts.
- A8: Backup, restore, and disaster recovery (Litestream, off-host).
- A9: Schema migrations so the engine evolves without manual ALTER TABLEs.
- A10: Time, timezone, and DST correctness.
- A11: Web UI mobile responsiveness and PWA shell.
- A12: Workflow templates library so first-day users have working examples.
- A13: Performance & scale ceiling testing — know the single-host envelope.
- A14: iOS client app.
- A15: Android client app.

Phases 16-17 are speculative — touch them only if you outgrow the single-host shape.

---

## Phase 1: Path & process portability audit (effort: 1-2 days)

The reason Linux is "solid" and Windows isn't is that I wrote everything assuming POSIX. This is the easiest phase. Fix it first because everything downstream depends on it.

### What changes

1. **All test fixtures use `pathlib.Path` and `tmp_path` already.** Verify there are no hardcoded `/tmp/...` paths in the *engine code itself*. The example YAMLs (`/tmp/automaton-hello.log`) are user-facing examples and only need to be updated when shipping a default for a given OS — the engine doesn't care.

2. **`shell` step type uses `shlex.split` and runs `sh -c` in examples.** That's POSIX-specific. Two changes:
   - On Windows, default to `cmd.exe /c` when the user passes a string. Detect via `os.name == "nt"` and document.
   - In `examples/retry.yaml`, ship two variants: `retry.posix.yaml` and `retry.windows.yaml`. Or one `retry.yaml` using a Python step that's cross-platform.

3. **`file_append` and any future file-based step type uses `os.path` / `Path` joins.** Already does — `path` is taken from the spec and used verbatim. Good.

4. **`db.py` uses `Path(db_path)` — verify it handles `C:\...` style paths.** Python's sqlite3 takes a string path and passes to the OS open; it works on Windows. The schema file is loaded relative to the module's directory via `Path(__file__).with_name`. That's portable. Good.

5. **`_iso` timestamps use UTC strftime.** Portable.

6. **Subprocess timeouts work on both.** `subprocess.run(..., timeout=...)` is cross-platform. Good.

7. **`signal` handling.** Python `signal.signal(SIGTERM, ...)` works on POSIX. On Windows there's no SIGTERM in the same sense — the process just exits. KeyboardInterrupt still works. For the worker / scheduler / UI to shut down gracefully on Windows, we may need to listen for `SIGBREAK` and Console Control Events. Defer until Phase 3 (Windows service); for a CLI invocation in a terminal, `Ctrl-C` is fine.

### Verification

- `pytest tests/` passes on macOS (test it on your Mac before doing more work).
- `pytest tests/` passes on Windows. Add a CI workflow that runs the test suite on `windows-latest` and `macos-latest` in addition to Linux. Catch regressions before they ship.

### Risk

Low. Mostly cosmetic. Biggest hazard is hidden POSIX assumptions in step types (env var inheritance, signal handling, line endings in file_append) that surface only under specific Windows conditions. The CI matrix is the answer.

### Effort

1-2 days of focused work to audit + add the CI matrix. The actual code change set is small.

---

## Phase 2: macOS host support (effort: 1 week)

### What changes

1. **launchd plists** mirroring the systemd units in `deploy/systemd/`. Three plists:
   - `com.automaton.worker.plist`
   - `com.automaton.scheduler.plist`
   - `com.automaton.ui.plist`

   Each declares `KeepAlive = true`, the `automaton` binary path, the env file, working directory, and stdout/stderr log paths. Same env-file convention as Linux (`/usr/local/etc/automaton/automaton.env`). Install location for the plists is `/Library/LaunchDaemons/` (system-wide) or `~/Library/LaunchAgents/` (per-user).

2. **Homebrew formula.** A `automaton.rb` formula in your own tap (`brew tap your-name/automaton`) so `brew install automaton` works. The formula installs the Python package, the plists, an example env file, and the example workflows. Standard Homebrew Python formula pattern — pull `pyyaml`, `httpx`, `croniter` as resource declarations.

3. **`deploy/macos/` directory** in the repo containing:
   - The three plists
   - An install/uninstall script (`install.sh`) that copies the plists, sets up paths, generates a token, and runs `launchctl load`.
   - A README explaining the install steps.

4. **Path defaults adjustment.** On macOS, conventional locations differ from Linux:
   - DB: `~/Library/Application Support/automaton/automaton.db` (per-user) or `/var/lib/automaton/` (system).
   - Logs: `~/Library/Logs/automaton/` or `/var/log/automaton/`.
   - The `AUTOMATON_DB` env var already lets users override; the install script just picks the convention for whichever mode (user or system) they choose.

### Verification

- Fresh macOS box: `brew install your-name/automaton/automaton` succeeds.
- `launchctl load ~/Library/LaunchAgents/com.automaton.worker.plist` and `launchctl list | grep automaton` shows all three running.
- Trigger a workflow, observe in the UI.
- Reboot the Mac, verify processes come back.
- Use `Console.app` or `log show` to confirm logs are flowing.

### Risk

Medium. launchd is well-documented but has its own conventions (UserName, GroupName, EnvironmentVariables vs EnvironmentFile — launchd doesn't natively read env files like systemd does, so the install script may need to translate the env file to inline plist Env entries). Solvable, just fiddly.

### Effort

1 week. Two days for plists and the install script, two days for the Homebrew formula and its tap repo, one day to test on a clean macOS install.

### Don't do this

- Don't try to build a `.pkg` installer or notarize anything. Homebrew is the right install vector for a CLI/server tool on macOS — fighting Gatekeeper for a launchd-managed CLI is wasted effort.

---

## Phase 3: Windows host support (effort: 2 weeks)

### What changes

1. **Service wrapping.** Windows doesn't have a one-to-one analog of systemd. Two reasonable paths:

   **3a. NSSM (recommended).** [NSSM](https://nssm.cc/) is a small, stable, MIT-licensed tool that wraps any binary as a Windows service. Ship a PowerShell install script that:
   - Downloads NSSM (or bundles it).
   - Registers three services: `automaton-worker`, `automaton-scheduler`, `automaton-ui`.
   - Configures them to read env vars from a config file.
   - Sets up dependency on the Windows Event Log for service logging.

   **3b. pywin32 + a custom service class.** Build a Python service using `win32serviceutil.ServiceFramework`. More native, more code, more failure modes. Skip unless NSSM has a specific problem you hit.

2. **MSI installer (optional but kind to users).** Use [WiX](https://wixtoolset.org/) or `cx_Freeze` to build an MSI. Bundles Python, the package, NSSM, the example workflows. Installs to `C:\Program Files\automaton\`. Skip if you're OK telling Windows users to install Python first and run `pip install` — that's the same UX as the Linux side.

3. **Path defaults.**
   - DB: `%APPDATA%\automaton\automaton.db` (per-user) or `%ProgramData%\automaton\` (system).
   - Logs: `%ProgramData%\automaton\logs\`.
   - Env file: `%ProgramData%\automaton\automaton.env`.

4. **`shell` step type.** Document that `cmd:` can be a string (interpreted by the platform's shell — `sh -c` on POSIX, `cmd.exe /c` on Windows) or a list (passed directly to `subprocess.run`). The list form is portable; the string form is OS-specific.

5. **WAL behavior on Windows.** SQLite WAL works on Windows for local-disk DB files. Document that putting the DB on a network share or removable drive is unsupported.

6. **Signal handling.** The systemd-style `KillSignal=SIGTERM` translates on Windows to the service stop event. NSSM handles this. Our processes need to respond to `SIGINT` (already do via KeyboardInterrupt) and on Windows also to `SIGBREAK`. One-line addition to the daemon loops.

7. **`deploy/windows/` directory.** PowerShell install/uninstall scripts, NSSM config files, a README with the install commands.

### Verification

- Fresh Windows 11 or Server 2022 box: install Python 3.11+, run `pip install -e .`, run install script.
- All three services show up in `services.msc` as Running.
- Reboot, confirm they come back.
- Pull logs from Event Viewer or the file log handler.
- Run the full test suite on Windows in CI.

### Risk

Medium-high. Windows is the platform with the most "different" semantics from where the system was designed. Real things to watch:
- File locking: SQLite holds shared locks. NTFS may behave differently than ext4 under pathological IO patterns.
- Path length limit (260 chars unless long paths enabled). Probably never hit, but worth documenting.
- The example workflows that use `/tmp/...` need a Windows alternate.
- Antivirus may flag the install script's NSSM download.

### Effort

2 weeks. One week for service wrapping + install scripts + Windows-specific tests, one week for an MSI if you want one. Allocate buffer for inevitable surprises.

### Don't do this

- Don't try to ship a single cross-platform installer that handles macOS / Linux / Windows. Each platform's install conventions are too different. Three separate, simple paths is right.

---

## Phase 4: TLS in the built-in UI server (effort: 3-5 days)

Today's posture: bind to localhost, expect the user to put nginx or Caddy in front for LAN/internet exposure. That's fine for Linux servers, but for a personal-infra goal across devices, you'll want to hit the API from your phone over Wi-Fi or from a laptop on another network. Pinning a reverse proxy on every host is friction.

### What changes

1. **`automaton serve` accepts `--tls-cert PATH --tls-key PATH`.** Use Python's stdlib `ssl.SSLContext.load_cert_chain` and wrap the `HTTPServer.socket` in the context. Roughly 10 lines.

2. **Self-signed cert helper.** `automaton tls init [--hostname H]` generates a private key and self-signed cert into `$AUTOMATON_HOME/tls/` using `cryptography` (already common, would add as optional dep). For a personal-infra scenario where you trust the cert manually, this is enough. Document how to install the generated cert on iOS / Android / macOS / Windows as a trusted root.

3. **ACME / Let's Encrypt path (optional).** If you ever expose this on the public internet:
   - Use `certbot` outside the process to provision certs.
   - Document the renewal cron / systemd timer.
   - Don't build an in-process ACME client. That's a half-baked rabbit hole.

4. **HSTS, secure cookie posture.** The UI doesn't use cookies today, so this is minimal. Add `Strict-Transport-Security` to all responses when TLS is on.

5. **Document the trade-off.** A self-signed cert + manual trust install on each device is fine for a personal setup; for anything beyond that, use a real cert from Let's Encrypt or an internal CA.

### Verification

- `automaton tls init --hostname automaton.local` produces cert + key.
- `automaton serve --tls-cert ... --tls-key ... --host 0.0.0.0` listens on 8080 (or 8443 — let the user pick).
- `curl --cacert tls/cert.pem https://automaton.local:8443/healthz` returns `{"ok": true}`.
- Browser visits work with the cert installed as trusted.
- The bearer-token auth and webhook HMAC logic are unchanged — TLS is orthogonal.

### Risk

Low. TLS is a solved problem. The main hazard is people misconfiguring (wrong key for the cert, expired cert, cert hostname mismatch) — surface diagnostics in the startup logs.

### Effort

3-5 days including the self-signed cert helper, install docs, and testing across the four target platforms.

---

## Phase 5: Remote access via mesh networking (effort: 2-3 days)

TLS (Phase 4) makes it safe to expose the API; this phase decides *how* you reach it from outside your LAN. The right answer for personal infrastructure is not "open port 8443 to the internet." It's "join all your devices to a private mesh."

### What changes

1. **Pick a mesh provider.** Tailscale (managed, free tier covers up to 100 devices) is the path of least resistance. Headscale (self-hosted Tailscale control plane, MIT licensed) is the right call if "I don't want a third party in the loop" matters to you — it's been stable for years and crossed 30k+ GitHub stars by early 2026. Devices still run the official Tailscale client; only the coordination plane moves to your box.

2. **Install the client on every host that runs automaton, and every client device** (phones, laptops). Each gets a stable `100.x.y.z` IP that works regardless of NAT, Wi-Fi changes, or being on cellular. Direct peer-to-peer adds roughly 1ms of latency; DERP-relayed paths add 10-50ms.

3. **Bind `automaton serve` to the Tailscale IP** (or `0.0.0.0` and rely on Tailscale ACLs to gate who can reach it). A short doc page in `deploy/mesh/` covers this for both providers.

4. **MagicDNS.** Tailscale (and Headscale 0.23+) lets you hit `https://automaton.your-tailnet.ts.net:8443` from anywhere — a stable hostname even when IPs renumber.

5. **Tailscale Serve / Funnel (optional).** Tailscale Serve auto-provisions a real Let's Encrypt cert; Funnel exposes it on the public internet if you ever want that. With Serve in the loop you can probably skip the self-signed-cert helper from Phase 4 entirely.

### Verification

- From a phone on cellular: `curl https://automaton.your-tailnet.ts.net:8443/healthz` returns `{"ok": true}`.
- ACL audit: only your devices, no accidental exit-node traffic, no Funnel unless explicitly enabled.
- Tailscale status shows the automaton host as `online` with a recent handshake.

### Risk

Low. The hardest decision is Tailscale vs Headscale, and that decision is reversible — the wire protocol is the same.

### Effort

2-3 days including the deploy guide and validating from a phone over cellular.

### Don't do this

- Don't open the UI port to the public internet just because TLS is enabled. The mesh approach is materially safer (no scanning, no auth-bypass attempts on your logs) and cheaper to operate.
- Don't build a custom VPN layer into automaton. WireGuard via Tailscale already exists and is better than anything you'd build.

---

## Phase 6: Secrets management (effort: 1 week)

Right now, plugin tokens, webhook signing keys, and downstream-service API tokens all live in env files. Fine when one person runs it on one box. For multi-host personal infra, each host should pull secrets from its OS keychain instead of from a plaintext file.

### What changes

1. **Adopt `keyring`.** The Python package wraps Windows Credential Manager (DPAPI-backed), macOS Keychain, and Linux Secret Service (libsecret / GNOME Keyring / KWallet) behind one API. Backend priority is `wincred > secret_service > macos_keychain`, picked at import time.

2. **`automaton secret set NAME` / `secret get NAME` / `secret rm NAME` / `secret ls`** CLI subcommands. Stores under the service name `automaton`. Cross-platform from day one.

3. **Spec-level reference.** Step definitions can reference `${secret:NAME}` instead of `${env:NAME}`. The engine resolves these at lease time, never at parse / persist time, so secret values never enter the SQLite event log or the run history. The logger redacts any value that originated from a secret reference.

4. **Migration path from env files.** `automaton secret import .env` reads an env file and copies all `AUTOMATON_SECRET_*` entries into the keychain. Leaves the file behind so the user can decide when to delete it.

5. **Headless Linux servers.** The Linux Secret Service expects a D-Bus session, which doesn't exist on a vanilla systemd box. Two options:
   - `keyrings.alt` — file-based encrypted-at-rest fallback, passphrase typed once or held in another env var. Easy, weaker.
   - `keyring-pass` — backed by GPG via the standard unix `pass` password manager. Better posture if you already use `pass`.

   Document both; default to `keyrings.alt` for ease.

### Verification

- `automaton secret set GITHUB_TOKEN` prompts, stores, and produces no plaintext on disk.
- A step referencing `${secret:GITHUB_TOKEN}` resolves at runtime; the value never appears in the event log.
- The same workflow YAML works unchanged on macOS, Linux, and Windows because `keyring` abstracts the backend.

### Risk

Medium. Hidden gotchas: the Linux Secret Service backend on headless servers fails opaquely without D-Bus, and macOS Keychain prompts for permission the first time a new process accesses it. The `keyrings.alt` fallback dodges both but is the weakest backend.

### Effort

1 week. Two days for CLI + resolver, two days for the env-file migration, one day for tests covering each backend, two days for docs.

### Don't do this

- Don't roll your own crypto. `keyring` plus the OS-native stores is the right shape.
- Don't put secrets in the workflow YAML itself, even encrypted. Reference-by-name is cleaner and keeps secrets out of version control.

---

## Phase 7: Notifications & alerting (effort: 1 week)

"Did my workflow finish?" is the question users ask most often when they can't tail logs. Today the answer is "log into the UI and look." This phase makes the engine push status to a channel of the user's choosing.

### What changes

1. **Reuse the existing webhook step type.** It already exists; you can build "tell me when a run fails" entirely in user-space by adding a webhook step at the end of each workflow. Document this pattern first — for many users it's enough.

2. **Engine-level notification hooks.** Two new env config entries:
   - `AUTOMATON_NOTIFY_ON_FAILURE=apprise://...` — fires on any run reaching terminal `failed`.
   - `AUTOMATON_NOTIFY_ON_TIMEOUT=apprise://...` — fires on `timed_out`.

   Fire-and-forget HTTP POST after the run finishes; failures to notify are logged but don't affect run state.

3. **Use Apprise as the dispatch layer.** [Apprise](https://github.com/caronc/apprise) is one Python package, 110+ notification backends. Set the URL once, change destinations without touching code: `ntfy://`, `pover://` (Pushover), `discord://`, `slack://`, `mailto://`, `gotify://`, `tgram://`, etc.

4. **Recommend a self-hosted ntfy as the default backend.** A `ntfy` instance runs in ~30 MB RAM. Subscribe from your phone (free apps on iOS/Android), the desktop, or via `curl`. Quickstart: `docker run -d -p 80:80 binwiederhier/ntfy serve`.

5. **Notification templates.** A short Jinja template per channel with `{{ run.id }}`, `{{ run.workflow }}`, `{{ run.status }}`, `{{ run.failed_step }}`, `{{ run.duration }}`. Sensible defaults ship; override per-workflow if you want.

6. **Quiet hours.** `AUTOMATON_NOTIFY_QUIET_HOURS=22:00-07:00` buffers non-critical notifications until morning. Workflows tagged `urgent: true` ignore quiet hours.

7. **Self-test command.** `automaton notify test` sends a hello message to every configured channel. Catches the "I had a typo in my URL and silently never got notifications" failure mode.

### Verification

- A failing test workflow triggers a phone push within seconds.
- Subscribing/unsubscribing in the ntfy app changes which devices buzz.
- Notification text includes a tappable link back to the run detail in the web UI.
- Quiet hours actually defer non-critical notifications.

### Risk

Low. Apprise is well-maintained; ntfy is simple. The main hazard is silent misconfig — the self-test command is the mitigation.

### Effort

1 week. Two days for the hook + Apprise integration, one day for the test CLI, one day for templates and quiet hours, three days for docs and testing across two or three backends.

### Don't do this

- Don't write a custom APNs / FCM integration at this layer. The phone-app phases (now 14 and 15) can talk APNs/FCM directly if you go that route; this layer stays channel-agnostic.

---

## Phase 8: Backup, restore, & disaster recovery (effort: 3-5 days)

Today `automaton backup` snapshots the SQLite file to a local path. That protects you from `rm -rf /var/lib/automaton`. It does not protect you from "the SSD died" or "the house burned down."

### What changes

1. **Litestream alongside automaton.** [Litestream](https://litestream.io/) streams SQLite WAL frames continuously to S3 / Backblaze B2 / SFTP / NFS. Recovery point objective drops from "last cron snapshot" to "under a second." Add a `deploy/litestream/litestream.yml` template plus install docs.

2. **Off-host backup target.** Recommend Backblaze B2 (~$5/TB/mo) or rsync.net via `sftp://`. Both work with the same Litestream config. The S3 path also covers MinIO if the user runs their own object store.

3. **Restore drill in CI.** A new test that (a) sets up Litestream against a temp dir, (b) runs some workflows, (c) deletes the live DB, (d) restores from Litestream, (e) asserts run history is intact and in-flight runs resume cleanly. Catches the "we had backups but never restored from them" failure mode.

4. **`automaton restore --from PATH`** command. Wraps `litestream restore`. Refuses to clobber an existing DB without `--force`. Verifies the restored schema version matches the engine's expected version (uses the migrations from Phase 9).

5. **Recovery runbook.** `docs/runbooks/restore.md` — step-by-step for corrupt DB, full host loss, and accidental `DELETE` from the API. Include rough RTO estimates.

6. **Sanity-check the existing snapshot backups.** `automaton backup` stays for users who don't want to run Litestream. Add a `PRAGMA integrity_check` to its output so silent corruption gets caught at backup time, not restore time.

### Verification

- Kill `automaton` mid-run, then `litestream restore` and start fresh. In-flight runs complete correctly via lease timeout + retry — no duplicates.
- CI restore drill passes.
- A "host loss" scenario can be walked through end-to-end on a clean VM in under 30 minutes following the runbook.

### Risk

Medium. Backups are easy; restores are the actual product, and people get them wrong by not testing. The CI drill is the mitigation.

### Effort

3-5 days. One day for the Litestream config + docs, one day for the restore command, two days for the CI drill and runbook.

### Don't do this

- Don't run Litestream and `automaton backup` *both* on the same DB without coordinating WAL checkpointing. Pick one. (Litestream is the better choice for anything beyond a single box.)
- Don't put Litestream's checkpointing into the engine itself. Litestream runs as a separate process; that boundary is the right one.

---

## Phase 9: Schema migrations (effort: 2-3 days)

Today the schema lives in `schema.sql`, applied at first-run. Subsequent changes require a hand-rolled `ALTER TABLE`. Fine for one user who is also the developer. Not fine for "personal infra you trust over years."

### What changes

1. **Adopt `yoyo-migrations`.** Pure SQL files in `automaton/migrations/`, each prefixed with a version (e.g. `0001-initial.sql`, `0002-add-cancel-status.sql`). Yoyo tracks applied versions in a `_yoyo_migration` table inside the DB.

2. **`automaton migrate`** subcommand. Idempotent. Refuses to run if it can't read the current schema version (e.g. you ran an older binary against a newer DB).

3. **Auto-migrate on startup, gated.** `AUTOMATON_AUTO_MIGRATE=true` (default `false`) makes worker/scheduler/UI run pending migrations before starting. Off by default because automatic schema changes on a multi-host setup are how data gets corrupted — the user should run `automaton migrate` deliberately on a primary, then start workers.

4. **Convert today's `schema.sql` into `0001-initial.sql`.** Existing installs don't have a `_yoyo_migration` table. The first run of `automaton migrate` detects this case, inserts the row for 0001 retroactively, and proceeds with anything newer. One-time forward-compat shim.

5. **Pre-migration backup.** `automaton migrate` snapshots the DB to `automaton.db.pre-migrate-{ts}` before doing anything. Easy rollback if a migration goes sideways.

6. **Tests for each migration.** Every new migration ships with a test that starts from the previous schema's DB seeded with realistic data, applies the migration, and asserts the new schema is correct and the data is intact.

### Verification

- `automaton migrate` on a fresh DB applies all migrations and is idempotent.
- `automaton migrate` on an existing pre-yoyo DB does the shim then applies anything new.
- The pre-migration backup file appears next to the live DB.
- Tests cover each migration's data preservation.

### Risk

Low to medium. The "old DBs without yoyo tracking" shim is the trickiest piece; get it right once and the rest is mechanical.

### Effort

2-3 days. Half a day for the yoyo integration, one day for the shim and pre-migrate backup, one day for tests and docs.

### Don't do this

- Don't use Alembic. It's great if you're using SQLAlchemy, which automaton isn't. Yoyo's SQL-first model fits the existing codebase.
- Don't hand-roll a `PRAGMA user_version` system. It works, but yoyo costs no more and gives you better ergonomics.

---

## Phase 10: Time, timezone, & DST correctness (effort: 2-3 days)

`croniter` parses cron expressions; the scheduler runs them in the host's local time. On a single Linux box in one timezone this is invisible. On multi-host setups, when a user travels, or when DST changes, "every day at 2:30am" silently does the wrong thing.

### What changes

1. **All cron expressions interpret in UTC by default.** Add a per-workflow `timezone:` field (IANA name like `America/Los_Angeles`); croniter has timezone support, just have to plumb it through.

2. **Store cron expressions and next-fire timestamps as UTC** in the DB. Convert at render time when showing in the UI.

3. **DST handling.** "Every day at 2:30am US/Pacific" fires twice on fall-back day and zero times on spring-forward day under naive `crontab` semantics. Document this explicitly. `croniter`'s `croniter_range` with `expand_from_start_time=True` handles it correctly; default to it.

4. **Workflow-level timezone validation.** The `validate_spec` helper added in the previous round should refuse unknown IANA timezones with a specific error message.

5. **`automaton scheduler next [WORKFLOW]`** debug command — prints the next 10 fire times in both the configured timezone and UTC. Surfaces DST surprises before they bite.

6. **UI timezone display.** Browser local time + (UTC) in parens, consistently. No surprises.

### Verification

- A workflow scheduled "every Sunday 03:00 America/New_York" fires correctly across both spring-forward and fall-back transitions in a test that fast-forwards `datetime.now`.
- The same workflow exported and re-imported on a host in Asia/Tokyo behaves identically.
- `automaton scheduler next` during DST week shows the expected ten timestamps.

### Risk

Low (it's a correctness problem, not a system-design one), but the bugs are silent and embarrassing. Mitigation: explicit tests for DST transitions and a documented timezone-semantics page.

### Effort

2-3 days. One day for the timezone field + UTC storage, half a day for validator changes, half a day for the debug command, one day for tests.

### Don't do this

- Don't store cron expressions as local-time strings. The number of timezones a single workflow might travel through over its lifetime is greater than zero.
- Don't try to handle leap seconds. Nobody needs that for personal automations.

---

## Phase 11: Web UI mobile responsiveness (effort: 1-2 weeks)

The current `automaton serve` UI is a thin HTML render of the API. Usable on a laptop, painful on a phone. A responsive pass makes "I just want to glance at runs from my couch" work without committing to native mobile apps (Phases 14/15).

### What changes

1. **Tailwind via CDN, no build step.** Standard `<script src="https://cdn.tailwindcss.com"></script>` in the layout, semantic markup, mobile-first breakpoints. The UI server is Python stdlib — keeping the frontend zero-build is consistent.

2. **Three responsive screens** matching the planned native apps: runs list, run detail, workflows + trigger. Card layout on phone, table on desktop, same template.

3. **Add-to-home-screen (PWA shell).** A minimal `manifest.json` and a one-line service worker (offline-shows-cached-runs-list). Lets you "install" the web UI on a phone and it looks like an app. ~30 lines, very high polish-vs-effort ratio.

4. **Server-Sent Events for live updates.** Replace run-detail polling with `EventSource`. The Python `http.server` can stream by holding the response open and writing `data: ...\n\n` frames. Cleaner UX, less server load than 2s polling from five tabs.

5. **Auth tweak for browser use.** Today's bearer token goes in `Authorization` — easy from `curl`, awkward from a browser bookmark. Add a `?token=` query-string fallback for GETs only (with a startup warning that this leaks the token into web server logs), or do a real session-cookie sign-in with a one-time-token URL. Pick one before the responsive pass.

### Verification

- Runs list usable on iPhone SE width (320 logical px).
- "Install to home screen" on iOS Safari and Android Chrome produces a working app icon.
- SSE updates appear within ~1s of a real run transition.

### Risk

Low to medium. SSE through Python's stdlib `http.server` is the one piece with a real complexity tail — `http.server` was not designed for long-lived connections. If it bites, switch the UI to `uvicorn`/`starlette` for that endpoint. One added dependency, not a rewrite.

### Effort

1-2 weeks. Two days for the responsive pass, two days for the PWA shell, three days for SSE + the auth tweak, two days for cross-browser testing.

### Don't do this

- Don't introduce React or Vue here. The UI's job is to render four screens; a build pipeline is overkill.
- Don't skip this and go straight to native apps if you're not sure you want to maintain two phone codebases — a good responsive web app covers 80% of the use cases at 10% of the long-term cost.

---

## Phase 12: Workflow templates library (effort: 1 week)

The engine is general-purpose. The empty-template experience today is "open up the YAML reference and start typing." A curated library of templates collapses time-to-first-useful-workflow from hours to minutes.

### What changes

1. **`templates/` directory** in the repo with categorized starters:
   - `backup/home-folder.yaml` — rclone/restic to B2, daily.
   - `health/website-up.yaml` — curl + alert via Phase 7 notifications.
   - `health/cert-expiry.yaml` — checks your TLS certs weekly, alerts at 14 days.
   - `dev/git-mirror.yaml` — mirrors GitHub repos to a local NAS nightly.
   - `dev/docker-prune.yaml` — weekly system cleanup with a notify-summary.
   - `agent/claude-loop.yaml` — wait_for_signal pattern showing the agent loop end-to-end.
   - `media/photo-import.yaml` — moves new photos off an SD card, organizes by date.
   - `infra/letsencrypt-renew.yaml` — certbot wrapper with notifications.
   - `infra/log-rotation.yaml` — rotates the engine's own logs.
   - `personal/morning-brief.yaml` — calendar + weather + RSS digest piped to notify.

2. **`automaton init NAME [--template TEMPLATE]`** subcommand. Picks a template, copies it to `./<name>.yaml`, replaces placeholder values (paths, URLs, channels) interactively.

3. **Template README in each file's leading comment.** What it does, what variables it expects, what secrets it needs (using Phase 6 references), what host permissions.

4. **`templates/INDEX.md`** with one-line descriptions tagged by category. Generated from a small script, regenerated in CI.

5. **CI validation.** Every template runs through `validate_spec` and is parsed in CI. Catches "I changed the YAML schema and forgot to update templates."

### Verification

- `automaton init backup --template backup/home-folder` produces a working YAML in the current directory.
- Every shipped template validates cleanly.
- Following the README in one of the templates produces an actually-working workflow on a clean install.

### Risk

Low. The hazard is template rot — they atrophy unless someone tests them periodically. CI validation covers schema, not behavior; add a one-line note to each template's leading comment with the date of last manual verification.

### Effort

1 week. Most of this is taste and docs work; the `init` command itself is half a day.

### Don't do this

- Don't ship templates that pull heavy external dependencies (Ansible, Docker Compose stacks) without flagging it loudly. The point of a template is "this works after I save the file."
- Don't try to ship a hundred templates. Ten well-chosen, well-tested ones beat a hundred bit-rotted ones.

---

## Phase 13: Performance & scale ceiling testing (effort: 3-5 days)

Before scaling to multi-host, you need to know what the single-host ceiling actually is. Today nobody knows. This phase fixes that with a few load tests and a documented operating envelope.

### What changes

1. **`tests/load/` directory** with three scripts:
   - **Steady state.** N workflows queued/sec, M workers, for T minutes. Records p50/p99 lease wait, p50/p99 step duration, queue depth over time, DB size growth, RAM/CPU.
   - **Burst.** 10,000 workflows enqueued in one second; measure drain time.
   - **Long-tail.** A mix of short (10ms) and long (10s) steps to see if either starves the other.

2. **SQLite hardening verification.** Confirm `PRAGMA journal_mode = WAL`, `synchronous = NORMAL`, `busy_timeout = 5000` (the documented production minimum is 3-5 seconds), and `wal_autocheckpoint = 1000`. Assert these at startup so a misconfigured DB doesn't ship to production silently.

3. **Write serialization (if needed).** SQLite WAL allows concurrent reads, but only one writer at a time. If load tests show write contention, add an application-level queue funneling all writes through a single thread. Documented in the README under "scale limits."

4. **Documented operating envelope.** A short table in `docs/scale.md`:
   - Steady-state runs/sec sustainable on a 4-core / 4 GB box.
   - Maximum useful worker count.
   - DB size at which compaction (vacuum, pruning) becomes mandatory.
   - The point at which Phase 16 (Postgres / multi-host) deserves consideration.

5. **Regression alarm in CI.** Run a minimal load test on every PR; fail if p99 degrades by more than 50% versus main.

### Verification

- All three load scripts run to completion on a clean Linux box and produce a comparable report.
- The documented envelope numbers are based on actual measurements, not guesses.
- The CI regression alarm catches a deliberately-introduced regression on a test PR.

### Risk

Low. The risk you're *trying* to mitigate is "we didn't know our ceiling, hit it under real load, and panicked."

### Effort

3-5 days. One day per script, one day for the doc + CI alarm.

### Don't do this

- Don't optimize before measuring. The load tests exist to find real bottlenecks, not to validate guesses about where they are.
- Don't scale to multi-host because the load tests showed a number you didn't like — verify the workload that's actually going to run, not a synthetic one.

---

## Phase 14: iOS client (effort: 3-4 weeks)

iOS can't run automaton itself. What it can do is be a great client — push notifications when runs complete, a dashboard view of recent runs, a one-tap "trigger workflow" button, the ability to send signals to parked runs (i.e. respond to the "agent" loop from the design doc).

### What changes

1. **SwiftUI app.** Native, runs on iPhone and iPad. Single target, single bundle.

2. **API client in Swift.** Mirrors the Python `AutomatonClient`. URLSession-based, async/await, Codable structs matching the JSON API. ~500 lines.

3. **Settings screen.** Server URL + bearer token + cert pinning (optional, for self-signed certs). Stored in Keychain.

4. **Three core screens:**
   - **Runs**: list of recent runs, color-coded by status, pull-to-refresh, tap into detail.
   - **Run detail**: step tree, event log, output of each step. Live updates via polling (`GET /api/run/<id>` every 2s while status is running/pending).
   - **Workflows**: list of registered workflows with a "Trigger" button each. Optional payload editor.

5. **Push notifications (later).** Two paths:
   - **Pull**: app polls /api/runs in background. Doesn't really work — iOS aggressively suspends background apps.
   - **Push**: automaton emits push notifications via APNs when runs reach terminal state. Requires the server to hold an APNs key. Real work — add only if you want it.

6. **A "respond to signal" UX.** If a run has a parked `wait_for_signal` step, the run detail screen shows a "Respond" button. Tap → small payload editor → `POST /api/signals/<run_id>/<name>`. This is the iOS half of the agent loop.

7. **Distribution.**
   - **TestFlight** for personal use — sign with your $99/year Apple Developer account, install on your own devices and up to 100 testers. Sufficient for personal infra.
   - **App Store** only if you intend to make it public. Don't bother for personal use.
   - **AltStore / sideloading** if you don't want the developer account.

### Verification

- App connects to your automaton server over TLS.
- All three core screens work.
- Triggering a workflow from the app produces a run visible in the web UI.
- Responding to a signal from the app resumes a parked workflow.

### Risk

Medium. iOS development isn't hard for an experienced engineer but has its own learning curve. The biggest unknowns are Apple's app-review process (irrelevant for personal TestFlight builds) and APNs setup (if you do push).

### Effort

3-4 weeks for a functional client app. 1 week if you accept "polling, no push, no offline mode." 8 weeks if you go full polish.

### Don't do this

- Don't write this in React Native, Flutter, or Capacitor. For a personal-infra app that mostly displays lists and forms, native SwiftUI is faster to build and ship than wrestling a JS framework's iOS quirks.

---

## Phase 15: Android client (effort: 3-4 weeks)

Same shape as iOS. The hard parts and the easy parts are mostly mirror-imaged.

### What changes

1. **Jetpack Compose app.** Kotlin, single module.

2. **API client in Kotlin.** Use `OkHttp` + `kotlinx.serialization`. ~500 lines mirroring the Python client.

3. **Same three screens.** Runs list, run detail, workflows list with trigger.

4. **Background updates** are *much* easier on Android than iOS — WorkManager + a periodic ping is sufficient for a "did my workflow finish" use case without dealing with Firebase Cloud Messaging.

5. **Push notifications (later).** FCM if you want them. Same trade-off as APNs: real work, only if you actually need it.

6. **Distribution.**
   - **APK sideload** for personal use. Build with Gradle, copy to phone, install. Free and unrestricted.
   - **Play Store internal testing track** if you want auto-updates without going public.
   - **Play Store production** only if you intend to make it public.

7. **A note on Android's "trusted certs" story.** Android is *more* willing to trust user-installed certs than iOS in some contexts. For a self-signed cert install across devices, Android is slightly easier. Document the install steps.

### Verification

Same as iOS.

### Risk

Lower than iOS, because the distribution story is friendlier (no $99/year required) and you can sideload freely.

### Effort

3-4 weeks. Or 1 week for the minimum-viable version.

### Don't do this

- Don't pick Kotlin Multiplatform Mobile (KMM) hoping to share code with iOS. The "shared business logic" pitch sounds good but the JSON-API-client surface area is tiny — the savings don't justify the build-system complexity. Two separate apps, ~500 LOC of API client each, is the right shape here.

---

## Phase 16 (speculative): Real infrastructure scale

Only do this if you genuinely outgrow the single-host design. For a personal-infra deployment running your household automations and agent workflows, you almost certainly don't.

### What "real infrastructure" implies

1. **High availability.** Worker processes on multiple hosts. The current scheduler-via-DB-row leader-election extends to multi-host trivially because the lock row is in the DB; SQLite over NFS is NOT okay, so you need a real DB.

2. **Postgres backend.** ~2 weeks of work. The SQL is mostly portable, but the lease pattern changes from "BEGIN IMMEDIATE + WHERE leased_until < ..." to `SELECT ... FOR UPDATE SKIP LOCKED`. Schema is straightforward. The hard part is migrating data and writing a parallel test matrix for both backends.

3. **Metrics endpoint.** Add `/metrics` in Prometheus exposition format. Expose: runs by status, queue depth, lease age histogram, signal age histogram, scheduler lock contention. ~2 days.

4. **Distributed tracing.** OpenTelemetry hooks around lease, execute, commit. ~3 days. Whether you actually use it depends on whether you run a tracing backend.

5. **Multi-tenant identity.** A `user` table, API tokens per user, RBAC. ~1 week. Only if you have multiple users. For personal infra you don't.

6. **Backup story for Postgres.** WAL archival to S3, point-in-time recovery. Already a solved Postgres problem; just document it.

### Honest take

If you find yourself reaching for any of these — stop and ask whether you've outgrown a "personal automation platform" and now want a "production workflow engine." If yes, the right answer is to migrate workflows to **Temporal** (still self-hostable, open source, designed for this exact shape, vastly more battle-tested) rather than building Phase 16 of an in-house thing.

The decision tree:
- Less than 100 runs/day, one user, one or two hosts → automaton as is.
- 100-1000 runs/day, one or two users, two or three hosts → automaton with Postgres + multi-worker.
- More than 1000 runs/day, real team, real SLAs → Temporal.

### Effort

If you do all of Phase 16: 6-8 weeks. Don't do all of it; pick the pieces you need.

---

## Phase 17 (speculative): Distribution & polish

Only do this if you want others to use automaton, not just yourself.

1. **PyPI publish.** `automaton-engine` or similar package name (`automaton` is taken). Stable version, semver, changelog. ~2 days when ready.

2. **Docker image.** Multi-stage build with a slim final image. ~1 day.

3. **Helm chart for Kubernetes.** Only if you run k8s. Skippable.

4. **Documentation site.** mkdocs-material is the sweet spot. Hosts the design doc, the readiness doc, the deployment guides, the API reference. ~1 week.

5. **Stable contract.** Pin the HTTP API, document deprecation policy, version the workflow YAML schema.

Don't bother unless you have actual users beyond yourself.

---

## Order of operations recommendation

If you want this to be your personal infrastructure across all your devices, here's the order. The grouping matters more than the exact sequence within a group — but do the foundations before the things that depend on them.

**Foundations (about 2 weeks total).** Do these first; nothing later works well without them.

1. **Phase 1 (1-2 days)** — portability audit + CI matrix. Cheap, prevents bugs, prerequisite for everything else.
2. **Phase 9 (2-3 days)** — schema migrations. Do this *before* any new feature work that touches the DB, so you don't accumulate hand-rolled `ALTER TABLE`s you have to retire later.
3. **Phase 4 (3-5 days)** — TLS. Do this *before* the mobile clients so you don't develop them against unencrypted HTTP and then have to redo the cert pinning.
4. **Phase 5 (2-3 days)** — mesh networking. Establishes the secure reach-from-anywhere story before you build the things that need it.

**Operational maturity (about 4 weeks total).** Make it survivable for years, not just runnable today.

5. **Phase 6 (1 week)** — secrets management. Stop carrying plaintext tokens between hosts.
6. **Phase 7 (1 week)** — notifications. The "did it work?" question gets a real answer.
7. **Phase 8 (3-5 days)** — backup, restore, DR. Litestream + a tested restore drill.
8. **Phase 10 (2-3 days)** — time/timezone correctness. Quietly important, easy to forget.
9. **Phase 13 (3-5 days)** — load tests. Know your ceiling before you hit it.

**Cross-platform hosts (1-3 weeks depending on which you pick).** Add platforms you actually use.

10. **Phase 2 (1 week)** — macOS host, if you have a Mac in the loop.
11. **Phase 3 (2 weeks)** — Windows host, only if you want to automate something *on* the Windows box. Otherwise skip; remote access via the web UI / mobile app covers most uses.

**User experience (about 2 weeks total).** Make the engine pleasant to live with.

12. **Phase 11 (1-2 weeks)** — responsive web UI + PWA shell. This alone may make you decide you don't need native apps yet.
13. **Phase 12 (1 week)** — workflow templates library. Pays off the first time you set up a new use case.

**Native mobile apps (4-8 weeks total, optional).** Do these if and only if the responsive web UI didn't satisfy you.

14. **Phase 14 or 15 (3-4 weeks)** — pick whichever phone you actually have. Build for *your* device first; the second app is much faster once the API client patterns are established.
15. **Phase 15 or 14 (1 week)** — the second mobile client, leveraging what you learned.

Skipping Phase 16 and Phase 17 entirely is the right call for personal infrastructure.

**Total scoped to "all my devices, personal infra":**

- Foundations + operational maturity + macOS + responsive UI + templates: **~9-10 weeks**.
- Add native iOS + Android: **~13-18 weeks** depending on polish target.
- Add Windows: **+2 weeks**.
- All of the above, no skips: **~15-20 weeks** of focused part-time work.

The honest sequencing: most of the value lands by week 10. The mobile clients and Windows host are the long tail; defer them unless you know you want them.

---

## What to verify before each phase

A standing checklist. Run it at the end of every phase, not just before starting a new one.

1. The full test suite (currently 68 tests, growing) passes on every platform that phase touches.
2. You can do the full agent loop end to end from the affected platform (register → trigger → wait → signal → complete).
3. The deploy artifacts for that platform survive a reboot.
4. Backups still work — and, once Phase 8 lands, the restore drill in CI still passes.
5. Migrations apply cleanly to a fresh DB *and* to a DB representative of your real install — once Phase 9 lands.
6. Notifications fire on a deliberate failure — once Phase 7 lands.
7. Logs end up somewhere you can find them, and they don't leak secrets.

If any of these fail, stop and fix before moving on. The cost of unwinding later compounds quickly.

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Hidden POSIX assumption surfaces on Windows | Medium | Medium | Phase 1 + CI matrix |
| launchd plist subtlety I didn't predict | Low | Low | Test on a clean macOS box, allocate buffer |
| TLS cert misconfiguration on a device | Medium | Low | Document the install per platform; the self-signed-cert flow is well-trodden |
| iOS app review issues | High (if you go App Store) | Low (TestFlight bypasses it) | Don't ship to App Store unless you have to |
| Trying to do Phase 16 prematurely | Medium | High (wasted weeks) | Decision tree above; if in doubt, use Temporal |
| Building mobile clients in React Native and regretting it | Medium | High | Native, both platforms, no exceptions for this kind of app |
| Self-hosting public-internet exposure compromises the engine | Medium | Critical | Real cert + bearer token + locked-down host. Don't expose the UI without these |
| Linux Secret Service fails silently on headless servers (Phase 6) | Medium | Medium | Default to `keyrings.alt`; document the D-Bus prerequisite for the native backend |
| Notifications appear to work but go nowhere (typo'd URL, Phase 7) | Medium | High (you'll miss real failures) | `automaton notify test` self-test command, run as part of every deploy |
| Backups exist but restore was never tested (Phase 8) | High by default | Critical | CI restore drill; a manual host-loss walkthrough on a clean VM |
| Schema migration applied to wrong DB / wrong direction (Phase 9) | Low | High | Pre-migrate snapshot; refuse to run if schema version mismatches binary |
| DST transition silently skips or duplicates a critical job (Phase 10) | Medium | Medium-High | UTC storage + explicit DST tests; `automaton scheduler next` debug output |
| SSE through stdlib `http.server` deadlocks under load (Phase 11) | Low | Medium | Bail out to uvicorn/starlette for the SSE endpoint if it bites |
| Template rot — shipped workflows break against new schema (Phase 12) | High over time | Low | CI parses every template on every PR; "last verified" date in each comment |
| Optimizing before measuring (Phase 13) | Medium | Medium | Load tests *before* refactoring; document the workload assumptions |
| Mesh provider lock-in (Phase 5) | Low | Low | Tailscale and Headscale are wire-compatible; switching is a config change |

---

## What this plan does NOT do

It does not:
- Build a SaaS version of automaton (would require multi-tenant identity, billing, support).
- Build IDE integrations or workflow visual editors.
- Add a workflow versioning UI beyond what `register` already does.
- Replace the design doc's "use Temporal for real production" recommendation.

If any of those become priorities, they're separate plans.

---

## Decision points the plan deliberately leaves to you

1. **Do you want push notifications?** Without push, the mobile apps work fine for "open the app, see what's happening." With push, you get "your phone buzzes when a run finishes." Push roughly doubles the per-app effort (APNs / FCM setup, server-side notification dispatch). Note that Phase 7 (engine-level notifications) gets you 80% of the buzz-when-it-fails value via ntfy or Pushover *without* per-app push code — strongly consider doing that and skipping per-app push unless you genuinely need it.

2. **Do you want Windows host support, or just Windows client?** If your Windows box is just where you sometimes use Chrome to look at the web UI, you don't need Phase 3. If it's a machine you want to automate (sync files, run scripts on schedule, etc.) then yes.

3. **Cloud-hosted or home-hosted?** If you host this on a VPS, TLS via Let's Encrypt + a real DNS name is easier than self-signed certs. If you host it at home behind your router, Phase 5's mesh networking + a self-signed cert (or Tailscale Serve's auto-issued cert) is fine. The plan supports both.

4. **Tailscale or Headscale (Phase 5)?** Tailscale is the path of least resistance; Headscale is the right call if you don't want a third-party in the control plane. They're wire-compatible; the decision is reversible.

5. **Native mobile apps, or just the PWA (Phases 11 vs 14/15)?** A polished responsive web app installed to home screen handles the "look at runs, trigger a workflow" use case for most people. Native apps are worth it for: tight push-notification control, Apple Watch / Wear OS surfaces, offline editing of YAML specs. If none of those apply, ship the PWA and stop.

6. **Mesh provider or public exposure?** Phase 5 strongly recommends never opening the UI to the public internet. The only scenario where public exposure makes sense is if you're hosting on a VPS *and* you've layered a real cert + a WAF in front. Even then, mesh is simpler and safer.

7. **One token or per-user tokens?** Phase 16's identity work matters only if you have multiple humans using this. Probably no.

8. **How do you handle the secrets-on-headless-Linux gap (Phase 6)?** `keyrings.alt` (file-based, easy, weaker) vs `pass`-backed GPG (better posture, more setup). The plan defaults to `keyrings.alt`; pick differently if you already use `pass`.

9. **Litestream target (Phase 8)?** Backblaze B2 (cheapest), rsync.net (SFTP, no API keys to leak), or self-hosted MinIO (no third party). Pick based on your threat model and budget.

10. **Cancel Phase 16 entirely.** Strong recommendation: yes, unless you discover a specific concrete need that the single-host shape can't meet. The decision tree in Phase 16 is your check.
