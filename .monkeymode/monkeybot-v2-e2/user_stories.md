# User Stories: monkeybot-v2-e2 — Safety, Skills & Production Gateway

**Date:** 2026-05-13  
**Design Reference:** `.monkeymode/monkeybot-v2-e2/design/`

---

## Parallelization Plan

```
Batch 1 (parallel — zero file conflicts):
  Story 1: Provider Utilities & ClaudeProvider
  Story 2: Safety Inspector Factory
  Story 3: Built-in Skills

        ↓ (batch 1 complete)

Batch 2:
  Story 4: WebhookGateway, serve CLI & Docker
           (imports core/safety.py from Story 2 — must run after Batch 1)
```

**Why Story 4 is in Batch 2:** It modifies `cli.py` (adding `serve` command AND updating `_load_inspectors()`), which overlaps with Story 2's `cli.py` changes. Splitting into batches eliminates the conflict entirely.

All stories are independently testable. Integration is Phase 6.

---

## Story 1: Provider Utilities & ClaudeProvider

**Type:** Feature  
**Priority:** High  
**Size:** M  
**Batch:** 1 — parallel  
**Dependencies:** NONE (builds on E1's `Provider` Protocol and `providers/gemini.py`)

### Description

As a bot developer,  
I want to configure `MODEL_PROVIDER=claude` in my `.env`,  
So that my bot runs on Claude without any framework changes.

### Technical Context

- **Design reference:** 1A "ADR-E2-002", 1B "providers/claude.py", 1B "providers/_utils.py"
- **Affected modules:** `src/monkeybot/providers/` (existing)
- **Key files to create:**
  - `src/monkeybot/providers/_utils.py` — `estimate_cost()` shared utility
  - `src/monkeybot/providers/claude.py` — full streaming `ClaudeProvider`
  - `tests/unit/test_utils.py`
  - `tests/unit/test_claude_provider.py`
  - `tests/integration/test_claude_provider.py`
- **Key files to modify:**
  - `src/monkeybot/providers/gemini.py` — replace local `_estimate_cost()` with import from `_utils`
- **Patterns to follow:** `src/monkeybot/providers/gemini.py` — exact same structure and naming conventions

### Integration Contracts

**Defined by this story:**

```python
# src/monkeybot/providers/_utils.py
def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. pricing values are (input_$/M, output_$/M)."""
    ...

# src/monkeybot/providers/claude.py
class ClaudeProvider:
    name: str          # = "claude"
    supports_streaming: bool   # = True

    def __init__(self) -> None:
        """Raises ValueError immediately if ANTHROPIC_API_KEY is not set."""
        ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[ToolDef],
        *,
        model: str = "claude-3-5-sonnet-20241022",
        system: str = "",
        context: TurnContext | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        """Lazy-imports anthropic inside method (preserve cold-start budget)."""
        ...
```

**Used by this story (from E1 — no mocks needed):**
- `monkeybot.core.provider`: `Message`, `ToolDef`, `TextDelta`, `ToolCall`, `ProviderDone`, `ProviderUsage`, `ProviderEvent`
- `monkeybot.core.context`: `TurnContext`

### Acceptance Criteria

- [ ] **Given** `ANTHROPIC_API_KEY` is not set, **When** `ClaudeProvider()` is called, **Then** `ValueError` raised with message containing `"ANTHROPIC_API_KEY"`
- [ ] **Given** a mocked stream with text chunks, **When** `stream()` is called, **Then** `TextDelta` events are yielded for each chunk
- [ ] **Given** a mocked stream with a tool_use block, **When** `stream()` is called, **Then** one `ToolCall` event is yielded with correct `name` and `args`
- [ ] **Given** any stream (text or tool), **When** stream completes, **Then** `ProviderDone` is always the last event
- [ ] **Given** a `role="tool"` message, **When** `_convert_messages()` runs, **Then** output is Anthropic `tool_result` content block with correct `tool_use_id`
- [ ] **Given** a `ToolDef`, **When** `_convert_tools()` runs, **Then** output has `input_schema` key (not `parameters`)
- [ ] **Given** known model name, **When** `estimate_cost()` called, **Then** returns correct non-zero USD float
- [ ] **Given** unknown model name, **When** `estimate_cost()` called, **Then** returns `0.0`
- [ ] **Given** `ANTHROPIC_API_KEY` set (integration test), **When** `stream()` called with simple prompt, **Then** at least one `TextDelta` and one `ProviderDone` yielded
- [ ] `import monkeybot` time stays under 200ms (no eager `import anthropic` at module level)
- [ ] `ruff check` and `mypy --strict` clean on all new files

### Implementation Details

**Message pairing for tool calls (Anthropic-specific):**
The Anthropic API requires that a tool result (`role="user"`, type `tool_result`) immediately follows the assistant message that issued the `tool_use` block. The `AgentLoop` always appends `role="assistant"` (with `tool_call_id`) then `role="tool"` in sequence. The converter must group these into the correct structure:

```python
# Input from AgentLoop:
# Message(role="assistant", content="", tool_call_id="01J...")
# Message(role="tool", content="result text", tool_call_id="01J...", tool_name="read_file")

# Anthropic wire format output:
# {"role": "assistant", "content": [{"type": "tool_use", "id": "01J...", "name": "read_file", "input": {}}]}
# {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "01J...", "content": "result text"}]}
```

**Streaming tool input accumulation:**
Anthropic streams tool input as `input_json_delta` events containing partial JSON strings. Accumulate these into a buffer, then `json.loads()` at `content_block_stop`:

```python
_tool_input_buf: str = ""
_tool_id: str = ""
_tool_name: str = ""

# On content_block_start (type=tool_use): record id, name; reset buffer
# On content_block_delta (type=input_json_delta): buf += delta.partial_json
# On content_block_stop (when tool active): yield ToolCall(call_id=_tool_id, name=_tool_name, args=json.loads(_tool_input_buf))
```

**Pricing table:**
```python
_PRICING: dict[str, tuple[float, float]] = {
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022":  (0.80,  4.00),
    "claude-3-opus-20240229":     (15.00, 75.00),
}
```

**`gemini.py` change is a one-liner:**
```python
# Remove local _estimate_cost() function
# Add at top:
from monkeybot.providers._utils import estimate_cost
# Replace call: _estimate_cost(model, ...) → estimate_cost(model, ..., _PRICING)
```

**Integration test gating:**
```python
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set"
)
async def test_claude_stream_live(): ...
```

### Out of Scope

- Updating `cli.py` provider factory (Story 4)
- Any other provider (OpenAI etc.)
- Retry logic on API errors

### Notes for Developer

- `anthropic` SDK must be imported inside `stream()` — NOT at module top level. Cold-start budget is 200ms; the `anthropic` import alone costs ~80ms.
- Use `anthropic.AsyncClient` (async, not sync). The streaming context manager is `client.messages.stream(...)` as an async context manager.
- Check `tests/unit/test_gemini_provider.py` (E1) for the mock pattern to follow in `test_claude_provider.py`.

---

## Story 2: Safety Inspector Factory

**Type:** Feature  
**Priority:** High  
**Size:** S  
**Batch:** 1 — parallel  
**Dependencies:** NONE (uses E1's `CommandTierInspector` and `RulesInspector` directly)

### Description

As a bot developer,  
I want to declare safety tiers in `config.yaml`,  
So that I can control what tools the agent can use without writing Python.

### Technical Context

- **Design reference:** 1A "ADR-E2-001", 1B "core/safety.py"
- **Affected modules:** `src/monkeybot/core/` (existing)
- **Key files to create:**
  - `src/monkeybot/core/safety.py` — `load_inspectors()` factory
  - `tests/unit/test_safety.py`
- **Key files to modify:** NONE — `cli.py` changes are Story 4's responsibility
- **Patterns to follow:** `src/monkeybot/core/inspector.py` — type annotations, docstrings, `from __future__ import annotations`

### Integration Contracts

**Defined by this story:**

```python
# src/monkeybot/core/safety.py
from typing import Any
from monkeybot.core.inspector import ToolInspector

def load_inspectors(config: dict[str, Any]) -> list[ToolInspector]:
    """Build inspector chain from parsed config.yaml dict.

    Args:
        config: Full bot config dict (or {} for dev mode).

    Returns:
        Ordered list of ToolInspector instances. Empty list = allow all.
        Order: [CommandTierInspector, RulesInspector] when both present.
        CommandTierInspector runs first; first non-allow decision wins.

    Raises:
        Nothing. Missing/malformed keys are treated as absent.
    """
    ...
```

**Used by this story (from E1 — no mocks needed):**
- `monkeybot.core.inspector`: `CommandTierInspector`, `RulesInspector`, `ToolInspector`

### Acceptance Criteria

- [ ] **Given** `config={}`, **When** `load_inspectors()` called, **Then** returns `[]`
- [ ] **Given** config with no `safety` key, **When** called, **Then** returns `[]`
- [ ] **Given** config with `safety.command_tiers` only, **When** called, **Then** returns `[CommandTierInspector]`
- [ ] **Given** config with `safety.denied_patterns` only, **When** called, **Then** returns `[RulesInspector]`
- [ ] **Given** config with both `command_tiers` and `denied_patterns`, **When** called, **Then** returns `[CommandTierInspector, RulesInspector]` in that order
- [ ] **Given** a tool in `denied` tier, **When** `CommandTierInspector.check()` called, **Then** `Decision(kind="deny")` returned
- [ ] **Given** a tool in `pre_approved` tier, **When** `CommandTierInspector.check()` called, **Then** `Decision(kind="allow")` returned
- [ ] **Given** a tool in `requires_approval` tier, **When** `CommandTierInspector.check()` called, **Then** `Decision(kind="approve")` returned
- [ ] **Given** args containing a `denied_patterns` substring, **When** `RulesInspector.check()` called, **Then** `Decision(kind="deny")` returned
- [ ] **Given** malformed YAML value (e.g. `command_tiers: null`), **When** called, **Then** no exception raised, returns `[]` or partial list
- [ ] `ruff check` and `mypy --strict` clean

### Implementation Details

```python
# src/monkeybot/core/safety.py
from __future__ import annotations
from typing import Any
from monkeybot.core.inspector import CommandTierInspector, RulesInspector, ToolInspector

def load_inspectors(config: dict[str, Any]) -> list[ToolInspector]:
    safety = config.get("safety") or {}
    if not isinstance(safety, dict):
        return []
    inspectors: list[ToolInspector] = []
    tiers = safety.get("command_tiers")
    if isinstance(tiers, dict):
        inspectors.append(CommandTierInspector(tiers))
    patterns = safety.get("denied_patterns")
    if isinstance(patterns, list):
        inspectors.append(RulesInspector(denied_patterns=patterns))
    return inspectors
```

This is intentionally simple — the real logic lives in E1's `CommandTierInspector` and `RulesInspector`. This factory just reads the YAML shape and wires them.

### Out of Scope

- Updating `cli.py` to call `load_inspectors()` (Story 4)
- Any new inspector types beyond E1's two

### Notes for Developer

- The function body above is essentially the full implementation. Tests are the real deliverable here — cover all the YAML edge cases (null, missing, wrong type) since those are the most likely runtime failures.
- Do NOT modify `cli.py` in this story — that's Story 4's job.

---

## Story 3: Built-in Skills

**Type:** Feature  
**Priority:** Medium  
**Size:** S  
**Batch:** 1 — parallel  
**Dependencies:** NONE (pure markdown files; `list_skills()` scanner already exists in E1)

### Description

As a skill author,  
I want to drop a `SKILL.md` file into `.agents/skills/my-skill/`,  
So that the agent discovers and uses it without any framework changes.

### Technical Context

- **Design reference:** 1A "ADR-E2-006", 1B "Built-in Skills"
- **No Python files.** All 4 skills are pure markdown.
- **Key files to create:**
  - `.agents/skills/memory-save/SKILL.md`
  - `.agents/skills/memory-search/SKILL.md`
  - `.agents/skills/file-ops/SKILL.md`
  - `.agents/skills/self-improve/SKILL.md`
- **Key files to modify:** NONE
- **Patterns to follow:** Each SKILL.md must have its description on the second non-blank line (after the H1 title) — that's what `list_skills()` extracts as the description. First line is `# Skill Name`.

### Integration Contracts

**Defined by this story:** The content and instructions within each SKILL.md.

**Used by this story (from E1):** `list_skills()` in `tools/skill_ops.py` — already scans `{skills_path}/*/SKILL.md`, reads the first non-blank non-heading line as description.

```python
# Discovery already works (E1):
list_skills(skills_path=".agents/skills")
# Returns:
# "Available skills:
# - **memory-save** (.agents/skills/memory-save/SKILL.md): <description line>
# - **memory-search** ...
# ..."
```

### Skill Content Specifications

#### `.agents/skills/memory-save/SKILL.md`

```markdown
# memory-save
Save information to persistent memory so you can recall it later.

## When to use
Use this skill when the user asks you to remember something, or when you learn
something important that should persist across sessions.

## Steps
1. Decide on a descriptive filename (e.g. `user-preferences.md`, `project-context.md`)
2. Call write_file with:
   - path: `{memory_path}/{filename}.md`  (memory_path is in your context)
   - content: a well-structured markdown document with the information
3. Confirm to the user: "I've saved that to memory as `{filename}.md`"

## Example
User: "Remember that I prefer concise answers"
→ write_file(path="{memory_path}/user-preferences.md",
             content="# User Preferences\n\n- Prefers concise answers")
```

#### `.agents/skills/memory-search/SKILL.md`

```markdown
# memory-search
Search your persistent memory to recall previously saved information.

## When to use
Use this skill when the user asks about something you might have saved before,
or when context from a previous session would be helpful.

## Steps
1. Call search_memory with a descriptive query string
2. Review the returned results
3. If results are relevant, use them to inform your response
4. If no results found, tell the user you don't have that in memory

## Example
User: "What are my preferences?"
→ search_memory(query="user preferences", top_k=5)
→ Summarise relevant results in your response
```

#### `.agents/skills/file-ops/SKILL.md`

```markdown
# file-ops
Read and write files in the bot directory and memory directory.

## When to use
Use this skill when you need to read a configuration file, inspect a document,
or write output that should persist as a file.

## Allowed paths
- Bot directory (where AGENT.md lives) — read and write
- Memory directory — read and write
- Do NOT attempt to access paths outside these directories

## Steps — Reading a file
1. Call read_file(path="relative/path/from/bot/dir/file.txt")
2. Use the returned content in your response

## Steps — Writing a file
1. Decide on a clear filename and path
2. Call write_file(path="...", content="...")
3. Confirm the write to the user

## Notes
- Paths outside the allowed roots return "ERROR: Access denied"
- Use forward slashes even on Windows
```

#### `.agents/skills/self-improve/SKILL.md`

```markdown
# self-improve
Update your own AGENT.md to capture lessons learned and improve future behaviour.

## When to use
Use this skill after completing a task where you learned something that would
make you more effective in future sessions — a new pattern, a user preference,
a useful fact about the project.

## Steps
1. Read your AGENT.md with read_file(path="{agent_md_path}")
2. Identify the most relevant section to append to (or create a new ## section)
3. Draft a concise lesson — 1–3 bullet points maximum
4. Write the updated AGENT.md back with write_file
5. Tell the user: "I've updated my instructions to remember that."

## Rules
- Only add information, never remove existing instructions
- Keep additions concise — avoid padding
- Use the same markdown style as the existing AGENT.md
```

### Acceptance Criteria

- [ ] **Given** `skills_path=".agents/skills"`, **When** `list_skills()` called, **Then** all 4 skill names appear in the result
- [ ] **Given** each SKILL.md, **When** `list_skills()` runs, **Then** a non-empty description is extracted (second non-blank line after H1)
- [ ] **Given** `filter="memory"`, **When** `list_skills(filter="memory")` called, **Then** only `memory-save` and `memory-search` are returned
- [ ] **Given** each SKILL.md, **When** `read_file(path=skill_md_path)` called, **Then** content is returned (readable, no error)
- [ ] Each SKILL.md has: H1 title, non-blank description line, `## Steps` section with numbered steps, at least one example or note

### Out of Scope

- Python skill helpers (research-web deferred to later epic)
- Testing the agent's ability to follow skill instructions (that's an LLM eval, not a unit test)

### Notes for Developer

- The description extraction in `list_skills()` reads the first non-blank, non-heading line. Your SKILL.md must have the description as the SECOND line (or first non-blank line after the H1). Don't start with a blank line after `# Title`.
- `{memory_path}` and `{agent_md_path}` in skill content are literal placeholder strings — the agent reads them from its context/system prompt, not from the markdown file itself.
- Write a simple test that calls `list_skills()` pointing at the real `.agents/skills/` directory and asserts all 4 skills are found.

---

## Story 4: WebhookGateway, serve CLI & Docker

**Type:** Feature  
**Priority:** High  
**Size:** L  
**Batch:** 2 — after Batch 1 (Story 2 must be complete; imports `core/safety.py`)  
**Dependencies:** Story 2 (`core/safety.py` must exist before `cli.py` can import it)

### Description

As a bot operator,  
I want to run `monkeybot serve --bot-dir /bot` and have a platform-agnostic webhook endpoint available,  
So that I can connect any chat platform to my bot by providing a single `webhook.py` file.

### Technical Context

- **Design reference:** 1A "ADR-E2-004", 1A "ADR-E2-005", 1B "gateway/webhook.py", 1B "monkeybot serve", 1C "Docker"
- **Key files to create:**
  - `src/monkeybot/gateway/webhook.py` — `WebhookGateway` class + `load_bot_webhook()`
  - `bots/example-bot/webhook.py` — Google Chat extractor reference
  - `bots/example-bot/webhook_slack_example.py` — Slack extractor reference
  - `docker/Dockerfile` — multi-stage, framework-only base image
  - `docker/docker-compose.yml` — local dev with volume mounts
  - `tests/integration/test_gateway.py`
  - `tests/test_e2_cold_start.py`
- **Key files to modify:**
  - `src/monkeybot/cli.py` — add `serve` command; update `_load_inspectors()` to delegate to `core/safety.py`; update `_load_provider()` to support `claude`
  - `bots/example-bot/config.yaml` — add `safety.command_tiers` + `safety.denied_patterns`
  - `src/monkeybot/__init__.py` — lazy export of `WebhookGateway`
- **Patterns to follow:** `src/monkeybot/gateway/cli.py` (E1) for gateway structure; `cli.py` existing `run` command for `serve` command structure

### Integration Contracts

**Defined by this story:**

```python
# src/monkeybot/gateway/webhook.py

MessageExtractor = Callable[[dict[str, Any]], str | None]
ResponseFormatter = Callable[[str], dict[str, Any]]
SessionIdFn = Callable[[dict[str, Any]], str]

class WebhookGateway:
    def __init__(
        self,
        loop: AgentLoop,
        session_id_fn: SessionIdFn,
        extract_message: MessageExtractor,
        format_response: ResponseFormatter | None = None,
    ) -> None: ...

    def build_app(self) -> FastAPI:
        """Returns FastAPI app with POST /webhook and GET /health registered."""
        ...

def load_bot_webhook(
    bot_dir: str,
) -> tuple[MessageExtractor, ResponseFormatter, SessionIdFn]:
    """Dynamically load extract_message, format_response, session_id from
    {bot_dir}/webhook.py. Falls back to generic extractor if file absent."""
    ...
```

**Used by this story:**
- `monkeybot.core.loop`: `AgentLoop` (E1)
- `monkeybot.core.safety`: `load_inspectors` (Story 2 — must exist before this story runs)
- `monkeybot.providers.claude`: `ClaudeProvider` (Story 1 — must exist before this story runs)
- `fastapi`, `uvicorn` (already in `[gchat]` optional extras)

### Acceptance Criteria

**WebhookGateway:**
- [ ] **Given** `GET /health`, **When** server is running, **Then** `{"status": "ok"}` with HTTP 200, no auth required
- [ ] **Given** `POST /webhook` with valid JSON payload, **When** `extract_message()` returns a string, **Then** agent runs and response is returned via `format_response()`
- [ ] **Given** `POST /webhook` where `extract_message()` returns `None`, **When** called, **Then** `format_response("")` returned immediately, no agent call made
- [ ] **Given** non-JSON body, **When** `POST /webhook`, **Then** HTTP 422
- [ ] **Given** `WEBHOOK_SECRET` is set and wrong HMAC token in header, **When** `POST /webhook`, **Then** HTTP 401
- [ ] **Given** `WEBHOOK_SECRET` is set and correct HMAC token, **When** `POST /webhook`, **Then** HTTP 200

**`load_bot_webhook()`:**
- [ ] **Given** `webhook.py` present with all 3 functions, **When** called, **Then** all 3 are returned correctly
- [ ] **Given** `webhook.py` absent, **When** called, **Then** fallback generic extractor returned (no exception)
- [ ] **Given** `webhook.py` has a syntax error, **When** called, **Then** raises `ImportError` with clear message pointing to the file path

**Reference extractors:**
- [ ] **Given** Google Chat `MESSAGE` event payload, **When** `extract_message()` called, **Then** returns `message.text`
- [ ] **Given** Google Chat `ADDED_TO_SPACE` event, **When** called, **Then** returns `None`
- [ ] **Given** Slack `message` event payload, **When** Slack extractor's `extract_message()` called, **Then** returns `event.text`
- [ ] **Given** Slack `bot_message` subtype, **When** called, **Then** returns `None`

**`cli.py` changes:**
- [ ] **Given** `monkeybot serve --bot-dir ./bots/example-bot --port 8080`, **When** run with valid `GEMINI_API_KEY`, **Then** starts uvicorn and `/health` returns 200
- [ ] **Given** `MODEL_PROVIDER=claude`, **When** provider factory called, **Then** `ClaudeProvider()` returned (or `ValueError` if key missing)
- [ ] **Given** `config.yaml` with `safety.command_tiers`, **When** `_load_inspectors()` called, **Then** delegates to `core/safety.load_inspectors()` and returns correct chain

**Docker:**
- [ ] `docker build .` exits 0 from `docker/` directory
- [ ] Running container with `monkeybot serve --bot-dir /bot` + valid env starts and `/health` returns 200
- [ ] `docker/docker-compose.yml` valid YAML; `docker compose config` exits 0

### Implementation Details

**`POST /webhook` handler — full flow:**

```python
@app.post("/webhook")
async def webhook_handler(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > 64 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")

    # HMAC verification (if WEBHOOK_SECRET set)
    secret = os.getenv("WEBHOOK_SECRET", "")
    if secret:
        sig_header = request.headers.get("X-Hub-Signature-256") or \
                     request.headers.get("Authorization", "").removeprefix("Bearer ")
        if not _verify_hmac(secret, body, sig_header):
            raise HTTPException(status_code=401, detail="Invalid signature")

    payload: dict[str, Any] = json.loads(body)
    user_message = self._extract(payload)
    if not user_message:
        return self._format("")

    session_id = self._session_id_fn(payload)
    full_response_parts: list[str] = []
    async for event in self._loop.run(user_message, session_id):
        if isinstance(event, AssistantDelta):
            full_response_parts.append(event.text)
    return self._format("".join(full_response_parts))
```

**HMAC verification:**
```python
import hashlib, hmac as _hmac

def _verify_hmac(secret: str, body: bytes, header: str | None) -> bool:
    if not header:
        return False
    expected = "sha256=" + _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    # Also accept bare hex digest (without sha256= prefix)
    return _hmac.compare_digest(expected, header) or \
           _hmac.compare_digest(expected[7:], header)
```

**`cli.py` — `serve` command (mirrors `run` command structure):**
```python
@main.command()
@click.option("--bot-dir", required=True, type=click.Path(exists=True))
@click.option("--host", default="0.0.0.0")
@click.option("--port", default=8080, type=int)
def serve(bot_dir: str, host: str, port: int) -> None:
    """Start the webhook gateway server."""
    import uvicorn  # lazy import
    _setup_logging()
    asyncio.run(_serve_async(bot_dir, host, port))

async def _serve_async(bot_dir: str, host: str, port: int) -> None:
    # Same setup as _run_async but builds WebhookGateway instead of CLIGateway
    ...
    from monkeybot.gateway.webhook import WebhookGateway, load_bot_webhook
    extract, fmt, session_fn = load_bot_webhook(bot_dir)
    gateway = WebhookGateway(loop=agent_loop, session_id_fn=session_fn,
                              extract_message=extract, format_response=fmt)
    app = gateway.build_app()
    config = uvicorn.Config(app, host=host, port=port, log_config=None)
    server = uvicorn.Server(config)
    await server.serve()
```

**`cli.py` — `_load_inspectors` update (one-liner):**
```python
def _load_inspectors(bot_config: dict[str, Any]) -> list[object]:
    from monkeybot.core.safety import load_inspectors
    return load_inspectors(bot_config)
```

**`bots/example-bot/config.yaml` — add safety block:**
```yaml
# Add to existing config.yaml
safety:
  command_tiers:
    pre_approved:
      - read_file
      - list_skills
      - search_memory
    requires_approval:
      - write_file
    denied:
      - run_command
  denied_patterns:
    - "rm -rf"
    - "/etc/passwd"
    - "DROP TABLE"
```

**Test strategy — `test_gateway.py`:**
Use FastAPI's `TestClient` (synchronous). Mock `AgentLoop.run()` to yield `AssistantDelta(text="hello")` then `TurnComplete(...)`. This avoids any real LLM calls.

**`test_e2_cold_start.py`:**
```python
# Verifies monkeybot serve starts and /health returns 200
# Requires GEMINI_API_KEY in environment
# Starts server in background thread, hits /health, asserts 200
```

### Out of Scope

- Google-specific JWT token verification (user's job in their `webhook.py`)
- Rate limiting (E3)
- Multi-worker uvicorn (E3)
- Idempotency key deduplication (E3)

### Notes for Developer

- **Lazy imports everywhere in `serve`:** `fastapi`, `uvicorn` must be imported inside the `serve` command function, not at module top-level in `cli.py`. The 200ms cold-start budget applies to `monkeybot run` as well as `monkeybot serve`.
- **`build_app()` returns a new `FastAPI` instance each call** — don't store it as an instance variable, return fresh from the method.
- **`load_bot_webhook()` wraps `exec_module` in a broad try/except** — any failure in the user's `webhook.py` should raise with the file path in the error message, not a bare `ImportError`.
- **Reference to startup warning:** if `WEBHOOK_SECRET` is not set, log a WARNING at startup (once, not per request): `"WEBHOOK_SECRET not set — webhook endpoint is unauthenticated"`.
- Check `src/monkeybot/gateway/cli.py` (E1) for the import pattern and `CLIGateway` structure to mirror.

---

## Parallelization Summary

| Story | Batch | Files Created | Files Modified |
|-------|-------|--------------|----------------|
| 1: ClaudeProvider | 1 | `providers/_utils.py`, `providers/claude.py`, `tests/unit/test_utils.py`, `tests/unit/test_claude_provider.py`, `tests/integration/test_claude_provider.py` | `providers/gemini.py` |
| 2: Safety Factory | 1 | `core/safety.py`, `tests/unit/test_safety.py` | — |
| 3: Built-in Skills | 1 | `.agents/skills/*/SKILL.md` (×4) | — |
| 4: Gateway+serve+Docker | 2 | `gateway/webhook.py`, `bots/example-bot/webhook.py`, `bots/example-bot/webhook_slack_example.py`, `docker/Dockerfile`, `docker/docker-compose.yml`, `tests/integration/test_gateway.py`, `tests/test_e2_cold_start.py` | `cli.py`, `bots/example-bot/config.yaml`, `__init__.py` |

**No file conflicts within any batch.** Story 4's `cli.py` modifications are clean because Story 2 leaves `cli.py` untouched.
