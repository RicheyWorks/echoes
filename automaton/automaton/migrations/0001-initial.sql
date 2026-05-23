-- automaton schema. Everything consistency-critical lives in this database.
-- Apply with PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; before use.

CREATE TABLE IF NOT EXISTS workflow_def (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    version      INTEGER NOT NULL,
    spec_json    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_workflow_def_name ON workflow_def(name, version DESC);

CREATE TABLE IF NOT EXISTS run (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_def_id    INTEGER NOT NULL REFERENCES workflow_def(id),
    status             TEXT    NOT NULL CHECK (status IN ('pending','running','completed','failed','cancelled')),
    trigger_kind       TEXT    NOT NULL,
    trigger_payload    TEXT,
    started_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT
);

CREATE INDEX IF NOT EXISTS idx_run_status ON run(status, started_at);

CREATE TABLE IF NOT EXISTS step (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES run(id),
    name             TEXT    NOT NULL,
    attempt          INTEGER NOT NULL DEFAULT 1,
    status           TEXT    NOT NULL CHECK (status IN ('pending','running','completed','failed','skipped','cancelled')),
    input_json       TEXT,
    output_json      TEXT,
    error_json       TEXT,
    started_at       TEXT,
    finished_at      TEXT,
    idempotency_key  TEXT    NOT NULL,
    UNIQUE (run_id, name, attempt)
);

CREATE INDEX IF NOT EXISTS idx_step_run ON step(run_id, name);

-- The work queue. One row per step that is ready to be executed.
-- Workers lease a row by setting leased_by + leased_until atomically.
CREATE TABLE IF NOT EXISTS queue (
    step_id        INTEGER PRIMARY KEY REFERENCES step(id),
    ready_at       TEXT    NOT NULL DEFAULT (datetime('now')),
    leased_by      TEXT,
    leased_until   TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_ready ON queue(ready_at) WHERE leased_by IS NULL;

-- Append-only audit log. Sequential id gives linearizable order for free.
CREATE TABLE IF NOT EXISTS event_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES run(id),
    ts           TEXT    NOT NULL DEFAULT (datetime('now')),
    kind         TEXT    NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_log_run ON event_log(run_id, id);

-- Single-row lease for the scheduler leader (one process schedules cron triggers).
CREATE TABLE IF NOT EXISTS scheduler_lock (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    holder    TEXT,
    expires   TEXT
);

INSERT OR IGNORE INTO scheduler_lock (id, holder, expires) VALUES (1, NULL, NULL);

CREATE TABLE IF NOT EXISTS cron_trigger (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_name   TEXT    NOT NULL,
    cron_expr       TEXT    NOT NULL,
    next_fire_at    TEXT    NOT NULL,
    last_fire_at    TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (workflow_name, cron_expr)
);

CREATE INDEX IF NOT EXISTS idx_cron_due ON cron_trigger(next_fire_at) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS signal (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id               INTEGER NOT NULL REFERENCES run(id),
    name                 TEXT    NOT NULL,
    payload_json         TEXT,
    sent_at              TEXT    NOT NULL DEFAULT (datetime('now')),
    consumed_at          TEXT,
    consumed_by_step_id  INTEGER REFERENCES step(id)
);

CREATE INDEX IF NOT EXISTS idx_signal_unconsumed
    ON signal(run_id, name) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS webhook_endpoint (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL UNIQUE,
    workflow_name     TEXT    NOT NULL,
    secret_hex        TEXT    NOT NULL,
    signature_header  TEXT    NOT NULL DEFAULT 'X-Automaton-Signature',
    signature_algo    TEXT    NOT NULL DEFAULT 'sha256',
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_webhook_name ON webhook_endpoint(name) WHERE enabled = 1;
