-- Migration 0005: API key table for multi-tenant access control.
--
-- Roles:
--   admin    — full access (same as AUTOMATON_TOKEN)
--   operator — all reads + write routes (trigger, signal, cancel, agents)
--   viewer   — read routes only
--
-- Keys are stored as SHA-256(raw_key) — the plaintext is shown exactly once
-- at creation time and never persisted.

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,          -- "key_<hex8>"
    name         TEXT NOT NULL UNIQUE,      -- human label, e.g. "mobile-app"
    key_hash     TEXT NOT NULL UNIQUE,      -- SHA-256(raw_key) hex, 64 chars
    role         TEXT NOT NULL              -- 'admin' | 'operator' | 'viewer'
                 CHECK (role IN ('admin', 'operator', 'viewer')),
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at   TEXT NOT NULL,             -- ISO-8601 UTC
    last_used_at TEXT                       -- ISO-8601 UTC, NULL until first use
);

CREATE INDEX IF NOT EXISTS ix_api_keys_key_hash ON api_keys (key_hash);
CREATE INDEX IF NOT EXISTS ix_api_keys_name     ON api_keys (name);
