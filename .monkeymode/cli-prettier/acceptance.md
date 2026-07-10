# Acceptance

## Phase A — prompt_toolkit shell

- `monkeybot chat` on a TTY uses prompt_toolkit for the user prompt (not
  thread-wrapped `input()`).
- Up-arrow recalls prior submissions within the session; history persists
  across chat restarts for the same agent project.
- Multiline paste does not submit early; documented submit binding works.
- Ctrl-D on an empty buffer exits equivalently to `/bye`.
- Ctrl-C behavior is defined and tested: interrupt current prompt / turn
  without leaving the terminal in a broken cooked/raw state.
- Tool confirmation and elicitation prompts use the same REPL session.
- Non-TTY (or explicit fallback) still completes the existing pexpect e2e
  round-trip; CI does not require a real TTY for that test.
- `prompt-toolkit` is a declared direct dependency of `monkeybot-cli`.

## Phase B — toolbar + banner

- Context-window ring appears in prompt_toolkit `bottom_toolbar` (or
  equivalent) and remains correct across turns.
- DECSTBM scroll-region status bar is removed; no dual status owners.
- Welcome banner shows provider, model, gateway target, and whether the
  gateway was auto-spawned, plus a one-line keybinding hint.
- Existing spinner → tool activity → 🐵 stream loop still functions.

## Phase C — readiness colors

- On a TTY without `NO_COLOR`, `validate`/`doctor` use green pass, yellow
  warning, red error; `--json` unchanged and colorless.
- Passing checks never render as `id: pass` only because `message` was empty.

## Phase D — optional markdown

- If shipped: streamed assistant markdown on a TTY gets light styling without
  breaking chunked SSE deltas; non-TTY stays plain.

## Cross-cutting

- No Textual/fullscreen app; no `talk`/`loop` migration in this feature.
- Relevant `cli/tests/` pass, including updated chat e2e and new REPL unit
  tests for history path, fallback, and toolbar formatting.
