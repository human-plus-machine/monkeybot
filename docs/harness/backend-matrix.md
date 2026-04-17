# Backend matrix — shipped & non-shipped

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Derived from the shipped reference backends and extension contract suites in this repository.

monkey-bot ships a **reference** implementation per major ecosystem and
treats every unshipped backend as a first-class *extension target* —
documented, contract-tested, and buildable in ~80 LOC.

## Shipped (first-party) backends

| Surface | InMemory | LocalFS | Firestore | GCS | S3 | Postgres | Mongo | Bedrock | Vertex | AWS Secrets | GCP Secrets |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Checkpointer       | ✅ dev | —     | ✅ existing | —     | —     | ✅ new | ✅ new | —     | —     | —     | —     |
| MemoryStore        | ✅ dev | —     | ✅ existing | ✅ existing | ✅ new | ✅ new | ✅ new | —     | —     | —     | —     |
| JobStorage         | ✅ dev | ✅ json (legacy alias: `json_file`) | ✅ existing | —     | —     | ✅ new | ✅ new | —     | —     | —     | —     |
| IdentitySource     | —     | ✅ new | ✅ new      | ✅ new | ✅ new | ✅ new | ✅ new | —     | —     | —     | —     |
| + `CallableIdentitySource` | — | — | — | — | — | — | — | — | — | — | — |
| SecretResolver     | —     | —     | —           | —     | —     | —     | —     | —     | —     | ✅ new | ✅ new |
| + `EnvSecretResolver`, `CompositeSecretResolver` | — | — | — | — | — | — | — | — | — | — | — |
| ModelProvider      | —     | —     | —           | —     | —     | —     | —     | ✅ new | ✅ new (retrofit) | — | — |
| + `OpenAIProvider`, `AnthropicProvider`, `OllamaProvider` | — | — | — | — | — | — | — | — | — | — | — |

**Cohesive stacks:**

- **AWS enterprise** — `PostgresCheckpointer` + `S3MemoryStore` (or `PostgresMemoryStore`) + `PostgresJobStorage` + `S3IdentitySource` + `AWSSecretsManagerResolver` + `BedrockProvider`.
- **GCP preserved** — `FirestoreCheckpointer` + `FirestoreMemoryStore` (or `GCSMemoryStore`) + `FirestoreJobStorage` + `GCSIdentitySource` + `GCPSecretManagerResolver` + `VertexProvider`.
- **On-prem / any-cloud** — `PostgresCheckpointer` + `PostgresMemoryStore` (pgvector) + `PostgresJobStorage` + `LocalRunPackageWriter` + `PostgresIdentitySource` + `EnvSecretResolver` + `OllamaProvider`.

## Non-shipped (extensible) backends

Every row below is a documented extension target, not a dead end. Each is
reachable with the three mechanisms described in
[`docs/extending-the-harness.md`](../extending-the-harness.md).

| Surface | Backend | Why not shipped | How to extend |
|---|---|---|---|
| Checkpointer | **DynamoDB** | AWS enterprises satisfied by `PostgresCheckpointer`; DynamoDB adds an AWS-only code path | See [`examples/extension-dynamodb-checkpointer/`](../../examples/extension-dynamodb-checkpointer/) — ~100 LOC pip-installable |
| Checkpointer | Redis | Memory-only semantics don't match checkpoint durability expectations | Subclass + register; sample in the master guide |
| Checkpointer | SQLite | Dev-only convenience; `InMemoryCheckpointer` covers dev today | Subclass + register |
| JobStorage | DynamoDB | Same reasoning as Checkpointer; Postgres covers AWS | Subclass `JobStorage`, use DynamoDB conditional writes for `claim_job` leases |
| JobStorage | Redis | Works well but not universal enough to ship by default | `SET NX EX` for leases |
| MemoryStore | DynamoDB | Postgres/S3 cover the AWS surface | Subclass + register |
| MemoryStore | Elasticsearch / OpenSearch | Vector-search work deferred | Subclass + register |
| MemoryStore | Pinecone / Weaviate / Qdrant / Chroma | Same | Subclass + register |
| IdentitySource | Consul / etcd | Rare in agent-use cases | Subclass + register |
| SecretResolver | Vault / Azure Key Vault / 1Password | Not ubiquitous enough for default | Subclass + register; compose with `CompositeSecretResolver` |
| ModelProvider | Azure OpenAI / SageMaker / vLLM / LiteLLM | Deferred | Subclass `ModelProvider.build()` returning a `BaseChatModel` |

**Contract guarantee.** The same pytest invariants run against every
backend, shipped or consumer-owned. A shipped backend that regresses a
consumer's extension is a framework bug.

## Choosing a backend (decision matrix)

- Greenfield GCP deployment → GCP preserved stack.
- Greenfield AWS deployment → AWS enterprise stack; adopt DynamoDB via the
  worked example if you prefer serverless durability.
- On-prem, air-gapped, or multi-cloud portable → Postgres stack.
- Dev / local tests → `InMemoryCheckpointer` + `InMemoryMemoryStore` +
  `JSONFileJobStorage` + `LocalFSIdentitySource` + `EnvSecretResolver`.

## Runnable snippet

```python
# Print every registered backend, grouped by surface.
from emonk.core.harness.extensions import (
    Checkpointer, MemoryStore, JobStorage,
    IdentitySource, SecretResolver, ModelProvider,
)

for abc in (Checkpointer, MemoryStore, JobStorage, IdentitySource, SecretResolver, ModelProvider):
    names = sorted(entry.name for entry in abc.registry.entries())
    print(f"{abc.__name__}: {names}")
```
