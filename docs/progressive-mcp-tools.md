# Progressive MCP tool disclosure

**Status:** implemented (catalog `mcp.json` + `enable_mcp` / `disable_mcp` + mid-turn tool refresh)  
**Related:** [MCP](mcp.md) · [Features — MCP](features.md) · [Browser MCP](browser-mcp.md) · [Skills](skills.md)

---

## Problem

Local / small models (e.g. Ollama `ornith:9b`) are fast in bare chat (~1–2s for `"hello"`) but slow under monkeybot (~20s TTFT) even when thinking is off.

Measured on the demo agent session:

| Phase | Time |
|-------|------|
| Harness prep (`build_context`, memory, system prompt) | ~0.5–0.9s |
| Ollama time-to-first-token (prompt_eval on fat payload) | **~19–20s** |

Root cause is not thinking, the TUI, or gateway overhead. It is **eager tool advertising**: every turn sends the full tool list to the provider.

Typical demo payload:

- ~14k chars of system prompt (AGENT.md + harness + memory index)
- **~39 tools**, of which **~21 are `browser__*`** (~15k chars of JSON schemas)

Bare `ollama run` sends almost none of that. Turning `thinking_budget` off does not shrink tools or prompt, so it does not fix TTFT.

Disabling browser MCP locally proves the diagnosis; it is not the product solution.

---

## What already exists

### Runtime add / remove MCP

Built-in tools:

- `add_mcp_server` — `MCPClient.connect(...)`; discovers tools; registers `server__tool` names
- `remove_mcp_server` — `MCPClient.disconnect(...)`; drops that server from `all_tools()`

Docs (`features.md`): runtime add/remove mutate live connections.

### Context snapshot timing

`build_context()` runs **once per user message** and freezes tools into `TurnContext.tools`:

```python
tools.extend(mcp_client.all_tools())
```

`loop.run()` reuses that frozen list for every inner provider round. After `add_mcp_server`, the tool result today notes:

> New tools apply on the **next user message** (context is built per turn).

So:

| Capability | Today |
|------------|--------|
| Connect / disconnect MCP mid-turn | Yes (client mutates) |
| Advertise new tools to the model mid-turn | **No** (frozen `ctx.tools`) |
| Default demo policy | Catalog `browser` in `mcp.json`; connect via `enable_mcp` |

Chicken-and-egg: the model could call `remove_mcp_server` later, but `"hello"` already paid TTFT with all 39 schemas present.

---

## Design goals

1. **Default turns stay small** — core tools only; no fat MCP schemas on `"hello"`.
2. **Heavy MCP is opt-in** — browser (and similar) connect when needed.
3. **Same user turn can use new tools** — after activate, the next inner provider round sees the new schemas (not only the next user message).
4. **Model does not invent launch commands** — no requiring the LLM to pass `uv run --project …` paths for known servers.

Non-goals for v1: compact-schema-on-demand for every core tool; intent classifiers; provider-specific prompt caching.

---

## Proposed solution

Two complementary harness changes.

### A. Mid-turn tool list refresh

After a tool batch that mutates the MCP registry (`add_mcp_server`, `remove_mcp_server`, and the new meta-tool below), **before** the next `provider_stream` in the same `run()`:

```text
tool batch finishes
  → if MCP tool set mutated:
       ctx = dataclasses.replace(ctx, tools=rebuild_tool_list(...))
  → next provider_stream(..., ctx.tools)   # same user turn
```

`rebuild_tool_list` must match `build_context` composition:

- core built-ins (+ optional `task`, attachments, loops, …)
- `mcp_client.all_tools()`
- custom / extra tools

Update the add-tool note from “next user message” → “available on the next model step this turn.”

This is a small loop change and unblocks same-turn progressive disclosure.

### B. Meta-tool only: `enable_mcp(name)` (preferred activation UX)

Do **not** rely on the model calling raw `add_mcp_server` with `command` / `args` / `env` for servers already declared in config.

Add a small always-on meta-tool:

```text
enable_mcp(name: str)
```

Behavior:

1. Look up `name` in the loaded `mcp.json` (same config gateway used at startup).
2. If missing → error with a clear message listing known server names.
3. If already connected → no-op success; return current tool names.
4. Otherwise connect using the config entry (stdio or streamable HTTP), same path as `load_from_config` / `connect`.
5. Return `{ ok, server, tools: [{name, description}, ...] }`.
6. Loop refreshes `ctx.tools` (section A) so the **next inner turn** can call `browser__goto`, etc.

Optional twin: `disable_mcp(name)` wrapping `disconnect` for symmetry (or keep `remove_mcp_server`).

**Why meta-tool-only for activation:**

- Always advertise **one** small schema (`enable_mcp`), never 21 browser schemas up front.
- Model never invents `uv` project paths or env vars; config stays the source of truth.
- Works with skills: browser `SKILL.md` can say “call `enable_mcp(\"browser\")` before browser tools.”

Keep raw `add_mcp_server` for ad-hoc / user-supplied servers not in `mcp.json`.

---

## Config: catalog by default

Every server under `mcpServers` is:

- **Known** to `enable_mcp` (catalogued from `mcp.json`) unless `"enabled": false`
- **Not** connected or advertised until the model activates it (or `"autoConnect": true`)

