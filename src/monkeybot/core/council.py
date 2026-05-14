"""LLM Council — async post-processor for agent memory.

When run, the council reads raw observation files from ``{memory_dir}/raw/``,
compresses each into a summary, classifies into a typed folder, and updates
INDEX.md for deterministic retrieval.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.language_models import BaseChatModel

from .interfaces import MonkeybotError

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


class CouncilError(MonkeybotError):
    """Raised when INDEX.md cannot be written after a council run."""


@dataclass
class CouncilResult:
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


class LLMCouncil:
    """Async post-processor that organises raw agent observations into typed memory."""

    def __init__(
        self,
        model: BaseChatModel,
        memory_dir: Path,
        custom_folders: list | None = None,
        index_path: Path | None = None,
    ) -> None:
        self.model = model
        self.memory_dir = Path(memory_dir)
        self.raw_dir = self.memory_dir / "raw"
        self.processed_dir = self.raw_dir / "processed"
        self.index_path = index_path or (self.memory_dir / "INDEX.md")
        self._all_folders: list[str] = BUILT_IN_FOLDERS + [
            cf.name for cf in (custom_folders or [])
        ]
        self._custom_folder_descriptions: dict[str, str] = {
            cf.name: cf.description for cf in (custom_folders or [])
        }

    async def run(self) -> CouncilResult:
        """Process all unprocessed raw .md files in raw_dir.

        Returns immediately with zeros if raw_dir is missing or empty.
        Per-file errors are captured in CouncilResult.errors, not propagated.
        """
        if not self.raw_dir.exists():
            return CouncilResult(0, 0, False)

        raw_files = [
            f for f in self.raw_dir.glob("*.md")
            if f.parent == self.raw_dir  # exclude processed/ subdir
        ]
        if not raw_files:
            return CouncilResult(0, 0, False)

        loop = asyncio.get_event_loop()
        self.processed_dir.mkdir(parents=True, exist_ok=True)

        entries: list[IndexEntry] = []
        files_written = 0
        errors: list[str] = []

        for raw_file in raw_files:
            try:
                content = await loop.run_in_executor(None, raw_file.read_text)
                summary = await self._summarize(content)
                folder = await self._classify(summary)
                filename = self._generate_filename(raw_file.name, folder)

                dest_dir = self.memory_dir / folder
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_path = dest_dir / filename
                await loop.run_in_executor(None, dest_path.write_text, summary)

                entries.append(
                    IndexEntry(folder=folder, filename=filename, tags="", summary=summary)
                )

                processed_path = self.processed_dir / raw_file.name
                await loop.run_in_executor(None, raw_file.rename, processed_path)

                files_written += 1

            except Exception as e:
                logger.warning(
                    "Council failed to process %s: %s", raw_file.name, str(e)
                )
                errors.append(raw_file.name)

        if entries:
            await self._update_index(entries)

        return CouncilResult(
            files_processed=len(raw_files),
            files_written=files_written,
            index_updated=bool(entries),
            errors=errors,
        )

    def _generate_filename(self, raw_name: str, folder: str) -> str:
        """Derive typed memory filename from raw filename."""
        stem = Path(raw_name).stem
        return f"{stem}.md"

    async def _summarize(self, raw_content: str) -> str:
        """Summarize raw observation via LLM.

        Raises:
            Exception: Propagates LLM errors so the per-file handler in run()
                can capture them as processing errors.
        """
        result = await self.model.ainvoke(
            _SUMMARIZE_PROMPT.format(content=raw_content)
        )
        return result.content

    async def _classify(self, summary: str) -> str:
        """Classify summary into a folder name. Falls back to 'episodic' on unknown folder."""
        folder_list = "\n".join(
            f"- {f}: {self._custom_folder_descriptions.get(f, f)}"
            for f in self._all_folders
        )
        result = await self.model.ainvoke(
            _CLASSIFY_PROMPT.format(folder_list=folder_list, summary=summary)
        )
        folder = result.content.strip().lower()
        if folder not in self._all_folders:
            logger.warning(
                "Classify returned unknown folder '%s', using 'episodic'", folder
            )
            return "episodic"
        return folder

    async def _update_index(self, entries: list[IndexEntry]) -> None:
        """Merge entries into INDEX.md, creating or appending sections as needed.

        Raises:
            CouncilError: If INDEX.md cannot be written.
        """
        loop = asyncio.get_event_loop()

        existing_content = ""
        if self.index_path.exists():
            try:
                existing_content = await loop.run_in_executor(
                    None, self.index_path.read_text
                )
            except Exception:
                existing_content = ""

        if not existing_content:
            existing_content = "# Memory Index\n\n"

        for entry in entries:
            try:
                result = await self.model.ainvoke(
                    _INDEX_ENTRY_PROMPT.format(
                        filename=entry.filename, summary=entry.summary
                    )
                )
                response = result.content

                tags = ""
                summary_line = entry.summary
                for line in response.splitlines():
                    if line.lower().startswith("tags:"):
                        tags = line[5:].strip()
                    elif line.lower().startswith("summary:"):
                        summary_line = line[8:].strip()

                entry.tags = tags
                entry.summary = summary_line
            except Exception:
                pass  # use defaults already set

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
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            await loop.run_in_executor(None, self.index_path.write_text, existing_content)
        except Exception as e:
            raise CouncilError(f"Failed to write INDEX.md: {e}") from e
