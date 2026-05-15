"""Vertex Gemini streaming via the official ``google-genai`` SDK (no LangChain in this module)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.content_blocks import Text, Thinking, ToolRequest, ToolResponse
from monkeybot.core.interfaces import LLMError
from monkeybot.core.provider import (
    Done,
    Message,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.types_tools import ToolDef

THOUGHT_SIGNATURE_KEY = "thoughtSignature"
SYNTHETIC_THOUGHT_SIGNATURE = "skip_thought_signature_validator"


def _normalize_vertex_model(model: str) -> str:
    """Return a ``model`` value accepted by ``google.genai`` for Vertex (see SDK docstring).

    Accepts bare ids (``gemini-2.5-flash``), ``models/...``, ``publishers/...``, ``google/...``,
    or a full ``projects/.../locations/.../publishers/google/models/...`` resource name.
    Strips a mistaken ``models/`` prefix when using the Vertex client (Vertex uses bare ids
    or publisher paths, not the AI Studio ``models/`` prefix).
    """
    m = (model or "").strip()
    if not m:
        raise LLMError("MODEL_NAME (model parameter) is empty.")
    if m.startswith("projects/"):
        return m
    if m.startswith("publishers/") or m.startswith("google/"):
        return m
    if m.startswith("models/"):
        return m[len("models/") :].strip()
    return m


def _location_from_full_vertex_model(model: str) -> str | None:
    """If ``model`` is a full Vertex resource, return the ``locations/{loc}`` segment."""
    if not model.startswith("projects/") or "/locations/" not in model:
        return None
    try:
        idx = model.index("/locations/") + len("/locations/")
        return model[idx:].split("/", 1)[0].strip() or None
    except (ValueError, IndexError):
        return None


def _vertex_project_and_location(model_param: str) -> tuple[str, str]:
    """Resolve project id and API location for ``genai.Client(vertexai=True, ...)``.

    Preview model ids (e.g. ``*-preview``) are typically **not** published under regional
    endpoints like ``us-central1``; Vertex serves them from ``global`` unless you override
    ``VERTEX_AI_LOCATION`` / ``GOOGLE_CLOUD_LOCATION``.
    """
    project = (
        os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("VERTEX_AI_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    if not project or not str(project).strip():
        raise LLMError(
            "Set VERTEX_AI_PROJECT_ID, GCP_PROJECT_ID, or GOOGLE_CLOUD_PROJECT for Vertex Gemini."
        )
    explicit = os.environ.get("VERTEX_AI_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION")
    if explicit and str(explicit).strip():
        return str(project).strip(), str(explicit).strip()

    embedded = _location_from_full_vertex_model(model_param)
    if embedded:
        return str(project).strip(), embedded

    tail = model_param.split("/")[-1]
    if "preview" in tail.lower():
        return str(project).strip(), "global"

    return str(project).strip(), "us-central1"


def _split_system_and_rest(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    systems: list[str] = []
    rest: list[Message] = []
    for m in messages:
        if m.role == "system":
            texts = [b.text for b in m.content if isinstance(b, Text)]
            systems.append("\n\n".join(texts))
        else:
            rest.append(m)
    joined = "\n\n".join(s for s in systems if s).strip()
    return joined, rest


def _flatten_tool_response_result(block: ToolResponse) -> str:
    parts: list[str] = []
    for b in block.result:
        if isinstance(b, Text):
            parts.append(b.text)
        else:
            raise LLMError(
                "Cannot replay tool result to Vertex: unsupported result block "
                f"{type(b).__name__}"
            )
    return "".join(parts)


def _is_user_loop_boundary(message: Message) -> bool:
    """A user turn that carries at least one non-ToolResponse block opens an active loop."""
    if message.role != "user":
        return False
    return any(not isinstance(b, ToolResponse) for b in message.content)


def _active_loop_start_index(messages: Sequence[Message]) -> int | None:
    """Return the index of the most recent user-loop boundary, or ``None``.

    Goose's contract: only messages from this index onward must replay
    ``thoughtSignature`` to satisfy Vertex Gemini's strict validation.
    """
    for i in range(len(messages) - 1, -1, -1):
        if _is_user_loop_boundary(messages[i]):
            return i
    return None


def _signature_from_metadata(metadata: dict[str, object] | None) -> str | None:
    if not metadata:
        return None
    sig = metadata.get(THOUGHT_SIGNATURE_KEY)
    return sig if isinstance(sig, str) and sig else None


def _normalize_signature(value: Any) -> str | None:
    """Coerce SDK ``thought_signature`` (bytes or str) to a non-empty Python string.

    The google-genai pydantic model normalizes wire signatures to ``bytes``
    using base64 decoding for string inputs. Round-tripping requires preserving
    the original textual representation; we standardise on base64 strings when
    we receive bytes that aren't valid UTF-8.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            decoded = value.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            decoded = base64.b64encode(value).decode("ascii")
        return decoded or None
    if isinstance(value, str):
        return value or None
    return None


