-- IdentitySource base DDL for PostgresIdentitySource.
-- See 1b-contracts.md §8.1.4. ``__SCHEMA__`` is substituted with the target
-- schema name at connection time by ``_postgres_pool.get_pool``.
CREATE TABLE IF NOT EXISTS "__SCHEMA__".files (
    principal_id TEXT        NOT NULL,
    file_name    TEXT        NOT NULL,
    content      TEXT        NOT NULL DEFAULT '',
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (principal_id, file_name)
);

CREATE INDEX IF NOT EXISTS idx_identity_principal
    ON "__SCHEMA__".files (principal_id);
