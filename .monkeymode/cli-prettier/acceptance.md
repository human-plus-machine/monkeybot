# Acceptance

## Phase 1 — readiness output

- On a TTY without `NO_COLOR`, `validate`/`doctor` human output uses green pass,
  yellow warning, and red error icons; header color matches overall status.
- With `NO_COLOR=1` or non-TTY stdout, output has no ANSI escapes.
- `--json` schema and field values are unchanged (no color keys, no layout
  fields).
- Passing checks never render as `id: pass` solely because `message` was empty.
- If `--quiet` ships: full success prints one summary line; any warning/error
  still prints the checklist.

## Phase 2 — chat banner

- `chat` prints a one-block welcome including provider, model, and gateway
  target before the first prompt.
- Auto-spawned vs attached gateway is distinguishable in that banner.
- Existing turn markers (🧑 / 🐵), spinner, and tool activity lines still work.

## Phase 3 — markdown + status

- Streamed assistant markdown on a TTY applies bold / header / inline-code
  styling without breaking mid-stream chunks; non-TTY remains plain stripped
  text.
- Context ring remains on the status bar; optional token counts stay on one row
  and do not displace the ring.
- Existing `cli/tests/` chat/markdown/status-bar coverage still passes; new
  cases cover color gating, empty-message pass labels, banner fields, and
  styled vs plain markdown paths.