def _messages_to_contents(rest: Sequence[Message]) -> list[Any]:
    """Build ``google.genai.types.Content`` list from harness messages (no system rows).

    Vertex Gemini 2.5+ enforces ``thoughtSignature`` round-trip on the active
    conversation loop. This builder mirrors Goose's contract:

    - Signatures are re-attached to ``functionCall`` and reasoning parts in the
      active loop (from the last user-loop boundary onward).
    - Earlier turns drop signatures (Vertex would reject them as stale).
    - If the active loop's first model tool call has no captured signature, a
      synthetic placeholder (``skip_thought_signature_validator``) is inserted
      so Vertex skips strict validation rather than 400-ing.
    - ``Thinking`` blocks are round-tripped as ``Part(text, thought=True,
      thought_signature=...)`` only inside the active loop.
    """
    from google.genai import types

    for m in rest:
        if m.role != "user":
            continue
        for block in m.content:
            if isinstance(block, ToolResponse) and not str(block.tool_name or "").strip():
                raise LLMError(
                    "Cannot replay tool result to Vertex: empty tool_name "
                    f"(tool_call_id={block.id!r})."
                )

    active_start = _active_loop_start_index(rest)

    contents: list[Any] = []
    for idx, m in enumerate(rest):
        gemini_role = "user" if m.role == "user" else "model"
        in_active_loop = active_start is not None and idx >= active_start
        needs_synthetic_for_first_model_tool_call = in_active_loop and m.role != "user"
        parts: list[Any] = []
        for block in m.content:
            if isinstance(block, Text):
                parts.append(types.Part(text=block.text))
            elif isinstance(block, Thinking):
                if not in_active_loop:
                    continue
                kwargs: dict[str, Any] = {"text": block.thinking, "thought": True}
                if block.signature:
                    kwargs["thought_signature"] = block.signature.encode("utf-8")
                parts.append(types.Part(**kwargs))
            elif isinstance(block, ToolRequest):
                fc_kwargs: dict[str, Any] = {
                    "name": block.name,
                    "args": dict(block.args),
                    "id": block.id,
                }
                part_kwargs: dict[str, Any] = {
                    "function_call": types.FunctionCall(**fc_kwargs),
                }
                if in_active_loop:
                    sig = _signature_from_metadata(block.metadata)
                    if sig is None and needs_synthetic_for_first_model_tool_call:
                        sig = SYNTHETIC_THOUGHT_SIGNATURE
                    if sig is not None:
                        # SDK validator base64-decodes string inputs; pass raw bytes
                        # so the literal signature survives the round-trip.
                        part_kwargs["thought_signature"] = sig.encode("utf-8")
                needs_synthetic_for_first_model_tool_call = False
                parts.append(types.Part(**part_kwargs))
            elif isinstance(block, ToolResponse):
                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(
                            name=block.tool_name,
                            response={"result": _flatten_tool_response_result(block)},
                        )
                    )
                )
            else:
                raise LLMError(
                    "Cannot replay message to Vertex Gemini: unsupported block "
                    f"{type(block).__name__}"
                )
        if not parts:
            parts.append(types.Part(text=""))
        contents.append(types.Content(role=gemini_role, parts=parts))
    return contents


def _tool_defs_to_declarations(tools: Sequence[ToolDef]) -> list[Any]:
    from google.genai import types

    out: list[Any] = []
    for t in tools:
        schema = dict(t.input_schema) if t.input_schema else {"type": "object"}
        out.append(
            types.FunctionDeclaration(
                name=t.name,
                description=t.description or "",
                parameters_json_schema=schema,
            )
        )
    return out


def _merge_function_call_args(existing: dict[str, Any], fc: Any) -> dict[str, Any]:
    merged = dict(existing)
    if isinstance(getattr(fc, "args", None), dict):
        merged.update(fc.args)
    partial = getattr(fc, "partial_args", None)
    if isinstance(partial, dict):
        merged.update(partial)
    elif isinstance(partial, str) and partial.strip():
        try:  # noqa: SIM105 — preserve streaming merge semantics (spec: verbatim)
            merged.update(json.loads(partial))
        except json.JSONDecodeError:
            pass
    return merged


