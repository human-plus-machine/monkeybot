# User stories: real CLI REPL

## Product REPL

As a presenter or daily user, I use `monkeybot chat` as a real terminal REPL:
arrow-key history scoped to my agent project, multiline paste, Ctrl-D to leave,
Ctrl-C that matches a documented state machine, and a stable bottom status
line — not a bare `input()` prompt with a competing scroll-region bar.

## HITL without breaking the shell

As a user approving a tool or answering an elicitation, I stay inside the same
REPL chrome. If the confirmation/elicitation POST fails, I see an explicit
error and the CLI does not pretend the agent was acknowledged.

## Demo + CI both work

As a presenter on a TTY, the REPL owns the terminal (prompt + toolbar + banner).
As CI/pexpect on a pipe, chat still round-trips on the non-TTY fallback without
CPR or toolbar requirements.
