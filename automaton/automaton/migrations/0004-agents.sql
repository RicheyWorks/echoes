-- Agent memory persistence for Option C integration.
--
-- Allows the echoes Rust agent to use automaton as its durable store.
-- Each agent has a row in `agent` (goal, tick count) and one row per
-- memory entry in `agent_memory` (full hash-chain entry serialised as JSON).
--
-- The `agent_memory` table is append-only by convention. `tick` values are
-- expected to be sequential and unique per agent, but we enforce this only
-- with a UNIQUE constraint rather than a CHECK so that resume logic can
-- verify the chain in application code before deciding whether to accept
-- a new entry.

CREATE TABLE IF NOT EXISTS agent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    goal        TEXT    NOT NULL DEFAULT '',
    tick        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_agent_name ON agent(name);

CREATE TABLE IF NOT EXISTS agent_memory (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name  TEXT    NOT NULL REFERENCES agent(name) ON DELETE CASCADE,
    tick        INTEGER NOT NULL,
    entry_json  TEXT    NOT NULL,   -- full MemoryEntry serialised as JSON
    recorded_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (agent_name, tick)
);

CREATE INDEX IF NOT EXISTS idx_agent_memory_agent ON agent_memory(agent_name, tick ASC);
