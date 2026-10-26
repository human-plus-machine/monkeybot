# Phase 3 — Record / replay (≈ 1 week)

> Part of the [regression plan](../README.md). Section numbers (§5.9, §6.U, D12, …) are global across the plan; the README's section map says which file holds each one.

## Summary (from the roadmap, §8)

- `t3/tools/from_transcript.py`; record the five catalogued T3a cassettes (T3-LOOP-01, 02, T3-CTX-01, T3-SHELL-01, T3-VER-01) from T4 runs. More cassettes (for example `coding/*`, `drift/*`) need new T3 catalog rows first, which the user approves.
- `record_wire.py`; record `ollama_cloud/glm_tool_turn.json` first (the only T3b cassette the Ollama key can record). Other providers stay unrecorded until that provider's key exists (D13). `check_staleness.py`.
- `T3-*` replay tests.

Exit: the five T3a cassettes and the Ollama T3b cassettes replay green; staleness check wired into the local gate and CI; every T3 catalog row has a Test path except T3-PROV-01..05 and the non-Ollama families of T3-PROV-06, which stay unrecorded until a key exists (D13).

## Tasks

Order:

- P3-01 first.
- P3-02 records live and needs the converted T4 scenarios (P4-03); P3-03 records live and needs the agent project and preflight check (P4-02).
- P3-04 needs no live run and can go right after P3-01.

---

### P3-01 — Cassette model, `from_transcript`, replay driver

- **Depends on:** P0B-07
- **Blocked by:** none
- **Read:** [05.5-cassettes.md](../reference/05.5-cassettes.md) §5.5.1; [04-file-structure.md](../reference/04-file-structure.md) §4.4, the `t3/` subtree only; [6Q-store.md](../reference/06-coverage/6Q-store.md), row T2-STORE-03; `regression/drivers/record.py`; the transcript writer and reader (`rg -ln 'text_seq|result_seq' src/monkeybot`)
- **Creates / changes:** `regression/t3/tools/{cassette,from_transcript}.py`, `regression/drivers/replay.py`, `regression/t3/test_replay_trajectories.py`, `regression/selftest/test_from_transcript.py`; `scripted_from_cassette(cassette) -> ScriptedProvider` in `testkit/provider.py` (not in `src/`; the §13.5 seam list is closed); `regression/plugin.py` collects `regression/t3/trajectories/**/*.yaml`; `regression/t2/loop/scenarios/store/transcript_convertible.yaml` (T2-STORE-03)
- **Steps:**
  1. Write `cassette.py`: a Pydantic model of the §5.5.1 JSON, with `schema_version: 1`.
  2. `from_transcript.py` resolves the `*_seq` / `base_seq` + `diff` pointers, then maps each `ProviderResponse` to `thinking` + `tool_call`s + `text` + `usage` + `done`. Cassette text is scrubbed with `testkit/scrub.py`.
  3. The replay driver feeds the cassette's `calls` into the loop driver. (Gateway replay is wired in P3-04.) A `mode: replay` YAML next to each cassette names the fixture and the assertions.
  4. Strict replay: if a recorded tool name or argument key is missing from the current tools, fail with `cassette_stale: tool <name> changed`. If only a digest differs, warn and do not fail.
  5. `--record` sets `MONKEYBOT_RECORD_CASSETTES=1` for the run; both mean the same thing.
  6. Write T2-STORE-03: a scripted run with `runtime.transcript_enabled: true`, converted by `from_transcript.py`, replays green.
- **Verify:** `uv run pytest regression/selftest/test_from_transcript.py regression/t3/test_replay_trajectories.py -q`. Check three cases:
  - a hand-written cassette replays;
  - renaming a tool argument in it gives `cassette_stale`;
  - changing only `prompt_digest` gives a warning and still passes.
- **Done when:** replays run with no network, and both staleness behaviors are tested.
- **Stop and ask if:** existing transcripts lack the data needed to rebuild provider calls (for example thinking blocks are not stored).

### P3-02 — Record the T3a cassettes (live)

