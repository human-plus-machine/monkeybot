# 11. Known issues and doc drift found during exploration

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Fix these during the relevant phase; several make current results misleading.

1. Live CI provider mismatch: workflow has only `NVIDIA_API_KEY` and an NVIDIA judge; smoke agent is `ollama-cloud` / `glm-5.3-flash` needing `OLLAMA_API_KEY`; baseline was recorded with `JUDGE_PROVIDER=fake` and $0 cost. **Resolution:** Ollama Cloud for agent and judge (5.11); the old baseline is replaced by a fresh one in Phase 5 rather than reused.
2. `ci.yml` is `workflow_dispatch` only; `docs/live-evals.md` says `tests/evals` runs on every push/PR. (D2)
3. `monkeybot.verifier-on.yaml` has `judge.enabled: false` and `max_severity: none`; drift results so far measure tracker verdicts only. `docs/live-evals.md` still says the verifier-on config is a parsed no-op.
4. `tool_categories_used` is an intersection, not a subset.
5. Live loader silently ignores unknown assertion keys; drift `verifier_on/off` blocks silently vanish when `EVAL_VERIFIER_MODE` is unset.
6. `max_tool_errors` forgiveness is by tool name only.
7. `FakeProvider` returns nothing past the script, masking under-scripted tests as empty completions.
8. `agent_event_to_wire_dict` (test-only serializer) differs from production `event_to_json`; `docs/sse-gateway-ui.md` documents the test shape (`chat_request_id`).
9. monkeyapp fake gateway drift (section 6.U).
10. `scripts/create_schema.py` does not run the `_ensure_*` upgrades or the branch rebuild.
11. Goal ledger is absent on Postgres/Firestore; usage/runs/loops/turn locks are not agent-scoped; turn-lock PK is `session_id` only.
12. Default gateway port is 8000 in `main.py`, 8080 in Docker and docs; Docker CMD runs SSE-only while `monkeybot run` runs SSE + realtime.
13. `docs/features.md` subagent defaults (600 s / 25 turns) are stale versus `SubagentSettings` (3600 s / 1000); it also says the worker has no claim heartbeat (it does).
14. `docs/skills.md` / `docs/features.md` mention a legacy skill loader; `src/monkeybot/skills/` and several `providers/` / `tests/providers/` entries exist only as stale `__pycache__`.
15. `evals/TEST_COVERAGE.md` says MCP scenarios had no green live run; the 2026-09-06 baseline shows them passing.
16. `context/summarization_trigger` can pass without compacting depending on model verbosity.
17. Smoke scenarios share one workspace, memory palace, and sqlite in one process, so order matters. T4 runner should give each scenario a fresh session workspace or document which ones intentionally share.
