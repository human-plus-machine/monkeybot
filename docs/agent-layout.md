# Agent project layout

Every MonkeyBot agent has one root: the directory that contains
`monkeybot_config/`. `monkeybot new` creates this layout:

```
my-agent/
├── monkeybot_config/  # control plane: committed and read-only at runtime
├── skills/            # trusted skill code: committed and read-only at runtime
├── workspace/         # agent-writable working files: gitignored
│   ├── browser/       # browser playbooks and screenshots
│   └── .monkeybot/    # transcripts, spill, and subagent scratch
├── data/              # harness-managed runtime state: gitignored
│   ├── monkeybot.db
│   └── memory/
├── Dockerfile
├── .dockerignore
├── .env.example
├── pyproject.toml
└── uv.lock
```

`monkeybot_config/` and `skills/` are source-controlled inputs. `workspace/` is
the file-tool sandbox, while `data/` is local state that is replaced by managed
backends in cloud deployments. Do not put writable data, browser playbooks, or
screenshots under `skills/`.

## Path resolution

All relative paths in `monkeybot_config/monkeybot.yaml` resolve from the agent
root, never from the process working directory. Startup finds that root from
`MONKEYBOT_CONFIG`, or by walking up from the current directory to the nearest
directory containing `monkeybot_config/`. It then loads the root `.env`, applies
YAML values to still-unset environment variables, and constructs the layout.

The portable override contract is `MONKEYBOT_CONFIG`,
`MONKEYBOT_WORKSPACE_ROOT`, `MONKEYBOT_WORKSPACE_ROOT_OVERRIDE` (absolute path;
beats yaml `paths.workspace_root` for one process — used by Monkeybot Mac
workspace sessions), `SKILLS_PATH`, `DB_URL`, `MEMORY_STORAGE_URI`,
`MCP_CONFIG`, `SANDBOX_ENABLED`, `SANDBOX_SERVER_URL`, `SANDBOX_IMAGE`,
`SANDBOX_API_KEY` (with `SANDBOX_AUTH_TOKEN` accepted as a compatibility alias),
`SANDBOX_SHARED_FILESYSTEM`, and `PORT`. Use absolute override values when a
container or platform places a zone somewhere other than the agent root.

`monkeybot doctor` prints the resolved layout and flags a legacy `skills`
directory nested inside `workspace`. It offers a preview only: inspect and
resolve collisions before moving any existing files.

## File-tool paths and skills

File tools use a virtual two-root namespace:

- `skills/...` reads from the trusted skills root.
- Every other relative path reads from or writes to `workspace/`.

There is no fallback between roots. Writes and patches to `skills/...` are
rejected, and symlinks escaping either root are rejected after real-path checks.
The container runtime also makes `monkeybot_config/` and `skills/` root-owned;
the non-root agent user can write only `workspace/` and `data/`.

## Browser MCP and sandbox defaults

The browser MCP package and static browser skill are bundled into every new
agent, but the `browser` MCP entry is initially `"enabled": false`. Enable it
in `monkeybot_config/mcp.json`, then let the model call `enable_mcp("browser")`
before using `browser__*` tools. Browser playbooks and screenshots belong below
`workspace/browser/`; on ephemeral-workspace deployments they are an
instance-local cache and disappear when the instance is recycled.

The sandbox recipe is bundled in the repository, while the scaffold keeps the
known-pullable `python:3.12` default until the versioned MonkeyBot image is
published. `sandbox.enabled` (or `SANDBOX_ENABLED`) remains the only switch;
set `SANDBOX_IMAGE` to a published MonkeyBot image or another custom image when
the additional sandbox tools are needed.
See [Browser MCP](browser-mcp.md) and [Pattern A](deploy-pattern-a-container.md)
for runtime details.

## Deployment matrix

“Tested locally” means the generated agent was exercised against the local
package build. “Configuration validated” means Compose rendering was checked;
it is not a claim of a running Docker integration test. “Pattern only” is
deployment guidance, not a claim that MonkeyBot runs that managed service in CI.

| Target | Status | config + skills | workspace | data | sandbox | browser |
|---|---|---|---|---|---|---|
| Local CLI | tested locally | plain directories | plain directory | SQLite + local memory | off or Compose sidecar | desktop Chrome or local headless |
| Local Docker / Compose | configuration validated | baked into the agent image | anonymous volume | SQLite volume or Postgres | Compose overlay | headless Chromium in image |
| Cloud Run / ECS Fargate / Container Apps | pattern only | baked into image, read-only | ephemeral `/agent/workspace` | managed DB + object memory | remote, compute-only | in-image Chromium or Browser Use Cloud |
| GKE / EKS / ECS-EC2 / VM | pattern only | baked into image | volume or `emptyDir` | managed DB + object memory, or PVC | co-located sidecar with Docker socket | Chromium sidecar or in-image |
| AWS AgentCore | pattern only | handler bundle | ephemeral or platform file mounts | managed session storage or URI overrides | none or remote compute-only | Browser Use Cloud |
| Vertex Agent Engine | pattern only | packaged source artifact | ephemeral temporary storage | URI overrides | none or remote compute-only | Browser Use Cloud |

Cloud Run's writable filesystem is in memory: workspace files, screenshots, and
browser profiles consume instance memory. Size the service for that workload and
keep browser artifacts bounded. `data/` must use `DB_URL` and
`MEMORY_STORAGE_URI` rather than local files on scale-to-zero targets.

## Remote sandbox contract

OpenSandbox mounts are host bind mounts. A remote sandbox server cannot mount an
agent image's `skills/` directory or its ephemeral workspace across the network.
Set `SANDBOX_SHARED_FILESYSTEM=false` for that topology. MonkeyBot then treats
the sandbox as **compute-only**: commands run in the remote container, with data
passed through command arguments, standard input, and standard output.
Operations needing mounted workspace or skills paths fail with a capability
error; `monkeybot doctor` reports `sandbox: remote (compute-only)`.

The Compose `/tmp/monkeybot-workspace` host-path setup is a local Compose
workaround, not a cloud deployment design. Full mounted-path behavior requires a
co-located runtime that shares the relevant filesystem with the sandbox host.
