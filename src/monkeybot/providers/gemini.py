"""Vertex Gemini streaming via the official ``google-genai`` SDK (no LangChain in this module)."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any

from monkeybot.core.llm.provider import (
    Done,
    GroundingEvent,
    Message,
    ProviderEvent,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    UsageEvent,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.types.content_blocks import (
    File,
    Image,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)
from monkeybot.core.types.interfaces import LLMError
from monkeybot.core.types.types_tools import ToolDef
from monkeybot.providers.sampling import resolve_model_sampling

THOUGHT_SIGNATURE_KEY = "thoughtSignature"
SYNTHETIC_THOUGHT_SIGNATURE = "skip_thought_signature_validator"
# Prefix marks signatures stored as base64 by :func:`_normalize_signature` so
# replay can distinguish them from legacy plain UTF-8 strings that happen to
# be valid base64 alphabet (would otherwise silently mis-decode).
_SIGNATURE_B64_PREFIX = "b64:"

_log = logging.getLogger(__name__)

# Keep this in sync when onboarding another Vertex Gemini model that must use "global".
_GLOBAL_VERTEX_MODEL_IDS = frozenset({"gemini-3-flash-preview"})


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

    Some preview model ids are not published under regional
    endpoints like ``us-central1``; Vertex serves them from ``global`` unless you override
    ``VERTEX_AI_LOCATION`` / ``GOOGLE_CLOUD_LOCATION``.
    """
    from monkeybot.core.config.snapshot import current_env

    project = (
        os.environ.get("GCP_PROJECT_ID")
        or current_env("VERTEX_AI_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    if not project or not str(project).strip():
        raise LLMError(
            "Set VERTEX_AI_PROJECT_ID, GCP_PROJECT_ID, or GOOGLE_CLOUD_PROJECT for Vertex Gemini."
        )
    explicit = current_env("VERTEX_AI_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION")
    if explicit and str(explicit).strip():
        return str(project).strip(), str(explicit).strip()

    embedded = _location_from_full_vertex_model(model_param)
    if embedded:
        return str(project).strip(), embedded

    tail = model_param.split("/")[-1]
    if tail.lower() in _GLOBAL_VERTEX_MODEL_IDS:
        return str(project).strip(), "global"

    return str(project).strip(), "us-central1"


_THINKING_DISABLED = 0
"""Sentinel passed as ``thinking_budget`` to explicitly disable Gemini extended thinking.

Distinct from ``-1`` (the "omit ThinkingConfig" default) so auxiliary calls can
send ``ThinkingConfig(thinking_budget=0)`` to the API, which instructs the server
not to think rather than leaving it to the server's default for the model.
"""


def _suppress_thinking_for_auxiliary_call(
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
) -> bool:
    """True when this call is a known auxiliary job that must not use extended thinking.

    Context curation and history summarization use ``tools=()`` and a fixed 2-message
    shape. Extended thinking on preview models can stall the stream for 10s+ with no
    visible output, burning the curator timeout before ``Done`` is received.
    """
    if tools:
        return False
    if len(messages) != 2 or messages[0].role != "system":
        return False
    sys_txt = "".join(b.text for b in messages[0].content if isinstance(b, Text))
    markers = (
        "You narrow context for another assistant",
        "You compress prior agent conversation turns",
    )
    return any(m in sys_txt for m in markers)


def _resolve_thinking_budget(
    configured: int | None,
    *,
    override: int | None,
    messages: Sequence[Message],
    tools: Sequence[ToolDef],
) -> int:
    if override is not None:
        thinking_budget = int(override)
    elif configured is not None:
        thinking_budget = int(configured)
    else:
        from monkeybot.core.config.snapshot import current_env

        thinking_budget = int(current_env("MODEL_THINKING_BUDGET", "-1"))
    if _suppress_thinking_for_auxiliary_call(messages, tools):
        return _THINKING_DISABLED
    return thinking_budget


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
        elif isinstance(b, (Image, File)):
            continue
        else:
            raise LLMError(
                "Cannot replay tool result to Vertex: unsupported result block "
                f"{type(b).__name__}"
            )
    return "".join(parts)


def _media_parts_from_blocks(blocks: Sequence[object]) -> list[Any]:
    import base64

    from google.genai import types

    parts: list[Any] = []
    for b in blocks:
        if isinstance(b, (Image, File)):
            parts.append(
                types.Part(
                    inline_data=types.Blob(
                        mime_type=b.mime_type,
                        data=base64.b64decode(b.data),
                    )
                )
            )
    return parts


def _is_user_loop_boundary(message: Message) -> bool:
    """A pure user turn (no tool results) opens an active thought-signature loop.

    Gemini's turn boundary is the most recent user message that is *not* a
    ``functionResponse`` turn. Mid-conversation system-context updates are often
    folded into the trailing tool-result user message as extra ``Text`` blocks;
    those must not reset the loop or we strip ``thoughtSignature`` from earlier
    ``functionCall`` parts that Gemini still validates → 400 missing signature.
    """
    if message.role != "user":
        return False
    if any(isinstance(b, ToolResponse) for b in message.content):
        return False
    return bool(message.content)


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
    """Coerce SDK ``thought_signature`` (bytes or str) to a JSON-safe string.

    The google-genai SDK exposes signatures as ``bytes``. Real Gemini thought
    signatures are opaque binary, so we always store ``bytes`` as prefixed
    standard base64 (``b64:…``). Passing a base64 *string* into
    ``Part(thought_signature=...)`` makes the SDK base64-decode it; passing
    ``bytes`` uses them as-is — so replay must restore the original byte
    sequence (see :func:`_signature_wire_bytes`).
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        if not value:
            return None
        return _SIGNATURE_B64_PREFIX + base64.b64encode(value).decode("ascii")
    if isinstance(value, str):
        return value or None
    return None


def _signature_wire_bytes(sig: str) -> bytes:
    """Convert a stored signature string to bytes for ``types.Part``.

    - Synthetic skip tokens must be sent as literal UTF-8 bytes (the SDK
      base64-decodes *strings*, which would corrupt the sentinel).
    - Prefixed (``b64:``) values from :func:`_normalize_signature` decode as
      base64 payload.
    - Unprefixed values: try strict base64 (pre-prefix storage / older PR
      builds), else legacy/test plain UTF-8 text.
    """
    if sig == SYNTHETIC_THOUGHT_SIGNATURE:
        return sig.encode("utf-8")
    if sig.startswith(_SIGNATURE_B64_PREFIX):
        payload = sig[len(_SIGNATURE_B64_PREFIX) :]
        try:
            return base64.b64decode(payload, validate=True)
        except binascii.Error:
            _log.debug(
                "Gemini thought signature has b64: prefix but failed to decode; "
                "using UTF-8 bytes %s",
                kv(sig_len=len(sig)),
            )
            return sig.encode("utf-8")
    try:
        return base64.b64decode(sig, validate=True)
    except binascii.Error:
        _log.debug(
            "Gemini thought signature is not base64; using UTF-8 bytes (legacy/plain) %s",
            kv(sig_len=len(sig)),
        )
        return sig.encode("utf-8")


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
    # No clear user-text boundary (e.g. repaired tool-only history): treat the
    # whole transcript as the active loop so we still attach signatures / synthetic.
    if active_start is None and rest:
        _log.debug(
            "Gemini history has no user-loop boundary; treating full history as active loop %s",
            kv(n_messages=len(rest)),
        )
        active_start = 0

    contents: list[Any] = []
    for idx, m in enumerate(rest):
        gemini_role = "user" if m.role == "user" else "model"
        in_active_loop = active_start is not None and idx >= active_start
        needs_synthetic_for_first_model_tool_call = in_active_loop and m.role != "user"
        parts: list[Any] = []
        for block in m.content:
            if isinstance(block, Text):
                parts.append(types.Part(text=block.text))
            elif isinstance(block, (Image, File)):
                parts.extend(_media_parts_from_blocks([block]))
            elif isinstance(block, Thinking):
                if not in_active_loop:
                    continue
                kwargs: dict[str, Any] = {"text": block.thinking, "thought": True}
                if block.signature:
                    kwargs["thought_signature"] = _signature_wire_bytes(block.signature)
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
                        part_kwargs["thought_signature"] = _signature_wire_bytes(sig)
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
                parts.extend(_media_parts_from_blocks(block.result))
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
        model_schema = t.to_model_schema()
        params = (
            dict(model_schema["input_schema"])
            if model_schema["input_schema"]
            else {"type": "object"}
        )
        out.append(
            types.FunctionDeclaration(
                name=model_schema["name"],
                description=model_schema["description"] or "",
                parameters_json_schema=params,
            )
        )
    return out


# Matches context_budget / estimate_anthropic_input_tokens (~4 chars per token).
_CHARS_PER_TOKEN = 4


def _estimate_developer_api_tool_tokens(
    tools: Sequence[ToolDef],
    *,
    vertex_google_search: bool = False,
) -> int:
    """Local tool-token estimate for AI Studio CountTokensConfig.

    The Developer API rejects ``tools`` on ``CountTokensConfig``, but ``stream``
    still sends function declarations (and optional ``google_search``). Without a
    local add-on, prompt estimates undercount and delay context compaction.
    """
    payloads: list[dict[str, Any]] = [t.to_model_schema() for t in tools]
    if vertex_google_search:
        payloads.append({"google_search": {}})
    if not payloads:
        return 0
    text = json.dumps(payloads, ensure_ascii=False, default=str)
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _grounding_metadata_to_dict(gm: Any) -> dict[str, Any] | None:
    """Flatten Vertex ``GroundingMetadata`` into a small, wire-friendly dict.

    Only carries what a UI needs for citations/search-suggestion display: source
    chunks (title/uri) and web search queries. Returns ``None`` when there is
    nothing worth surfacing.
    """
    if gm is None:
        return None
    chunks: list[dict[str, str]] = []
    for chunk in getattr(gm, "grounding_chunks", None) or []:
        web = getattr(chunk, "web", None)
        if web is None:
            continue
        title = str(getattr(web, "title", "") or "")
        uri = str(getattr(web, "uri", "") or "")
        if title or uri:
            chunks.append({"title": title, "uri": uri})
    queries = [str(q) for q in (getattr(gm, "web_search_queries", None) or [])]
    if not chunks and not queries:
        return None
    out: dict[str, Any] = {}
    if chunks:
        out["sources"] = chunks
    if queries:
        out["search_queries"] = queries
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
    # Vertex Gemini implicit caching reports a single read count; there is no
    # separate cache-creation cost, so creation is 0 and the total equals the read.
    cache_read = int(getattr(um, "cached_content_token_count", 0) or 0)
    return UsageEvent(
        input_tokens=inp,
        output_tokens=out,
        cached_tokens=cache_read,
        cache_read_tokens=cache_read,
        cache_creation_tokens=0,
    )


class GeminiProvider:
    def __init__(
        self,
        *,
        supports_streaming: bool = True,
        temperature: float | None = None,
        max_tokens: int | None = None,
        max_output_tokens: int | None = None,
        thinking_budget: int | None = None,
        api_key: str | None = None,
    ) -> None:
        """Gemini streaming provider (Vertex AI or Google AI Studio).

        When ``api_key`` is provided, the provider connects to Google AI Studio
        (``generativelanguage.googleapis.com``). Otherwise it connects to Vertex AI
        using application default credentials or the project/location environment variables.

        ``max_output_tokens`` is a backward-compatible alias for ``max_tokens``.

        Native ``google_search`` grounding is opt-in per call via the
        ``vertex_google_search`` keyword on :meth:`stream` and
        :meth:`count_input_tokens` (Gemini-only; the runtime loop passes it only
        when ``provider.name == \"gemini\"``).
        """
        if max_output_tokens is not None and max_tokens is not None and max_output_tokens != max_tokens:
            raise ValueError("pass only one of max_tokens or max_output_tokens")
        effective_max_tokens = max_tokens if max_tokens is not None else max_output_tokens
        self._supports_streaming = supports_streaming
        sampling = resolve_model_sampling(temperature=temperature, max_tokens=effective_max_tokens)
        self._temperature = sampling.temperature
        self._max_tokens = sampling.max_tokens
        self._thinking_budget = thinking_budget
        self._api_key = api_key

    def _client(self, model_param: str) -> Any:
        """Create a google-genai client for Vertex AI or Google AI Studio."""
        from google import genai

        if self._api_key:
            return genai.Client(api_key=self._api_key)
        project, location = _vertex_project_and_location(model_param)
        return genai.Client(vertexai=True, project=project, location=location)

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def supports_streaming(self) -> bool:
        return self._supports_streaming

    async def count_input_tokens(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> int:
        model_param = _normalize_vertex_model(model)
        try:
            from google.genai import types
        except ImportError as exc:
            raise LLMError(
                "google-genai is required for GeminiProvider. Install with: uv sync (monkeybot dependencies)."
            ) from exc

        system_instruction, rest = _split_system_and_rest(messages)
        contents = _messages_to_contents(rest)
        # Developer API CountTokensConfig rejects system_instruction/tools/gen_cfg; fold system into contents.
        count_cfg_kwargs: dict[str, Any] = {}
        if self._api_key:
            if system_instruction:
                contents = [
                    types.Content(role="user", parts=[types.Part(text=system_instruction)]),
                    *contents,
                ]
        else:
            if system_instruction:
                count_cfg_kwargs["system_instruction"] = system_instruction
            decls = _tool_defs_to_declarations(tools)
            count_tools: list[Any] = []
            if decls:
                count_tools.append(types.Tool(function_declarations=decls))
            if vertex_google_search:
                count_tools.append(types.Tool(google_search=types.GoogleSearch()))
            if count_tools:
                count_cfg_kwargs["tools"] = count_tools
            budget = _resolve_thinking_budget(
                self._thinking_budget,
                override=thinking_budget,
                messages=messages,
                tools=tools,
            )
            gen_cfg_kwargs: dict[str, Any] = {
                "temperature": float(self._temperature),
                "max_output_tokens": int(self._max_tokens),
            }
            if budget != -1:
                gen_cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)
            count_cfg_kwargs["generation_config"] = types.GenerationConfig(**gen_cfg_kwargs)

        ct_cfg = types.CountTokensConfig(**count_cfg_kwargs) if count_cfg_kwargs else None
        client = self._client(model_param)
        try:
            resp = await client.aio.models.count_tokens(
                model=model_param,
                contents=contents,
                config=ct_cfg,
            )
        except LLMError:
            raise
        except Exception as exc:
            _log.warning(
                "Gemini count_tokens error %s",
                kv(provider="gemini", model=model, n_messages=len(messages), n_tools=len(tools)),
                exc_info=True,
            )
            raise LLMError(str(exc)) from exc
        total = int(resp.total_tokens or 0)
        if self._api_key:
            # Developer API cannot count tool schemas; add a local estimate so
            # context-pressure / summarization gates are not systematically low.
            total += _estimate_developer_api_tool_tokens(
                tools, vertex_google_search=vertex_google_search
            )
        return total

    async def stream(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolDef],
        *,
        model: str,
        thinking_budget: int | None = None,
        vertex_google_search: bool = False,
    ) -> AsyncIterator[ProviderEvent]:
        model_param = _normalize_vertex_model(model)
        try:
            from google.genai import types
        except ImportError as exc:
            raise LLMError(
                "google-genai is required for GeminiProvider. Install with: uv sync (monkeybot dependencies)."
            ) from exc

        temperature = float(self._temperature)
        max_tokens = int(self._max_tokens)
        thinking_budget = _resolve_thinking_budget(
            self._thinking_budget,
            override=thinking_budget,
            messages=messages,
            tools=tools,
        )

        system_instruction, rest = _split_system_and_rest(messages)
        contents = _messages_to_contents(rest)

        cfg_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_instruction:
            cfg_kwargs["system_instruction"] = system_instruction
        # -1 means "omit ThinkingConfig entirely" (model default).
        # 0 means explicitly disable thinking (sent to API).
        # Any other positive value sets a token budget.
        if thinking_budget != -1:
            cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=thinking_budget)

        decls = _tool_defs_to_declarations(tools)
        stream_tools: list[Any] = []
        if decls:
            stream_tools.append(types.Tool(function_declarations=decls))
        if vertex_google_search:
            stream_tools.append(types.Tool(google_search=types.GoogleSearch()))
        if stream_tools:
            cfg_kwargs["tools"] = stream_tools

        config = types.GenerateContentConfig(**cfg_kwargs)

        client = self._client(model_param)

        pending_tools: dict[str, ToolCall] = {}
        last_usage: Any = None
        last_signature: str | None = None
        grounding_meta: dict[str, Any] | None = None
        truncated = False

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
                    fr = getattr(cand, "finish_reason", None)
                    if fr is not None:
                        fr_name = str(getattr(fr, "name", fr)).upper()
                        if "MAX_TOKEN" in fr_name:
                            truncated = True
                    cand_gm = _grounding_metadata_to_dict(getattr(cand, "grounding_metadata", None))
                    if cand_gm is not None:
                        if grounding_meta is not None and cand_gm != grounding_meta:
                            _log.debug(
                                "grounding metadata replaced by later candidate %s",
                                kv(provider="gemini", model=model),
                            )
                        grounding_meta = cand_gm
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
            _log.warning(
                "Gemini stream error %s",
                kv(provider="gemini", model=model, n_messages=len(messages), n_tools=len(tools)),
                exc_info=True,
            )
            raise LLMError(str(exc)) from exc

        if grounding_meta is not None:
            yield GroundingEvent(
                sources=list(grounding_meta.get("sources", [])),
                search_queries=list(grounding_meta.get("search_queries", [])),
            )

        ev = _usage_from_response(last_usage)
        if ev is not None:
            yield ev

        # Insertion order = provider stream arrival order. Do not sort by
        # call_id: Gemini thought_signature must stay on the first functionCall.
        for tool_call in pending_tools.values():
            yield tool_call

        yield Done(truncated=truncated)
