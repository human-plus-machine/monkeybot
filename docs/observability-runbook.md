# Observability runbook (production)

Operator guide for OpenTelemetry tracing in monkeybot. See also [observability-plan.md](./observability-plan.md) for the full env var table and Phoenix/Langfuse setup.

## Enable tracing

```bash
export MONKEYBOT_OTEL_ENABLED=true
export OTEL_TRACES_EXPORTER=otlp   # or console for local debug
export OTEL_METRICS_EXPORTER=none
export OTEL_LOGS_EXPORTER=none
export OTEL_SERVICE_NAME=monkeybot-gateway
export OTEL_EXPORTER_OTLP_ENDPOINT=https://your-collector:4318
```

Install the optional extra: add `monkeybot[observability]` to the agent `pyproject.toml` dependencies, then run `uv sync`. (Harness checkout: `uv sync --extra observability`.)

## Sampling

| Environment | `OTEL_TRACES_SAMPLER` | `OTEL_TRACES_SAMPLER_ARG` |
|-------------|-------------------------|---------------------------|
| **Production** | `parentbased_traceidratio` | `0.1` (10% of root traces; children follow parent) |
| **Staging / dev** | unset or `parentbased_always_on` | `1.0` or unset |

Production example:

```bash
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1
```

## PII and data minimization

- Span attributes use `truncate()` (default 8 KiB) for user messages, tool I/O, and errors.
- Denylist blocks attribute keys containing `api_key`, `secret`, `password`, `token`, `authorization`, etc. (`gen_ai.*` usage keys are exempt).
- Hook span events record **`tool.name` only** — never tool args or results.
- Use **HTTPS** for `OTEL_EXPORTER_OTLP_ENDPOINT` in production.
- Do not commit secrets in `OTEL_EXPORTER_OTLP_HEADERS`; use your secret store / runtime env injection.

## Cost and volume

- Lower production volume with sampling (above).
- Use an OpenTelemetry Collector for fan-out and optional filtering (see `docs/otel-collector.example.yaml`).
- Tune backend retention (Phoenix / Langfuse) per your compliance budget.

## Troubleshooting

**Tracing not appearing**

1. Confirm `MONKEYBOT_OTEL_ENABLED=true` and `OTEL_TRACES_EXPORTER` is `otlp` or `console`.
2. Check gateway logs for `observability enabled` vs `disabled` messages.
3. Verify OTLP endpoint reachability and TLS.
4. For subagent spans: confirm `traceparent` in task envelope and `OTEL_SERVICE_NAME=monkeybot-subagent` in child process.

**Double LLM spans**

- Do not wrap the gateway `Provider` with `ObservingProvider` — the agent loop already emits `monkeybot.llm.stream`. Use `ObservingProvider` only for direct `provider.stream()` calls outside the loop.
