-- MemoryStore base DDL for PostgresMemoryStore.
-- See 1b-contracts.md §8.1.2. ``__SCHEMA__`` is substituted with the target
-- schema name at connection time by ``_postgres_pool.get_pool``.
--
-- The pgvector column/index are NOT created here — they require superuser
-- privileges (``CREATE EXTENSION``) and are therefore opt-in. Enable them
-- by instantiating ``PostgresMemoryStore(enable_pgvector=True)`` which
-- additionally executes ``emonk_memory_pgvector.sql``.
CREATE TABLE IF NOT EXISTS "__SCHEMA__".items (
    namespace   TEXT[]      NOT NULL,
    key         TEXT        NOT NULL,
    value       JSONB       NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ,
    PRIMARY KEY (namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_mem_namespace
    ON "__SCHEMA__".items USING GIN (namespace);

CREATE INDEX IF NOT EXISTS idx_mem_expires
    ON "__SCHEMA__".items (expires_at) WHERE expires_at IS NOT NULL;
