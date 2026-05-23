# System design: a strongly consistent personal automation platform

**Status:** Proposed
**Date:** 2026-05-19
**Decider:** Richmond (sole sign-off)
**Constraint anchor:** Strong consistency

---

## 1. What I'm assuming you meant

"Automation" is broad, so I'm pinning an interpretation up front so you can correct it before reading further. I'm designing a personal workflow automation platform — the Zapier / n8n / Temporal shape — where:

- **Triggers** (cron schedule, inbound webhook, file change, polled API, manual run) start a **workflow**
- A **workflow** is a DAG of **steps**, each step is a unit of work (HTTP call, shell command, file write, notification, branch, wait)
- Steps have side effects on external systems (sending emails, writing to APIs, moving files)
- You, the operator, want to look at any workflow run and trust what you see

The single non-negotiable is strong consistency. In this domain that has a precise meaning: **exactly-once observable effects** (each step's side effect happens once and only once from the outside world's point of view), **durable workflow state** (a crash never loses progress), and **linearizable reads** of run history (if a step is marked completed, every subsequent read sees it completed). That rules out a lot of cheap designs and is the spine of everything below.

If you actually meant home automation (Home Assistant style), CI/CD pipelines, or test automation, stop reading — the requirements diverge enough that I'd start over.

## 2. Requirements

**Functional.** Define workflows in code or YAML. Trigger them on schedule, webhook, or manual click. Each step can call external systems, branch on output, wait for time or external signals, fan out and join. Inspect every run: which steps ran, with what inputs, returning what, in what order. Retry failed steps without re-running successful ones. Cancel and resume in flight.

**Non-functional (in priority order, since you flagged strong consistency).**
1. **Exactly-once observable effects.** A step's external side effect occurs once, even across crashes, restarts, and retries.
2. **Durability.** Every state transition (`step queued → running → completed/failed`) is committed before any side effect is acknowledged.
3. **Linearizable run history.** The UI and any API caller see a single, consistent timeline of what happened. No "ran on machine A but not yet visible on machine B."
4. **Operability for one.** You should be able to run this on a single small VM or your laptop, debug it from a terminal, and back it up by copying one directory.
5. **Latency.** Trigger-to-first-step under one second is fine; this isn't a trading system.
6. **Throughput.** Tens of workflows per minute, not thousands per second.

The cost of putting consistency first is the explicit trade I'm making against horizontal scale and write throughput. For a one-person system that trade is correct.

## 3. High-level architecture

Four components, all on one host by default. The boundaries exist so you can split them later, not because you must today.

**API / control plane.** A small HTTP service that accepts workflow definitions, trigger registrations, manual run requests, and serves the inspection UI. Stateless — all state lives in the store.

**Scheduler.** A single process that owns "what needs to run next." It reads cron specs, watches for due timers, and enqueues runs into the work queue. Single-leader (see §5) so there's never a question of whether two schedulers both fired the same cron.

**Worker pool.** Processes that pull ready steps from the queue, execute them, and write results back. Workers are stateless — all progress is in the store. Start with one worker, add more when you want concurrency.

**State store.** A single SQLite database (with WAL mode) for the personal-scale default. Postgres is a drop-in upgrade when you want concurrent workers across machines. This store holds workflow definitions, run state, step state, the work queue, timers, and the idempotency ledger. **Everything consistency-critical lives here, in one database, behind one transaction boundary.** That is the central design move.

```
trigger (cron/webhook/manual)
   |
   v
[scheduler] --enqueue--> [work queue table]
                              |
                              v
                         [worker] --execute step--> external system
                              |                          |
                              +---write result tx-------+
                              |
                              v
                         [state store]  <-- inspect via [API/UI]
```

## 4. Data model (the heart of the design)

Five tables, all in one database, all reads/writes inside transactions. Schema sketch:

- `workflow_def(id, name, version, spec_json, created_at)` — versioned, immutable. A new version is a new row; running workflows pin to the version they started with.
- `run(id, workflow_def_id, status, trigger_kind, trigger_payload, started_at, finished_at)` — one row per workflow execution. `status` ∈ {pending, running, completed, failed, cancelled}.
- `step(id, run_id, name, status, attempt, input_json, output_json, error_json, started_at, finished_at, idempotency_key)` — one row per step instance. The `(run_id, name, attempt)` triple is unique. The `idempotency_key` is what the step sends to external systems to deduplicate.
- `queue(step_id, ready_at, leased_by, leased_until)` — the work queue. A worker "leases" a row by atomic update, executes, then deletes the row in the same transaction that writes the step's terminal state.
- `event_log(id, run_id, ts, kind, payload_json)` — append-only audit trail. Sequential primary key gives you the linearizable order for free.

The reason this is one database and not five microservices: every consistency invariant you care about is enforced by a single SQL transaction. "Mark step complete, drop its queue entry, append the event log row, schedule successor steps" is one `BEGIN…COMMIT`. Spread across services with eventual consistency, you'd be reinventing two-phase commit or saga compensation, and you'd get it wrong because you're one person.

## 5. How exactly-once actually works

Exactly-once is famously almost-impossible. Here's the realistic version, which is what every serious workflow engine (Temporal, Cadence, AWS Step Functions) does under the hood.

**The trick: split each step into "do the side effect" and "record that we did it," and use an idempotency key the external system honors.**

For each step the worker:

