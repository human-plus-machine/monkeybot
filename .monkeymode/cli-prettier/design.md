# CLI real REPL (prompt_toolkit)

## Goal

Replace the thread-wrapped `input()` chat loop with a real terminal REPL built on
**prompt_toolkit**, so `monkeybot chat` feels like a product shell: history,
multiline paste, clean keybindings, and a first-class status/toolbar — while
keeping the existing SSE turn renderer (spinner → tools → 🐵 stream).

Demo path this supports:

```bash
monkeybot new --provider fake --model fake-model --dest ./demo --yes
monkeybot validate --cwd ./demo
monkeybot doctor --cwd ./demo
monkeybot chat --cwd ./demo
```

## Why prompt_toolkit (now)

The previous polish plan avoided prompt_toolkit because it only touched output
cosmetics. A real REPL is an **input-stack** change: history, multiline,
interrupt handling, and bottom UI belong in a library that owns the prompt
lifecycle. Extending more ANSI/`input()` helpers would fight that.

Rich remains out of scope for v1 (optional later for markdown). prompt_toolkit
is the dependency we add.

## Current state (what we replace)

- `_read_line` runs `input()` on a worker thread and races an asyncio interrupt
  event (`chat.py`).
- HITL (`tool-confirmations`, elicitations) also uses raw `input()`.
- Welcome is a one-line dim exit hint.
- Status is a custom DECSTBM pinned bar (`chat_status_bar.py`) that fights any
  fullscreen/application input library.
- Streaming assistant text is print-based `MarkdownPlainStream` (strip markers).
- E2E uses pexpect against the current prompt (`cli/tests/test_chat_e2e.py`).

## Design

### Architecture

Keep the asyncio session loop and SSE consumer. Swap only the **prompt /
HITL / chrome** layer:

```
┌─────────────────────────────────────────────┐
│  prompt_toolkit PromptSession (async)       │
│  - history, multiline, keybindings          │
│  - bottom_toolbar = context ring (+ tokens) │
│  - styled 🧑 prompt                         │
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

Between prompts, continue to print freely (spinner, tools, deltas). Do **not**
move the whole chat into a prompt_toolkit `Application` fullscreen layout in
v1 — `PromptSession.prompt_async()` + `bottom_toolbar` is enough and preserves
the current stream-printing model.

Retire the DECSTBM `ChatStatusBar` scroll-region approach once the toolbar
carries the same context-ring signal (avoids two owners of the alternate
screen geometry).

### REPL features (v1)

- **History**: in-memory + file under the agent project
  (e.g. `data/chat_history` or `~/.monkeybot/chat_history`), surviving restarts.
- **Multiline**: paste and explicit continue (e.g. meta+enter / escape+enter);
  Enter submits a non-empty buffer. Document the binding in the banner.
- **Keybindings**: Ctrl-C cancels the prompt / signals interrupt consistently
  with today’s “exit or abort turn” behavior; Ctrl-D on empty buffer exits
  like `/bye`.
- **Slash commands**: keep `/bye` `/quit` `/exit`; structure bindings so more
  `/…` commands can land later without another input rewrite.
- **HITL**: route confirmations and elicitations through the same session
  (yes/no and free-text prompts), not bare `input()`.
- **Non-TTY / CI**: if stdin or stdout is not a TTY, fall back to the current
  line reader (or prompt_toolkit’s plain mode) so pexpect/pipes keep working.
- **Dependency**: add `prompt-toolkit` to `cli/pyproject.toml` (direct dep).

### Still in scope (output polish, secondary)

Land after or with the REPL shell — do not block the input migration on these:

1. **Session banner** — provider, model, gateway URL, auto-spawned vs attached,
   short keybinding hint.
2. **validate/doctor colors** — TTY color + distinct warning/error icons in
   `output.py`; `--json` unchanged; honor `NO_COLOR`.
3. **Light markdown styling** — optional follow-on: ANSI bold/headers/code in
   `terminal_markdown.py`, or a later Rich renderer. Not required to call the
   REPL done.

### Phases

| Phase | Deliverable |
|-------|-------------|
| **A** | prompt_toolkit `PromptSession` wired into `chat`; history + multiline + Ctrl-D/Ctrl-C; non-TTY fallback; HITL via same session |
| **B** | Move context ring into `bottom_toolbar`; remove DECSTBM status bar; session banner |
| **C** | Readiness output colors (`validate`/`doctor`); optional quiet mode |
| **D** | (Optional) styled streaming markdown |

## Boundaries

- No Textual / fullscreen TUI app in v1.
- No Rich requirement in v1 (may revisit in Phase D).
- No `talk` / `loop` REPL migration.
- No gateway/SSE protocol changes; check IDs unchanged.
- No change to `--json` schema for validate/doctor.
- Do not require `cli-demoability` to merge first, but honest green checks
  still matter for demos.

## Risks

- **pexpect e2e**: prompt_toolkit cursor-position requests can confuse dumb
  pipes — gate on TTY, set known env (e.g. disable CPR) in tests, and keep a
  non-TTY fallback path covered by CI.
- **SIGINT**: today a custom asyncio signal handler races `input()`; with
  prompt_toolkit, prefer its interrupt/`KeyboardInterrupt` model and one
  clear “abort turn vs exit” policy.
- **Status bar migration**: toolbar updates must refresh without corrupting
  mid-turn prints; update toolbar between turns (and on usage fetch), not on
  every streamed delta unless proven safe.
- **History path**: must respect agent `--cwd` / project root and stay
  gitignore-friendly under `data/`.

## Affected code

- `cli/pyproject.toml` / `cli/uv.lock` — add `prompt-toolkit`
- `cli/src/monkeybot_cli/commands/chat.py` — session loop, `_read_line`, HITL
- New helper module e.g. `cli/src/monkeybot_cli/chat_repl.py` — session factory,
  history, toolbar, keybindings
- `cli/src/monkeybot_cli/chat_status_bar.py` — shrink to pure formatting helpers
  used by the toolbar; remove DECSTBM owner logic in Phase B
- `cli/src/monkeybot_cli/output.py` — Phase C colors
- Tests: `test_chat_e2e.py`, `test_chat_errors.py`, `test_chat_status_bar.py`,
  new REPL unit tests (history path, non-TTY fallback, toolbar formatting)
