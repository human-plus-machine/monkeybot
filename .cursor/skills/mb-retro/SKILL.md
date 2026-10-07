---
name: mb-retro
description: >-
  Review one MonkeyBot session transcript and propose environment fixes
  (checks, verifier criteria, AGENT.md pointers, tool access). Use ONLY when
  the user explicitly asks for /mb-retro, a MonkeyBot retro, or to review a
  MonkeyBot transcript for why the agent struggled. Do not run during ordinary
  coding, and do not edit anything until the user picks a candidate.
disable-model-invocation: true
---

# mb-retro

Look at one MonkeyBot session and propose changes to the agent's environment so the next run goes better. Propose only. Edit nothing until the user picks a candidate.

This is the MonkeyBot form of a session retro. It does not review the code the session produced. If the user wants a verdict on a diff, that is a code review, not this skill.

## Input

A session id, a session directory, or a `transcript.ndjson` path. Transcripts live under `{workspace}/.monkeybot/transcripts/` and exist only when `runtime.transcript_enabled` is true in `monkeybot.yaml`.

From the monkeybot repo:

```bash
uv run monkeybot trace list --cwd <agent-root>
uv run monkeybot trace digest <session-id-or-path> --cwd <agent-root>
```

If the user does not name a session, run `trace list` and suggest the session with the highest `score`. Say which one you are using. Do not retro every session.

## Read

1. Run `uv run monkeybot trace digest <id>`. That output is already resolved. Do not hand-expand `text_seq`, `result_seq`, `schema_seq`, `content_seq`, or `base_seq` diffs.
2. Open one record only when the digest line is not enough: `uv run monkeybot trace digest <id> --seq N`. Repeat `--seq` for a few records, not the whole file.
3. Subagent work is inlined under the `task` call that started it, from `{session}/subagents/`. Runs the digest cannot tie to a call appear under `Unlinked subagent runs`. If a line says `no child transcript`, say so and do not invent what the child did.
4. A `--seq` record with `text_error: diff_mismatch` was written by an older harness whose prompt diff cannot be rebuilt. Do not quote its text.

A smooth digest (score 0, short timeline, no interventions) has little to teach. Say that and stop.

## Find struggle moments

Every candidate must cite one or more `@seq` lines from this digest. Discard any candidate you cannot point at a specific moment. Do not fill categories with generic advice.

Look for:

- Tool errors, especially the same `error_kind` more than once
- `HarnessIntervention` (`doom_loop`, `truncated_batch`, `empty_completion`, `post_tool_empty`, `background_jobs_nudge`, `compaction_fallback`, `max_turns`, `verifier_replan`)
- Steers, verifier verdicts, empty assistant replies, context summaries
- Slow tools and repeated identical calls
- A long search before a fact that a pointer would have named
- A subagent that struggled while the parent looked fine

## Classify

Put each moment in one category. The category decides where the fix goes.

| What went wrong | Propose |
| --- | --- |
| Slow to find a fact | A navigation pointer in the agent's `AGENT.md`, a skill description, or a memory entry |
| A mechanical mistake | An inspector or `PRE_TOOL` hook, tighter tool-schema validation, `permissions.yaml` or a path grant, or a guard threshold |
| A judgement-call miss | A verifier criterion. The verifier is the reviewer. |
| Bloated `AGENT.md` or harness prompt (`src/monkeybot/core/prompts/harness_prompt.py`) | Move the steering into an on-demand skill, or delete the no-op lines |
| An expensive tool call | Trim tool output, spill it to a file, or load tools progressively |
| Missing information or access | An MCP server, a tool, or wider permissions |
| The agent behaved reasonably and the harness misbehaved | A code fix plus a regression test in `tests/`, or an eval scenario in `evals/scenarios/` |

## Rules

- A mechanical problem gets a check that can fail, never a sentence in a steering file.
- `AGENT.md` gets navigation pointers only. Do not add new standing rules there, and do not add them to a root `AGENTS.md` or `CLAUDE.md`.
- If a check already exists but is not wired up, the fix is to wire it, not to write a second one.
- A missing guardrail (no test or eval covers this failure) is its own finding.
- Rank by severity for the next session, not by how loud the log line was. A quiet expensive miss can outrank a noisy cheap one. Say that the order is a draft.
- Do not propose deleting lint rules, hooks, or CI jobs from other sessions. This skill sees one transcript.

## Output

Ranked candidates, most severe first. For each one:

1. **Moment** — quote the digest lines and their `@seq` numbers.
2. **Category** — one row from the table.
3. **Target** — the exact file or config key.
4. **Change** — the proposed check, pointer, criterion, or deletion, in a few lines. Not a patch yet.

Then stop and ask which candidates to apply. Do not edit files, install hooks, or change `monkeybot.yaml` before that.