1. **Lease** the queue row (atomic `UPDATE queue SET leased_by = me, leased_until = now()+30s WHERE step_id = ? AND (leased_by IS NULL OR leased_until < now())`). If zero rows updated, somebody else got it — move on.
2. **Read** the step's idempotency key from the row. It was generated when the step was created (e.g., `sha256(run_id || step_name || attempt)`) so it's stable across retries-of-the-same-attempt.
3. **Call** the external system, passing the idempotency key. If the external system supports idempotency keys natively (Stripe, most modern APIs), it dedupes. If it doesn't, the step must first do a "check" call ("does a record with this key already exist?") before the "do" call. Steps that can't be made idempotent are flagged at workflow-definition time and the engine refuses to run them with retries enabled.
4. **Commit** the result in one transaction: write `step.status = completed`, write `step.output_json`, append to `event_log`, delete the queue row, insert queue rows for successor steps. If the process crashes between (3) and (4), recovery re-leases the step and re-calls — the external system dedupes on the key, so the observable side effect happens exactly once.
5. **Retries** bump the `attempt` counter and use a new idempotency key (because the previous attempt's effect may or may not have landed; the workflow author chooses whether to keep the key stable across attempts or rotate it).

The corollary: **steps without an idempotency story are not strongly consistent**, period. The system should make that explicit in the workflow spec rather than papering over it.

**The scheduler's leader election** uses the same store: a `lock` table with a single row, `UPDATE lock SET holder = me, expires = now()+10s WHERE expires < now()`. Only the row owner schedules. If you ever run two scheduler processes by accident, only one ever wins the row. No separate Zookeeper / etcd.

## 6. Failure modes worth naming

**Worker crashes mid-step.** Lease expires, another worker (or the same one on restart) re-leases and reruns the step. External system dedupes by idempotency key. State store never saw a partial commit because the result write is one transaction.

**Scheduler crashes between firing a trigger and the run being inserted.** The trigger source decides: cron is recovered on next tick (with catch-up policy in the spec — skip vs. backfill). Webhooks return only after the run row is committed, so if the caller got a 200 the run exists. Manual triggers are user-driven; if the API call failed they retry.

**State store corruption.** SQLite WAL gives you durability per `COMMIT`. Back up the file daily (or use Litestream for continuous replication to S3). Losing the store is the only unrecoverable failure mode, which is why it sits at the center of the operability story.

**External system is slow or returns ambiguous errors.** Step times out, lease expires, another attempt runs. Idempotency carries the day. The 5% case — external system doesn't honor your key and double-charges someone — gets escalated by the engine flagging "idempotency-unsafe step retried" in the run history. You read about it, you reconcile manually. That's the honest answer; "exactly-once" against a misbehaving counterparty is fiction.

**Clock skew.** Single host: not an issue. If you ever split across machines, switch leases to use the store's monotonically increasing transaction ID (`xmin` in Postgres, sequence in SQLite) instead of wall clock.

## 7. Alternatives I considered and why I didn't pick them

**Use Temporal.** It is exactly this design, hardened over years, and would do the job correctly. Honest recommendation: if you don't enjoy building the substrate and just want workflows, run Temporal in single-host mode and write workflow code against its SDK. The design above is what you'd build if you specifically want to own it. I'd lean Temporal for a real production use case, and the from-scratch design for learning or for very small, very personal use.

**Event-sourced with Kafka as the log.** Tempting because the event log is already central. Rejected because Kafka introduces a second consistency boundary (broker state vs. your state) and for one user the operational cost of running Kafka swamps any benefit. Revisit if you ever cross 100 workflows/sec.

**A bag of cron jobs and shell scripts.** What most people actually have. Fails the consistency bar immediately: no exactly-once, no inspection, no resumption. Worth saying out loud because if your real need is "fire-and-forget housekeeping," the answer might be "you don't need a workflow engine, you need three cron entries and a Slack notifier."

**Distributed consensus (Raft) from day one.** Massively over-engineered for one user on one machine. The single-leader-via-database-row pattern in §5 gives you correctness with vastly less code. Real Raft enters the picture if and only if you outgrow a single store.

## 8. Trade-off summary

| Dimension | This design | Cost |
|---|---|---|
| Consistency | Strong — exactly-once observable, linearizable history | Pay for it in write throughput; single-store bottleneck |
| Complexity | Medium — five tables, four components, one transaction model | Higher than "cron + scripts"; lower than microservices |
| Scale | Single host, tens of workflows/minute | Doesn't scale to thousands/sec without a rewrite |
| Cost | One small VM or laptop | Negligible |
| Team fit | One person, full-stack | None — you are the team |
| Failure recovery | File copy / Litestream restore | You must remember to back up the store |

## 9. Consequences

What gets easier: you can trust the system. Every run has a story, every step has a transaction, retries are safe by default.

What gets harder: every step author has to think about idempotency. There is no "just call the API and hope." That mental tax is the price of the consistency guarantee — pretending otherwise is the bug.

What you'll need to revisit: when you have more than one worker machine, you must move the state store from SQLite to Postgres and make sure the queue's lease semantics survive (they will — Postgres `SELECT … FOR UPDATE SKIP LOCKED` is the standard idiom). When you cross ~10k runs in history, partition the event log by month. When you want workflows to wait for human input, add a `signal` table with the same transactional treatment as `queue`.

## 10. Action items

1. [ ] Confirm the scope read in §1 — is this the "automation" you meant?
2. [ ] Pick the store: SQLite (default) or Postgres (if you'll ever want multi-machine).
3. [ ] Write the five-table schema and a migration script.
4. [ ] Implement the lease/commit transaction for one trivial step type (HTTP GET) end-to-end. This is the smallest thing that proves the consistency story.
5. [ ] Add cron triggers and the leader-row scheduler.
6. [ ] Add the inspection UI (a single page listing recent runs and their step tree is enough to start).
7. [ ] Set up Litestream or a daily file copy for the store. Without backups, the design's central pillar is on fire.
8. [ ] Decide a policy for "idempotency-unsafe" steps: refuse to retry, or run with a loud warning. I'd refuse, but it's your call.
