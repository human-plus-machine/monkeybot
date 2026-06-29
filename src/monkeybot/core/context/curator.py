"""Optional secondary LLM pass to pick memory lines and skills for the system prompt."""

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

from monkeybot.core.context import SkillRef, TurnContext
from monkeybot.core.env_utils import env_float, env_int
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta, ToolCall, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.types.content_blocks import Text

_log = logging.getLogger(__name__)


def curation_enabled_from_env() -> bool:
    v = os.getenv("CONTEXT_CURATION_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def curation_threshold_met(ctx: TurnContext) -> bool:
    """Option C: run curator only when the catalog is large enough to benefit."""
    skill_n = env_int("CONTEXT_CURATION_SKILL_THRESHOLD", 4)
    mem_n = env_int("CONTEXT_CURATION_MEMORY_THRESHOLD", 8)
    return len(ctx.skills) > skill_n or len(ctx.memory_index) > mem_n


def curator_model_id(ctx: TurnContext) -> str:
    return os.getenv("CONTEXT_CURATOR_MODEL", "").strip() or ctx.model


@dataclass(frozen=True)
class CuratedPromptParts:
    """Subset of memory lines and skills chosen for this user message (frozen for follow-up turns)."""

    memory_lines: list[str]
    skills: list[SkillRef]
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


def _validate_memory_lines(selected: list[object], allowed: set[str], cap: int) -> list[str]:
    out: list[str] = []
    for item in selected:
        if not isinstance(item, str):
            continue
        line = item.strip()
        if line in allowed and line not in out:
            out.append(line)
        if len(out) >= cap:
            break
    return out


def _validate_skill_names(
    names: list[object],
    by_name: dict[str, SkillRef],
    cap: int,
) -> list[SkillRef]:
    out: list[SkillRef] = []
    for item in names:
        if not isinstance(item, str):
            continue
        key = item.strip()
        ref = by_name.get(key)
        if ref is not None and ref not in out:
            out.append(ref)
        if len(out) >= cap:
            break
    return out


async def run_context_curator(
    *,
    ctx: TurnContext,
    provider: Provider,
    curator_model: str,
    user_message: str,
    curator_provider: Provider | None = None,
) -> CuratedPromptParts:
    """Call a small JSON-only completion to pick verbatim memory lines and skill names.

    ``curator_provider`` is an optional dedicated provider instance tuned for this
    auxiliary call (e.g. ``thinking_budget=0, max_output_tokens=1024``). Falls back
    to ``provider`` when not supplied.
    """
    _provider = curator_provider if curator_provider is not None else provider
    max_mem = max(1, env_int("CONTEXT_CURATION_MAX_MEMORY_LINES", 12))
    max_sk = max(1, env_int("CONTEXT_CURATION_MAX_SKILLS", 5))
    search_hits = max(1, env_int("CONTEXT_CURATION_SEARCH_MAX_HITS", 8))
    timeout_sec = max(1.0, env_float("CONTEXT_CURATION_TIMEOUT_SEC", 10.0))

    index_lines = list(ctx.memory_index)
    search_lines: list[str] = []
    memory_scan_sec = 0.0
    if ctx.memory is not None:
        t_scan = time.monotonic()
        search_lines = await _gather_search_pool_lines(ctx.memory, user_message, max_hits=search_hits)
        memory_scan_sec = time.monotonic() - t_scan

    allowed_memory: set[str] = set(index_lines) | set(search_lines)
    by_skill = {s.name: s for s in ctx.skills}

    catalog_user = []
    catalog_user.append("## MEMORY_POOL (each line is selectable verbatim)")
    for i, ln in enumerate(index_lines, 1):
        catalog_user.append(f"{i}. {ln}")
    if search_lines:
        catalog_user.append("\n## SEARCH_HIT_LINES (selectable verbatim)")
        for i, ln in enumerate(search_lines, start=len(index_lines) + 1):
            catalog_user.append(f"{i}. {ln}")
    catalog_user.append("\n## SKILL_NAMES (pick from this set only)")
    catalog_user.append(", ".join(sorted(by_skill.keys())) or "(none)")
    catalog_user.append("\n## USER_MESSAGE")
    catalog_user.append(user_message.strip() or "(empty)")

    system = Message(
        role="system",
        content=[
            Text(
                text=(
                    "You narrow context for another assistant. Reply with ONLY a JSON object, no markdown fences. "
                    f'Schema: {{"memory_lines": string[], "highlighted_skills": string[]}}. '
                    f"At most {max_mem} memory strings and {max_sk} skill names. "
                    "Every element of memory_lines MUST be copied EXACTLY from MEMORY_POOL or SEARCH_HIT_LINES "
                    "(same characters). Every element of highlighted_skills MUST exactly match a name from SKILL_NAMES. "
                    "If nothing helps, return empty arrays."
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
                    _log.warning("[curation] unexpected tool call from curator model; aborting curation")
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
            "[curation] provider stream exceeded %ss without Done (curator_model=%r "
            "index_lines=%d search_pool_lines=%d skills=%d catalog_user_chars=%d "
            "memory_tree_scan=%.2fs; timeout applies only to the LLM stream, not local prep)",
            timeout_sec,
            curator_model,
            len(index_lines),
            len(search_lines),
            len(by_skill),
            catalog_chars,
            memory_scan_sec,
        )
        return CuratedPromptParts([], [], success=False)
    except Exception as exc:
        _log.warning("[curation] provider error: %s", exc)
        return CuratedPromptParts([], [], success=False)

    parsed = _parse_json_object(raw)
    if parsed is None:
        _log.warning("[curation] invalid JSON from model")
        return CuratedPromptParts([], [], success=False)

    raw_mem = parsed.get("memory_lines", [])
    raw_sk = parsed.get("highlighted_skills", [])
    if not isinstance(raw_mem, list):
        raw_mem = []
    if not isinstance(raw_sk, list):
        raw_sk = []

    mem_out = _validate_memory_lines(raw_mem, allowed_memory, max_mem)
    sk_out = _validate_skill_names(raw_sk, by_skill, max_sk)

    proposed = bool(raw_mem or raw_sk)
    if proposed and not mem_out and not sk_out:
        _log.warning("[curation] model proposed memory/skills but none matched the allowed pool")
        return CuratedPromptParts([], [], success=False)

    skill_names = [sk.name for sk in sk_out]
    _log.info(
        "[curation] selected %d/%d memory lines, %d/%d skills (model=%s) | mem=%s skills=%s",
        len(mem_out), len(index_lines) + len(search_lines),
        len(sk_out), len(by_skill),
        curator_model,
        [ln.split("|")[-1].strip()[:60] if "|" in ln else ln[:60] for ln in mem_out],
        skill_names,
    )
    if mem_out:
        for line in mem_out:
            _log.debug("[curation] memory → %s", line)
    if sk_out:
        for sk in sk_out:
            _log.debug("[curation] skill  → %s", sk.name)

    return CuratedPromptParts(mem_out, sk_out, success=True)
