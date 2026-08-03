# Handoff: subagent "context exhaustion" (PRT-5022 item #1)

Start a fresh session from this file. Run `/diagnosing-bugs`.

## The ask

The FSD3 harness review (`MonkeyBot_Harness_Recommendations.docx`, PRT-5022) reports four
subagent deaths in one run, each forcing the parent to redo the work:

- Phase 1B designer — died before writing the contracts doc
- Phase 1C designer — same, operations doc
- Phase 3 — three parallel spec-writers, none wrote their spec
- Phase 6 integrator — timed out mid-fix

They label all four "context exhaustion" and ask for checkpoint-write-before-death.

## Do not build the fix yet

"Context exhaustion" is their narrative, not an observed mechanism. There are at least
three distinct failure modes behind that phrase and they need different fixes:

| Mechanism | Where | Fix shape |
|---|---|---|
| Turn budget | `max_turns` default **1000** ([settings.py:84](../src/monkeybot/core/config/settings.py:84)) | raise/park the budget |
| Parent timeout | `timeout_sec` default **600.0** ([settings.py:83](../src/monkeybot/core/config/settings.py:83)) | checkpoint before kill |
| Real window overflow | compaction failed or was skipped | fix compaction in child |

600s is a very plausible ceiling for a spec-writer; 1000 turns is not. My prior is this is
mostly **timeout**, not context. That prior is worth exactly nothing until it's reproduced.

## Get the feedback loop first

`/diagnosing-bugs` refuses to theorise before one command goes red. Build these:

1. **Turn budget** — subagent with `subagents.max_turns: 3` against a task needing more.
   Expect `exit_reason == "max_turns"`.
2. **Timeout** — `subagents.timeout_sec: 5` against a slow task.
   Expect `exit_reason == "timeout"`.
3. **Window overflow** — a task that genuinely fills 200k in the child.
   This is the one that might not reproduce, which would itself be the finding.

`exit_reason` landed in 54508b1 and is exactly the discriminator these need — that work was
done partly to make this diagnosis cheap. Use it.

## Leads worth checking early

- **Compaction probably does run in subagents.** It lives in the shared turn loop
  ([turn_loop.py:1292](../src/monkeybot/core/runtime/turn_loop.py:1292) → `_maybe_compact_and_shape`),
  and `enable_context_curation=False` in the worker is the *memory-line curator*, a different
  thing. So a child should not be able to overflow cleanly. If it did, compaction failed.
- **Suspect the compaction failure path.** `_summarization_viable` returning false, or the
  last-resort tail truncation that logs `history exceeds load max after compact attempt`
  ([turn_loop.py:581](../src/monkeybot/core/runtime/turn_loop.py:581)). If PRT-5022 hit that
  line, this is a compaction bug wearing a context-exhaustion costume.
- **Ask them for one grep**, though it is not the critical path:
  `max turns exceeded`, `subagent exceeded`, and `history exceeds load max` in their run logs.
  Any hit names the mechanism in one line.

## Then, and only then

Once diagnosis names the mechanism, checkpoint-write-before-death becomes a real design
question with a hard-to-reverse answer: where the checkpoint lands, who owns it, and what
the parent is allowed to trust from a dead child. That is `/grill-with-docs`, then `/to-spec`
if it spans sessions. If the finding is "there is no good seam to hang a checkpoint on",
that is the `/improve-codebase-architecture` handoff.

## State as of this handoff

Branch `develop`, both fixes pushed, 1309 tests green.

- `0043344` — Bedrock/Vertex model ids now price correctly (was `$0.0000` on every run)
- `54508b1` — `exit_reason` + `artifact_exists` on the task result

Closed with the FSD3 team: #2 (partly already shipped), #6 (works as designed — config lives
under `MONKEYBOT_AGENT_ROOT`, not the workspace sandbox), #7 (already shipped —
`connect_from_catalog` hard-fails on unknown servers).

Dropped: the `MODEL_CONTEXT_WINDOW` passthrough I originally proposed. Parent and child read
the same env var with identical logic and the child inherits `os.environ` — there was no
mismatch. Related smell, not blocking: `_env_context_window_tokens` is copy-pasted in five
places and is only harmless because the copies agree.

Not done, deliberately: the repo is not `ruff format` clean at HEAD. Worth its own pass; keep
it out of feature diffs.
