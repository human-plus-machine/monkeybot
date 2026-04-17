# Postgres backends

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Pool sizing and DDL strategy are documented inline below; authoritative schemas live under `src/core/harness/extensions/migrations/postgres/`.

monkey-bot ships four Postgres-backed surfaces that share a single pool per
`(dsn_env, schema_name)`:

- `PostgresCheckpointer` — session checkpoint rows.
- `PostgresMemoryStore` — namespaced memory (optional pgvector column).
- `PostgresJobStorage` — scheduler job leases.
- `PostgresIdentitySource` — principal identity rows.

Registering the same DSN across all four consumes **one** connection pool,
not four. RDS `max_connections` budget is the operational bottleneck; the
sizing table below is tuned around that constraint.

## DDL

The canonical DDL lives under
[`src/core/harness/extensions/migrations/postgres/`](../../src/core/harness/extensions/migrations/postgres/):

- [`emonk_ckpt.sql`](../../src/core/harness/extensions/migrations/postgres/emonk_ckpt.sql)
- [`emonk_memory.sql`](../../src/core/harness/extensions/migrations/postgres/emonk_memory.sql)
- [`emonk_memory_pgvector.sql`](../../src/core/harness/extensions/migrations/postgres/emonk_memory_pgvector.sql)
- [`emonk_scheduler.sql`](../../src/core/harness/extensions/migrations/postgres/emonk_scheduler.sql)
- [`emonk_identity.sql`](../../src/core/harness/extensions/migrations/postgres/emonk_identity.sql)

Each file is **idempotent** (`CREATE TABLE IF NOT EXISTS` + idempotent
index creation) and is executed at first connect. The DDL path is always
authoritative — no tool-chain dependency on Alembic is required to boot.

## Alembic (opt-in)

Teams that prefer explicit, numbered migrations can bundle the same DDL as
Alembic revisions. The opt-in lives *inside your consumer repo* — the
framework deliberately keeps Alembic out of the default install:

```
# your-consumer-repo/alembic/versions/0001_initial.py
def upgrade() -> None:
    op.execute(open("emonk_ckpt.sql").read())
    op.execute(open("emonk_memory.sql").read())
    ...
```

Pin your Alembic baseline to the sha of the DDL files you shipped; when
the framework publishes a DDL change, roll a new revision in your repo.
Because the DDL is idempotent, running it twice is safe — a belt-and-suspenders
posture for ops that want both migrations and auto-heal.

## Pool sizing

| Workload | Postgres `pool_max_size` | Mongo connection pool | Rationale |
|---|---|---|---|
| Single-tenant agent (marketing-bot-shaped) | **5** | 10 | Low concurrency; idle pool wastes RDS slots |
| Multi-tenant AgentCore deployment (default) | **10** | 25 | Balances RDS `max_connections=100` with headroom |
| Heavy-parallel subagent deployment | **25** | 50 | Subagent spawns multiply concurrent DB ops |

## RDS sizing

| Tier | Instance | `max_connections` | Use case |
|---|---|---|---|
| Dev | `db.t4g.medium` | 100 | Single dev; single-tenant |
| Prod-S | `db.m6g.large` | 200 | ≤ 10 concurrent sessions |
| Prod-M | `db.m6g.xlarge` | 500 | ≤ 100 concurrent sessions, subagents |
| Prod-L | `db.r6g.2xlarge` + read replica | 1000 | Multi-tenant AgentCore, heavy subagent fanout |

Keep pool `in_use / pool_max_size` under 0.8 p95 over 5 min windows (SLO-B1).
`EV_HEALTH_DEGRADED` with `reason="postgres_pool_saturation"` emits at 0.95
sustained > 60 s — that's your alert threshold.

## Multi-schema layout

Each surface lands in its own schema so teams can grant least-privilege
roles and isolate migrations:

```
emonk_ckpt.checkpoints
emonk_memory.items
emonk_memory.namespaces
emonk_scheduler.jobs
emonk_identity.principals
```

Pass `schema_name` explicitly in each spec:

```yaml
checkpointer:
  backend: postgres
  kwargs:
    dsn_env: CKPT_DSN
    schema_name: emonk_ckpt

memory_store:
  backend: postgres
  kwargs:
    dsn_env: CKPT_DSN
    schema_name: emonk_memory
    enable_pgvector: true

job_storage:
  backend: postgres
  kwargs:
    dsn_env: CKPT_DSN
    schema_name: emonk_scheduler
```

## pgvector opt-in

Pass `enable_pgvector=True` and install the Postgres extension once:

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

The `emonk_memory_pgvector.sql` DDL file adds the vector columns + ivfflat
index. When the capability is on, `memory_store.capabilities().vector_search`
returns `True` and the assembler lets callers opt into embedding-powered
`search()`.

## Runnable snippet

```python
# Smoke-test a Postgres checkpointer locally against `postgres://localhost`.
import asyncio
from emonk.core.harness.extensions.checkpointers import PostgresCheckpointer


async def main() -> None:
    ckpt = PostgresCheckpointer(dsn_env="POSTGRES_DSN", schema_name="emonk_ckpt_dev")
    ref = await ckpt.write("demo", {"hello": "world"}, reason="manual")
    roundtripped = await ckpt.read("demo", ref.checkpoint_id)
    print(ref.checkpoint_id, "→", roundtripped)
    await ckpt.delete_session("demo")


asyncio.run(main())
```
