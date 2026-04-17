# Extending the Harness

> **Audience:** third-party developers who want to ship a new backend (checkpointer,
> memory store, job storage, identity source, secret resolver, or model provider)
> to monkey-bot without forking the framework.

This is the master guide for monkey-bot's extensibility layer. If you only have
time for one page, read this one — every sub-topic (identity, secrets, models,
per-cloud runbooks) is linked at the bottom.

## Table of contents

1. [Why extensibility exists](#1-why-extensibility-exists)
2. [Registry resolution precedence](#2-registry-resolution-precedence)
3. [Three extension mechanisms](#3-three-extension-mechanisms)
    - [3.1 Programmatic `registry.register(...)`](#31-programmatic-registryregister)
    - [3.2 `import_path:` in YAML](#32-import_path-in-yaml)
    - [3.3 Entry points (opt-in discovery)](#33-entry-points-opt-in-discovery)
4. [Worked example: Redis Checkpointer](#4-worked-example-redis-checkpointer)
5. [Worked example: DynamoDB Checkpointer](#5-worked-example-dynamodb-checkpointer)
6. [Writing a contract-test suite for your backend](#6-writing-a-contract-test-suite-for-your-backend)
7. [CI hookup](#7-ci-hookup)
8. [Supply-chain hygiene](#8-supply-chain-hygiene)

---

## 1. Why extensibility exists

Enterprise consumers run on too many substrates for monkey-bot to ship backends
for all of them. We ship one shipped reference implementation per major
ecosystem (Firestore for GCP, Postgres for on-prem / AWS, Mongo for
replica-set deployments) and rely on a clean contract + worked examples for
everything else: DynamoDB, Redis, Vault, Azure Key Vault, Pinecone, and so on.

The non-negotiables are simple:

- **Additive-only.** Adding a backend requires zero framework changes.
- **Contract parity.** Every backend — shipped or consumer-owned — runs the
  same pytest invariants (`CKPT-C-01 … 07`, `MEM-C-01 … 08`, etc.).
- **80-line story.** A typical new backend is fewer than 100 LOC of real code.
- **Supply-chain safe.** Plugin discovery is opt-in; signed lockfiles are the
  norm for consumer images.

See [`docs/agent-harness.md`](agent-harness.md) for the architecture overview;
this guide is the "how".

---

## 2. Registry resolution precedence

Every extension surface (`Checkpointer`, `MemoryStore`, `JobStorage`,
`IdentitySource`, `SecretResolver`, `ModelProvider`) owns a `registry` class
variable. When the assembler builds a `CompiledAgent`, it resolves each
surface through the following precedence table (copied verbatim from
[`HarnessConfig` / registry resolution](agent-harness.md#harnessconfig-reference)):

| Tier | Input shape | Description |
|------|-------------|-------------|
| **1** | `spec` is already an instance of the ABC | Returned as-is (used when you pass a pre-built object) |
| **2** | `spec` is a string `"pkg.mod:Class"` | Treated as an `import_path` |
| **3** | `spec["import_path"]` is set | Dynamic import + instantiate with `kwargs` |
| **4** | `spec["backend"]` matches a programmatic registration | Invoke the registered factory |
| **5** | `spec["backend"]` matches an entry-point plugin | Only if `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1` |
| **6** | `spec["backend"]` matches a builtin | Framework-shipped reference backend |
| **7** | `spec` has no `backend` key | Fall back to the configured default |

Collision rules:

- Same-tier collisions → last-writer-wins for programmatic registrations;
  `emonk-harness plugin ls --strict` fails for entry-point collisions.
- Cross-tier collisions → the higher tier wins, and `plugin ls` surfaces the
  shadowed entry so operators know the "why".

Resolution raises one of three typed errors on failure:

- `BackendNotFound` — no tier yielded a factory.
- `BackendConfigError` — factory raised while constructing (bad kwargs).
- `BackendCapabilityMismatch` — backend does not advertise a capability the
  caller asked for (e.g. `memory_store.require_vector_search=True` but the
  chosen backend returns `MemoryStoreCapabilities(vector_search=False)`).

---

## 3. Three extension mechanisms

You pick between them based on **who owns the wiring**. All three work side
by side — a single `HarnessConfig` can mix programmatic registrations for
dev-only fakes, `import_path` strings for config-managed production
backends, and entry points for pip-installed plugins.

### 3.1 Programmatic `registry.register(...)`

**Best for:** dev-time fakes, in-process testing, last-mile customization
inside a monorepo.

```python
# app/startup.py
from emonk.core.harness.extensions import Checkpointer
from mycorp.ckpt.redis_backed import RedisCheckpointer

Checkpointer.registry.register(
    "redis",
    lambda: RedisCheckpointer(url="redis://redis.internal:6379"),
)
```

Call this **before** `build_universal_agent(cfg)`. The registry is a module
global so the order of imports matters less than the order of
`register()` calls.

Registering the same name twice raises `BackendConfigError` unless you pass
`overwrite=True`. That default is intentional — it catches the "two people
both named their backend `redis`" class of bug at import time rather than at
resolve time.

### 3.2 `import_path:` in YAML

**Best for:** ops-managed config, rolling out a new backend without shipping
a code change, hotfix during an incident.

```yaml
# harness.yaml
checkpointer:
  import_path: "mycorp.ckpt.redis_backed:RedisCheckpointer"
  kwargs:
    url: "redis://redis.internal:6379"
    namespace: "session"
```

`"pkg.mod:Class"` is the canonical format (Python dotted path, colon
separator, symbol name). monkey-bot imports the module, resolves the symbol,
and instantiates it with the declared `kwargs`. No code change, no
registration call, no env var.

This is the same mechanism that powers `SandboxSpec(backend="custom",
custom_import_path=...)` — reused everywhere.

### 3.3 Entry points (opt-in discovery)

**Best for:** packaging a backend as a first-class pip-installable plugin
distributed through PyPI (public or private).

Declare the entry point in your extension's `pyproject.toml`:

```toml
[project.entry-points."emonk.checkpointers"]
redis = "mycorp.ckpt.redis_backed:RedisCheckpointer"
```

One group per surface — the full list is:

```
emonk.checkpointers
emonk.memory_stores
emonk.job_storage
emonk.identity_sources
emonk.secret_resolvers
emonk.model_providers
```

Discovery is **opt-in**: set `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1` in the
runtime environment. This gate is the supply-chain control referenced in
[`docs/harness/plugin-operations.md`](harness/plugin-operations.md):
a compromised dependency on `site-packages` can only register as a plugin
if the operator has explicitly turned discovery on.

Inspect the resolved state with:

```bash
emonk-harness plugin ls
emonk-harness plugin ls --strict   # fail on collisions; CI-friendly
```

---

## 4. Worked example: Redis Checkpointer

All three mechanisms take the same backend class. Here is a minimal,
self-contained Redis Checkpointer — ~80 LOC, no framework internals
imported:

```python
# mycorp/ckpt/redis_backed.py
from __future__ import annotations

import itertools
import pickle
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from emonk.core.harness.extensions import (
    Checkpointer,
    CheckpointMissing,
    CheckpointRef,
)


class RedisCheckpointer(Checkpointer):
    """Redis-backed checkpointer using a sorted-set index per session.

    Pseudo-Redis client calls are shown inline; plug in ``redis.asyncio``
    or ``aioredis`` at your preferred version pin. The namespace layout is:

        HSET  emonk:ckpt:{session_id}:{checkpoint_id}  ...fields...
        ZADD  emonk:ckpt:{session_id}:index  <seq> <checkpoint_id>
    """

    def __init__(self, *, url: str, namespace: str = "emonk:ckpt") -> None:
        self._url = url
        self._ns = namespace
        self._counters: dict[str, itertools.count[int]] = {}
        self._client: Any | None = None

    async def _redis(self) -> Any:
        import redis.asyncio as redis  # lazy

        if self._client is None:
            self._client = redis.from_url(self._url)
        return self._client

    def _next_id(self, session_id: str) -> str:
        counter = self._counters.setdefault(session_id, itertools.count(1))
        seq = next(counter)
        return f"{seq:016d}-{uuid.uuid4().hex[:8]}"

    async def write(
        self,
        session_id: str,
        state: Mapping[str, Any],
        *,
        reason: Literal["turn_end", "pre_destructive", "manual", "rewind"] = "turn_end",
    ) -> CheckpointRef:
        client = await self._redis()
        payload = pickle.dumps(dict(state))
        checkpoint_id = self._next_id(session_id)
        ref = CheckpointRef(
            session_id=session_id,
            checkpoint_id=checkpoint_id,
            reason=reason,
            created_at=datetime.now(UTC),
            bytes=len(payload),
            uri=f"redis://{self._ns}/{session_id}/{checkpoint_id}",
        )
        key = f"{self._ns}:{session_id}:{checkpoint_id}"
        await client.hset(
            key,
            mapping={
                "payload": payload,
                "reason": reason,
                "created_at": ref.created_at.isoformat(),
                "bytes": str(len(payload)),
                "uri": ref.uri,
            },
        )
        await client.zadd(f"{self._ns}:{session_id}:index", {checkpoint_id: 0})
        return ref

    async def read(
        self, session_id: str, checkpoint_id: str | None = None
    ) -> Mapping[str, Any] | None:
        client = await self._redis()
        if checkpoint_id is None:
            ids = await client.zrevrange(f"{self._ns}:{session_id}:index", 0, 0)
            if not ids:
                return None
            checkpoint_id = ids[0].decode() if isinstance(ids[0], bytes) else ids[0]
            raise_on_missing = False
        else:
            raise_on_missing = True
        data = await client.hget(f"{self._ns}:{session_id}:{checkpoint_id}", "payload")
        if data is None:
            if raise_on_missing:
                raise CheckpointMissing(session_id, checkpoint_id)
            return None
        return pickle.loads(data)

    async def list(self, session_id: str, *, limit: int = 100) -> list[CheckpointRef]:
        client = await self._redis()
        ids = await client.zrevrange(f"{self._ns}:{session_id}:index", 0, limit - 1)
        return [await self._load_ref(session_id, cid) for cid in ids]

    async def _load_ref(self, session_id: str, checkpoint_id: bytes | str) -> CheckpointRef:
        cid = checkpoint_id.decode() if isinstance(checkpoint_id, bytes) else checkpoint_id
        client = await self._redis()
        fields = await client.hgetall(f"{self._ns}:{session_id}:{cid}")
        return CheckpointRef(
            session_id=session_id,
            checkpoint_id=cid,
            reason=fields[b"reason"].decode(),
            created_at=datetime.fromisoformat(fields[b"created_at"].decode()),
            bytes=int(fields[b"bytes"]),
            uri=fields[b"uri"].decode(),
        )

    async def delete_session(self, session_id: str) -> None:
        client = await self._redis()
        ids = await client.zrange(f"{self._ns}:{session_id}:index", 0, -1)
        keys = [f"{self._ns}:{session_id}:{cid.decode() if isinstance(cid, bytes) else cid}" for cid in ids]
        keys.append(f"{self._ns}:{session_id}:index")
        if keys:
            await client.delete(*keys)
        self._counters.pop(session_id, None)
```

Register it:

```python
Checkpointer.registry.register("redis", lambda: RedisCheckpointer(url="redis://..."))
```

That is the whole extension.

---

## 5. Worked example: DynamoDB Checkpointer

For a full package layout (`pyproject.toml`, entry-point declaration,
contract-test file, Dockerfile, README) see the canonical example:

- [`examples/extension-dynamodb-checkpointer/`](../examples/extension-dynamodb-checkpointer/)

The `DynamoDBCheckpointer` implementation is ~100 lines and exercises every
API surface the contract expects (`put_item`, `get_item`, `query` ordered by
sort key, paginated `batch_write_item` for deletion). It is pip-installable
today and boots under `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1` with zero
framework edits.

---

## 6. Writing a contract-test suite for your backend

monkey-bot exposes the per-surface invariant suite as a public callable so
your backend runs the same checks the framework uses internally. Import it
from `emonk.core.harness.extensions.testing`:

```python
# tests/test_my_redis_ckpt_contract.py
from emonk.core.harness.extensions.testing import checkpointer_contract_suite

from mycorp.ckpt.redis_backed import RedisCheckpointer


def test_redis_checkpointer_matches_contract() -> None:
    def factory() -> RedisCheckpointer:
        return RedisCheckpointer(url="redis://localhost:6379")

    checkpointer_contract_suite(factory)
```

The same module exposes:

- `checkpointer_contract_suite(backend_factory)` — CKPT-C-01 … 07
- `memory_store_contract_suite(backend_factory)` — MEM-C-01 … 07
- `job_storage_contract_suite(backend_factory)` — JOB-C-01 … 04
- `identity_source_contract_suite(backend_factory)` — ID-C-01 … 04
- `secret_resolver_contract_suite(backend_factory)` — SEC-C-01, 02, 04
- `model_provider_contract_suite(backend_factory)` — MP-C-01, 02

Each suite treats `backend_factory` as a zero-argument callable and invokes
it once per invariant so each case starts clean. Invariants that are
logically inapplicable (TTL on a no-TTL store, `write_memory` on a read-only
identity source) are skipped internally; failures surface as `AssertionError`
with the invariant id as a prefix (`"CKPT-C-04: ..."`).

---

## 7. CI hookup

Drop this workflow in your extension repo at `.github/workflows/contract.yml`:

```yaml
name: contract-tests
on:
  push:
    branches: [main]
  pull_request:

jobs:
  contract:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install
        run: |
          python -m pip install --upgrade pip
          pip install -e .[dev]
      - name: Run framework contract suite
        env:
          HARNESS_PLUGINS_FROM_ENTRY_POINTS: "1"
        run: pytest -q -x
      - name: Verify plugin discovery
        env:
          HARNESS_PLUGINS_FROM_ENTRY_POINTS: "1"
        run: |
          python -c "
          from emonk.core.harness.extensions import Checkpointer
          entries = [e.name for e in Checkpointer.registry.entries()]
          assert 'redis' in entries or 'dynamodb' in entries, entries
          print('plugin discovery OK')
          "
```

The `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1` env var toggles discovery at the
exact boundary the framework enforces in production, so CI verifies the same
code path operators run.

---

## 8. Supply-chain hygiene

New extension packages widen the attack surface of any consumer that
installs them. The framework bakes three controls:

- **Opt-in entry-point discovery** (`HARNESS_PLUGINS_FROM_ENTRY_POINTS=1`).
  A compromised dependency on `site-packages` is inert unless the operator
  has explicitly flipped the switch.
- **`--require-hashes` in `Dockerfile.extension-template`.** The shipped
  [`Dockerfile.extension-template`](../Dockerfile.extension-template) uses
  `pip install --require-hashes -r requirements.lock.txt` so wheel bodies
  match the hashes a reviewer signed off on. Generate the lockfile with
  `uv pip compile --generate-hashes` or `pip-compile --generate-hashes`.
- **`plugin ls --strict` CI gate.** Run it in CI; it fails the pipeline if
  two sources register the same plugin name (e.g. a shipped
  `FirestoreCheckpointer` and an entry-point plugin both claiming
  `"firestore"`). Collisions are almost always a packaging accident; fix
  them before they reach production.

Additional hardening recipes:

- Pin your extension package with `emonk>=1.0,<2.0` and gate breaking
  changes on a major bump.
- Keep cloud SDKs **lazy-imported** inside methods (see the Redis/DynamoDB
  examples) so the `import ..ext_pkg` cost on containers that never
  instantiate the class stays at zero.
- Treat contract-suite parity as a release gate — the suite is the shipped
  definition of "compatible backend".

---

## Further reading

- [`docs/harness/backend-matrix.md`](harness/backend-matrix.md) — shipped
  vs. non-shipped backend grid.
- [`docs/harness/identity-source.md`](harness/identity-source.md) —
  per-invocation identity lifecycle and cache semantics.
- [`docs/harness/secret-resolver.md`](harness/secret-resolver.md) —
  composite chains and rotation playbook.
- [`docs/harness/model-provider.md`](harness/model-provider.md) —
  Bedrock, OpenAI, Anthropic, Vertex, Ollama recipes.
- [`docs/harness/aws-enterprise-runbook.md`](harness/aws-enterprise-runbook.md) —
  ≤ 30-minute AWS deployment runbook.
- [`docs/harness/postgres-backends.md`](harness/postgres-backends.md) —
  pool sizing, DDL layout, Alembic opt-in.
- [`docs/harness/mongo-backends.md`](harness/mongo-backends.md) —
  replica-set guidance and non-RS fallback caveats.
- [`docs/harness/plugin-operations.md`](harness/plugin-operations.md) —
  `plugin ls`, collision resolution, supply-chain posture.
