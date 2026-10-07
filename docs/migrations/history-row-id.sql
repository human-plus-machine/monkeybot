-- Stable history row ids (Message.row_id).
-- Apply when paths.auto_schema is false (migration-owned schema).
--
-- SQLite: run against data/monkeybot.db. The store detects the column, so
--   rows written before this runs keep working and get derived ids on load.
-- Postgres: required before deploying; the store writes row_id on every insert.
--   Use ALTER TABLE ... ADD COLUMN IF NOT EXISTS row_id TEXT to make it re-runnable.
-- Firestore: no migration; the field is added on write.

ALTER TABLE conversation_history ADD COLUMN row_id TEXT;
