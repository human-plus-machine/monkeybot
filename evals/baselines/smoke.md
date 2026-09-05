# Smoke baseline (pre-verifier)

Captured 2026-09-05 against `evals/smoke_agent` on `feat/verifier-agent`. Machine-readable twin: [`smoke.json`](smoke.json).

This is the **pre-verifier** reference: `verifier:` is parsed but has no consumer, so these numbers are the harness + model cost of the smoke suite before the ledger, tracker, or judge add work.

## Setup

| | |
|---|---|
| Agent | `ollama-cloud` / `glm-5.3-flash` |
| Judge | skipped (`JUDGE_PROVIDER=fake`) |
| Gateway | `http://127.0.0.1:8787`, `SANDBOX_ENABLED=false` |
| Suite | `evals/suites/smoke.yaml` (11 scenarios) |
| Wall time | ~216s for the suite (plus 5s between scenarios) |

Quality scores are empty on purpose. Pointing deepeval's `GPTModel` at `https://ollama.com/v1` with the same GLM id timed out at 180s per scenario and recorded no metric scores (`All metrics errored across every test case`). Harness assertions (`required_tools`, `max_verdicts: 0`, `response_contains`, …) still ran.

Cost is `$0.0000` because `glm-5.3-flash` is not in `MODEL_PRICING`. Token and latency gates are the ones that will move when the verifier lands.

Do **not** add `--require-baseline` to `.github/workflows/live-eval-smoke.yml` until CI uses this same provider. That workflow still expects `NVIDIA_API_KEY`; a mixed NVIDIA-vs-Ollama comparison would be noise, and a `$0` cost baseline would trip the +15% cost gate the moment pricing is filled in.

## Suite totals

| Metric | Value |
|---|---:|
| Passed | 11/11 |
| Failed / errored | 0 / 0 |
| Total tokens | 405,045 |
| Cost | $0.0000 (unpriced model) |
| Mean latency | 14,322 ms |
| p95 latency | 34,105 ms |
| Tool errors | 1 (`memory/recall_cross_session`) |

p95 is the memory-recall pair. Core tool scenarios sit around 2.5–3.1s.

## Per scenario

| Scenario | Status | Tokens in / out | Latency | Tools | Notes |
|---|---|---:|---:|---|---|
| `tools/core_read` | passed | 12,013 / 231 | 2.5s | `read_file` | |
| `tools/core_write` | passed | 17,920 / 241 | 3.1s | `write_file`, `read_file` | |
| `tools/core_run_command` | passed | 12,222 / 385 | 3.1s | `run_command` | |
| `skills/skill_invocation` | passed | 38,336 / 1,754 | 16.1s | `list_skills`, `read_file`, `enable_mcp`, `fixture__echo` | 2 turns |
| `subagents/dispatch_complete` | passed | 14,131 / 684 | 15.9s | `task` ×1 | |
| `memory/recall_single_session` | passed | 101,147 / 3,340 | 34.1s | `run_command` ×11, `write_file` | 2 turns; heaviest token use |
| `memory/recall_cross_session` | passed | 95,044 / 4,041 | 52.6s | `run_command` ×9, `read_file` ×2, `write_file`, `glob` | 1 tool error, assertions still passed |
| `multi_turn/task_tracking` | passed | 43,435 / 1,933 | 16.2s | `glob` ×3 | 3 turns |
| `mcp/tool_invoke` | passed | 12,667 / 493 | 3.7s | `fixture__echo` | |
| `mcp/list_resources` | passed | 18,845 / 496 | 3.9s | `list_mcp_resources`, `read_mcp_resource` | |
| `mcp/get_prompt` | passed | 25,310 / 377 | 6.4s | `list_mcp_prompts`, `get_mcp_prompt`, `enable_mcp` | |

## What this is for

Later verifier phases should diff against `smoke.json` with `evals.report --suite smoke --baseline evals/baselines/smoke.json`. Expected pressure vs this snapshot: one extra model call per human message once the ledger is on, plus `tail_grace_s` on latency. `max_verdicts: 0` stays until a consumer is wired.
