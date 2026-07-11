# Acceptance

## Phase A — prompt_toolkit REPL (atomic terminal ownership)

- On a TTY, `monkeybot chat` uses prompt_toolkit for the user prompt (not
  thread-wrapped `input()`).
- Context-window ring is shown via prompt_toolkit `bottom_toolbar` (or
  equivalent). DECSTBM scroll-region status bar is not active on that path —
  no dual terminal owners.
- Session banner prints provider, model, gateway target, auto-spawned vs
  attached, and a one-line keybinding hint before the first prompt.
- History persists at `<agent_root>/data/chat_history` (`FileHistory`);
  up-arrow recalls prior submissions across chat restarts for that project.
- Multiline paste does not submit early; documented submit binding works.
- Ctrl-D on an empty buffer exits equivalently to `/bye`.
- Ctrl-C follows the design state machine and is tested separately for:
  - idle empty prompt → exit
  - idle non-empty prompt → clear buffer, stay
  - active SSE turn → local abort, clean return to prompt
  - HITL prompt → cancel/deny path, return to prompt
- Exit commands remain exactly `/bye`, `/quit`, `/exit` (no command registry).
- Tool confirmation and elicitation use the same REPL session.
- HITL response POSTs check HTTP success (`raise_for_status` or equivalent).
  Failed confirmation and failed elicitation each have a test: CLI reports the
  error and does not claim success.
- Non-TTY fallback completes the existing pexpect e2e round-trip without a
  real TTY; toolbar/DECSTBM off on that path.
- `prompt-toolkit` is a declared direct dependency of `monkeybot-cli`.
- Existing spinner → tool activity → 🐵 stream loop still functions between
  prompts.

## Phase B — hardening (only if needed)

- Toolbar refresh/resize issues found after A are fixed without reintroducing
  DECSTBM or a second status owner.

## Out of scope (follow-up PR)

- validate/doctor colors, `--quiet`, streaming markdown styling.

## Cross-cutting

- No Textual/fullscreen app; no `talk`/`loop` migration.
- Relevant `cli/tests/` pass, including updated chat e2e and new REPL unit
  tests listed above.
