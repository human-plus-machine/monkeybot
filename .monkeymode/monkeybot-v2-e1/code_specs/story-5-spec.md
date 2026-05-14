# Code Spec: Story 5 — Gemini Provider

**Story:** user_stories.md "Story 5: Gemini Provider"  
**Design Reference:** 1b-contracts.md "core/provider.py", "Real Provider Integration Tests", 1c-operations.md "Import Budget"  
**Date:** 2026-05-13  
**Complexity:** S  
**Batch:** 2 (requires Story 1 types to be merged first)

## Implementation Summary
- **Files to Create:** 2 (1 source + 1 integration test)
- **Files to Modify:** 0
- **Estimated LOC:** ~150 source, ~60 test

## Technical Context

**Key gotcha — SDK package name:** The `google-genai>=0.8` package may expose as either `google.generativeai` or `google.genai` depending on exact version. Check the installed version in `uv.lock` before writing the import. Look for the entry under `google-genai` and its installed version to confirm the API surface. Add a comment in the file documenting which version was confirmed.

**Lazy import is mandatory:** `import google.generativeai as genai` (or `google.genai`) belongs INSIDE `stream()` only — never at the module top level. The module-level import alone takes ~150ms which would blow the 200ms cold start budget.

**Open item from 1A:** Confirm that the `google-genai >= 0.8` streaming API surfaces `ToolCall` (function_call) events during streaming. Document your finding in a comment. The current plan code attempts `part.function_call` — verify this is the correct attribute name for the installed version.

---

## Task 1: `providers/gemini.py`

**Files:** `src/monkeybot/providers/gemini.py` (create)  
**Deps:** `google-genai>=0.8` (lazy import), `ulid-py` (for call IDs), `core/provider.py` (Story 1)

**Full implementation** — start from `monkeybot_v2_plan.md` Section 7 "providers/gemini.py" and apply these corrections/additions:

**Pricing dict** (add at module top, no import cost):
```python
_PRICING: dict[str, tuple[float, float]] = {
    # (input_per_million, output_per_million) in USD
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-2.0-flash-lite": (0.0375, 0.15),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
}

def _estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _PRICING.get(model, (0.0, 0.0))
    return (input_tokens * rates[0] + output_tokens * rates[1]) / 1_000_000
```

**Error handling in `stream()`:** Wrap the entire streaming loop in `try/except Exception`. On any exception, yield `ProviderDone(usage=ProviderUsage(input_tokens=0, output_tokens=0))` and return — do not propagate. Log the error at WARNING level.

**`_convert_messages` for tool role:** The Gemini API requires tool results in a specific format. For messages with `role == "tool"`:
```python
# Tool result message — Gemini expects function_response part
{"role": "user", "parts": [{"function_response": {"name": m.tool_name or "", "response": {"result": m.content}}}]}
```
For `role == "assistant"` / `role == "user"`: standard `{"role": "model"/"user", "parts": [m.content]}`.

**`GEMINI_API_KEY` check:** Access `os.environ["GEMINI_API_KEY"]` inside `stream()` — this raises `KeyError` if missing, which propagates naturally (acceptable for misconfiguration).

**Full `stream()` skeleton:**
```python
async def stream(self, messages, tools, *, model="gemini-2.0-flash", system="", context=None):
    import google.generativeai as genai  # lazy import — MUST stay inside method

    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    client = genai.GenerativeModel(
        model_name=model,
        system_instruction=system or None,
        tools=self._convert_tools(tools) if tools else None,
    )

    history = self._convert_messages(messages[:-1])
    last_msg = messages[-1].content if messages else ""
    chat = client.start_chat(history=history)

    input_tokens = output_tokens = 0
    try:
        response = await chat.send_message_async(last_msg, stream=True)
        async for chunk in response:
            if chunk.text:
                yield TextDelta(text=chunk.text)
            for part in (chunk.candidates[0].content.parts if chunk.candidates else []):
                if hasattr(part, "function_call") and part.function_call:
                    fc = part.function_call
                    yield ToolCall(
                        call_id=str(ulid.new()),
                        name=fc.name,
                        args=dict(fc.args),
                    )
            if hasattr(chunk, "usage_metadata") and chunk.usage_metadata:
                um = chunk.usage_metadata
                input_tokens = getattr(um, "prompt_token_count", 0) or 0
                output_tokens = getattr(um, "candidates_token_count", 0) or 0
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("Gemini stream error: %s", exc)

    yield ProviderDone(
        usage=ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_estimate_cost(model, input_tokens, output_tokens),
        )
    )
```

