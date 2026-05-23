# Off-host backup with Litestream

`automaton backup` produces a one-shot consistent snapshot of the live
DB and is fine for nightly cron. But it has two limitations:

1. **Recovery window.** If you snapshot once a day, you can lose up to a
   day of runs on host loss.
2. **Same host.** A `rm -rf` or disk failure on the host loses both the
   live DB and the snapshots stored next to it.

[Litestream](https://litestream.io/) fixes both: it streams WAL frames
to off-host storage continuously (sub-second RPO) and keeps incremental
snapshots that you can restore from any retained point.

## Install

```bash
# Debian / Ubuntu
curl -fsSL https://github.com/benbjohnson/litestream/releases/latest/download/litestream_*linux_amd64.deb -o /tmp/litestream.deb
sudo dpkg -i /tmp/litestream.deb

# macOS
brew install benbjohnson/litestream/litestream

# Then drop the config in:
sudo cp deploy/litestream/litestream.yml /etc/litestream.yml
sudo systemctl enable --now litestream
```

## Configure

Edit `/etc/litestream.yml`. The shipped template
([`litestream.yml`](./litestream.yml)) walks through three backends:

- **Backblaze B2** (recommended for cost; ~$5/TB/month, S3-compatible API)
- **rsync.net** via SFTP (no API keys in the cloud; great if you already use it)
- **MinIO** (self-hosted S3 on your own VPS or NAS)

You can run more than one replica simultaneously - belt and braces. The
data flow is one-way from your host to the replica, so this is read-only
from the replica's perspective; the only thing that ever writes is the
local Litestream daemon.

### Retention and snapshot interval

`retention: 168h` (one week) means Litestream keeps WAL frames + the
prior week's incremental snapshots reachable for restore. Older state is
GC'd from the replica. `snapshot-interval: 12h` checkpoints WAL into a
new snapshot twice a day; longer intervals save remote space but make
restores slower.

The defaults in the template (one week retention, 12h snapshots) are
fine for a one-person deployment.

## Verify it's replicating

```bash
sudo litestream replicas /etc/litestream.yml
# Should show: db=/var/lib/automaton/automaton.db replica=s3 lag=<1s

sudo litestream snapshots /var/lib/automaton/automaton.db
# Lists recent snapshots and their sizes on the replica.
```

## Restore

Two paths, depending on what failed:

### a) The whole host is gone

On a fresh box, install Litestream and put the same config in place
(including the creds for the replica). Then:

```bash
sudo systemctl stop automaton-worker automaton-scheduler automaton-ui
sudo litestream restore -o /var/lib/automaton/automaton.db /var/lib/automaton/automaton.db
automaton restore /var/lib/automaton/automaton.db.copy  # see note below
automaton migrate                                       # bring schema forward if binary's newer
sudo systemctl start automaton-worker automaton-scheduler automaton-ui
```

The double-step is intentional: `litestream restore` produces a copy at
the path you ask for, and `automaton restore` then puts it in place
with integrity and schema-version checks. If you want to skip the
verification, you can write Litestream's output directly to the live
DB path; the engine will still run, but you've given up the integrity
check.

### b) The DB on this host is corrupt or accidentally deleted

```bash
sudo systemctl stop automaton-worker automaton-scheduler automaton-ui
sudo mv /var/lib/automaton/automaton.db /var/lib/automaton/automaton.db.bad
sudo litestream restore -o /var/lib/automaton/automaton.db /var/lib/automaton/automaton.db
automaton migrate
sudo systemctl start automaton-worker automaton-scheduler automaton-ui
```

See [`docs/runbooks/restore.md`](../../docs/runbooks/restore.md) for the
full step-by-step including verification commands.

## Important: don't run Litestream and `automaton backup` against the same DB

Both tools coordinate WAL checkpointing. Running them simultaneously can
hold checkpoints open longer than expected and bloat your WAL. Pick one:

- Litestream → off-host continuous; this is what you want for anything beyond a single laptop.
- `automaton backup` (cron'd) → simpler when off-host isn't possible; keep snapshots on a separate disk.
