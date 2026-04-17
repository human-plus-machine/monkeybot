# Secret resolvers

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Rotation playbook mirrors production guidance for AWS Secrets Manager and GCP Secret Manager in the resolver implementations under `src/core/harness/extensions/secret_resolvers/`.

A `SecretResolver` maps an opaque handle (e.g. `"DATABASE_PASSWORD"`) to a
`pydantic.SecretStr`. Wrapping in `SecretStr` guarantees that `repr()`,
`str()`, and pydantic serialization never leak the raw value — which
matters for event streams, RunPackages, and logs.

## Shipped backends

| Backend | Import path | Use when |
|---|---|---|
| `EnvSecretResolver` | `emonk.core.harness.extensions.secret_resolvers:EnvSecretResolver` | Dev / local testing — reads `os.environ` |
| `AWSSecretsManagerResolver` | `emonk.core.harness.extensions.secret_resolvers:AWSSecretsManagerResolver` | AWS enterprise stack |
| `GCPSecretManagerResolver` | `emonk.core.harness.extensions.secret_resolvers:GCPSecretManagerResolver` | GCP preserved stack |
| `CompositeSecretResolver` | `emonk.core.harness.extensions.secret_resolvers:CompositeSecretResolver` | Chain multiple resolvers; first-to-resolve wins |

Non-shipped, extensible: HashiCorp Vault, Azure Key Vault, 1Password. Use
the `import_path` mechanism or declare an `emonk.secret_resolvers`
entry point.

## Composite chains

A composite resolver tries legs in order and returns the first hit. Typical
recipes:

### Recipe 1 — env → AWS Secrets Manager (dev → prod)

```python
from emonk.core.harness.extensions.secret_resolvers import (
    AWSSecretsManagerResolver,
    CompositeSecretResolver,
    EnvSecretResolver,
)

resolver = CompositeSecretResolver(
    legs=[
        EnvSecretResolver(),
        AWSSecretsManagerResolver(region="us-east-1"),
    ]
)
```

Developers set the handle in their shell (`export DATABASE_PASSWORD=...`);
production picks it up from Secrets Manager because the env leg misses.

### Recipe 2 — env → Vault → AWS (cross-cloud, failover)

```python
resolver = CompositeSecretResolver(legs=[
    EnvSecretResolver(),
    VaultSecretResolver(url="https://vault.internal"),  # custom extension
    AWSSecretsManagerResolver(region="us-east-1"),
])
```

`CompositeSecretResolver` raises `SecretNotFound` only if *every* leg
misses. Each leg's latency + outcome is recorded as a separate
`secret.resolved` event so you can dashboard per-leg health.

### Recipe 3 — env-only, locked down

```yaml
# harness.yaml
secret_resolver:
  backend: env
```

Use this for on-prem / air-gapped deployments where env vars are
orchestrated by the surrounding process supervisor.

## Rotation playbook (AWS)

1. Create a new version of the secret in **AWS Secrets Manager** (standard
   AWS rotation). Do not retire the previous version yet.
2. Wait for the in-process TTL to elapse (default 60 s). Resolvers refresh
   on the next miss.
3. Hit the health endpoint to confirm both legs reachable:

   ```bash
   curl -s https://agent.example.com/harness/secrets/health | jq
   ```
4. Monitor `secret.resolved` events: `handle_hash` stable, `latency_ms`
   normal. Any `secret.resolve_failed` spike indicates the new version is
   not yet propagated.
5. Once the new version is in use for ≥ 15 min with zero failures, retire
   the prior AWS Secrets Manager version.

Break-glass: flip to `EnvSecretResolver` temporarily by setting
`HARNESS_SECRET_RESOLVER_OVERRIDE=env` and injecting the handle via
`os.environ` while diagnosing.

## Supply-chain hygiene

- Secret handles appear in `events` only via `handle_hash` (SHA-256 of the
  handle string). The resolved value never hits the wire.
- `SecretStr` wrapping is verified by the `SEC-C-04` contract invariant
  — custom backends must return `SecretStr`, never a bare string.
- Composite legs are tried sequentially; a failing leg emits a `secret.leg_failed`
  event with its class name and latency so you can spot a silently-unhealthy
  backend.

## Runnable snippet

```python
# Resolve a handle through a two-leg composite.
import asyncio
import os
from emonk.core.harness.extensions.secret_resolvers import (
    CompositeSecretResolver,
    EnvSecretResolver,
)


async def main() -> None:
    os.environ["EXAMPLE_HANDLE"] = "from-env"
    resolver = CompositeSecretResolver(legs=[EnvSecretResolver()])
    secret = await resolver.resolve("EXAMPLE_HANDLE")
    print("wrapped:", repr(secret))
    print("value:", secret.get_secret_value())


asyncio.run(main())
```
