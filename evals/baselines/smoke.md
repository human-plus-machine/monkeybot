# Smoke baseline (Phase 1 — ledger in tree, defaults off)

Captured 2026-09-05 against `evals/smoke_agent` on `feat/verifier-agent` after Phase 1. Machine-readable twin: [`smoke.json`](smoke.json).

The goal ledger is wired but **off** in the default smoke YAML, so these numbers are still the harness + model cost without a classifier call per message. Diff against the Phase 0 snapshot for noise vs real ledger cost when `verifier.ledger.enabled` is turned on.

## Setup

| | |
|---|---|
| Agent | `ollama-cloud` / `glm-5.3-flash` |
| Judge | skipped (`JUDGE_PROVIDER=fake`) |
| Gateway | `http://127.0.0.1:8787`, `SANDBOX_ENABLED=false` |
| Suite | `evals/suites/smoke.yaml` (11 scenarios) |
| Wall time | ~167s for the suite (plus 5s between scenarios) |

Quality scores are empty on purpose. Pointing deepeval's `GPTModel` at `https://ollama.com/v1` with the same GLM id timed out at 180s per scenario and recorded no metric scores. Harness assertions (`required_tools`, `max_verdicts: 0`, `response_contains`, …) still ran.

Cost is `$0.0000` because `glm-5.3-flash` is not in `MODEL_PRICING`. Token and latency gates are the ones that will move when the ledger is enabled on this suite.

Do **not** add `--require-baseline` to `.github/workflows/live-eval-smoke.yml` until CI uses this same provider. That workflow still expects `NVIDIA_API_KEY`; a mixed NVIDIA-vs-Ollama comparison would be noise, and a `$0` cost baseline would trip the +15% cost gate the moment pricing is filled in.

## Suite totals

| Metric | Value |
|---|---:|
| Passed | 11/11 |
| Failed / errored | 0 / 0 |
| Total tokens | 435,405 |
| Cost | $0.0000 (unpriced model) |
| Mean latency | 9,793 ms |
| p95 latency | 10,748 ms |
| Tool errors | 2 (`memory/recall_single_session`, `memory/recall_cross_session`) |

p95 is now `memory/recall_single_session`. `multi_turn/task_tracking` is the heaviest token user this run (model chose a longer tool path). Core tool scenarios sit around 1.6–2.8s.

## Per scenario

| Scenario | Status | Tokens in / out | Latency | Tools | Notes |
|---|---|---:|---:|---|---|
| `tools/core_read` | passed | 12,972 / 204 | 2.5s | `read_file` | |
| `tools/core_write` | passed | 18,938 / 197 | 2.7s | `write_file`, `read_file` | |
| `tools/core_run_command` | passed | 12,807 / 270 | 2.8s | `run_command` | |
| `skills/skill_invocation` | passed | 26,128 / 1,255 | 7.9s | `list_skills`, `read_file` | |
| `subagents/dispatch_complete` | passed | 13,806 / 433 | 8.0s | `task` ×1 | |
| `memory/recall_single_session` | passed | 32,293 / 1,843 | 10.7s | `glob`, `read_file`, `run_command` | 1 tool error |
| `memory/recall_cross_session` | passed | 32,015 / 1,868 | 10.2s | `run_command`, `read_file` ×2, `glob` | 1 tool error |
| `multi_turn/task_tracking` | passed | 205,487 / 10,852 | 52.1s | `write_file` ×12, `run_command` ×11, `glob` ×6 | 3 turns; heaviest token use |
| `mcp/tool_invoke` | passed | 12,615 / 145 | 1.6s | `fixture__echo` | |
| `mcp/list_resources` | passed | 25,172 / 318 | 5.0s | `enable_mcp`, `list_mcp_resources`, `read_mcp_resource` | |
| `mcp/get_prompt` | passed | 25,328 / 459 | 4.2s | `enable_mcp`, `list_mcp_prompts`, `get_mcp_prompt` | |

## What this is for

Later verifier phases should diff against `smoke.json` with `evals.report --suite smoke --baseline evals/baselines/smoke.json`. Expected pressure vs this snapshot: one extra model call per human message once the ledger is on, plus `tail_grace_s` on latency. `max_verdicts: 0` stays until a consumer is wired.
