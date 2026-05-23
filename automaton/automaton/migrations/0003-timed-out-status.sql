-- Add 'timed_out' as a valid terminal status for runs.
--
-- SQLite doesn't support ALTER TABLE ... MODIFY CONSTRAINT, so we recreate
-- the run table with the expanded CHECK constraint. Foreign-key enforcement
-- must be disabled for the swap; that PRAGMA must run outside a transaction,
-- so we opt out of yoyo's automatic transaction wrapper.
--
-- Also adds timeout_seconds to workflow_def: workflows can cap their total
-- wall-clock duration; the scheduler reaps stale running runs that exceed it.
--
-- transactional: false

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE run_new (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_def_id    INTEGER NOT NULL REFERENCES workflow_def(id),
    status             TEXT    NOT NULL CHECK (
                           status IN ('pending','running','completed',
                                      'failed','cancelled','timed_out')
                       ),
    trigger_kind       TEXT    NOT NULL,
    trigger_payload    TEXT,
    started_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at        TEXT
);

INSERT INTO run_new SELECT * FROM run;

DROP TABLE run;
ALTER TABLE run_new RENAME TO run;

CREATE INDEX IF NOT EXISTS idx_run_status ON run(status, started_at);

ALTER TABLE workflow_def ADD COLUMN timeout_seconds INTEGER;

COMMIT;

PRAGMA foreign_keys = ON;
