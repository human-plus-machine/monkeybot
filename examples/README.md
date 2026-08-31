# monkeybot Examples

Example skills and deploy adapters for the monkeybot harness.

## Skills

Copy into your agent `SKILLS_PATH` (scaffolded default: `./skills`). Skills are
read-only to file tools and are not mirrored into `workspace/`.

| Skill | Purpose |
|---|---|
| [diagnostics](skills/diagnostics/) | Reference skill (`SKILL.md` + plain async entry point) |
| [browser](skills/browser/) | Browser MCP workflow + playbooks |
| [image-generator](skills/image-generator/) | Vertex image generation script |

```bash
cp -r examples/skills/diagnostics/ ./skills/
```

## Deploy patterns

| Example | Pattern |
|---|---|
| [agentcore](agentcore/) | AWS Bedrock AgentCore (Pattern C) |
| [agentengine](agentengine/) | Vertex AI Agent Engine (Pattern C) |
| [lambda](lambda/) | AWS Lambda (Pattern B) |
| [cloud-functions](cloud-functions/) | GCP Cloud Functions (Pattern B) |

## Resources

- [Getting Started](../docs/getting-started.md)
- [Local Ollama prefix cache](../docs/ollama-local.md) — `keep_alive`, `num_ctx`, prefix-stable Modelfile
- Config templates: `cli/src/monkeybot_cli/scaffold_defaults/` (via `monkeybot new`)
- [Cloud deployment](../docs/cloud-deployment-design.md)
- [SSE gateway / custom UI](../docs/sse-gateway-ui.md)

## Ollama

| Example | Purpose |
|---|---|
| [ollama/PrefixStable.Modelfile](ollama/PrefixStable.Modelfile) | Pin tool schemas in the system block so local KV prefix cache survives tool turns |