Flags:

| Key | Meaning |
|-----|---------|
| *(omit)* | Catalog only — model calls `enable_mcp` |
| `"enabled": false` | Not catalogued; model cannot connect (trust gate for inert/admin entries) |
| `"autoConnect": true` | Connect at startup (compat for skills that never call `enable_mcp`) |

**Breaking change vs pre-progressive-MCP:** listing a server no longer auto-connects at
startup. Migrate by either teaching skills to call `enable_mcp("name")` or setting
`"autoConnect": true` on servers that must stay hot.

There is no separate `lazy` flag — progressive disclosure is the default.

Bundled browser entry (disabled until approved):

```json
"browser": {
  "enabled": false,
  "command": "python",
  "args": ["-m", "browser_mcp.server"],
  "env": {
    "BROWSER_MCP_PLAYBOOKS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/playbooks",
    "BROWSER_MCP_SCREENSHOTS_DIR": "${MONKEYBOT_WORKSPACE_ROOT}/browser/Screenshots"
  }
}
```

Startup must not populate `all_tools()` for catalog servers, and `build_context` must not include their schemas until after `enable_mcp`.

---

## Activation flow (happy path)

```text
User: "go to auriga-os.com and summarize the homepage"

1. build_context → core tools + enable_mcp only (no browser__*)
2. Model: enable_mcp("browser")
3. MCPClient.connect from mcp.json "browser" entry
4. Loop refreshes ctx.tools → browser__* now advertised
5. Same user turn, next provider round: browser__goto / get_elements / …
6. Final answer
```

```text
User: "hello"

1. build_context → small tool list
2. Model answers without enable_mcp
3. TTFT stays close to bare Ollama (+ small core tools tax)
```

---

## Prompt / skill copy

- Harness prompt: state that heavy MCP servers are activated via `enable_mcp(name)`; list catalog server names from config (e.g. `browser`).
- Browser skill: first step = `enable_mcp("browser")` (or equivalent), then use `browser__*` tools.
- Do not tell the model browser tools are “always available” when they are catalog-only.

---

## What not to rely on

| Approach | Why insufficient |
|----------|------------------|
| `thinking_budget: 0` | Does not shrink tools/prompt; TTFT unchanged |
| Faster TUI spinner | Cosmetic; wait is provider prompt_eval |
| Hoping model calls `remove_mcp_server` after hello | Cost already paid that turn |
| Only trimming AGENT.md | Helps a little; tool schemas dominate |

---

## Implementation sketch

| Area | Change |
|------|--------|
| `core/tools` | Add `enable_mcp` (and optionally `disable_mcp`) tool defs + executor handlers |
| `core/mcp` | Lazy catalog from `mcp.json`; connect-by-name without advertising until connected |
| `core/runtime/loop.py` | After MCP-mutating tools, `replace(ctx, tools=rebuild_tool_list(...))` |
| `core/context` | Shared `rebuild_tool_list` / export helper used by `build_context` and the loop |
| `core/prompts/harness_prompt.py` | Document `enable_mcp`; list catalog server names |
| Demo `mcp.json` | Browser listed (catalog only until enable_mcp) |
| Browser `SKILL.md` | Activate via `enable_mcp("browser")` first |
| Tests | Mid-turn refresh advertises new tools on next inner stream; `enable_mcp` connects from config; hello payload tool count excludes `browser__*` |

---

## Success criteria

Same machine, same model (e.g. Ollama `ornith:9b`):

1. `"hello"` TTFT is near bare Ollama order of magnitude (plus small core-tools overhead), not ~20s.
2. `"go to auriga-os.com…"` performs `enable_mcp("browser")` then browser tools in the **same** user turn.
3. Transcript `ProviderRequest.tools`: ~core-sized on hello; browser tools appear only after activate.
4. Model never needs to supply `command`/`args` for configured servers.

### Known limitation — realtime / voice

`realtime_loop` refreshes harness `ctx.tools` after successful MCP registry mutations and
injects a note that a new session is required. Live vendor sessions (Gemini Live, etc.)
fix tool schemas at `RealtimeSessionConfig` connect time and do not yet support
mid-session tool updates. Newly enabled MCP tools become usable only after starting a
new realtime session (v1 has no reconnect/resume).

---

## Suggested ship order

1. Mid-turn `ctx.tools` refresh after add/remove (correctness; unblocks same-turn use).
2. `enable_mcp(name)` reading `mcp.json` + browser catalog entry in demo.
3. Harness prompt + browser skill alignment.
4. Later (optional): compact schemas for core tools; agent profiles (`chat` / `browser` / `coding`).

---

## Appendix — evidence snapshot

From demo transcript analysis (2026-07-12):

- `"hello"` with full tools: ~20s user → first assistant token when thinking was off.
- `"what tools do u have"` with thinking on: ~0.6s harness, **~19.4s** provider → first `ThinkingBlockDelta`.
- Bare Ollama `"hello"`: ~1.5s total (`prompt_eval` ~90ms).
- ProviderRequest: **39 tools**, **21 `browser__*`**, ~14k system chars, ~15k tool-schema chars.

Conclusion: progressive disclosure belongs in the harness; config toggles alone are a workaround that validates the diagnosis.
