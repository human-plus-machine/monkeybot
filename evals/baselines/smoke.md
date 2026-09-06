# Smoke baseline (Phase 5 — replan in tree, defaults off)

Captured 2026-09-05 against `evals/smoke_agent` on `feat/verifier-agent` after Phase 5. Machine-readable twin: [`smoke.json`](smoke.json).

Ledger, tracker, durable `verifierVerdict` rows, PRE_TOOL nudge, and doom-loop `replan` are wired but **off** in the default smoke YAML. A separate verifier-on smoke (`monkeybot.verifier-on.yaml`) passed **11/11 with zero `VerifierVerdict` events**. That on-config run spends extra classifier tokens and is not this baseline.

## Setup

| | |
|---|---|
| Agent | `ollama-cloud` / `glm-5.3-flash` |
| Judge | skipped (`JUDGE_PROVIDER=fake`) |
| Gateway | `http://127.0.0.1:8787`, `SANDBOX_ENABLED=false` |
| Suite | `evals/suites/smoke.yaml` (11 scenarios) |
| Wall time | ~180s for the suite (plus 5s between scenarios) |

Quality scores are empty on purpose. Pointing deepeval's `GPTModel` at `https://ollama.com/v1` with the same GLM id timed out at 180s per scenario and recorded no metric scores. Harness assertions (`required_tools`, `max_verdicts: 0`, `response_contains`, …) still ran.

Cost is `$0.0000` because `glm-5.3-flash` is not in `MODEL_PRICING`. Token and latency gates are the ones that will move when the ledger is enabled on this suite.

Do **not** add `--require-baseline` to `.github/workflows/live-eval-smoke.yml` until CI uses this same provider. That workflow still expects `NVIDIA_API_KEY`; a mixed NVIDIA-vs-Ollama comparison would be noise, and a `$0` cost baseline would trip the +15% cost gate the moment pricing is filled in.

## Suite totals

| Metric | Value |
|---|---:|
| Passed | 11/11 |
| Failed / errored | 0 / 0 |
| Total tokens | 396,114 |
| Cost | $0.0000 (unpriced model) |
| Mean latency | 11,047 ms |
| p95 latency | 23,690 ms |
| Tool errors | 1 (`memory/recall_cross_session`) |

p95 is `memory/recall_single_session`. Variance vs Phase 4 is model noise with verifier still off.

## Per scenario

| Scenario | Status | Tokens in / out | Latency | Tools | Notes |
|---|---|---:|---:|---|---|
| `tools/core_read` | passed | 12,953 / 241 | 3.1s | `read_file` | |
| `tools/core_write` | passed | 18,953 / 250 | 3.4s | `write_file`, `read_file` | |
| `tools/core_run_command` | passed | 12,813 / 190 | 2.6s | `run_command` | |
| `skills/skill_invocation` | passed | 25,908 / 1,252 | 9.6s | `list_skills`, `read_file` | |
| `subagents/dispatch_complete` | passed | 24,537 / 1,260 | 20.4s | `task` ×1 | |
| `memory/recall_single_session` | passed | 71,638 / 2,698 | 23.7s | | |
| `memory/recall_cross_session` | passed | 45,360 / 1,838 | 15.9s | | 1 tool error |
| `multi_turn/task_tracking` | passed | 108,192 / 3,356 | 29.6s | | |
| `mcp/tool_invoke` | passed | 12,662 / 224 | 2.8s | | |
| `mcp/list_resources` | passed | 25,378 / 409 | 5.3s | | |
| `mcp/get_prompt` | passed | 25,511 / 491 | 5.2s | | |

## What this is for

Later verifier phases should diff against `smoke.json` with `evals.report --suite smoke --baseline evals/baselines/smoke.json`. Expected pressure vs this snapshot: one extra model call per human message once the ledger is on, plus `tail_grace_s` on latency. `max_verdicts: 0` stays on smoke.
