# Deploy on AWS Bedrock AgentCore Runtime

[AgentCore Runtime](https://docs.aws.amazon.com/bedrock/latest/userguide/agents-core.html) is a managed **HTTP container** runtime (linux/arm64). monkeybot ships Pattern C wiring via `monkeybot.core.bootstrap` and `examples/agentcore/`.

For Lambda-style action groups, see `docs/deploy-pattern-c-agent-platform.md`. This page covers the **container HTTP** contract and CLI pitfalls.

---

## Container contract

| Requirement | Value |
|---|---|
| Listen address | `0.0.0.0:8080` |
| Health | `GET /ping` → `{"status": "Healthy", "time_of_last_update": <unix_seconds>}` |
| Invoke | `POST /invocations` with JSON body (your adapter parses session + message) |
| Architecture | **linux/arm64** image only |

Example HTTP adapter: `examples/agentcore/runtime_app.py` (FastAPI).

---

## Build and register (manual path)

The official `bedrock-agentcore-starter-toolkit` generates its own Dockerfile from a Jinja template with **no override**. For custom installs (monorepo parent, system packages, non-root user), build your image and register with the control plane API:

```bash
# Build for AgentCore (arm64)
docker build --platform linux/arm64 -t my-monkeybot-agent .

# Push to ECR, then create runtime (names: [a-zA-Z][a-zA-Z0-9_]{0,47} — no hyphens)
aws bedrock-agentcore-control create-agent-runtime \
  --agent-runtime-name my_agent_runtime \
  --protocol-configuration '{"serverProtocol":"HTTP"}' \
  ...
```

**Runtime name:** `my-agent` fails; use `my_agent`.

**Protocol:** pass `--protocol-configuration '{"serverProtocol":"HTTP"}'` even when HTTP seems implied.

---

## Invoke from CLI

`aws bedrock-agentcore invoke-agent-runtime --payload` expects **base64-encoded** bytes, not raw JSON. Default content types yield FastAPI **422** before your app logs run.

```bash
PAYLOAD=$(echo -n '{"sessionId":"s1","inputText":"hello"}' | base64)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$ARN" \
  --payload "$PAYLOAD" \
  --content-type application/json \
  --accept application/json
```

---

## Environment variables

Prefer `file://env.json` for values containing commas (CLI shorthand `--environment-variables KEY=val,KEY2=val2` breaks on commas inside values, e.g. `python3,ls,cat` allowlists).

Minimum for bootstrap examples:

| Variable | Purpose |
|---|---|
| `DB_URL` | SQLite or Postgres history |
| `AGENT_MD_PATH` | Path to `AGENT.md` |
| `SKILLS_PATH` | Skills root (each subfolder needs `SKILL.md`) |
| `WORKSPACE_ROOT` | Workspace for `read_file` / `run_command` cwd (alias: `MONKEYBOT_WORKSPACE_ROOT`; handlers use `resolve_agent_workspace_root()`) |
| `MODEL_PROVIDER` | e.g. `aws_bedrock` with `monkeybot[bedrock]` |
| `MODEL_NAME` | Bedrock model id |
| `COMMAND_ALLOWLIST_CONFIG` | Optional; path to `command_allowlist.yaml` |

Set `LOG_LEVEL` to `INFO` or `info` (case-insensitive).

---

## Harness notes

- Use `create_harness_deps()` + `run_pattern_bc_turn()` (see `examples/agentcore/handler.py` for event-shaped handlers).
- Loop failures raise `PatternBcTurnError` instead of returning empty text.
- Empty `run_command` allowlist is no longer the bootstrap default; omit `COMMAND_ALLOWLIST_CONFIG` to use built-in defaults, or ship `monkeybot_config/command_allowlist.yaml`.

---

## Model provider

```bash
pip install "monkeybot[bedrock,postgres,aws]"
export MODEL_PROVIDER=aws_bedrock
export MODEL_NAME=anthropic.claude-sonnet-4-20250514-v1:0   # example; use your account id
export AWS_REGION=us-east-1
```

Bedrock does not support Anthropic `count_tokens` yet; the provider falls back to a character-based estimate for summarization thresholds.

---

## Related

- `examples/agentcore/handler.py` — Lambda/action-group style adapter
- `examples/agentcore/runtime_app.py` — HTTP `/invocations` + `/ping`
- `examples/skills/diagnostics/` — post-deploy health skill
