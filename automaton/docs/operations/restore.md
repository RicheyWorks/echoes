# Restore runbook

Three scenarios you actually run into, in order of likelihood. Each has
a rough RTO (recovery time objective) so you know whether to expect
this to take 5 minutes or an hour.

For continuous-replication setup see
[`deploy/litestream/`](../../deploy/litestream/). For the underlying
`backup.snapshot` semantics see [`BACKUP.md`](../../BACKUP.md).

## Scenario A: corrupt or accidentally-deleted DB on a live host

**Symptom.** `automaton` commands fail with `database disk image is
malformed`, `automaton.db` is missing, or `PRAGMA integrity_check`
reports errors.

**RTO.** ~2 minutes if Litestream is in the loop, ~5 minutes if you're
restoring from `automaton backup` snapshots, plus whatever it takes for
any in-flight runs to retry their leases.

**Steps.**

```bash
# 1. Stop everything that writes to the DB
sudo systemctl stop automaton-worker automaton-scheduler automaton-ui

# 2. Move the bad DB aside (don't delete - it might be salvageable
#    with `sqlite3 .recover` later if you really need it)
sudo mv /var/lib/automaton/automaton.db /var/lib/automaton/automaton.db.bad-$(date +%Y%m%d-%H%M%S)

# 3a. Restore from Litestream (preferred - sub-second RPO)
sudo litestream restore -o /var/lib/automaton/automaton.db.copy /var/lib/automaton/automaton.db
sudo -u automaton automaton restore /var/lib/automaton/automaton.db.copy

# 3b. Or restore from an automaton backup snapshot
sudo -u automaton automaton restore /var/backups/automaton/latest.snap

# 4. Apply any pending migrations (e.g. if the binary moved forward
#    while the DB was offline)
sudo -u automaton automaton migrate

# 5. Restart the workers
sudo systemctl start automaton-worker automaton-scheduler automaton-ui

# 6. Verify
automaton inspect | head             # recent runs visible?
curl http://localhost:8080/healthz   # UI responding?
automaton mesh status                # mesh + port reachable?
```

**What to verify.**

- `automaton restore` prints `integrity_check (destination): ok`.
- The schema version it reports matches what you expect (e.g. matches
  the binary version you're running).
- The first run after restore reaches `completed` (parked runs resume
  on signal as expected).

**Failure modes to watch.**

- *Integrity check fails on the source.* Your snapshot is bad. Try the
  previous snapshot in your replica's history (`litestream snapshots`
  lists them).
- *`automaton restore` refuses to clobber.* It's a guardrail. Pass
  `--force` after you've moved the existing DB aside.
- *Workers come up but immediately exit.* You skipped step 4. Run
  `automaton migrate` and start them again.

## Scenario B: complete host loss

**Symptom.** The machine running automaton is gone (disk failure,
fire, "I gave away that laptop"). You have a fresh box and you need to
get the engine back online with all run history intact.

**RTO.** ~30 minutes including OS install + automaton install. The
restore itself is ~2 minutes; the rest is plumbing.

**Steps.**

```bash
# On the new host

# 1. Install automaton (the engine + CLI)
pip install -e .

# 2. Install Litestream
curl -fsSL https://github.com/benbjohnson/litestream/releases/latest/download/litestream_*linux_amd64.deb -o /tmp/litestream.deb
sudo dpkg -i /tmp/litestream.deb

# 3. Drop the same Litestream config in place (litestream.yml + the
#    creds for whichever replica you used). Same bucket, same path.
sudo cp /your/source/of/truth/litestream.yml /etc/litestream.yml
sudo cp /your/source/of/truth/litestream.env /etc/litestream.env
chmod 600 /etc/litestream.env

# 4. Restore the DB from the replica
sudo mkdir -p /var/lib/automaton
sudo litestream restore -o /var/lib/automaton/automaton.db.copy \
  /var/lib/automaton/automaton.db

# 5. Run the verified restore (integrity_check + schema version)
sudo -u automaton automaton restore /var/lib/automaton/automaton.db.copy

# 6. Migrate forward if the binary is newer than what shipped to the replica
sudo -u automaton automaton migrate

# 7. Install the systemd units and start everything
sudo cp deploy/systemd/*.service /etc/systemd/system/
sudo cp deploy/automaton.env.example /usr/local/etc/automaton/automaton.env
sudo systemctl enable --now litestream automaton-worker automaton-scheduler automaton-ui
```

**What to verify.**

- The set of registered workflows on the new host matches the old one
  (`automaton schedule list`).
- Run history is intact (`automaton inspect` should show old run IDs).
- A test workflow registers + triggers + completes cleanly.
- Litestream is replicating to the same replica (`sudo litestream
  replicas /etc/litestream.yml`) - you want continuous backup on the
  new host too.

**Failure modes.**

- *Replica is empty.* The original host wasn't actually replicating.
  Recover from whatever local snapshots you had instead, accept the
  data loss, and fix the replication on the new host.
- *Schema mismatch.* The new host's binary expects newer migrations
  than the snapshot. `automaton migrate` is the answer (already in
  step 6).
- *Step 5 reports pending migrations.* See above.

## Scenario C: accidental DELETE via the API or CLI

**Symptom.** Someone cancelled the wrong run, or `automaton prune`
deleted too much. The DB itself is fine but the data you want is gone.

**RTO.** ~15 minutes including the point-in-time restore lookup.

**Steps.**

This is the only scenario where you don't restore into the live DB -
you restore to a sidecar so you can copy the data out without
clobbering anything that's happened since.

```bash
# 1. Find a snapshot that predates the deletion. Litestream keeps
#    timestamped snapshots:
sudo litestream snapshots /var/lib/automaton/automaton.db
# Pick the timestamp you want (the most recent one before the bad
# DELETE).

# 2. Restore to a sidecar path
sudo litestream restore -timestamp '2026-05-19T14:00:00Z' \
  -o /tmp/automaton.snapshot.db \
  /var/lib/automaton/automaton.db

# 3. Diff what's in the snapshot vs what's currently live
sqlite3 /tmp/automaton.snapshot.db \
  "SELECT id, status FROM run WHERE id NOT IN (SELECT id FROM \
     dbattached.run); ATTACH DATABASE '/var/lib/automaton/automaton.db' AS dbattached;"
# (Use whatever query matches the data shape you need to recover.)

# 4. Copy the missing rows back in. The exact SQL depends on what
#    was lost; refer to the schema for the right tables and FKs.
#    Generally: cancel any active runs of the same workflow first,
#    then INSERT OR IGNORE the historical rows.
```

**What to verify.**

- The restored rows have unique IDs in the live DB (or you've reassigned
  them).
- Run history is consistent: every `step` row's `run_id` references a
  `run` that exists in the live DB.
- The event log isn't internally inconsistent (event IDs are
  monotonically increasing per run).

**This scenario is genuinely awkward.** If you find yourself doing it
regularly, that's a signal to add per-user write tokens, an audit
trail, or a soft-delete pattern. See Phase 16 of
`PLATFORM-EXPANSION-PLAN.md`.

## A note on Litestream vs `automaton backup`

Don't run both against the same DB. Both tools coordinate WAL
checkpointing and the interaction is undefined. Pick one:

- **Litestream.** The right answer for anything beyond "I have one
  laptop." Continuous off-host replication, sub-second RPO.
- **`automaton backup`.** Cron'd snapshots to a path. Easier to set up,
  fine for a single-host personal deployment if you're OK with losing
  up to a day. Keep the snapshots on a different disk than the live DB.

The `automaton restore` command is the same regardless of which backup
strategy you use - it just wants a path to a snapshot file.