- **Depends on:** P3-01, P4-03 (the T4 scenarios it records from)
- **Blocked by:** user confirmation of the live run (AGENT_GUIDE §13.5)
- **Read:** [05.11-live-provider.md](../reference/05.11-live-provider.md); the T3 rows of [6A-loop.md](../reference/06-coverage/6A-loop.md), [6B-ctx.md](../reference/06-coverage/6B-ctx.md), [6E-shell.md](../reference/06-coverage/6E-shell.md), [6K-ver.md](../reference/06-coverage/6K-ver.md)
- **Creates:** `regression/t3/trajectories/<area>/<name>.{cassette.json,yaml}` for T3-LOOP-01, 02, T3-CTX-01, T3-SHELL-01 and T3-VER-01 (the names in §4.4)
- **Steps:**
  1. Run the source T4 scenarios once against `ollama-cloud/glm-5.3-flash` (how to start the agent: `docs/live-evals.md`). A doom loop or a truncation may not happen on demand: re-run at most three times, then stop and ask.
  2. Convert each transcript with `from_transcript.py`.
  3. Replay each cassette offline three times.
  4. Read every cassette diff for anything the scrubber missed.
- **Verify:** `uv run pytest regression/t3/test_replay_trajectories.py -q` with the network off.
- **Done when:** the five cassettes replay green, and their catalog rows have Test cells.
- **Stop and ask if:**
  - a cassette still contains anything that looks like a secret or personal data after scrubbing;
  - the run you need (doom loop, truncation) did not occur in three tries;
  - T3-VER-01's classifier and judge outputs do not fit the §5.5.1 `calls` format (they are not provider calls).

### P3-03 — Wire cassettes and the staleness check (T3-PROV-06 for Ollama, T3-PROV-07, T3-PROV-08)

- **Depends on:** P3-01, P4-02
- **Blocked by:** user confirmation of the live recording. D13 is decided (2026-10-26): do not record T3-PROV-01..05 or the non-Ollama families of T3-PROV-06 here, and do not invent their bytes from SDK examples.
- **Read:** [05.5-cassettes.md](../reference/05.5-cassettes.md) §5.5.2; [6T-prov.md](../reference/06-coverage/6T-prov.md), the T3 rows; `src/monkeybot/providers/ollama*.py` and `providers/_openai_compat*`
- **Creates:** `regression/t3/tools/{record_wire,check_staleness}.py`, `regression/t3/wire/ollama_cloud/{glm_tool_turn,thinking_only_completion}.json`, `regression/t3/test_replay_wire.py`
- **Steps:**
  1. `record_wire.py` runs when `MONKEYBOT_RECORD_CASSETTES=1` is set. It captures the request and the raw response chunks at the `httpx` transport, scrubbed as §5.5.2 lists.
  2. Replay through an `httpx.MockTransport`, so the real SDK and adapter parse the frames. The Ollama adapter builds its client in `providers/_openai_compat.py`; the test monkeypatches `_keepalive_http_client` to return an `httpx.AsyncClient(transport=MockTransport(...))`. No `src/` change. Assert both `expected_events` and the request body.
  3. `check_staleness.py` fails when the installed SDK version differs from the cassette's `sdk` field. Add it to `scripts/ci-local.sh`, and add the `cassette-staleness` step from §9 to `.github/workflows/ci.yml`.
- **Verify:** `uv run pytest regression/t3/test_replay_wire.py -q` with the network off. Changing a cassette's `sdk` version makes `uv run python -m regression.t3.tools.check_staleness` fail.
- **Done when:** the Ollama cassettes replay green, and staleness is checked in the local gate.

### P3-04 — Golden SSE from a cassette; MCP, subagent and wake-up replays (T3-GW-01, T3-MCP-01, T3-SUB-01, T3-MEM-01)

- **Depends on:** P3-01, P2-02
- **Blocked by:** none
- **Read:** [05.5-cassettes.md](../reference/05.5-cassettes.md) §5.5.1 (replay rules) and §5.5.3; the T3 rows of [6U-gw.md](../reference/06-coverage/6U-gw.md), [6N-mcp.md](../reference/06-coverage/6N-mcp.md), [6J-sub.md](../reference/06-coverage/6J-sub.md), [6P-mem.md](../reference/06-coverage/6P-mem.md)
- **Creates:** gateway replay in `regression/drivers/replay.py`; `regression/t3/sse/cassette_turn.sse`, `regression/t3/test_golden_sse.py`, `regression/t3/mcp/tools_list_call.json`, `regression/t3/subagents/child_tape.ndjson`, `regression/t3/memory/wake_up_snapshot.json`, `regression/t3/test_replay_{mcp,subagent_tape,wake_up}.py`
- **Notes:**
  - The MCP, subagent and wake-up recordings can come from a local run of the fixture MCP server, a fake worker, or a palace; none of them need a live model.
  - Generate goldens with `--update-golden` and show them in the report.
- **Verify:** `uv run pytest regression/t3 -q` with the network off.
- **Done when:** its four cases have Test cells and replay green.
