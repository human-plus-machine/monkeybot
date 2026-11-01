# 6.W CLI, observability, packaging (`OPS`)

> Part of the [regression plan](../../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Code: `cli/` (`monkeybot-cli`: `new`, `refresh`, `validate`, `doctor`, `run`, `chat`, `talk`, `loop`, `trace`, `mcp-login`), `src/monkeybot/cli` (shim), `observability/*`, `docker/`, `docker-compose.yml`, `scaffold/`.

Existing: `cli/tests/*`, `tests/cli/*`, `tests/observability/*`, `tests/acceptance/*`, `tests/test_cold_start.py`, `test_packaging_config.py`, `test_release_script.py`.

| ID | Case | Asserts |
|---|---|---|
| T1-OPS-01 | `scaffold_reexports` | Names re-exported by `monkeybot.scaffold` exist in `monkeybot_cli.scaffold`. |
| T1-OPS-02 | `span_names_contract` | Span names and required attributes for run/turn/llm/tool/summarize/subagent match a committed list. |
| T2-OPS-01 | `cli_run_boots` (`slow`) | `monkeybot run` subprocess with `MODEL_PROVIDER=fake`: `/health` 200, `/realtime/health` 200; SIGINT exits 130. |
| T2-OPS-02 | `cli_new_then_validate` | `monkeybot new` → `monkeybot validate` passes on the scaffold. |
| T2-OPS-03 | `cli_chat_against_gateway` | `monkeybot chat` one message against the in-process gateway prints the scripted reply. |
| T2-OPS-04 | `otel_span_tree` | `MONKEYBOT_OTEL_ENABLED=1`, sqlite exporter, scripted tool turn: span tree run → turn → llm + tool; `TurnComplete.trace_id` equals the root trace id; tool I/O stored whole; secret keys denylisted. |
| T2-OPS-05 | `traceparent_to_subagent` | `task` envelope carries `traceparent`; child spans share the trace id. |
| T2-OPS-06 | `transcript_manifest` | Transcript has `SessionManifest`, provider request/response records, no deltas. |
| T4-OPS-01 | `docker_image_health` (manual/nightly) | Built image answers `/health` on 8080. |
