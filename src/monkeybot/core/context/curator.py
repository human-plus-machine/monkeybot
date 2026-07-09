"""Optional secondary LLM pass to pick memory lines for the system prompt."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any, cast

from monkeybot.core.types.content_blocks import Text
from monkeybot.core.context import TurnContext
from monkeybot.core.logging_utils import kv
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta, ToolCall, UsageEvent
from monkeybot.core.runtime.context_budget import estimate_tokens

_log = logging.getLogger(__name__)


def curation_enabled_from_env() -> bool:
    v = os.getenv("CONTEXT_CURATION_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


def memory_index_token_estimate(lines: list[str]) -> int:
    """Cheap local estimate of memory-index prompt size (no provider call)."""
    if not lines:
        return 0
    return estimate_tokens("\n".join(lines))


# Cap on search hits added to the curator MEMORY_POOL (not user-configurable).
_CURATOR_SEARCH_MAX_HITS = 8


def curator_model_id(ctx: TurnContext) -> str:
    return os.getenv("CONTEXT_CURATOR_MODEL", "").strip() or ctx.model


@dataclass(frozen=True)
class CuratedPromptParts:
    """Subset of memory lines chosen for this user message (frozen for follow-up turns)."""

    memory_lines: list[str]
    success: bool
    """False on timeout, provider error, invalid JSON, or invalid selections when the model proposed content."""


def _parse_json_object(text: str) -> dict[str, object] | None:
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```\s*$", "", raw).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


async def _gather_search_pool_lines(
    memory: MemorySubsystem | None, user_text: str, *, max_hits: int
) -> list[str]:
    if memory is None or not user_text.strip():
        return []
    q = user_text.strip()[:400]
    payload = await memory.search_files(q, max_hits=max_hits, skip_raw=True)
    hits = payload.get("hits") or []
    lines: list[str] = []
    if not isinstance(hits, list):
        return []
    for h in hits:
        if not isinstance(h, dict):
            continue
        path = str(h.get("path", "")).strip()
        snip = str(h.get("snippet", "")).strip()
        if path and snip:
            lines.append(f"{path} — {snip}")
    return lines


def _coerce_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _lines_from_indices(indices: list[object], pool: list[str], cap: int) -> list[str]:
    out: list[str] = []
    for item in indices:
        idx = _coerce_index(item)
        if idx is None or idx < 1 or idx > len(pool):
            continue
        line = pool[idx - 1]
        if line not in out:
            out.append(line)
        if len(out) >= cap:
            break
    return out


async def run_context_curator(
    *,
    ctx: TurnContext,
    provider: Provider,
    curator_model: str,
    user_message: str,
    max_memory_lines: int,
    curator_provider: Provider | None = None,
) -> CuratedPromptParts:
    """Call a small JSON-only completion to pick numbered memory pool lines.

    ``max_memory_lines`` is the caller's window size (no env re-read here).
    ``curator_provider`` is an optional dedicated provider instance tuned for this
    auxiliary call (e.g. ``thinking_budget=0, max_output_tokens=1024``). Falls back
    to ``provider`` when not supplied.
    """
    _provider = curator_provider if curator_provider is not None else provider
    max_mem = max(1, max_memory_lines)
    search_hits = _CURATOR_SEARCH_MAX_HITS
    timeout_sec = max(1.0, _env_float("CONTEXT_CURATION_TIMEOUT_SEC", 10.0))

    index_lines = list(ctx.memory_index)
    search_lines: list[str] = []
    memory_scan_sec = 0.0
    if ctx.memory is not None:
        t_scan = time.monotonic()
        search_lines = await _gather_search_pool_lines(ctx.memory, user_message, max_hits=search_hits)
        memory_scan_sec = time.monotonic() - t_scan

    pool = index_lines + search_lines

    catalog_user = []
    catalog_user.append("## MEMORY_POOL (1-based line numbers)")
    for i, ln in enumerate(pool, 1):
        catalog_user.append(f"{i}. {ln}")
    catalog_user.append("\n## USER_MESSAGE")
    catalog_user.append(user_message.strip() or "(empty)")

    system = Message(
        role="system",
        content=[
            Text(
                text=(
                    "You narrow context for another assistant. Reply with ONLY a JSON object, no markdown fences. "
                    f'Schema: {{"memory_line_indices": number[]}}. '
                    f"At most {max_mem} indices. "
                    "Each index must be a 1-based line number from MEMORY_POOL. "
                    "If nothing helps, return an empty array."
                )
            )
        ],
    )
    catalog_text = "\n".join(catalog_user)
    catalog_chars = len(catalog_text)
    user = Message(role="user", content=[Text(text=catalog_text)])

    async def _stream_once() -> str:
        buf: list[str] = []
        async with aclosing(
            cast(Any, _provider.stream([system, user], [], model=curator_model))
        ) as stream:
            async for ev in stream:
                if isinstance(ev, TextDelta):
                    buf.append(ev.text)
                elif isinstance(ev, ToolCall):
                    _log.warning(
                        "curation unexpected tool call %s",
                        kv(curator_model=curator_model),
                    )
                    return ""
                elif isinstance(ev, UsageEvent):
                    pass
                elif isinstance(ev, Done):
                    break
        return "".join(buf)

    try:
        raw = await asyncio.wait_for(_stream_once(), timeout=timeout_sec)
    except TimeoutError:
        _log.warning(
            "curation timeout %s",
            kv(
                timeout_sec=timeout_sec,
                curator_model=curator_model,
                index_lines=len(index_lines),
                search_pool_lines=len(search_lines),
                catalog_chars=catalog_chars,
                memory_scan_sec=f"{memory_scan_sec:.2f}",
            ),
        )
        return CuratedPromptParts([], success=False)
    except Exception as exc:
        _log.warning("curation provider error %s", kv(error=exc))
        return CuratedPromptParts([], success=False)

    parsed = _parse_json_object(raw)
    if parsed is None:
        _log.warning("curation invalid JSON %s", kv(curator_model=curator_model))
        return CuratedPromptParts([], success=False)

    raw_indices = parsed.get("memory_line_indices", [])
    if not isinstance(raw_indices, list):
        raw_indices = []

    mem_out = _lines_from_indices(raw_indices, pool, max_mem)

    proposed = bool(raw_indices)
    if proposed and not mem_out:
        _log.warning(
            "curation indices unmatched %s",
            kv(curator_model=curator_model, proposed=len(raw_indices), pool=len(pool)),
        )
        return CuratedPromptParts([], success=False)

    _log.info(
        "curation selected %s",
        kv(
            selected=len(mem_out),
            pool=len(pool),
            curator_model=curator_model,
        ),
    )
    return CuratedPromptParts(mem_out, success=True)
