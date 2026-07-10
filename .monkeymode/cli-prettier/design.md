# CLI prettier (demo polish)

## Goal

Make the first-demo path look intentional on a projector: readiness checks read as
a clean green light, and `chat` looks like a product REPL rather than a debug
console — without rewriting the CLI into a TUI framework.

Demo path this polish supports:

```bash
monkeybot new --provider fake --model fake-model --dest ./demo --yes
monkeybot validate --cwd ./demo
monkeybot doctor --cwd ./demo
monkeybot chat --cwd ./demo
```

## Principles

- Correctness stays ahead of cosmetics (pair with `cli-demoability` readiness work).
- Extend the existing hand-rolled ANSI style in `chat`; do not introduce Rich,
  prompt_toolkit, Textual, or a full TUI rewrite.
- `--json` output stays machine-stable: no ANSI, no layout changes to the schema.
- Honor non-TTY and `NO_COLOR`: plain text fallback, no broken escape sequences.
- Prefer small, reversible layers over one big visual redesign.

## Phases

### Phase 1 — Readiness output (`validate` / `doctor`)

Surface: `cli/src/monkeybot_cli/output.py` (shared `CommandReport.print_human`).

- Colorize when stdout is a TTY and `NO_COLOR` is unset:
  - pass → green `✓`
  - warning fail → yellow `!` (or `⚠`)
  - error fail → red `✗`
  - header `OK` / `FAILED` matches severity
- On full success, keep the checklist (useful on a projector) but tighten copy:
  - never print bare `: pass` when `message` is empty — use a short affirmative
    or omit the trailing message
  - keep remediation only on fails (already true for human; leave JSON as-is)
- Optional `--quiet`: one summary line on full success
  (`validate: OK (N checks)`); full checklist on any warning/error.
  Out of the critical path for v1 if time-boxed — checklist + color alone is
  enough for demos.

### Phase 2 — Chat session presence

Surface: `cli/src/monkeybot_cli/commands/chat.py`.

- Replace the dim exit-only welcome with a short session banner:
  provider, model, gateway URL/port, and whether the gateway was auto-spawned.
- Keep 🧑 / 🐵 turn markers; do not add badge clutter.
- Unify error styling (red on TTY for user-visible failures; stderr stays plain
  when not a TTY).

### Phase 3 — Chat markdown + status (bounded)

Surfaces: `terminal_markdown.py`, `chat_status_bar.py`, `chat.py`.

- Upgrade streaming markdown from “strip markers” to light ANSI styling on TTY:
  bold, dim headers, dim/cyan inline code. Still line-oriented and stream-safe;
  no full CommonMark, no syntax-highlighted fences in v1.
- Status bar: keep the context ring as the primary signal; optionally append
  compact `in/out` token counts when `--usage` is set (or a small always-on
  token pair if it stays ≤ one terminal row). No cost on the bar by default.
- Handle terminal resize for the pinned bar (recompute scroll region) — only if
  a focused fix is cheap; otherwise document as known limitation.

## Boundaries

- No Rich / prompt_toolkit / Textual adoption.
- No `talk` or `loop` visual redesign (first demo still leads with `chat`).
- No `demo_agent` / Docker / observability stack work.
- No change to SSE protocol, gateway behavior, or check IDs.
- No multiline editor / history / completion in the REPL for v1.
- Do not block on `cli-demoability` correctness fixes, but prefer landing
  readiness truthfulness before or with Phase 1 so green checks are honest.

## Affected code

- `cli/src/monkeybot_cli/output.py`
- `cli/src/monkeybot_cli/commands/chat.py`
- `cli/src/monkeybot_cli/terminal_markdown.py`
- `cli/src/monkeybot_cli/chat_status_bar.py`
- Focused tests under `cli/tests/` (`test_cli.py`, `test_terminal_markdown.py`,
  `test_chat_status_bar.py`, plus small banner/color helpers as needed)
