"""Memory organizer — async post-processor for agent memory.

Reads raw observation files from ``raw/``, summarizes each,
classifies into a typed folder, and updates INDEX.md for deterministic retrieval.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from monkeybot.core.memory.storage_ops import INDEX_FILENAME
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.interfaces import MonkeybotError
from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)

BUILT_IN_FOLDERS: list[str] = ["episodic", "semantic", "procedural", "working"]

_SUMMARIZE_PROMPT = (
    "Summarize the following agent observation in 3-5 sentences. "
    "Be concrete and factual. Do not add opinions.\n\n"
    "<observation>\n{content}\n</observation>\n\nSummary:"
)

_CLASSIFY_PROMPT = (
    "Classify the following summary into exactly one memory folder. "
    "Reply with ONLY the folder name — nothing else.\n\n"
    "Folders:\n{folder_list}\n\nSummary:\n{summary}\n\nFolder:"
)

_INDEX_ENTRY_PROMPT = (
    "Generate an INDEX.md entry. Reply in EXACTLY this format:\n"
    "tags: <comma_separated>\nsummary: <one_sentence>\n\n"
    "File: {filename}\nContent: {summary}\n\nEntry:"
)


class _CustomMemoryFolderLike(Protocol):
    name: str
    description: str


class MemoryOrganizerError(MonkeybotError):
    """Raised when INDEX.md cannot be written after an organizer run."""


@dataclass
class MemoryOrganizerResult:
    files_processed: int
    files_written: int
    index_updated: bool
    errors: list[str] = field(default_factory=list)


@dataclass
class IndexEntry:
    folder: str    # destination folder name
    filename: str  # filename inside folder (not full path)
    tags: str      # "comma,separated"
    summary: str   # one sentence


async def _complete_text(
    provider: Provider,
    *,
    model: str,
    prompt: str,
) -> str:
    """Single-turn text completion (no tools) from streaming deltas."""
    parts: list[str] = []
    async with aclosing(
        cast(Any, provider.stream([Message(role="user", content=[Text(text=prompt)])], [], model=model))
    ) as stream:
        async for ev in stream:
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, Done):
                break
    return "".join(parts).strip()


class MemoryOrganizer:
    """Async post-processor that organises raw agent observations into typed memory."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        storage: WorkspaceStorage,
        custom_folders: Sequence[_CustomMemoryFolderLike] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._storage = storage
        self._all_folders: list[str] = BUILT_IN_FOLDERS + [
            cf.name for cf in (custom_folders or [])
        ]
        self._custom_folder_descriptions: dict[str, str] = {
            cf.name: cf.description for cf in (custom_folders or [])
        }

    async def run(self) -> MemoryOrganizerResult:
        """Process all unprocessed raw .md files directly under ``raw/``.

        Returns immediately with zeros if ``raw/`` is missing or empty.
        Per-file errors are captured in MemoryOrganizerResult.errors, not propagated.
        """
        all_under_raw = await self._storage.list_files("raw/")
        raw_files = [
            p
            for p in all_under_raw
            if p.startswith("raw/")
            and p.endswith(".md")
            and not p.startswith("raw/processed/")
            and p.count("/") == 1
        ]
        if not raw_files:
            return MemoryOrganizerResult(0, 0, False)

        entries: list[IndexEntry] = []
        files_written = 0
        errors: list[str] = []

        for raw_rel in sorted(raw_files):
            raw_name = Path(raw_rel).name
            try:
                content = await self._storage.read_text(raw_rel)
                summary = await self._summarize(content)
                folder = await self._classify(summary)
                filename = self._generate_filename(raw_name, folder)

                dest_path = f"{folder}/{filename}"
                await self._storage.write_text(dest_path, summary)

                entries.append(
                    IndexEntry(folder=folder, filename=filename, tags="", summary=summary)
                )

                processed_rel = f"raw/processed/{raw_name}"
                await self._storage.move(raw_rel, processed_rel)

                files_written += 1

            except Exception as e:
                logger.warning(
                    "Memory organizer failed to process %s: %s", raw_name, str(e)
                )
                errors.append(raw_name)

        if entries:
            await self._update_index(entries)

        return MemoryOrganizerResult(
            files_processed=len(raw_files),
            files_written=files_written,
            index_updated=bool(entries),
            errors=errors,
        )

    def _generate_filename(self, raw_name: str, folder: str) -> str:
        del folder
        stem = Path(raw_name).stem
        return f"{stem}.md"

    async def _summarize(self, raw_content: str) -> str:
        return await _complete_text(
            self._provider,
            model=self._model,
            prompt=_SUMMARIZE_PROMPT.format(content=raw_content),
        )

    async def _classify(self, summary: str) -> str:
        folder_list = "\n".join(
            f"- {f}: {self._custom_folder_descriptions.get(f, f)}"
            for f in self._all_folders
        )
        text = await _complete_text(
            self._provider,
            model=self._model,
            prompt=_CLASSIFY_PROMPT.format(folder_list=folder_list, summary=summary),
        )
        folder = text.strip().lower()
        if folder not in self._all_folders:
            logger.warning(
                "Classify returned unknown folder '%s', using 'episodic'", folder
            )
            return "episodic"
        return folder

    async def _update_index(self, entries: list[IndexEntry]) -> None:
        existing_content = ""
        if await self._storage.exists(INDEX_FILENAME):
            try:
                existing_content = await self._storage.read_text(INDEX_FILENAME)
            except Exception:
                existing_content = ""

        if not existing_content:
            existing_content = "# Memory Index\n\n"

        for entry in entries:
            try:
                response = await _complete_text(
                    self._provider,
                    model=self._model,
                    prompt=_INDEX_ENTRY_PROMPT.format(
                        filename=entry.filename, summary=entry.summary
                    ),
                )

                tags = ""
                summary_line = entry.summary
                for line in response.splitlines():
                    if line.lower().startswith("tags:"):
                        tags = line[5:].strip()
                    elif line.lower().startswith("summary:"):
                        summary_line = line[8:].strip()

                entry.tags = tags
                entry.summary = summary_line
            except Exception as exc:
                logger.debug("index entry LLM call failed for %s: %r", entry.filename, exc)

            formatted = (
                f"- [[{entry.folder}/{entry.filename}]]"
                f" | tags: {entry.tags} | {entry.summary}"
            )
            section_header = f"## {entry.folder}/"

            if section_header in existing_content:
                lines = existing_content.splitlines()
                insert_idx = len(lines)
                in_section = False
                for i, line in enumerate(lines):
                    if line.strip() == section_header:
                        in_section = True
                        continue
                    if in_section and line.startswith("## "):
                        insert_idx = i
                        break
                    if in_section:
                        insert_idx = i + 1
                lines.insert(insert_idx, formatted)
                existing_content = "\n".join(lines) + "\n"
            else:
                existing_content = (
                    existing_content.rstrip("\n")
                    + f"\n\n{section_header}\n{formatted}\n"
                )

        try:
            await self._storage.write_text(INDEX_FILENAME, existing_content)
        except Exception as e:
            raise MemoryOrganizerError(f"Failed to write INDEX.md: {e}") from e
