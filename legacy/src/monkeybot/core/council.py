from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from monkeybot.core.memory import save_memory

if TYPE_CHECKING:
    from monkeybot.core.provider import Provider

log = logging.getLogger("monkeybot.council")

MANAGED_CATEGORIES: tuple[str, ...] = ("user-preferences", "key-facts", "open-questions")

COUNCIL_PROMPT = """\
You are the LLM Memory Council. Your job is to maintain the agent's long-term memory.

## Existing Memory
{existing_memories_block}

## Session Conversation
{conversation_text}

## Instructions
Produce updated memory by merging existing memory with new insights from the session above.

Rules:
- PRESERVE all existing facts unless this session contradicts or supersedes them.
- ADD new facts, preferences, and insights from this session.
- NEVER duplicate a fact — if it already exists, do not repeat it.
- CONSOLIDATE redundant or overlapping entries into a single concise bullet.
- REMOVE entries that were explicitly resolved or corrected in this session.
- Keep each section concise — bullet points only, no prose.
- If a category has nothing new and nothing to change, reproduce its existing content unchanged.
- Output ONLY the sections below — no preamble, no commentary, no extra text.

## Summary
<2-4 sentences summarizing ONLY this session — what happened, what was requested, what was decided>

## user-preferences
<complete merged bullet list — user style, habits, communication preferences>

## key-facts
<complete merged bullet list — facts, decisions, outcomes, domain knowledge>

## open-questions
<complete merged bullet list — unresolved questions and follow-up items;
omit bullets that are now resolved>
"""


def _load_existing_categories(memory_path: str) -> dict[str, str]:
    """Load existing category memory files from *memory_path*.

    Returns a dict mapping category slug → content string.
    Missing or unreadable files map to "".
    """
    result: dict[str, str] = {cat: "" for cat in MANAGED_CATEGORIES}
    p = Path(memory_path)
    if not p.exists():
        return result
    for cat in MANAGED_CATEGORIES:
        f = p / f"{cat}.md"
        if f.exists():
            try:
                result[cat] = f.read_text()
            except OSError:
                pass
    return result


def _parse_council_sections(response: str) -> dict[str, str]:
    """Parse the council LLM response into a dict of section slug → content.

    Splits on "\\n## " so each section header becomes a key.
    The preamble before the first ## header is discarded.
    """
    text = response.strip()
    # Prepend newline so we can always split on "\n## "
    if not text.startswith("\n"):
        text = "\n" + text
    parts = text.split("\n## ")
    result: dict[str, str] = {}
    for part in parts[1:]:  # skip preamble before first ##
        lines = part.split("\n", 1)
        header = lines[0].strip()
        content = lines[1].strip() if len(lines) > 1 else ""
        slug = header.lower().replace(" ", "-")
        result[slug] = content
    return result


async def run_council(
    conversation_text: str,
    memory_path: str,
    provider: Provider,
    model: str,
    session_id: str,
) -> list[str]:
    """Run the LLM Council to consolidate session memory.

    Loads existing category files, prompts the provider with the full
    conversation, parses the response, and writes updated memory files.

    Args:
        conversation_text: Full session transcript as a plain string.
        memory_path: Directory path where memory .md files are stored.
        provider: Provider instance used for streaming.
        model: Model identifier string to pass to the provider.
        session_id: Unique session identifier (used for the summary filename).

    Returns:
        List of filenames written (e.g. ["2026-05-13-session-abc12345.md", ...]).
        Returns [] if conversation_text is blank or on any error.
    """
    if not conversation_text.strip():
        return []

    try:
        existing = _load_existing_categories(memory_path)

        existing_block_parts: list[str] = []
        for cat in MANAGED_CATEGORIES:
            content = existing.get(cat, "") or "(no existing memories)"
            existing_block_parts.append(f"### {cat}\n{content}")
        existing_memories_block = "\n\n".join(existing_block_parts)

        prompt = COUNCIL_PROMPT.format(
            existing_memories_block=existing_memories_block,
            conversation_text=conversation_text,
        )

        from monkeybot.core.provider import Message  # noqa: PLC0415

        chunks: list[str] = []
        async for chunk in provider.stream(  # type: ignore[attr-defined]
            [Message(role="user", content=prompt)],
            [],
            model=model,
            system="",
        ):
            if hasattr(chunk, "text") and chunk.text:
                chunks.append(chunk.text)
        response = "".join(chunks)

        sections = _parse_council_sections(response)
        written: list[str] = []

        # Write session summary file
        session_filename = f"{date.today()}-session-{session_id[:8]}"
        summary = sections.get("summary", "")
        save_memory(memory_path, session_filename, summary)
        written.append(f"{session_filename}.md")

        # Write category files (only if section present and non-empty)
        for cat in MANAGED_CATEGORIES:
            content = sections.get(cat, "")
            if content:
                save_memory(memory_path, cat, content)
                written.append(f"{cat}.md")

        return written
    except Exception:
        log.error("council error for session %s", session_id, exc_info=True)
        return []