---

## Task 2: Integration Test

**Files:** `tests/integration/test_gemini_provider.py` (create)  
**Deps:** `GEMINI_API_KEY` env var (skip if not set)

```python
import os
import pytest
from monkeybot.core.provider import Message, ToolDef, TextDelta, ToolCall, ProviderDone, Provider
from monkeybot.providers.gemini import GeminiProvider

def test_isinstance_check():
    """No API key needed — pure structural subtyping test."""
    assert isinstance(GeminiProvider(), Provider)

def test_lazy_import():
    """Module import must not pull in google-genai."""
    import time, subprocess
    start = time.monotonic()
    subprocess.run(["python", "-c", "import monkeybot.providers.gemini"], check=True)
    elapsed_ms = (time.monotonic() - start) * 1000
    assert elapsed_ms < 100, f"Import took {elapsed_ms:.0f}ms — google-genai may be at module level"

@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="No GEMINI_API_KEY set")
async def test_real_text_response():
    provider = GeminiProvider()
    events = []
    async for event in await provider.stream(
        messages=[Message(role="user", content="Reply with exactly the word: pong")],
        tools=[],
        model="gemini-2.0-flash",
        system="You are a minimal test assistant. Follow instructions exactly.",
    ):
        events.append(event)
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    done = [e for e in events if isinstance(e, ProviderDone)]
    assert "pong" in text.lower()
    assert len(done) == 1
    assert done[0].usage.input_tokens > 0
    assert events[-1] is done[0], "ProviderDone must be last event"

@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="No GEMINI_API_KEY set")
async def test_tool_call_in_stream():
    """Confirm tool calls surface correctly in streaming mode."""
    provider = GeminiProvider()
    tool = ToolDef(
        name="get_weather",
        description="Get current weather for a city",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    )
    events = []
    async for event in await provider.stream(
        messages=[Message(role="user", content="What is the weather in Paris? Use the get_weather tool.")],
        tools=[tool],
        model="gemini-2.0-flash",
        system="You must use available tools when asked.",
    ):
        events.append(event)
    tool_calls = [e for e in events if isinstance(e, ToolCall)]
    assert len(tool_calls) >= 1, "Expected at least one ToolCall event"
    assert tool_calls[0].name == "get_weather"
    assert "city" in tool_calls[0].args
```

---

## Final Verification

**Functionality:**
- [ ] `isinstance(GeminiProvider(), Provider)` is `True`
- [ ] `import monkeybot.providers.gemini` < 100ms (google-genai NOT at module level)
- [ ] `ProviderDone` is always the last event in the stream
- [ ] Real API response (when key available): `input_tokens > 0`
- [ ] Tool call test passes — `ToolCall.call_id` is non-empty ULID

**Code Quality:**
- [ ] `ruff check` and `mypy --strict` on `providers/gemini.py` pass
- [ ] SDK version and `function_call` attribute name confirmed in comment
- [ ] `_PRICING` dict covers the 4 common Gemini models
- [ ] `_safe_env()` not needed here (API key accessed directly, not passed to subprocess)

**Tests:**
- [ ] `test_isinstance_check` and `test_lazy_import` pass with no API key
- [ ] Real-API tests skipped cleanly when `GEMINI_API_KEY` not set
- [ ] `pytest tests/integration/test_gemini_provider.py -v` shows skip markers correctly
