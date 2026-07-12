# Acceptance

## Textual TUI (TTY)

- On a TTY (without `MONKEYBOT_CHAT_PLAIN`), `monkeybot chat` runs a Textual app
  (not thread-wrapped `input()` / PromptSession).
- Layout: header (provider/model/gateway), scrollable log, docked input, footer
  with context-window ring that stays visible between turns (no flash).
- DECSTBM scroll-region status bar is not used.
- History persists at `<agent_root>/data/chat_history`.
- Ctrl-C follows the design state machine (idle empty / idle non-empty / active
  turn / HITL).
- Exit commands remain exactly `/bye`, `/quit`, `/exit`.
- Tool confirmation and elicitation use the Textual input dock.
- HITL POSTs check HTTP success; failures are reported; CLI does not claim
  success.
- Spinner → tool activity → assistant stream still appear in the log between
  prompts.
- `textual` is a declared direct dependency of `monkeybot-cli`.

## Plain / CI

- Non-TTY or `MONKEYBOT_CHAT_PLAIN=1` completes the pexpect e2e round-trip
  without Textual.

## Out of scope

- validate/doctor colors, `--quiet`, `talk`/`loop` migration.

## Cross-cutting

- Relevant `cli/tests/` pass, including updated chat e2e and controller/TUI tests.
