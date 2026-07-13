# User stories: Textual chat TUI

## Product shell

As a presenter or daily user, I use `monkeybot chat` as a Claude Code–style
terminal app: persistent header, scrolling transcript, docked input, always-on
context ring, history, and a documented Ctrl-C state machine.

## HITL without leaving the shell

As a user approving a tool or answering an elicitation, I stay inside the same
Textual chrome. If the confirmation/elicitation POST fails, I see an explicit
error and the CLI does not pretend the agent was acknowledged.

## Demo + CI both work

As a presenter on a TTY, Textual owns the terminal. As CI/pexpect on a pipe (or
`MONKEYBOT_CHAT_PLAIN=1`), chat still round-trips on the plain fallback without
Textual.
