# Backups

The whole consistency story rests on the state store. Lose `automaton.db` and you lose every run history, every cron trigger, every workflow definition. Back it up. Pick one of the three options below depending on how much loss you're willing to tolerate.

## Option 1: Periodic snapshot (built in)

The `automaton backup` command uses SQLite's online backup API. It produces a consistent snapshot even while workers and the scheduler are writing. No need to stop anything.

```bash
automaton backup /path/to/automaton.backup.db
```

The destination is a complete, transactionally-consistent SQLite file. Treat it like any other file — copy it to a USB drive, rsync it to another host, upload it to S3. To restore, just replace `automaton.db` with the backup.

Pair this with cron for a daily snapshot:

```cron
0 3 * * *  /usr/local/bin/automaton backup /var/backups/automaton/$(date +\%F).db
```

**Recovery point objective with this approach: up to 24 hours of data loss.** Good enough for most personal use.

## Option 2: Continuous replication with Litestream (recommended for anything you care about)

[Litestream](https://litestream.io) replicates SQLite to S3 (or any S3-compatible store, or another local disk) continuously by tailing the WAL. Recovery point is seconds, not days.

### Install

macOS: `brew install benbjohnson/litestream/litestream`
Linux: download the binary from the releases page.

### Configure

Create `/etc/litestream.yml`:

```yaml
dbs:
  - path: /var/lib/automaton/automaton.db
    replicas:
      - type: s3
        bucket: my-automaton-backups
        path: automaton
        region: us-east-1
        # Credentials via environment, ~/.aws/credentials, or IAM role.
```

For a local-disk replica (no S3 needed) swap the replica for:

```yaml
      - type: file
        path: /mnt/backup/automaton
```

### Run

Litestream runs as its own long-lived process alongside automaton:

```bash
litestream replicate
```

On Linux, drop it under systemd so it restarts on boot. Litestream watches the database file and ships every WAL segment to the replica within a few seconds of the COMMIT.

### Restore

```bash
litestream restore -o /var/lib/automaton/automaton.db s3://my-automaton-backups/automaton
```

Restore brings the database to the most recent replicated WAL frame. Recovery point in practice: under a minute of loss, often seconds.

## Option 3: Plain file copy (don't)

`cp automaton.db backup.db` while the system is running can produce a corrupt file. SQLite's WAL means a naive copy can miss in-flight pages, and `automaton.db-wal` and `automaton.db-shm` are part of the live state too. Don't use this unless you've stopped the worker, scheduler, and UI first.

## What to back up

- `automaton.db` — the only thing that matters
- `automaton.db-wal` and `automaton.db-shm` — created by SQLite while running; safe to leave behind if you used Option 1 (the backup file is a single self-contained DB)

You do **not** need to back up workflow YAML files separately — once `automaton register` ran, the spec is in `workflow_def.spec_json` inside the database. That said, keeping the YAMLs in version control is the right move for auditability.

## Verifying a backup

A backup file that's never been restored is a backup file that's never been verified. Periodically:

```bash
# Pretend the backup is the live DB and list runs from it
AUTOMATON_DB=/path/to/backup.db automaton inspect | head
```

If that prints the runs you expect, the backup is intact.
