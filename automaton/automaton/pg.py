"""PostgreSQL backend adapter for automaton.

Provides ``PgConn``, a thin wrapper around a ``psycopg`` (v3) connection that
presents the same interface as a ``sqlite3.Connection``.  This lets
``engine.py`` and all other callers use ``conn.execute(sql, params)`` without
modification: the wrapper transparently applies all necessary SQL translations.

Translations applied on every ``execute()`` call
-------------------------------------------------
* Parameter placeholder:      ``?``             → ``%s``
* Timestamp (formatted):      ``strftime('%Y-%m-%d %H:%M:%f', 'now')``
                               → ``to_char(NOW() AT TIME ZONE 'UTC',
                                           'YYYY-MM-DD HH24:MI:SS.MS')``
* Timestamp (plain):          ``datetime('now')``  → ``NOW()``
* Transaction mode:           ``BEGIN IMMEDIATE``   → ``BEGIN``
* Upsert guard:               ``INSERT OR IGNORE``
                               → ``INSERT … ON CONFLICT DO NOTHING``
* Row identity after INSERT:  ``RETURNING id`` appended automatically;
                               result exposed as ``cursor.lastrowid``.

Row access
----------
All rows are returned as plain dicts (psycopg ``dict_row`` factory), which
supports ``row["column"]`` the same way ``sqlite3.Row`` does.

Usage
-----
    from automaton.pg import connect as pg_connect, migrate as pg_migrate

    conn = pg_connect("postgresql://user:pw@host/dbname")
    pg_migrate(conn)          # idempotent CREATE TABLE IF NOT EXISTS
    # … use exactly like an sqlite3 connection …
"""
from __future__ import annotations

import re
from typing import Any, Iterator, Optional, Sequence

try:
    import psycopg
    from psycopg.rows import dict_row as _dict_row
    _PSYCOPG_AVAILABLE = True
except ImportError:                                    # pragma: no cover
    psycopg = None                                     # type: ignore[assignment]
    _PSYCOPG_AVAILABLE = False

# ---------------------------------------------------------------------------
# SQL translation
# ---------------------------------------------------------------------------

# Ordered: most-specific patterns first so broad replacements don't clobber
# narrow ones.  Each entry is (compiled_re, replacement_string).
_RAW_TRANSLATIONS: list[tuple[str, str]] = [
    # strftime('%Y-%m-%d %H:%M:%f', 'now') — used in _SQL_NOW
    (
        r"strftime\('%Y-%m-%d %H:%M:%f',\s*'now'\)",
        "to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS.MS')",
    ),
    # datetime('now') — used in DEFAULT and UPDATE SET … = datetime('now')
    (r"datetime\('now'\)", "NOW()"),
    # (julianday(a) - julianday(b)) * 86400 — SQLite's seconds-between idiom
    # (timeout sweep, notify durations, wait steps).  Postgres: interval
    # subtraction + EXTRACT(EPOCH ...).  Must run before the qmark rule so
    # `?` args inside still get converted afterwards.
    (
        r"\(julianday\('now'\)\s*-\s*julianday\(([^()?]+|\?)\)\)\s*\*\s*86400",
        r"EXTRACT(EPOCH FROM (NOW() - CAST(\1 AS timestamptz)))",
    ),
    (
        r"\(julianday\(([^()?]+|\?)\)\s*-\s*julianday\(([^()?]+|\?)\)\)\s*\*\s*86400",
        r"EXTRACT(EPOCH FROM (CAST(\1 AS timestamptz) - CAST(\2 AS timestamptz)))",
    ),
    # SQLite exclusive transaction → standard BEGIN for Postgres
    (r"\bBEGIN IMMEDIATE\b", "BEGIN"),
    # SQLite qmark → Postgres positional placeholder
    (r"\?", "%s"),
]

_TRANSLATIONS = [
    (re.compile(pat, re.IGNORECASE), repl) for pat, repl in _RAW_TRANSLATIONS
]

