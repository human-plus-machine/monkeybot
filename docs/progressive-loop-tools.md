# Progressive scheduled-loop tool disclosure

**Status:** implemented  
**Related:** [Progressive MCP tools](progressive-mcp-tools.md) · [Features](features.md) · [Skills](skills.md)

## Behavior

1. **Meta-tool by default** — only `enable_loops` stays in the core tool list.
2. **Lifecycle tools on demand** — `start_loop`, `loop_status`, `pause_loop`, `resume_loop`, `stop_loop`, and `disable_loops` stay out of the provider payload until activated.
3. **Activate with `enable_loops`** — requires durable storage (`DB_URL`). Success returns the tool list; new schemas appear on the **next model step this turn**. Advertisement is process-local (like MCP connections): it sticks across user turns on that gateway process until `disable_loops` or process restart. Multi-replica gateways need a single instance or sticky routing so enable/disable stays consistent for a session.
4. **Deactivate with `disable_loops`** — drops progressive loop tools (including itself) from the next model step. Running loops keep scheduler state; call `stop_loop` first to end them.
5. **Default skill** — `monkeybot new` installs `skills/loop/SKILL.md` with the procedure (plan → guards → confirm → manage).

## Config flags

| Key | Meaning |
|-----|---------|
| *(default)* | Loop progressive tools hidden until `enable_loops` |
| `DB_URL` / `paths.db_url` | Required for `enable_loops` / `start_loop` |
| `MONKEYBOT_SCHEDULER_ENABLED` | Scheduler worker must run for ticks to fire |

## Why

The progressive loop schemas are ~2k characters. Most chats never start a loop. Keep default turns on core tools + `enable_loops` only; pay schema cost only when the model opts in via `enable_loops`. `disable_loops` is useless until tools are advertised, so it stays progressive too.
