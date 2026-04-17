# Greenfield Agent Harness example

Runnable skeleton an enterprise consumer would deploy on top of monkey-bot's
Agent Harness.

## What it shows

- Building `HarnessConfig` in Python (the canonical spec mode)
- Wiring a custom tool via `ToolSpec.import_path`
- Mounting the control plane and AgentCore routes
- Hooking Phoenix and DeepEval as event subscribers (illustrative stubs)
- Writing RunPackages to local disk

## Run

```bash
pip install -e .
python -m examples.greenfield_agent.main
```

Then:

```bash
curl -X POST http://localhost:8080/agentcore/invocations \
  -H 'content-type: application/json' \
  -d '{
    "inputText":"hello",
    "sessionId":"demo-1",
    "sessionState":{"sessionAttributes":{"user_id":"alice"}}
  }'

curl http://localhost:8080/harness/introspect
curl http://localhost:8080/harness/runs
```

Replace the Phoenix and DeepEval stubs with real handlers. Swap
`SandboxSpec(backend="local_shell")` for `"modal"` or a custom adapter before
promoting to production. See `docs/agent-harness.md` for full
guidance.
