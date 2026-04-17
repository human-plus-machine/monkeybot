# Identity sources

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Derived from `IdentitySource` ABC + `IdentityResolutionMW` in `src/core/harness/` and the identity contract suite under `tests/harness/extensions/`.

## Lifecycle (per invocation)

Identity is **invocation-scoped** — resolved at the start of every turn and
cached, not baked into the `CompiledAgent`.

```
┌── turn start ─────────────────────────────────────────────────────────┐
│ 1. IdentityResolutionMW receives the Principal                        │
│ 2. Cache key = (principal_id, session_id) is looked up                │
│    ├─ HIT  → ctx["identity"] = cached LoadedIdentity    (<1 ms)       │
│    └─ MISS → IdentitySource.load(principal=..., session_id=...)       │
│              → LoadedIdentity validated by pydantic                   │
│              → cached for ttl_seconds                                 │
│ 3. Downstream middleware/nodes read ctx["identity"]                   │
│ 4. `write_memory` calls (MEMORY.md / HEARTBEAT.md edits) persist via  │
│    the same IdentitySource (if supported) or the shared MemoryStore   │
└──────────────────────────────────────────────────────────────────────┘
```

## `LoadedIdentity` shape

A frozen pydantic model — validated once on `IdentitySource.load` return,
never re-validated at cache fetch:

| Field | Type | Meaning |
|---|---|---|
| `principal_id` | `str` | The caller's stable id (user id, service id, etc.) |
| `session_id` | `str | None` | Optional session scope |
| `soul`, `rules`, `identity`, `user`, `index` | `str` | Read-only identity files |
| `memory`, `heartbeat` | `str` | Mutable — written via `write_memory()` |
| `loaded_at` | `datetime` | Cache timestamp |
| `ttl_seconds` | `int` | Re-validate after this many seconds |
| `source_backend` | `str` | The backend that produced the bundle (for tracing) |
| `extras` | `Mapping[str, str]` | Bag-of-extras for backend-specific fields |

`LoadedIdentity.system_prompt_block()` composes the text blocks into a
deterministic system prompt section (SOUL → IDENTITY → USER → INDEX →
RULES → MEMORY → HEARTBEAT).

## Cache semantics

- **Scope:** one cache per `CompiledAgent` (process-local).
- **Key:** `(principal_id, session_id)` — a null session means "principal-wide".
- **TTL:** governed by `LoadedIdentity.ttl_seconds` (default 300 s).
- **Eviction:** LRU with a configurable cap; p99 warm-hit latency < 1 ms is
  a contract invariant (`ID-C-04`).
- **Bust:** `POST /harness/identity/bust` removes entries matching an
  optional `{principal_id, session_id, reason}` filter.

## Bust endpoint walk-through

Rotate a principal's `SOUL.md` on S3 and drain the cache without a
restart:

```bash
# 1. Upload the new file out-of-band.
aws s3 cp ./SOUL.md s3://emonk-identity/prod/alice/SOUL.md

# 2. Invalidate that principal's cache entry.
curl -X POST https://agent.example.com/harness/identity/bust \
    -H 'Authorization: Bearer $ADMIN_TOKEN' \
    -H 'Content-Type: application/json' \
    -d '{"principal_id": "alice", "reason": "rotation"}'

# 3. Verify next load picks up the new hash.
curl https://agent.example.com/harness/introspect/<session_id> \
    | jq '.identity_files_loaded'
```

`identity.bust` events appear in the event stream, and `identity.load`
p95 latency temporarily rises as the cache refills — expected, drains
naturally over TTL.

### Event emission (Phase 6)

`IdentityResolutionMW` publishes every identity-flow event through the
shared `EventBus` when one is wired in:

| Event kind                 | When                                                                 |
|----------------------------|----------------------------------------------------------------------|
| `identity.load`            | Every successful resolution (cache hit _and_ cold-miss load)         |
| `identity.load_failed`     | `IdentityNotFound` raised after the retry budget                     |
| `identity.cache_evict`     | TTL / capacity evictions from `IdentityCache`                        |
| `identity.bust`            | Explicit `invalidate(predicate)` (driven by `/harness/identity/bust`) |

Payload includes `principal_id`, `session_id`, `cache_hit`, `latency_ms`,
`backend`, and (for cold-miss paths) the single-flight role
(`leader` / `waiter`). Subscribers plug in via `event_bus.subscribe(handler)`
— consumers use this to ship Phoenix / OpenTelemetry / custom sinks
without modifying the framework.

### Cold-miss single-flight (R-16)

Concurrent invocations for the same `(principal_id, session_id)` collapse
to **exactly one** backend `load()` call. The first arriver is the
*leader*; subsequent callers await an `asyncio.Future` and reuse the
resolved identity. This protects the `IdentitySource` from warm-up /
post-eviction stampedes. See
[`tests/integration/test_cache_single_flight.py`](../../tests/integration/test_cache_single_flight.py)
for the integration-level invariant.

## Writing an `IdentitySource`

Subclass the ABC, implement `load`, and optionally `write_memory`:

```python
from emonk.core.harness.extensions import IdentitySource, LoadedIdentity
from datetime import UTC, datetime


class RedisIdentitySource(IdentitySource):
    async def load(self, *, principal, session_id=None):
        raw = await self._redis.hgetall(f"identity:{principal.id}")
        if not raw:
            from emonk.core.harness.extensions import IdentityNotFound
            raise IdentityNotFound(principal.id)
        return LoadedIdentity(
            principal_id=principal.id,
            session_id=session_id,
            soul=raw.get(b"soul", b"").decode(),
            rules=raw.get(b"rules", b"").decode(),
            identity=raw.get(b"identity", b"").decode(),
            user=raw.get(b"user", b"").decode(),
            index=raw.get(b"index", b"").decode(),
            memory=raw.get(b"memory", b"").decode(),
            heartbeat=raw.get(b"heartbeat", b"").decode(),
            loaded_at=datetime.now(UTC),
            ttl_seconds=300,
            source_backend="redis",
        )
```

`write_memory` is optional; omit it if your store is read-only. The
framework falls back to `MemoryStore` under namespace `("identity",
principal.id)` for mutable files in that case.

## Runnable snippet

```python
# Load an identity via the configured source and render its system prompt block.
import asyncio
from emonk.core.harness.extensions.identity_sources import LocalFSIdentitySource
from emonk.core.harness.events import Principal


async def main() -> None:
    src = LocalFSIdentitySource(root="./agent_mem")
    identity = await src.load(principal=Principal(kind="user", id="alice"))
    print(identity.system_prompt_block())


asyncio.run(main())
```
