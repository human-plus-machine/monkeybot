# 1. Goals and non-goals

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


Goals:

1. Every harness feature has a named owner tier and named cases. No feature is covered only by "a live model happened to call it once."
2. The deterministic tiers (T1–T3) catch harness regressions on every push, cost nothing, and finish in under 5 minutes with `-n auto`.
3. The live tiers (T4–T5) measure model + harness behavior and are judged statistically against a baseline keyed by provider and model.
4. One scenario schema, one assertion engine, one doubles package (`testkit/`). No test module imports from another `test_*.py` module, in any tier.
5. Every bug fix lands with a regression case (section 10.1).
6. You can tell a test's tier from its path alone: `tests/**` is T1, `regression/t2/**` is T2, and so on. The one exception is `regression/selftest/**`, which tests the regression machinery itself and is marked `t1`.
7. Every failure, from any tier, is written to a run file that is enough on its own to reproduce and fix it later. Agents that run tests report failures; they do not fix them unless asked. Gates stay exactly as strict as before.
8. One document, `docs/testing.md`, says which tests exist, in which category and tier, what each one expects, and which file covers it; a test fails when the document and the tests disagree.

Non-goals:

- Rewriting the existing unit tests. Tier 1 moves them (`git mv`, history preserved) and rewires their helper imports to `testkit/`; test bodies do not change in that step.
- Moving `cli/tests/` or `integrations/browser-mcp/tests/`.
- Production trace mining (the "tier 6" from the original discussion).
- Testing `integrations/browser-mcp` internals. That package keeps its own suite.
- Performance benchmarking. `profiling/` already owns that.


## 2. First principles

### 2.1 Assumptions baked into the request

| # | Assumption | Status |
|---|---|---|
| A1 | The existing unit tests become tier 1 and move into a structure that makes sense at the repo root. | Given by the owner (revised 2026-10-25; earlier draft kept `tests/core` untouched). |
| A2 | Tier 1 = the existing unit tests + new cross-cutting contract tests, both under root `tests/`. | Chosen because `tests/` is where pytest, IDEs, `scripts/precommit-pytest.sh`, and `ci-local.sh` already look, and tier 1 starts at 196 test files (3,320 collected tests). |
| A2b | Moving files with `git mv` and only rewriting imports keeps behavior identical. | Verifiable: the collected test count and pass/fail set must be identical before and after (Phase 0a exit criteria). |
| A3 | Given a fixed provider output, clock, and filesystem, the harness is deterministic. | Mostly true. Exceptions found: background summary timing, hook settlement timeouts (`MONKEYBOT_HOOK_SETTLEMENT_TIMEOUT_S`), scheduler wall clock, `POST_TOOL` hooks scheduled in the background, xdist ordering. Section 5.1 adds a fake clock and explicit settlement waits. |
| A4 | Live runs can be turned into replay cassettes. | Verified. `TranscriptWriter` `ProviderResponse` records hold full `text`, `thinking`, `tool_requests[{call_id,name,args}]` and `usage`. They do not hold token deltas, so golden SSE streams need a new capture. |
| A5 | One live run proves little. | True by construction; that is what T5 is for. |
| A6 | Live runs have a working provider key. | **Resolved by the owner (2026-10-25):** all live runs use Ollama Cloud with the `OLLAMA_API_KEY` from the local environment (section 5.11). Today's workflow only has `NVIDIA_API_KEY`, so it is switched to `OLLAMA_API_KEY`; CI can only run live jobs once that is also a repository secret (D1). |
| A7 | The deterministic suites run on every PR. | **False.** `.github/workflows/ci.yml` is `workflow_dispatch` only. The real gate is the pre-push hook running `scripts/ci-local.sh`. See D2. |
| A8 | monkeyapp can vendor a schema file from monkeybot. | Not verified. Cross-repo change. |
| A9 | An agent that sees a failing test will try to fix it, and the owner does not want that by default. | Given by the owner. |
| A10 | Telling the agent in more than one place (run output, an always-on rule, a hook) is enough to stop most auto-fixing. | Not verified; it is a behavioral bet. The hook is the strongest of the three because it attaches the instruction to the tool result itself. A hard block (denying edits while failures are open) is listed as an option under D9. |
| A11 | Recording failures must not change any test result or gate. | Chosen. A ledger that turned failures green would hide them from pre-push and CI too. |
| A12 | monkeyapp's `docs/testing.md` layout (tiers → gates → architecture → rules → catalog with ID/Expect/Pri/test path, enforced by a parser test) works for a Python harness. | Verified by reading `monkeyapp/docs/testing.md` and `tests/harness/catalog.ts`; the structure has nothing JS-specific. |

### 2.2 What is known for certain

1. Harness bugs (dispatch, guards, policies, persistence, wire format) are reproducible with a scripted model. They should never need a live model to be caught.
2. Model-quality questions (does the agent pick the right tool, fix the bug, stay on task) cannot be answered by a scripted model.
3. A check on resulting state (files, exit codes, stored rows, event stream) is more reliable than an LLM judge.
4. Two processes that talk over a wire need a shared contract or one side's tests go stale. monkeyapp's fake gateway already drifted (section 6.U).
5. Vendor SDKs change their wire format on upgrade. Tests that build the parsed object by hand cannot see that.

### 2.3 What I could not verify

- Whether a Firestore emulator or Postgres service container is acceptable in CI (cost and runtime).
- Whether a macOS runner is available for the computer-use tools.
- Whether `httpx.MockTransport` can be injected into `google-genai`. It uses its own transport, so Gemini wire cassettes may need the second-choice approach in section 5.5.2.
- Which provider and model produced `evals/baselines/smoke.json`. The file has no provider/model field. It moves unchanged to `regression/t4/baselines/smoke.json` (P4-01); the first Ollama Cloud run should refresh it with `--update-baseline` once the user confirms.
- Whether Cursor's `postToolUse` `additional_context` is shown to the model for every `Shell` call in every agent mode, and whether other agents (Claude Code in `.claude/worktrees`, cloud agents) read `.cursor/rules` and `.cursor/hooks.json`. If not, they only get the run-output banner.
- Whether `OLLAMA_API_KEY` is visible to every process that starts a live run. It was not set in the agent's sandboxed shell or in the repo `.env` when this was written, so it is presumably exported in the owner's own terminal profile. The preflight in 5.11 makes a missing key a loud failure, not a skip.
- Ollama Cloud's rate limits and quota for this key, and so how many T5 trials fit in one night.
- Which Ollama Cloud model names are available to this key for the judge (D12).
- Whether xdist forwards collection errors from workers to the controller in every pytest-xdist version pinned here; the ledger test (5.9.6) checks it.
- Whether the existing `tests/evals` suite is collected in every local pre-push run. `ci-local.sh` runs `pytest tests/`, which includes it, but `--if-needed` can skip the whole run.
