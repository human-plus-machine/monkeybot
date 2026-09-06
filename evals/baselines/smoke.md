# Smoke baseline (Phase 2 — tracker in tree, defaults off)

Captured 2026-09-05 against `evals/smoke_agent` on `feat/verifier-agent` after Phase 2. Machine-readable twin: [`smoke.json`](smoke.json).

The goal ledger and progress tracker are wired but **off** in the default smoke YAML, so these numbers are still the harness + model cost without classifier or tracker overhead. A separate verifier-on smoke (`monkeybot.verifier-on.yaml`, `escalation.max_severity: none`) passed **11/11 with zero `VerifierVerdict` events** (`max_verdicts: 0`). That on-config run is not this baseline: it spent extra classifier tokens and is not comparable for the token/latency gates.

## Setup

| | |
|---|---|
| Agent | `ollama-cloud` / `glm-5.3-flash` |
| Judge | skipped (`JUDGE_PROVIDER=fake`) |
| Gateway | `http://127.0.0.1:8787`, `SANDBOX_ENABLED=false` |
| Suite | `evals/suites/smoke.yaml` (11 scenarios) |
| Wall time | ~213s for the suite (plus 5s between scenarios) |

Quality scores are empty on purpose. Pointing deepeval's `GPTModel` at `https://ollama.com/v1` with the same GLM id timed out at 180s per scenario and recorded no metric scores. Harness assertions (`required_tools`, `max_verdicts: 0`, `response_contains`, …) still ran.

Cost is `$0.0000` because `glm-5.3-flash` is not in `MODEL_PRICING`. Token and latency gates are the ones that will move when the ledger is enabled on this suite.

Do **not** add `--require-baseline` to `.github/workflows/live-eval-smoke.yml` until CI uses this same provider. That workflow still expects `NVIDIA_API_KEY`; a mixed NVIDIA-vs-Ollama comparison would be noise, and a `$0` cost baseline would trip the +15% cost gate the moment pricing is filled in.

## Suite totals

| Metric | Value |
|---|---:|
| Passed | 11/11 |
| Failed / errored | 0 / 0 |
| Total tokens | 449,500 |
| Cost | $0.0000 (unpriced model) |
| Mean latency | 13,988 ms |
| p95 latency | 23,756 ms |
| Tool errors | 0 |

p95 is `multi_turn/task_tracking`. `memory/recall_single_session` is the heaviest this run (the model took a long `run_command` path). Variance vs Phase 1 is model noise with verifier still off.

## Per scenario

| Scenario | Status | Tokens in / out | Latency | Tools | Notes |
|---|---|---:|---:|---|---|
| `tools/core_read` | passed | 12,944 / 152 | 2.8s | `read_file` | |
| `tools/core_write` | passed | 18,818 / 228 | 3.7s | `write_file`, `read_file` | |
| `tools/core_run_command` | passed | 12,732 / 227 | 2.7s | `run_command` | |
| `skills/skill_invocation` | passed | 25,909 / 1,217 | 10.1s | `list_skills`, `read_file` | |
| `subagents/dispatch_complete` | passed | 15,487 / 711 | 17.9s | `task` ×1 | |
| `memory/recall_single_session` | passed | 200,886 / 7,856 | 65.6s | `glob` ×2, `read_file`, `run_command` ×20 | |
| `memory/recall_cross_session` | passed | 31,895 / 1,379 | 11.1s | `glob`, `read_file` ×3 | |
| `multi_turn/task_tracking` | passed | 51,052 / 3,112 | 23.8s | `glob` ×3, `read_file` ×5, `run_command` | |
| `mcp/tool_invoke` | passed | 18,738 / 853 | 6.9s | `fixture__echo` ×2 | |
| `mcp/list_resources` | passed | 18,943 / 562 | 5.0s | `list_mcp_resources`, `read_mcp_resource` | |
| `mcp/get_prompt` | passed | 25,444 / 355 | 4.4s | `enable_mcp`, `list_mcp_prompts`, `get_mcp_prompt` | |

## What this is for

Later verifier phases should diff against `smoke.json` with `evals.report --suite smoke --baseline evals/baselines/smoke.json`. Expected pressure vs this snapshot: one extra model call per human message once the ledger is on, plus `tail_grace_s` on latency. `max_verdicts: 0` stays on smoke; `budget_burn` / `no_progress` are logged only so healthy long tool loops do not fail that pin.
