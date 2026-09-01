"""Core agent components for monkeybot v2.

Subpackages (import concrete modules from these paths, e.g.
``monkeybot.core.runtime.loop``):

- ``types`` — protocols, errors, content blocks, tool definitions
- ``config`` — bot config, secrets, runtime env mapping
- ``llm`` — streaming ``Provider`` protocol and per-turn usage; vendor backends in ``monkeybot.providers``
- ``runtime`` — agent loop and typed streaming events
- ``context`` — per-turn context assembly
- ``memory`` — MemPalace wake-up, outbox writer, and drawer recall
- ``persistence`` — SQLite schema, conversation history, run ids, durable runs
- ``tools`` — tool executor, workspace I/O, sandbox, terminal, inspector
- ``mcp`` — MCP client and port types
- ``prompts`` — system prompt and harness helpers
- ``hooks`` — lifecycle hook manager
- ``subagents`` — subprocess worker and spawn protocol
- ``testing`` — in-repo test doubles (mocks)
"""
