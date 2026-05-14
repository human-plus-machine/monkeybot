# Code Spec: Story 1 — Provider Utilities & ClaudeProvider

**Story:** User Story 1 — Provider Utilities & ClaudeProvider  
**Design Reference:** 1A ADR-E2-002, 1B `providers/claude.py`, 1B `providers/_utils.py`  
**Date:** 2026-05-13  

## Implementation Summary

- **Files to Create:** 5 files
- **Files to Modify:** 1 file (`providers/gemini.py`)
- **Estimated Complexity:** M

## Codebase Conventions

- `from __future__ import annotations` on every module
- Module docstring at top describing purpose (see `gemini.py` header)
- Import order: stdlib → third-party → local (`monkeybot.*`)
- Type hints throughout; `mypy --strict` must pass
- `_log = logging.getLogger(__name__)` for module-level logger
- `_SNAKE_CAPS` for module-level constants (e.g. `_PRICING`)
- Private helpers prefixed `_` (e.g. `_convert_messages`, `_estimate_cost`)
- `pytest-asyncio` in `auto` mode — no `@pytest.mark.asyncio` needed
- `ruff` line length 100; `UP`, `I`, `F`, `E` rules

## Technical Context

**Key Gotchas:**
- `anthropic` SDK must be imported **inside** `stream()` only — not at module level. `anthropic` import costs ~80ms; the cold-start budget is 200ms.
- Anthropic requires tool results to use `"tool_result"` content blocks with `"tool_use_id"` (not `"tool_call_id"`). Tool call messages from `AgentLoop` arrive as `Message(role="assistant", tool_call_id=...)` + `Message(role="tool", tool_call_id=..., tool_name=...)`.
- Tool input comes from Anthropic as streaming `input_json_delta` events; must accumulate a buffer then `json.loads()` at `content_block_stop`.
- Tools use `"input_schema"` key (Anthropic) not `"parameters"` (OpenAI/Gemini).
- Use `anthropic.AsyncAnthropic()` — async client, not sync.
- Streaming via `await client.messages.stream(...) as stream:` + `async for event in stream:`.

**Reusable Utilities:**
- `monkeybot.core.provider`: `Message`, `ToolDef`, `TextDelta`, `ToolCall`, `ProviderDone`, `ProviderUsage`, `ProviderEvent`, `Provider`
- `monkeybot.core.context`: `TurnContext`
- `ulid` for generating `call_id` on `ToolCall` events (same as `gemini.py`)

**Pattern Reference:** `src/monkeybot/providers/gemini.py` — exact structure to mirror

## Task Breakdown

### Task 1: Create `providers/_utils.py`

**Dependencies:** None  
**Files:** `src/monkeybot/providers/_utils.py` (create)  
**Pattern:** Module-level `_PRICING` dict + standalone function, same as local `_estimate_cost` in `gemini.py`

**Function signature:**
```python
def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    pricing: dict[str, tuple[float, float]],
) -> float:
    """Return estimated USD cost. pricing values are (input_$/M, output_$/M)."""
```

**Implementation:** Lookup `pricing.get(model, (0.0, 0.0))`, return `(input * rate[0] + output * rate[1]) / 1_000_000`.

**Tests** (`tests/unit/test_utils.py`):
- Known model → correct non-zero float
- Unknown model → `0.0`
- Zero tokens → `0.0`
- Both token types contribute to total

---

### Task 2: Refactor `providers/gemini.py` to use `_utils`

**Dependencies:** Task 1  
**Files:** `src/monkeybot/providers/gemini.py` (modify)

**Changes (minimal):**
1. Add import: `from monkeybot.providers._utils import estimate_cost`
2. Delete the local `_estimate_cost()` function (lines 45–57)
3. Replace the call `_estimate_cost(model, input_tokens, output_tokens)` → `estimate_cost(model, input_tokens, output_tokens, _PRICING)`

