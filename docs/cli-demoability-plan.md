# CLI demoability plan

Working notes from an investigation of `cli/` (the `monkeybot` CLI): what it does today,
what's broken, and what's worth doing before it's shown in a demo. Priority order is
correctness/functionality first, polish second — a demo that quietly lies about its own
health is worse than one with a plain terminal.

## What's in `cli/`

Seven subcommands, ~2,800 lines, wrapping the SSE/realtime gateway:

| Command | File | Purpose |
|---|---|---|
| `new` | [commands/new.py](../cli/src/monkeybot_cli/commands/new.py) | Scaffold `monkeybot_config/`, workspace, skills |
| `validate` | [commands/validate.py](../cli/src/monkeybot_cli/commands/validate.py) | Static config/path checks |
| `doctor` | [commands/doctor.py](../cli/src/monkeybot_cli/commands/doctor.py) | Runtime readiness: Python version, provider creds, port, web-search backend |
| `run` | [commands/run_cmd.py](../cli/src/monkeybot_cli/commands/run_cmd.py) | Launch the SSE gateway as a subprocess |
| `chat` | [commands/chat.py](../cli/src/monkeybot_cli/commands/chat.py) | Terminal REPL — auto-spawns the gateway, streams SSE, renders markdown/spinners/tool activity |
| `talk` | [commands/talk.py](../cli/src/monkeybot_cli/commands/talk.py) | Realtime WebSocket (audio) client |
| `loop` | [commands/loop.py](../cli/src/monkeybot_cli/commands/loop.py) | Manage scheduled prompt-driven agent loops |

The `fake` model provider (`model.provider: fake` in `monkeybot.yaml`) is the obvious
zero-API-key demo path — no external service, no credentials, instant round trip.

## How it was verified

Live end-to-end run, not just reading code:

```
monkeybot new --provider fake --model fake-model --yes   # scaffold
monkeybot validate --cwd <dest>                            # static checks
monkeybot doctor --cwd <dest>                               # runtime checks
monkeybot run --cwd <dest> --port <p>  &  curl .../health   # gateway boot
POST /sessions, /reply, GET /events                          # full turn via curl
monkeybot chat --cwd <dest>  (via pexpect)                   # actual REPL UX
```

The gateway itself round-trips cleanly on `fake` (session create → reply → SSE
`AssistantDelta` → `TurnComplete`), and the `chat` REPL UX is already good: spinner,
🐵-prefixed streamed replies, live context-usage status bar, tool-call activity lines.
No changes recommended there.

## Bugs found (fixed on this branch)

Both bugs were hit on the very first two commands anyone runs after `monkeybot new` —
i.e., the first thing a demo audience sees.

### 1. `validate` showed "Missing X" next to a green checkmark

`check()` messages in `validate.py` were built unconditionally, independent of the
`passed` value passed in, e.g.:

```python
check(..., passed=ap.is_file(), message=f"Missing AGENT.md at {ap}")
```

So a fully healthy scaffold printed:

```
[✓] paths.agent_md.exists: Missing AGENT.md at /.../AGENT.md
[✓] paths.mcp_config.exists: Missing MCP config: /.../mcp.json
[✓] model.name.present: model.name is required
```

Six checks had this pattern: `paths.agent_md.exists`, `paths.skills_path.exists`,
`paths.mcp_config.exists`, `paths.command_allowlist.exists`, `model.name.present`,
`memory.backend.supported`. Fix: message is now `""` on pass, only populated on fail.

### 2. `doctor` failed the zero-setup `fake` provider for "missing credentials"

`PROVIDER_SPECS["fake"]` in [providers.py](../cli/src/monkeybot_cli/providers.py) never
set `credentials_optional=True` (unlike `ollama`, which correctly does). Result: the one
provider that needs no API key still failed `doctor` with exit code 1 on a completely
correct setup — breaking the "just try it, no keys needed" path.

### Verification

Re-ran the same scaffold after the fix: `validate` and `doctor` both report clean `OK`,
all green, zero surprising text. Full `cli/tests/` suite (63 tests) still passes; no
existing test locked in the old (buggy) message text.

## Recommendation for the demo itself

- **Script the demo around `fake`** (or a real provider with `.env` already populated) so
  `new → validate → doctor → chat` is a clean, boring, all-green sequence — that sequence
  is now trustworthy after the fixes above.
- **Lead with `chat`**, not `run` + curl. The REPL rendering (spinner → tool activity →
  streamed markdown → usage line) is the actual visual payoff and needs no further work.
- Skip `talk` (realtime/audio) and `loop` (scheduled loops) for a first demo unless
  specifically asked — they're real functionality but add setup surface (mic permissions,
  gateway-must-already-be-running) without adding clarity for a first look.
- `demo_agent/` (Docker, OpenSandbox, Langfuse/Phoenix observability stack) is a separate,
  much heavier surface than the CLI itself — out of scope here unless that's what's
  actually being demoed.

## Open items / not yet done

- No broader UX pass was requested or made — only the two correctness bugs above were
  fixed. If more demoability work is wanted, likely candidates (unverified, not yet
  investigated) worth a look before a real demo:
  - `doctor`/`validate` JSON output (`--json`) hasn't been eyeballed for the same
    message-copy issue in other severities.
  - No `--quiet`/one-line "all good" success mode for `validate`/`doctor` — current output
    is a full checklist even when everything passes, which is fine for a demo but verbose
    for scripting.
