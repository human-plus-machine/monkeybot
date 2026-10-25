# 3. Tier definitions

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.


| | T1 Contract | T2 Scripted trajectory | T3 Record / replay | T4 Live | T5 Trials |
|---|---|---|---|---|---|
| Driver | Direct calls into one module or a table of modules | Real `loop.run`, or the real FastAPI gateway in-process, with a scripted provider | Committed cassettes replayed through the real code | Real gateway + real provider over HTTP/SSE | T4 suites run N times |
| Deterministic | Yes | Yes | Yes | No | No (statistical) |
| Network | None | None | None | Provider API | Provider API |
| Cost | Free | Free | Free | Tokens | Tokens × N |
| Pass rule | Assertion | Assertion + global invariants (5.3.2) | Assertion + staleness check | Code graders; judge optional | Pass rate vs baseline (5.8) |
| Runs | Pre-push, CI | Pre-push, CI | Pre-push, CI | Release gate, lockfile PRs, manual | Nightly / manual / release |
| Budget | < 60 s total | < 1 s per scenario (real-workspace ones < 15 s) | < 60 s total | ~30–45 min smoke | hours |
| Catches | Contract drift, schema drift, policy math | Harness logic regressions end to end | Harness regressions on realistic model output; SDK wire changes; SSE contract drift | Real provider/gateway/SSE behavior; model + harness fit | Silent quality drops, flakiness |
| Does not catch | Wiring between modules | Whether a real model behaves this way | Whether the model would still choose this after a prompt change | Small regressions (noise) | Harness bugs (use T2) |

## T1 — Contract

Lives in root `tests/`. Two kinds:

- `tests/unit/**`: the existing unit tests, one folder per source package (`tests/unit/core/runtime/` tests `src/monkeybot/core/runtime/`, and so on). Single-module, no agent loop, or a loop driven only by in-file fakes.
- `tests/contracts/**`: new table-driven tests that pin a contract several modules depend on: the SSE schema, the tool error-code catalog, provider conversion invariants, storage-backend parity, config precedence, the harness prompt golden, the route table. If a `tests/unit` test already pins it, it does not get a contract case.

Rule for new tests: if it imports one source package and asserts on its return values, it goes in `tests/unit/<package>/`. If it asserts the same rule across several packages or adapters, it goes in `tests/contracts/`. If it drives `loop.run` or the gateway with a scripted provider and asserts on the trajectory, it is T2 and goes in `regression/t2/`.

## T2 — Scripted trajectory

Two drivers, both using the scripted provider from section 5.1:

- **Loop driver**: `monkeybot.core.runtime.loop.run` with a `TurnContext`, real hooks, and either a recording executor or a real `CoreToolExecutor` over a temp copy of a fixture workspace. This is today's `tests/evals/scenario_runner.py`, extended.
- **Gateway driver**: the real `create_app` with lifespan, a scripted provider resolved through `MODEL_PROVIDER=fake`, and a streaming `GET /sessions/{id}/events` reader. This is new. It is the only way to test `event_to_json`, session admission, replay, cancel, and branch routes end to end without a model.

## T3 — Record / replay

Three cassette kinds (section 5.5):

- **T3a trajectory cassettes**: the `ProviderEvent` sequence a live model produced, replayed through the loop or gateway driver. Converted from transcripts.
- **T3b provider wire cassettes**: raw vendor HTTP/SSE bytes, replayed through the real SDK and adapter. Re-recorded when SDKs change.
- **T3c golden SSE streams**: the framed `id:`/`data:` byte stream for fixed scripted turns. Shared with monkeyapp as the contract.

## T4 — Live

Real gateway (`evals/smoke_agent` successor), real provider, scenarios graded first on state (workspace files, commands, tool trace, verdicts) and only second on an LLM judge. Today's `evals/` moves here.

## T5 — Trials

`trials.py` successor. Runs a T4 suite N times, reports pass k/N per scenario with a Wilson interval, and compares to a baseline keyed by `(suite, provider, model)`.