No other changes to `gemini.py`.

---

### Task 3: Create `providers/claude.py`

**Dependencies:** Task 1  
**Files:** `src/monkeybot/providers/claude.py` (create)

**Pattern:** Mirror `gemini.py` structure exactly — same docstring style, `_PRICING` dict, `_convert_messages`, `_convert_tools`, `stream()` method.

**Module docstring** (top of file):
```
Anthropic Claude provider.
Implements the Provider Protocol from core/provider.py.
Lazy import: anthropic is only imported inside stream().
Keeps cold start fast — the SDK is not loaded until first use.
```

**Pricing table:**
```python
_PRICING: dict[str, tuple[float, float]] = {
    "claude-3-5-sonnet-20241022": (3.00, 15.00),
    "claude-3-5-haiku-20241022":  (0.80,  4.00),
    "claude-3-opus-20240229":     (15.00, 75.00),
}
```

**Class structure:**
```python
class ClaudeProvider:
    @property
    def name(self) -> str: return "claude"

    @property
    def supports_streaming(self) -> bool: return True

    def __init__(self) -> None:
        """Raises ValueError immediately if ANTHROPIC_API_KEY is not set."""
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is not set")

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]: ...
    def _convert_tools(self, tools: list[ToolDef]) -> list[dict[str, Any]]: ...
    async def stream(self, messages, tools, *, model, system, context=None) -> AsyncIterator[ProviderEvent]: ...
```

**`_convert_messages` algorithm:**
- `role="user"` → `{"role": "user", "content": m.content}`
- `role="assistant"` with `tool_call_id` → `{"role": "assistant", "content": [{"type": "tool_use", "id": m.tool_call_id, "name": <peek next msg>, "input": {}}]}`
- `role="tool"` → `{"role": "user", "content": [{"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}]}`
- `role="assistant"` without `tool_call_id` → `{"role": "assistant", "content": m.content}`

Note: The assistant tool_use block needs the tool name. Since the `role="tool"` message that follows has `tool_name`, look ahead: iterate with index so you can peek `messages[i+1].tool_name` when building the assistant tool_use block. Skip the `role="assistant"` with `tool_call_id` in isolation — it pairs with the following `role="tool"` message. Alternatively, build the tool_use block when processing the `role="tool"` message and insert both into output.

**`_convert_tools` algorithm:**
```python
return [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
```

**`stream()` algorithm:**
```python
async def stream(self, messages, tools, *, model="claude-3-5-sonnet-20241022", system="", context=None):
    import anthropic  # noqa: PLC0415  — lazy import

    client = anthropic.AsyncAnthropic()
    _tool_input_buf = ""
    _tool_id = ""
    _tool_name = ""
    input_tokens = 0
    output_tokens = 0

    try:
        async with client.messages.stream(
            model=model,
            system=system or anthropic.NOT_GIVEN,
            messages=self._convert_messages(messages),
            tools=self._convert_tools(tools) if tools else anthropic.NOT_GIVEN,
            max_tokens=4096,
        ) as stream:
            async for event in stream:
                match event.type:
                    case "content_block_start":
                        if event.content_block.type == "tool_use":
                            _tool_id = event.content_block.id
                            _tool_name = event.content_block.name
                            _tool_input_buf = ""
                    case "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield TextDelta(text=event.delta.text)
                        elif event.delta.type == "input_json_delta":
                            _tool_input_buf += event.delta.partial_json
                    case "content_block_stop":
                        if _tool_id:
                            yield ToolCall(call_id=_tool_id, name=_tool_name,
                                           args=json.loads(_tool_input_buf or "{}"))
                            _tool_id = _tool_name = _tool_input_buf = ""
                    case "message_delta":
                        if hasattr(event, "usage"):
                            output_tokens = event.usage.output_tokens or 0
                    case "message_start":
                        if hasattr(event, "message") and event.message.usage:
                            input_tokens = event.message.usage.input_tokens or 0
    except Exception as exc:
        _log.warning("Claude stream error: %s", exc)

    yield ProviderDone(usage=ProviderUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=estimate_cost(model, input_tokens, output_tokens, _PRICING),
    ))
```

