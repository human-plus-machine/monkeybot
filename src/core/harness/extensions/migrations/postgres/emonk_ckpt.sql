-- Checkpointer DDL for PostgresCheckpointer.
-- See 1b-contracts.md §8.1.1. ``__SCHEMA__`` is substituted with the target
-- schema name at connection time by ``_postgres_pool.get_pool``.
CREATE TABLE IF NOT EXISTS "__SCHEMA__".checkpoints (
    session_id       TEXT        NOT NULL,
    checkpoint_id    TEXT        NOT NULL,
    reason           TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    bytes            INTEGER     NOT NULL,
    payload          BYTEA       NOT NULL,
    PRIMARY KEY (session_id, checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_ckpt_session_time
    ON "__SCHEMA__".checkpoints (session_id, created_at DESC);
