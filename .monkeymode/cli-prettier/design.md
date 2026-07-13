# CLI Textual Chat TUI

## Goal

Replace the print/`input()` (and any PromptSession) chat loop with a **Textual**
app so `monkeybot chat` feels like Claude Code: one terminal owner, persistent
header/log/input/footer, streaming turns in a scroll view, HITL in the same
shell, and an always-visible context ring.

Demo path:

```bash
monkeybot new --provider fake --model fake-model --dest ./demo --yes
monkeybot validate --cwd ./demo
monkeybot doctor --cwd ./demo
monkeybot chat --cwd ./demo
```

## Why Textual

A Claude Code–like experience needs a long-lived layout (log + docked input +
footer), not prompt-then-print. Textual owns the screen, widgets, and bindings.
Rich comes along for free. prompt_toolkit PromptSession is not used on the TTY
path.

## Architecture

```
run_chat (gateway lifecycle)
    │
    ├─ TTY  → ChatApp (Textual) ──► ChatSessionController ──► gateway HTTP/SSE
    └─ plain → plain loop        ──► ChatSessionController ──► gateway HTTP/SSE
```

- **ChatSessionController** (`chat_session.py`): zero stdout — SSE, `/reply`,
  HITL POSTs with `raise_for_status`, usage fetch, abandoned `request_id` set.
- **ChatApp** (`chat_tui.py`): Header | RichLog | Input | Footer (context ring).
- **Formatters** (`chat_status_bar.py`): keep ring helpers; no DECSTBM.
- **Plain path**: `MONKEYBOT_CHAT_PLAIN=1` or non-TTY — line reader + prints for
  pexpect/CI; same controller.

## Ctrl-C state machine

| State | Behavior |
|-------|----------|
| Idle, empty input | Exit REPL (same as `/bye`) |
| Idle, non-empty | Clear buffer; stay |
| Active SSE turn | Local abort; ignore further events for that `request_id`; back to idle |
| HITL prompt | Cancel/deny POST; return to idle |

## HITL

Confirmations and elicitations use the Textual input dock (mode switch), never
bare `input()` on the TTY path. POSTs must `raise_for_status()`. Failures print
a clear error; CLI does not claim success.

## History

`<agent_root>/data/chat_history` — file-backed user submissions for the project.

## Exit commands

Exactly `/bye`, `/quit`, `/exit` (case-insensitive). No slash-command framework.

## Boundaries

- No `talk` / `loop` migration in this feature.
- No gateway/SSE protocol changes; no cancel-turn API required for v1.
- No validate/doctor redesign.
- Plain/non-TTY fallback required for CI.

## Affected code

- `cli/pyproject.toml` — add `textual`
- `cli/src/monkeybot_cli/chat_session.py` — new controller
- `cli/src/monkeybot_cli/chat_tui.py` — new Textual app
- `cli/src/monkeybot_cli/chat_tool_display.py` — shared tool hint helpers
- `cli/src/monkeybot_cli/commands/chat.py` — entry + plain path + gateway
- `cli/src/monkeybot_cli/chat_status_bar.py` — formatters only
- Tests: e2e plain, controller unit, Textual `run_test`