---

### Task 4: Unit tests for `ClaudeProvider`

**Dependencies:** Task 3  
**Files:** `tests/unit/test_claude_provider.py` (create)

**Pattern:** Follow `tests/unit/test_loop.py` — `FakeProvider` approach. Mock the `anthropic` SDK by patching `anthropic.AsyncAnthropic` inside `stream()`. Use `unittest.mock.AsyncMock` and `MagicMock`.

**Mock strategy:** Patch `anthropic` module in `monkeybot.providers.claude` using `pytest.monkeypatch` or `unittest.mock.patch`. The mock stream context manager should yield fake `event` objects with the right `.type` attribute.

**Test cases:**
- `ANTHROPIC_API_KEY` not set → `ValueError` containing `"ANTHROPIC_API_KEY"`
- Mock stream with text deltas → `TextDelta` events yielded in order
- Mock stream with `tool_use` block → single `ToolCall` with correct `name` and `args`
- Any stream → `ProviderDone` is always the last event
- `role="tool"` message → converted to `tool_result` content block with correct `tool_use_id`
- `_convert_tools()` output has `"input_schema"` key (not `"parameters"`)
- `estimate_cost()` known model → correct float; unknown model → `0.0`

---

### Task 5: Integration test for `ClaudeProvider`

**Dependencies:** Task 3  
**Files:** `tests/integration/test_claude_provider.py` (create)

**Pattern:** Mirror `tests/integration/test_gemini_provider.py` exactly.

```python
def test_isinstance_check() -> None:
    """No API key needed — structural subtyping only."""
    os.environ["ANTHROPIC_API_KEY"] = "test"
    assert isinstance(ClaudeProvider(), Provider)

def test_lazy_import() -> None:
    """anthropic must not be imported at module level."""
    # subprocess check — same pattern as test_gemini_provider.py
    # import monkeybot.providers.claude; assert elapsed < 200ms

@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
async def test_claude_stream_live() -> None:
    """Live API: at least one TextDelta and ProviderDone as last event."""
```

Note: `test_isinstance_check` must set env var before instantiating to avoid `ValueError`.

## Reference Code Example

**Provider Pattern** (from `src/monkeybot/providers/gemini.py`):
```python
class GeminiProvider:
    @property
    def name(self) -> str: return "gemini"
    @property
    def supports_streaming(self) -> bool: return True

    async def stream(self, messages, tools, *, model, system, context=None):
        import google.genai as genai  # noqa: PLC0415  — lazy import
        # ... implementation
        yield ProviderDone(usage=ProviderUsage(...))
```

## Final Verification

**Functionality:**
- [ ] `ClaudeProvider()` raises `ValueError` when `ANTHROPIC_API_KEY` missing
- [ ] `TextDelta` events yielded for text chunks
- [ ] `ToolCall` yielded with correct `call_id`, `name`, `args` (JSON-parsed)
- [ ] `ProviderDone` always last event
- [ ] `_convert_tools` emits `input_schema` not `parameters`
- [ ] `_convert_messages` handles `role="tool"` → `tool_result` block
- [ ] `estimate_cost` extracted to `_utils.py`; `gemini.py` updated to use it

**Code Quality:**
- [ ] `anthropic` imported only inside `stream()` — not at module level
- [ ] `ruff check` clean on all new/modified files
- [ ] `mypy --strict` clean on all new/modified files
- [ ] Cold-start test: `import monkeybot.providers.claude` completes in < 200ms

**Testing:**
- [ ] Unit tests pass (no API key required)
- [ ] Integration `test_lazy_import` passes
- [ ] Live integration test skipped when `ANTHROPIC_API_KEY` not set
