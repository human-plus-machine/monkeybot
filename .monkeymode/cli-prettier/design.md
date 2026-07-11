# CLI real REPL (prompt_toolkit)

## Goal

Replace the thread-wrapped `input()` chat loop with a real terminal REPL built on
**prompt_toolkit**, so `monkeybot chat` feels like a product shell: history,
multiline paste, clean keybindings, and a first-class bottom toolbar — while
keeping the existing SSE turn renderer (spinner → tools → 🐵 stream).

Demo path this supports:

```bash
monkeybot new --provider fake --model fake-model --dest ./demo --yes
monkeybot validate --cwd ./demo
monkeybot doctor --cwd ./demo
monkeybot chat --cwd ./demo
```

(`validate` / `doctor` remain the readiness gate; their visual polish is **out of
scope** here — see Boundaries.)

## Why prompt_toolkit

A real REPL is an **input-stack** change: history, multiline, interrupt
handling, and bottom UI belong in a library that owns the prompt lifecycle.
Extending more ANSI/`input()` helpers would fight that. prompt_toolkit is the
dependency we add. Rich/Textual stay out.

## Current state (what we replace)

- `_read_line` runs `input()` on a worker thread and races an asyncio `SIGINT`
  event (`chat.py`). Today any Ctrl-C sets that event and **exits** the session
  (welcome text: “Ctrl-C also exits”).
- HITL (`tool-confirmations`, elicitations) uses raw `input()` and POSTs without
  checking the HTTP result — a failed POST can leave the agent waiting while
  the CLI looks fine.
- Welcome is a one-line dim exit hint.
- Status is a custom DECSTBM pinned bar (`chat_status_bar.py`) that must not
  coexist with prompt_toolkit’s prompt/toolbar ownership.
- Streaming assistant text is print-based `MarkdownPlainStream` (unchanged).
- E2E uses pexpect (`cli/tests/test_chat_e2e.py`).

## Design

### Architecture

Keep the asyncio session loop and SSE consumer. Swap the **prompt / HITL /
chrome** layer. **One terminal owner on TTY:** prompt_toolkit owns the prompt
and bottom toolbar from the first shippable cut; the DECSTBM `ChatStatusBar`
is disabled/removed in the same change (not deferred).

```
┌─────────────────────────────────────────────┐
│  prompt_toolkit PromptSession (async)       │
│  - history, multiline, keybindings          │
│  - bottom_toolbar = context ring (+ tokens) │
│  - styled 🧑 prompt                         │
│  - session banner (print once before loop)  │
└─────────────────┬───────────────────────────┘
                  │ user line
                  ▼
┌─────────────────────────────────────────────┐
│  existing turn runner (mostly unchanged)    │
│  - POST /reply, SSE events                  │
│  - spinner / tool activity / 🐵 stream      │
│  - print-based output between prompts       │
└─────────────────────────────────────────────┘
```

Between prompts, continue to print freely. Do **not** move chat into a
fullscreen prompt_toolkit `Application` in v1 — `PromptSession.prompt_async()`
+ `bottom_toolbar` is enough.

`chat_status_bar.py` keeps pure formatting helpers (`format_context_ring`,
`parse_usage_response`, etc.) consumed by the toolbar callback; DECSTBM
activate/deactivate/scroll-region code goes away with the PromptSession land.

### History (single location)

- Path: `<agent_root>/data/chat_history` where `agent_root` is the resolved
  project root from `--cwd` / config (same root `resolve_agent_root` uses).
- Format: prompt_toolkit `FileHistory` (its native on-disk format).
- Rationale: project-scoped isolation; `data/` is already gitignored at repo
  root. No `~/.monkeybot/…` global history.

### Exit commands

Keep the existing three only: `/bye`, `/quit`, `/exit` (case-insensitive,
stripped). No slash-command registry or “future commands” abstraction in this
plan.

### Ctrl-C state machine

Remove the process-wide `loop.add_signal_handler(SIGINT, interrupt.set)` race
with prompt_toolkit. One policy, three states:

