-- Opt-in pgvector extensions for PostgresMemoryStore.
-- See 1b-contracts.md §8.1.2. Applied only when
-- ``PostgresMemoryStore(enable_pgvector=True)`` — the base
-- ``emonk_memory.sql`` DDL must already have been executed.
--
-- ``CREATE EXTENSION IF NOT EXISTS vector`` requires superuser privileges
-- the first time it runs; subsequent deployments are idempotent no-ops.
CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE "__SCHEMA__".items
    ADD COLUMN IF NOT EXISTS embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_mem_embedding
    ON "__SCHEMA__".items USING ivfflat (embedding vector_cosine_ops)
    WHERE embedding IS NOT NULL;
