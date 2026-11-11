-- Archive of history rows removed by compaction or the load-max truncation.
-- Apply when paths.auto_schema is false (migration-owned schema).
-- With auto_schema left on, startup creates the table from the backend DDL
-- (sqlite.SCHEMA_DDLS, postgres._SCHEMA_DDLS). scripts/create_schema.py runs
-- that same SQLite DDL.
--
-- SQLite: run the statements below against data/monkeybot.db.
-- Postgres: do not run the SQLite block. Run the Postgres block instead.
-- Firestore: no migration. The collection is created on the first archive write.
--   Reads filter on equality only, so no composite index is required.

-- SQLite -----------------------------------------------------------------

CREATE TABLE IF NOT EXISTS conversation_history_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_scope TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL,
    row_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    turn_id TEXT,
    message_id TEXT,
    archive_id TEXT NOT NULL,
    summary_row_id TEXT,
    reason TEXT NOT NULL,
    position INTEGER NOT NULL,
    archived_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_history_archive_thread
    ON conversation_history_archive(agent_scope, thread_id, archived_at, position);

-- Postgres ---------------------------------------------------------------
-- CREATE TABLE IF NOT EXISTS conversation_history_archive (
--     id BIGSERIAL PRIMARY KEY,
--     agent_scope TEXT NOT NULL DEFAULT '',
--     thread_id TEXT NOT NULL,
--     row_id TEXT NOT NULL,
--     role TEXT NOT NULL,
--     content TEXT NOT NULL,
--     created_at BIGINT NOT NULL,
--     turn_id TEXT,
--     message_id TEXT,
--     archive_id TEXT NOT NULL,
--     summary_row_id TEXT,
--     reason TEXT NOT NULL,
--     position INTEGER NOT NULL,
--     archived_at BIGINT NOT NULL
-- );
-- CREATE INDEX IF NOT EXISTS idx_history_archive_thread
--     ON conversation_history_archive(agent_scope, thread_id, archived_at, position);
