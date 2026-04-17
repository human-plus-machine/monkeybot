# Plugin operations

> Companion to [`docs/extending-the-harness.md`](../extending-the-harness.md).
> Supply-chain controls: opt-in entry-point discovery (`HARNESS_PLUGINS_FROM_ENTRY_POINTS=1`), `emonk-harness plugin ls --strict`, and hash-locked installs (`Dockerfile.extension-template`).

This guide covers day-two ops: inspecting what plugins are loaded,
resolving collisions, and keeping the supply chain healthy.

## `emonk-harness plugin ls`

List every registered backend across all six surfaces:

```bash
emonk-harness plugin ls
```

Example output:

```
kind               name               source              module
Checkpointer       in_memory          builtin             emonk.core.harness.extensions.checkpointers.in_memory
Checkpointer       firestore          builtin             emonk.core.harness.extensions.checkpointers.firestore
Checkpointer       postgres           builtin             emonk.core.harness.extensions.checkpointers.postgres
Checkpointer       mongo              builtin             emonk.core.harness.extensions.checkpointers.mongo
Checkpointer       dynamodb           entry_point         dynamodb_ckpt.checkpointer
MemoryStore        in_memory          builtin             ...
...
```

Columns:

- **kind** — the ABC surface.
- **name** — the registration key (`backend:` value in YAML).
- **source** — one of `builtin`, `programmatic`, `import_path`, or
  `entry_point`. `entry_point` entries only appear when
  `HARNESS_PLUGINS_FROM_ENTRY_POINTS=1`.
- **module** — the actual Python import path that will instantiate.

## Strict mode (CI gate)

```bash
emonk-harness plugin ls --strict
```

Fails with a non-zero exit status if *any* registry has ≥ 2 entries with
the same name from different sources (a collision). Run it in CI ahead of
`pytest` so packaging mistakes surface early.

Sample failing output:

```
COLLISION: Checkpointer.firestore
  → builtin      : emonk.core.harness.extensions.checkpointers.firestore
  → entry_point  : mycorp.ckpt.firestore
ExitCode: 2
```

## Resolving collisions

Cross-tier collisions (e.g. `builtin.firestore` vs.
`entry_point.firestore`) are legal — the higher tier wins — but they are
almost always a packaging accident. Fix them by:

1. **Renaming** the plugin to a unique key (`firestore_enterprise`,
   `redis_cluster`, …). Entry-point names are strings, so this is cheap.
2. **Removing the collision** from the other source. A third-party plugin
   shadowing a shipped reference almost always means you meant to replace
   it — either overwrite it programmatically with `overwrite=True` or
   swap to `import_path:` so the shadow is explicit.

Same-tier collisions are hard errors for entry points (CI catches them
above) and last-writer-wins for programmatic registrations.

## Supply-chain posture

Three controls protect consumer images from poisoned plugins:

### 1. Opt-in discovery

```
export HARNESS_PLUGINS_FROM_ENTRY_POINTS=1
```

Entry-point discovery is **off by default**. A compromised dependency on
`site-packages` does not self-register unless the operator explicitly
turns the flag on. Lock down production images with the opposite:

```dockerfile
ENV HARNESS_PLUGINS_FROM_ENTRY_POINTS=0   # AWS enterprise locked image
```

Use programmatic `registry.register()` or `import_path:` in those
environments instead.

### 2. Hashed lockfiles

[`Dockerfile.extension-template`](../../Dockerfile.extension-template) bakes
in `pip install --no-cache-dir --require-hashes -r requirements.lock.txt`.
Generate the lockfile with hashes:

```bash
uv pip compile --generate-hashes requirements.in -o requirements.lock.txt
# or
pip-compile --generate-hashes requirements.in
```

Every wheel body is verified against the `sha256` in the lockfile. A
compromised mirror cannot swap the contents of a package without a
lockfile edit.

### 3. `--strict` in CI

```yaml
- name: Plugin registry sanity
  run: emonk-harness plugin ls --strict
```

Wire this into your CI. It is the last line of defense against an
unnoticed name collision sneaking into production.

## Runtime hotfix

The registry supports programmatic `register()` at runtime, so a sidecar
process *could* patch the registry after boot — but this is explicitly
**not recommended** because replaced factories do not update
already-built `CompiledAgent` instances. The supported hotfix model is
redeploy with the fixed `import_path:` string.

## Runnable snippet

```python
# Print every shadowed entry to spot cross-tier collisions.
from emonk.core.harness.extensions import Checkpointer

for entry in Checkpointer.registry.entries():
    shadowed = getattr(entry, "shadowed_sources", ()) or ()
    if shadowed:
        print(f"SHADOW: {entry.name} (winner={entry.source}, shadowed={shadowed})")
```
