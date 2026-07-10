# User stories: real CLI REPL

## Product REPL

As a presenter or daily user, I use `monkeybot chat` as a real terminal REPL:
arrow-key history, multiline paste, Ctrl-D to leave, and a stable bottom
status line — not a bare `input()` prompt that resets every turn.

## HITL without breaking the shell

As a user approving a tool or answering an elicitation, I stay inside the same
REPL chrome (prompt_toolkit), with clear yes/no or free-text prompts, instead
of dropping into raw `input()` that ignores history and keybindings.

## Demo + CI both work

As a presenter on a TTY, the REPL looks intentional (banner, toolbar, styled
prompt). As CI/pexpect on a pipe, chat still round-trips without requiring a
real terminal capability database or CPR dance.

## Readiness still readable

As a presenter, `validate` / `doctor` on a healthy scaffold read as a clean
color-coded ready state before I enter the REPL (secondary to the REPL work,
but part of the same demo path).
