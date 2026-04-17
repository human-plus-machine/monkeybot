-- JobStorage base DDL for PostgresJobStorage.
-- See 1b-contracts.md §3.3 / §8.1.3. ``__SCHEMA__`` is substituted with the
-- target schema name at connection time by ``_postgres_pool.get_pool``.
--
-- The ``jobs`` table stores the full scheduler queue: ``payload`` is the
-- opaque job document, ``status`` + ``leased_until`` + ``lease_token``
-- implement the atomic lease used by ``claim_job`` via
-- ``SELECT … FOR UPDATE SKIP LOCKED``.
CREATE TABLE IF NOT EXISTS "__SCHEMA__".jobs (
    id            TEXT        PRIMARY KEY,
    payload       JSONB       NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'pending',
    leased_until  TIMESTAMPTZ,
    lease_token   TEXT,
    priority      INT         NOT NULL DEFAULT 0,
    next_run_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_pending
    ON "__SCHEMA__".jobs (status, leased_until, priority DESC, next_run_at);

CREATE INDEX IF NOT EXISTS idx_jobs_leased_until
    ON "__SCHEMA__".jobs (leased_until) WHERE leased_until IS NOT NULL;
