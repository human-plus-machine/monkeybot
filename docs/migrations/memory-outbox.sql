-- MemPalace outbox + history message_id uniqueness.
-- Apply when paths.auto_schema is false (migration-owned schema).
--
-- SQLite: run the statements below against data/monkeybot.db.
-- Postgres: the same CREATE TABLE / INDEX statements work; use
--   ALTER TABLE ... ADD COLUMN IF NOT EXISTS for turn_id / message_id / palace_id
--   instead of the plain ALTER TABLE lines.

CREATE TABLE IF NOT EXISTS memory_outbox (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL,
    turn_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    workspace_id TEXT,
    wing TEXT NOT NULL,
    room TEXT NOT NULL,
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT,
    traceparent TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    palace_id TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_memory_outbox_pending
    ON memory_outbox(agent_id, palace_id, status, created_at);

ALTER TABLE conversation_history ADD COLUMN turn_id TEXT;
ALTER TABLE conversation_history ADD COLUMN message_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_history_message_id
    ON conversation_history(message_id)
    WHERE message_id IS NOT NULL AND message_id != '';