def _usage_from_response(um: Any) -> UsageEvent | None:
    if um is None:
        return None
    inp = int(getattr(um, "prompt_token_count", 0) or 0)
    out = int(getattr(um, "candidates_token_count", 0) or 0)
    cached = int(getattr(um, "cached_content_token_count", 0) or 0)
    return UsageEvent(input_tokens=inp, output_tokens=out, cached_tokens=cached)


class GeminiProvider:
    """Vertex Gemini streaming using ``google.genai.Client`` (vertexai mode)."""

    def __init__(
        self,
        *,
        supports_streaming: bool = True,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        thinking_budget: int | None = None,
    ) -> None:
        self._supports_streaming = supports_streaming
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._thinking_budget = thinking_budget

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def supports_streaming(self) -> bool:
        return self._supports_streaming

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
    ) -> AsyncIterator[ProviderEvent]:
        model_param = _normalize_vertex_model(model)
        project, location = _vertex_project_and_location(model_param)
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise LLMError(
                "google-genai is required for GeminiProvider. Install with: uv sync (monkeybot dependencies)."
            ) from exc

        temperature = (
            float(self._temperature)
            if self._temperature is not None
            else float(os.environ.get("MODEL_TEMPERATURE", "0.7"))
        )
        max_tokens = (
            int(self._max_output_tokens)
            if self._max_output_tokens is not None
            else int(os.environ.get("MODEL_MAX_TOKENS", "60000"))
        )
        thinking_budget = (
            int(self._thinking_budget)
            if self._thinking_budget is not None
            else int(os.environ.get("MODEL_THINKING_BUDGET", "-1"))
        )

        system_instruction, rest = _split_system_and_rest(messages)
        contents = _messages_to_contents(rest)

        cfg_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        if thinking_budget != -1:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

        decls = _tool_defs_to_declarations(tools)
        if decls:
            cfg_kwargs["tools"] = [types.Tool(function_declarations=decls)]

        config = types.GenerateContentConfig(**cfg_kwargs)

        client = genai.Client(vertexai=True, project=project, location=location)

        pending_tools: dict[str, ToolCall] = {}
        last_usage: Any = None
        last_signature: str | None = None

        try:
            stream_it = await client.aio.models.generate_content_stream(
                model=model_param,
                contents=contents,
                config=config,
            )
            async for resp in stream_it:
                if getattr(resp, "usage_metadata", None) is not None:
                    last_usage = resp.usage_metadata

                for cand in resp.candidates or []:
                    content = getattr(cand, "content", None)
                    if content is None or not content.parts:
                        continue
                    for part in content.parts:
                        part_sig = _normalize_signature(getattr(part, "thought_signature", None))
                        if part_sig:
                            last_signature = part_sig

                        is_thought = bool(getattr(part, "thought", False))
                        if is_thought:
                            txt = getattr(part, "text", None) or ""
                            if txt:
                                yield ThinkingDelta(
                                    text=txt,
                                    signature=last_signature,
                                )
                            continue

                        if part.text:
                            yield TextDelta(text=part.text)

                        fc = part.function_call
                        if fc is None:
                            continue
                        name = str(getattr(fc, "name", "") or "")
                        if not name:
                            continue
                        fid = str(getattr(fc, "id", "") or "")
                        key = fid if fid else f"anon:{name}"
                        prev = pending_tools.get(key)
                        prev_args: dict[str, Any] = dict(prev.args) if prev else {}
                        merged_args = _merge_function_call_args(prev_args, fc)
                        call_id = fid if fid else key

                        # Per Goose: prefer the part's own signature, else carry forward
                        # from the most recent signed part in this stream.
                        effective_sig = part_sig or last_signature
                        prev_meta = dict(prev.metadata) if prev and prev.metadata else {}
                        if effective_sig:
                            prev_meta[THOUGHT_SIGNATURE_KEY] = effective_sig

                        pending_tools[key] = ToolCall(
                            call_id=call_id,
                            name=name,
                            args=merged_args,
                            metadata=prev_meta or None,
                        )
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc)) from exc

        ev = _usage_from_response(last_usage)
        if ev is not None:
            yield ev

        for call_id in sorted(pending_tools.keys()):
            yield pending_tools[call_id]

        yield Done()
