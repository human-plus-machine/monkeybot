# CLI demoability

## Goal

Make this fresh, zero-credential path truthful and clean before `chat` starts:

```bash
monkeybot new --provider fake --model fake-model --dest ./demo --yes
monkeybot validate --cwd ./demo
monkeybot doctor --cwd ./demo
monkeybot chat --cwd ./demo
```

## Design

- Keep the CLI surface unchanged.
- A newly created or forced `fake` config sets `web_search.backend: none`; do not
  change that setting in an existing user config.
- `validate` checks MCP environment placeholders only for enabled servers and
  never displays failure wording on a passing check.
- `doctor` keeps its check IDs but says credentials are not required for `fake`
  and omits credential remediation.
- Human and `--json` output use the same `CheckResult` data.

## Boundaries

No new command, quiet mode, real-provider changes, `talk`/`loop` work, or
`demo_agent` work. A busy configured port remains a valid `doctor` warning.

## Affected code

`src/monkeybot/scaffold/__init__.py`, `cli/src/monkeybot_cli/commands/validate.py`,
`cli/src/monkeybot_cli/commands/doctor.py`, and focused CLI/scaffold tests.
