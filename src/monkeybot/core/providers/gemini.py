"""Vertex Gemini streaming via the official ``google-genai`` SDK (no LangChain in this module)."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.interfaces import LLMError
from monkeybot.core.provider import Done, Message, ProviderEvent, TextDelta, ToolCall, UsageEvent
from monkeybot.core.types_tools import ToolDef


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


def _split_assistant_placeholder(content: str) -> tuple[str, list[dict[str, Any]]]:
    """Undo :func:`monkeybot.core.loop._assistant_tool_placeholder` for API replay."""
    last_nl = content.rfind("\n")
    if last_nl == -1:
        return content, []
    tail = content[last_nl + 1 :].strip()
    try:
        obj = json.loads(tail)
    except json.JSONDecodeError:
        return content, []
    tc = obj.get("tool_calls")
    if not isinstance(tc, list):
        return content, []
    head = content[:last_nl]
    return head, [x for x in tc if isinstance(x, dict)]


def _split_system_and_rest(messages: Sequence[Message]) -> tuple[str, list[Message]]:
    systems: list[str] = []
    rest: list[Message] = []
    for m in messages:
        if m.role == "system":
            systems.append(m.content)
        else:
            rest.append(m)
    joined = "\n\n".join(systems).strip()
    return joined, rest


def _enrich_tool_messages(rest: Sequence[Message]) -> list[Message]:
    """Fill ``Message.tool_name`` on tool rows when history omitted it (Vertex requires ``name``)."""
    from dataclasses import replace

    last_assistant_tool_names: dict[str, str] = {}
    out: list[Message] = []
    for m in rest:
        if m.role == "assistant":
            last_assistant_tool_names.clear()
            _, tcs = _split_assistant_placeholder(m.content)
            for tc in tcs:
                cid = str(tc.get("call_id") or tc.get("id") or "")
                name = str(tc.get("name") or "")
                if cid and name:
                    last_assistant_tool_names[cid] = name
            out.append(m)
            continue
        if m.role == "tool":
            tn = (m.tool_name or "").strip()
            if not tn and m.tool_call_id:
                tn = last_assistant_tool_names.get(m.tool_call_id, "")
            if not tn and m.tool_call_id:
                for j in range(len(out) - 1, -1, -1):
                    prev = out[j]
                    if prev.role != "assistant":
                        continue
                    _, tcs2 = _split_assistant_placeholder(prev.content)
                    for tc in tcs2:
                        cid = str(tc.get("call_id") or tc.get("id") or "")
                        if cid == m.tool_call_id:
                            tn = str(tc.get("name") or "")
                            break
                    if tn:
                        break
            out.append(replace(m, tool_name=tn) if tn else m)
            continue
        out.append(m)
    return out


def _messages_to_contents(rest: Sequence[Message]) -> list[Any]:
    """Build ``google.genai.types.Content`` list from harness messages (no system rows)."""
    from google.genai import types

    contents: list[Any] = []
    for m in rest:
        if m.role == "user":
            contents.append(
                types.Content(role="user", parts=[types.Part(text=m.content)])
            )
        elif m.role == "assistant":
            head, tool_calls_raw = _split_assistant_placeholder(m.content)
            parts: list[Any] = []
            if head:
                parts.append(types.Part(text=head))
            for tc in tool_calls_raw:
                cid = str(tc.get("call_id") or tc.get("id") or "")
                name = str(tc.get("name") or "")
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                if name:
                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                id=cid or None,
                                name=name,
                                args=dict(args),
                            )
                        )
                    )
            if not parts:
                parts.append(types.Part(text=""))
            contents.append(types.Content(role="model", parts=parts))
        elif m.role == "tool":
            tcid = m.tool_call_id or ""
            name = (m.tool_name or "").strip()
            if not name:
                raise LLMError(
                    "Cannot replay tool result to Vertex: tool message is missing tool_name "
                    f"(tool_call_id={tcid!r}). Start a new chat session, or upgrade the gateway "
                    "so tool rows persist tool_name."
                )
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=tcid or None,
                                name=name,
                                response={"result": m.content},
                            )
                        )
                    ],
                )
            )
        else:
            raise LLMError(f"Unsupported message role for Gemini provider: {m.role!r}")
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
        try:
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
        rest = _enrich_tool_messages(rest)
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
                        if getattr(part, "thought", None):
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
                        pending_tools[key] = ToolCall(
                            call_id=call_id,
                            name=name,
                            args=merged_args,
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
