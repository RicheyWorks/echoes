# Operating envelope

What one host can sustain, measured by the load scripts in
`tests/load/`. **These numbers describe a noop workload** -
``file_append`` steps that touch one local file. Real workloads with
network I/O, subprocess calls, or large templated specs will be slower
on a per-step basis but should preserve the same scaling shape.

The CI regression tripwires live in ``tests/test_load_regression.py``
and use small parameters; the numbers below come from running the full
scripts.

## Headline numbers

Measured on a shared Linux container (no GPU acceleration, modest IO):

| Scenario | Throughput | Notes |
|---|---|---|
| Burst, 1000 runs, 4 workers | ~1500 runs/s drain | Enqueue: 0.12s. Drain to zero: 0.68s. |
| Burst, 100 runs, 2 workers (CI tripwire) | ~960 runs/s drain | Drain in ~0.1s. Tripwire fails over 5s. |
| Steady-state, 100 runs/s, 4 workers, 5s | 99 runs/s sustained | DB size ~480 KB after 497 runs. |
| Long-tail, 20 short + 3 long, 2 workers | short p95 <1 ms | Long steps don't starve shorts. |

A consumer-grade server (4+ cores, NVMe SSD, no other tenants) should
sustain 2-5× these numbers comfortably. A Raspberry Pi-class device
will sustain ~30-50%.

## Reproducing locally

```bash
python -m tests.load.burst --count 1000 --workers 4
python -m tests.load.steady_state --rate 100 --workers 4 --seconds 30
python -m tests.load.long_tail --short 50 --long 5 --workers 4
```

Each script prints a JSON report. The fields you'll care about:

- ``drain_seconds`` - how long the workers took to empty the queue.
- ``throughput_runs_per_sec`` - sustained rate.
- ``step_duration.p95_ms`` / ``p99_ms`` - tail latency.
- ``db_size_bytes`` - growth from the test.

## Things that change the ceiling

### SQLite WAL + busy_timeout

The PRAGMAs we ship via ``db.connect()`` matter. ``automaton`` sets:

| PRAGMA | Value | Why |
|---|---|---|
| `journal_mode` | WAL | Readers don't block writers; one writer at a time. |
| `synchronous` | NORMAL (1) | Safe with WAL; better throughput than FULL. |
| `busy_timeout` | 30000 ms | Generous - lets migrations/backups not lose to a normal write. |
| `wal_autocheckpoint` | 1000 pages | Avoids one giant blocking checkpoint at end-of-burst. |

``db.verify_pragmas(conn)`` returns the observed values - the CI
regression suite asserts they match these defaults on every PR.
``test_pragmas_match_production_defaults`` is the regression guard if
anyone edits ``db.connect()``.

### Worker count

There's a single writer at a time at the SQLite layer, so adding more
workers past ~4 doesn't help on the engine-side serialization step. It
**does** help when individual steps are slow (network I/O,
subprocesses) - more workers means more in-flight concurrent step
bodies. Rule of thumb: 4-8 workers per host for I/O-heavy workloads,
2-4 for CPU-light noops.

### Step body cost

The numbers above are essentially "engine overhead per run". A real
workload with a 200 ms HTTP step will be dominated by that 200 ms; the
engine adds <5 ms in the median case.

### DB size + pruning

``automaton prune --older-than 30 --vacuum`` regularly. Past ~100 MB of
history the load scripts start to show ``p99`` creep on the steady-state
scenario; under ~50 MB it's flat.

## When to scale out

| Signal | What to consider |
|---|---|
| Steady throughput needed >500 runs/s | Add a worker process; SQLite write contention rises. |
| Worker p95 lease wait >100 ms | Check `wal_autocheckpoint` is actually being honored; profile. |
| Need to run workers on multiple machines | Phase 16 of the platform plan: Postgres backend, or migrate workflows to Temporal. |
| DB file approaching 1 GB | Aggressive pruning + scheduled VACUUM, or move history out to cold storage. |

The honest line from the system design doc still applies: under ~1000
runs/day, one user, one or two hosts, the single-host SQLite shape is
the right answer. Past that, the cost of running multi-host SQLite
correctly is higher than just running Postgres or Temporal.

## CI tripwires

The fast regression tests in ``tests/test_load_regression.py``:

- **`test_pragmas_match_production_defaults`** - WAL / busy_timeout /
  wal_autocheckpoint as we expect them.
- **`test_burst_drains_within_bound`** - 100 noop runs through 2
  workers in under 5 seconds. Catches a ~50x slowdown on a busy CI
  runner; tighter bounds tend to flake.
- **`test_long_tail_shorts_do_not_starve`** - p95 of short-step
  duration stays under 500 ms even with 1-second long steps in flight.