_INSERT_OR_IGNORE_RE = re.compile(r"\bINSERT\s+OR\s+IGNORE\b", re.IGNORECASE)
_INSERT_RE = re.compile(r"\bINSERT\b", re.IGNORECASE)
_RETURNING_RE = re.compile(r"\bRETURNING\b", re.IGNORECASE)
_INSERT_TABLE_RE = re.compile(r"\bINSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)

# Tables whose primary key is not an ``id`` column.  ``RETURNING id`` must
# not be appended to INSERTs on these — Postgres rejects the statement
# (SQLite silently satisfies ``lastrowid`` via the implicit rowid).  No
# caller reads ``lastrowid`` after inserting into them.
_NO_ID_TABLES = frozenset({"queue"})


def _translate(sql: str) -> tuple[str, bool]:
    """Return *(translated_sql, needs_lastrowid)*.

    *needs_lastrowid* is True when ``RETURNING id`` was appended, i.e. for
    any plain ``INSERT`` that did not already have a ``RETURNING`` clause.
    """
    # INSERT OR IGNORE → INSERT … ON CONFLICT DO NOTHING (must happen before
    # the generic INSERT detection so we don't also append RETURNING id).
    has_ignore = bool(_INSERT_OR_IGNORE_RE.search(sql))
    if has_ignore:
        sql = _INSERT_OR_IGNORE_RE.sub("INSERT", sql)

    for pattern, replacement in _TRANSLATIONS:
        sql = pattern.sub(replacement, sql)

    if has_ignore:
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    is_insert = bool(_INSERT_RE.search(sql))
    has_returning = bool(_RETURNING_RE.search(sql))
    table_match = _INSERT_TABLE_RE.search(sql)
    table = table_match.group(1).lower() if table_match else None
    needs_lastrowid = (is_insert and not has_returning and not has_ignore
                       and table not in _NO_ID_TABLES)

    if needs_lastrowid:
        sql = sql.rstrip().rstrip(";") + " RETURNING id"

    return sql, needs_lastrowid


# ---------------------------------------------------------------------------
# Cursor wrapper
# ---------------------------------------------------------------------------

class PgCursor:
    """Wraps a psycopg ``Cursor`` to match the ``sqlite3.Cursor`` interface.

    The only addition beyond delegation is ``lastrowid``: when a plain INSERT
    was translated to ``INSERT … RETURNING id``, the first row of the result
    set is consumed in ``__init__`` and its ``id`` column exposed here.
    """

    __slots__ = ("_cur", "_lastrowid")

    def __init__(self, cur: Any, needs_lastrowid: bool) -> None:
        self._cur = cur
        self._lastrowid: Optional[int] = None
        if needs_lastrowid:
            row = cur.fetchone()
            if row is not None:
                self._lastrowid = row["id"]

    # --- sqlite3.Cursor interface ---

    @property
    def lastrowid(self) -> Optional[int]:
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    def fetchone(self) -> Optional[dict]:
        return self._cur.fetchone()

    def fetchall(self) -> list[dict]:
        return self._cur.fetchall()

    def __iter__(self) -> Iterator[dict]:
        return iter(self._cur)


# ---------------------------------------------------------------------------
# Connection wrapper
# ---------------------------------------------------------------------------

class PgConn:
    """Postgres connection that looks like a ``sqlite3.Connection``.

    Opened with ``autocommit=True`` so that explicit ``BEGIN``/``COMMIT``/
    ``ROLLBACK`` in ``db.transaction()`` map 1-to-1 to Postgres transactions
    — exactly the same pattern used for SQLite with ``isolation_level=None``.
    """

    #: Sentinel checked by ``engine.lease_one`` to select the
    #: ``SELECT … FOR UPDATE SKIP LOCKED`` leasing strategy.
    is_postgres: bool = True

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    # --- sqlite3.Connection interface ---

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PgCursor:
        translated, needs_lastrowid = _translate(sql)
        cur = self._conn.cursor(row_factory=_dict_row)
        cur.execute(translated, params or ())
        return PgCursor(cur, needs_lastrowid)

    def executemany(self, sql: str, params_seq: Any) -> None:
        translated, _ = _translate(sql)
        with self._conn.cursor() as cur:
            cur.executemany(translated, params_seq)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PgConn":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def connect(dsn: str) -> PgConn:
    """Open a Postgres connection and return a ``PgConn`` adapter.

    Raises ``RuntimeError`` if psycopg is not installed (the ``[postgres]``
    extra is required: ``pip install 'automaton-engine[postgres]'``).
    """
    if not _PSYCOPG_AVAILABLE:
        raise RuntimeError(
            "psycopg is not installed.  "
            "Add the Postgres extra: pip install 'automaton-engine[postgres]'"
        )
    raw = psycopg.connect(dsn, autocommit=True)
    return PgConn(raw)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------

# Full Postgres schema — includes all columns from the three SQLite migrations.
# Intentionally a single idempotent script so ``migrate()`` is safe to call on
# every startup (``CREATE TABLE IF NOT EXISTS``, ``CREATE INDEX IF NOT EXISTS``).
_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflow_def (
    id               BIGSERIAL PRIMARY KEY,
    name             TEXT      NOT NULL,
    version          INTEGER   NOT NULL,
    spec_json        TEXT      NOT NULL,
    timeout_seconds  INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (name, version)
);

CREATE INDEX IF NOT EXISTS idx_workflow_def_name
    ON workflow_def(name, version DESC);

CREATE TABLE IF NOT EXISTS run (
    id               BIGSERIAL PRIMARY KEY,
    workflow_def_id  BIGINT    NOT NULL REFERENCES workflow_def(id),
    status           TEXT      NOT NULL
                         CHECK (status IN (
                             'pending','running','completed',
                             'failed','cancelled','timed_out')),
    trigger_kind     TEXT      NOT NULL,
    trigger_payload  TEXT,
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_run_status ON run(status, started_at);

CREATE TABLE IF NOT EXISTS step (
    id               BIGSERIAL PRIMARY KEY,
    run_id           BIGINT    NOT NULL REFERENCES run(id),
    name             TEXT      NOT NULL,
    attempt          INTEGER   NOT NULL DEFAULT 1,
    status           TEXT      NOT NULL
                         CHECK (status IN (
                             'pending','running','completed',
                             'failed','skipped','cancelled')),
    input_json       TEXT,
    output_json      TEXT,
    error_json       TEXT,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    idempotency_key  TEXT      NOT NULL,
    UNIQUE (run_id, name, attempt)
);

CREATE INDEX IF NOT EXISTS idx_step_run ON step(run_id, name);

CREATE TABLE IF NOT EXISTS queue (
    step_id          BIGINT PRIMARY KEY REFERENCES step(id),
    ready_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    leased_by        TEXT,
    leased_until     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_queue_ready
    ON queue(ready_at) WHERE leased_by IS NULL;

CREATE TABLE IF NOT EXISTS event_log (
    id           BIGSERIAL PRIMARY KEY,
    run_id       BIGINT    NOT NULL REFERENCES run(id),
    ts           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind         TEXT      NOT NULL,
    payload_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_log_run ON event_log(run_id, id);

CREATE TABLE IF NOT EXISTS scheduler_lock (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    holder  TEXT,
    expires TIMESTAMPTZ
);

INSERT INTO scheduler_lock (id, holder, expires)
    VALUES (1, NULL, NULL)
    ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS cron_trigger (
    id              BIGSERIAL PRIMARY KEY,
    workflow_name   TEXT      NOT NULL,
    cron_expr       TEXT      NOT NULL,
    next_fire_at    TIMESTAMPTZ NOT NULL,
    last_fire_at    TIMESTAMPTZ,
    enabled         INTEGER   NOT NULL DEFAULT 1,
    timezone        TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workflow_name, cron_expr)
);

CREATE INDEX IF NOT EXISTS idx_cron_due
    ON cron_trigger(next_fire_at) WHERE enabled = 1;

CREATE TABLE IF NOT EXISTS signal (
    id                   BIGSERIAL PRIMARY KEY,
    run_id               BIGINT    NOT NULL REFERENCES run(id),
    name                 TEXT      NOT NULL,
    payload_json         TEXT,
    sent_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    consumed_at          TIMESTAMPTZ,
    consumed_by_step_id  BIGINT REFERENCES step(id)
);

CREATE INDEX IF NOT EXISTS idx_signal_unconsumed
    ON signal(run_id, name) WHERE consumed_at IS NULL;

CREATE TABLE IF NOT EXISTS webhook_endpoint (
    id                BIGSERIAL PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE,
    workflow_name     TEXT    NOT NULL,
    secret_hex        TEXT    NOT NULL,
    signature_header  TEXT    NOT NULL DEFAULT 'X-Automaton-Signature',
    signature_algo    TEXT    NOT NULL DEFAULT 'sha256',
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhook_name
    ON webhook_endpoint(name) WHERE enabled = 1;
"""


def migrate(conn: PgConn) -> None:
    """Apply the full Postgres schema.  Idempotent — safe to call on startup.

    Uses ``CREATE TABLE IF NOT EXISTS`` / ``CREATE INDEX IF NOT EXISTS`` so
    re-running on an existing database is a no-op.
    """
    # Each statement must be executed individually; psycopg won't execute
    # a multi-statement string in a single execute() call.
    statements = [
        s.strip() for s in _PG_SCHEMA.split(";") if s.strip()
    ]
    conn.execute("BEGIN")
    try:
        for stmt in statements:
            conn.execute(stmt)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