| State | Ctrl-C behavior |
|-------|-----------------|
| **Idle prompt, empty buffer** | Exit REPL (same as `/bye`): teardown stream task, deactivate toolbar, stop auto-spawned gateway if any. |
| **Idle prompt, non-empty buffer** | Clear the buffer; stay at the prompt. Do not exit. |
| **Active SSE turn** (waiting on events after `/reply`) | **Abort turn locally**: clear spinner/activity, stop waiting for this turn, return to a clean idle prompt. Session and gateway stay up. Client ignores further SSE payloads for that `request_id` until the next user submission (server turn may still finish; no cancel API assumed). |
| **HITL prompt** (confirmation or elicitation) | Cancel the HITL attempt: for tool confirmation POST `approved=false` with reason `cancelled by user`; for elicitation POST a cancelled/empty payload if the API accepts it, otherwise skip POST and surface that the agent may still be waiting. Then return to idle prompt (do not exit the REPL). |

After any abort path, the terminal must be back in a normal cooked state with
a fresh prompt (prompt_toolkit session still valid). Double Ctrl-C is not
required for exit — empty-prompt Ctrl-C exits.

Tests must cover all three interactive states (idle empty, active turn, HITL)
plus buffer-clear on non-empty idle.

### HITL over the same session + HTTP failure handling

- Confirmations and elicitations use the same `PromptSession` (yes/no and
  free-text), never bare `input()`.
- Every HITL response `POST` must `raise_for_status()` (or equivalent explicit
  check).
- On HTTP failure: print a clear red error (endpoint + status), **do not**
  treat the action as acknowledged, return to idle prompt. No automatic retry
  loop in v1 (user can re-trigger by continuing the agent turn / re-prompt if
  the server still waits — document that limitation).
- Frontend-tool-unsupported path stays a printed warning (unchanged).

### Non-TTY / CI

If stdin or stdout is not a TTY, use a plain line reader fallback (no
prompt_toolkit CPR/toolbar) so pexpect and pipes keep working. Toolbar and
DECSTBM are both off on that path.

### Dependency

Add `prompt-toolkit` as a direct dependency of `monkeybot-cli`
(`cli/pyproject.toml` + lock).

### Phases

| Phase | Deliverable |
|-------|-------------|
| **A** | Atomic terminal ownership: `PromptSession` + `bottom_toolbar` (context ring) + remove DECSTBM bar; history at `<agent_root>/data/chat_history`; multiline; Ctrl-C state machine; Ctrl-D empty = `/bye`; HITL via same session with HTTP error handling; session banner; non-TTY fallback |
| **B** | Hardening only if needed after A: toolbar refresh timing, resize, pexpect env knobs — no second status owner |

No Phase C/D in this feature. validate/doctor colors, `--quiet`, and styled
markdown are a **separate follow-up** plan/PR.

## Boundaries

- No Textual / fullscreen TUI app.
- No Rich requirement.
- No `talk` / `loop` REPL migration.
- No gateway/SSE protocol changes; no new cancel-turn API required for v1.
- No validate/doctor output redesign, `--quiet`, or markdown styling in this PR
  series.
- No slash-command framework beyond the three exit strings.
- Do not require `cli-demoability` to merge first.

## Risks

- **pexpect e2e**: gate prompt_toolkit on TTY; keep non-TTY fallback in CI;
  set known env (e.g. disable CPR) if tests still flake.
- **Server turn after client abort**: abandoned `request_id` events must be
  ignored without corrupting the next turn’s rendering.
- **Toolbar vs mid-turn prints**: update toolbar between turns / on usage
  fetch, not on every streamed delta unless proven safe.
- **HITL POST failure**: agent may remain blocked server-side; CLI must say so
  explicitly rather than looking healthy.

## Affected code

- `cli/pyproject.toml` / `cli/uv.lock` — add `prompt-toolkit`
- `cli/src/monkeybot_cli/commands/chat.py` — session loop, `_read_line`, HITL,
  SIGINT policy, banner
- New helper e.g. `cli/src/monkeybot_cli/chat_repl.py` — session factory,
  `FileHistory` path, toolbar, keybindings
- `cli/src/monkeybot_cli/chat_status_bar.py` — formatting helpers only; remove
  DECSTBM owner
- Tests: `test_chat_e2e.py`, `test_chat_errors.py`, `test_chat_status_bar.py`,
  new REPL tests (history path, non-TTY fallback, toolbar formatting, Ctrl-C
  in three states, HITL POST failure for confirmation and elicitation)
