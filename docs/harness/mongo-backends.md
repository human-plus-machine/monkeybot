# Mongo backends

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Replica-set posture follows the Agent Harness contracts in `src/core/harness/specs.py` and the checkpointer/memory-store contract suites.

monkey-bot ships three MongoDB-backed surfaces:

- `MongoCheckpointer` — session checkpoint documents.
- `MongoMemoryStore` — namespaced memory (optional TTL index).
- `MongoJobStorage` — scheduler job leases (atomic `findOneAndUpdate`).

All three accept a Mongo URI (`uri_env=MONGO_URI`) and share a single
`AsyncIOMotorClient` per URI. No additional pool tuning is required for
typical deployments.

## Replica-set guidance (recommended)

Production deployments **SHOULD** use a replica set. The framework
detects replica-set availability at connect and unlocks:

- Multi-document transactions for bulk writes.
- Change streams (reserved for future features).
- Stronger read/write concern defaults.

Deploy MongoDB Atlas (3+ members) or a self-managed replica set. The
`mongodb+srv://` URI scheme wires it up:

```yaml
checkpointer:
  backend: mongo
  kwargs:
    uri_env: MONGO_URI           # mongodb+srv://user:pass@cluster.mongo.net
    db_name: emonk
    collection: checkpoints
```

## Non-RS fallback caveats

If the driver cannot negotiate a replica set at connect, the framework
does **not** abort. Instead:

- Transactional write paths fall back to `findOneAndUpdate` (which is
  atomic at the **document** level).
- `claim_job` still works correctly — the contract invariant
  `JOB-C-01` ("exactly one winner under contention") holds because
  `findOneAndUpdate` with a filter is atomic.
- Multi-document writes become non-atomic. The scheduler compensates via
  per-document idempotency keys; identity and memory writes are
  single-document by design.
- `EV_HEALTH_DEGRADED` emits once per process with
  `reason="mongo_no_replica_set"` — idempotent per 15 min window.

For dev / local tests this is fine. For production it is **not
recommended**. Ops should alert on the degraded event and either
provision a replica set or migrate to Postgres.

## Runnable snippet

```python
# Quick sanity check against a local mongod.
import asyncio
from emonk.core.harness.extensions.job_storage import MongoJobStorage


async def main() -> None:
    storage = MongoJobStorage(
        uri_env="MONGO_URI",
        db_name="emonk_dev",
        collection="jobs",
    )
    await storage.save_jobs([{"job_id": "demo", "payload": {"n": 1}}])
    claimed = await storage.claim_job("demo", lease_duration_seconds=60)
    print("claimed:", claimed)
    await storage.release_job("demo")


asyncio.run(main())
```

## TTL indexes (MemoryStore)

`MongoMemoryStore` advertises `ttl=True` and creates a TTL index on the
`expires_at` field. Mongo's TTL monitor runs every ~60 s, so keys expire
with that granularity — do not depend on sub-minute precision.

## Migration from Postgres / Firestore

Moving a running deployment between Mongo and another backend is a
straightforward config change:

1. Stand up the new backend (Mongo cluster or Postgres RDS).
2. Flip `checkpointer/memory_store/job_storage` to the new backend in
   `harness.yaml`. Old data does not auto-migrate — run a one-off
   migration script to copy rows if needed.
3. Redeploy. In-flight sessions lose their checkpoint history unless you
   ran the migration step.

## Security posture

- Use SRV URIs with TLS (`tls=true` implied by the `+srv` scheme).
- Bind the database user to the least-privileged role (`readWrite` on
  the specific DB, not cluster-wide).
- Rotate credentials via Secrets Manager → `EnvSecretResolver`-injected
  `MONGO_URI`; the DSN handle never appears in logs because it is
  resolved through `SecretStr`.
