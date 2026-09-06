# Smoke baseline (Phase 4 — nudge in tree, defaults off)

Captured 2026-09-05 against `evals/smoke_agent` on `feat/verifier-agent` after Phase 4. Machine-readable twin: [`smoke.json`](smoke.json).

Ledger, tracker, durable `verifierVerdict` rows, and PRE_TOOL nudge injection are wired but **off** in the default smoke YAML. A separate verifier-on smoke (`monkeybot.verifier-on.yaml`) passed **11/11 with zero `VerifierVerdict` events**. That on-config run spends extra classifier tokens and is not this baseline.

## Setup

| | |
|---|---|
| Agent | `ollama-cloud` / `glm-5.3-flash` |
| Judge | skipped (`JUDGE_PROVIDER=fake`) |
| Gateway | `http://127.0.0.1:8787`, `SANDBOX_ENABLED=false` |
| Suite | `evals/suites/smoke.yaml` (11 scenarios) |
| Wall time | ~160s for the suite (plus 5s between scenarios) |

Quality scores are empty on purpose. Pointing deepeval's `GPTModel` at `https://ollama.com/v1` with the same GLM id timed out at 180s per scenario and recorded no metric scores. Harness assertions (`required_tools`, `max_verdicts: 0`, `response_contains`, …) still ran.

Cost is `$0.0000` because `glm-5.3-flash` is not in `MODEL_PRICING`. Token and latency gates are the ones that will move when the ledger is enabled on this suite.

Do **not** add `--require-baseline` to `.github/workflows/live-eval-smoke.yml` until CI uses this same provider. That workflow still expects `NVIDIA_API_KEY`; a mixed NVIDIA-vs-Ollama comparison would be noise, and a `$0` cost baseline would trip the +15% cost gate the moment pricing is filled in.

## Suite totals

| Metric | Value |
|---|---:|
| Passed | 11/11 |
| Failed / errored | 0 / 0 |
| Total tokens | 322,106 |
| Cost | $0.0000 (unpriced model) |
| Mean latency | 9,174 ms |
| p95 latency | 22,254 ms |
| Tool errors | 0 |

p95 is `memory/recall_single_session`. Variance vs Phase 3 is model noise with verifier still off (tokens and latency both came down).

## Per scenario

| Scenario | Status | Tokens in / out | Latency | Tools | Notes |
|---|---|---:|---:|---|---|
| `tools/core_read` | passed | 12,954 / 278 | 3.1s | `read_file` | |
| `tools/core_write` | passed | 18,947 / 245 | 3.5s | `write_file`, `read_file` | |
| `tools/core_run_command` | passed | 12,813 / 332 | 2.6s | `run_command` | |
| `skills/skill_invocation` | passed | 32,662 / 2,007 | 14.4s | `list_skills`, `read_file`, `glob`, MCP list | |
| `subagents/dispatch_complete` | passed | 14,673 / 583 | 10.8s | `task` ×1 | |
| `memory/recall_single_session` | passed | 56,654 / 3,254 | 23.2s | | |
| `memory/recall_cross_session` | passed | 31,630 / 1,313 | 9.7s | | |
| `multi_turn/task_tracking` | passed | 66,902 / 2,864 | 22.3s | | |
| `mcp/tool_invoke` | passed | 12,641 / 179 | 2.4s | | |
| `mcp/list_resources` | passed | 25,143 / 305 | 4.0s | | |
| `mcp/get_prompt` | passed | 25,327 / 400 | 4.9s | | |

## What this is for

Later verifier phases should diff against `smoke.json` with `evals.report --suite smoke --baseline evals/baselines/smoke.json`. Expected pressure vs this snapshot: one extra model call per human message once the ledger is on, plus `tail_grace_s` on latency. `max_verdicts: 0` stays on smoke.
