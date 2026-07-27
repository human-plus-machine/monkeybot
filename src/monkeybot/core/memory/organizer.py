"""Memory organizer — async post-processor for agent memory.

Reads raw observation files from ``raw/``, summarizes each,
classifies into a typed folder, optionally links related notes, and updates
INDEX.md for deterministic retrieval.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

from monkeybot.core.llm.provider import Done, Message, Provider, TextDelta
from monkeybot.core.memory.index_format import (
    DEFAULT_INDEX_HEADER,
    INDEX_ARCHIVE_FILENAME,
    INDEX_FILENAME,
    append_index_entries,
    apply_index_entry_cap,
    format_index_document,
    index_cap_from_env,
    is_index_entry_line,
    merge_archive_content,
    split_index_document,
)
from monkeybot.core.memory.note_format import format_memory_note
from monkeybot.core.memory.repair import repair_memory_tree
from monkeybot.core.memory.storage_ops import MEMORY_SEARCH_STOPWORDS, async_load_index
from monkeybot.core.types.content_blocks import Text
from monkeybot.core.types.interfaces import MonkeybotError
from monkeybot.core.workspace.protocol import WorkspaceStorage

logger = logging.getLogger(__name__)

BUILT_IN_FOLDERS: list[str] = ["episodic", "semantic", "procedural", "working"]

_BUILT_IN_FOLDER_DESCRIPTIONS: dict[str, str] = {
    "episodic": "events and what happened in a session (tool outcomes, failures, milestones)",
    "semantic": "durable facts worth recalling later (preferences, configs, conclusions)",
    "procedural": "how-to recipes and repeatable procedures",
    "working": "short-lived scratch that will expire; prefer for ephemeral tool noise",
}

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

_LINK_PROMPT = (
    "You are linking a new memory note to existing notes (Obsidian-style).\n"
    "Link only when there is a strong relationship: same entity/decision, a continued "
    "thread, a how-to that depends on a fact, or this note replaces a prior fact.\n"
    "Prefer zero links over weak topical overlap. Prefer supersedes over a parallel "
    "conflicting durable fact.\n\n"
    "New note folder: {folder}\n"
    "New note summary:\n{summary}\n\n"
    "Candidates (path — blurb):\n{candidates}\n\n"
    "Reply in EXACTLY this format (paths must be copied verbatim from Candidates):\n"
    "related: <comma-separated paths or none>\n"
    "supersedes: <one path or none>\n"
)

_MAX_LINK_CANDIDATES = 12
_MAX_RELATED_LINKS = 3
_INDEX_PATH_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _search_query_from_summary(summary: str, *, max_terms: int = 8) -> str:
    """Build a short keyword query so INDEX title search can actually hit."""
    terms: list[str] = []
    seen: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_./-]{3,}", summary or ""):
        tok = raw.lower().strip(".-_/")
        if len(tok) < 3 or tok in MEMORY_SEARCH_STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        terms.append(tok)
        if len(terms) >= max_terms:
            break
    return " ".join(terms)


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
    folder: str  # destination folder name
    filename: str  # filename inside folder (not full path)
    tags: str  # "comma,separated"
    summary: str  # one sentence


@dataclass(frozen=True)
class _LinkDecision:
    related: tuple[str, ...]
    supersedes: str | None


@dataclass(frozen=True)
class _LinkCandidate:
    path: str
    blurb: str


async def _complete_text(
    provider: Provider,
    *,
    model: str,
    prompt: str,
) -> str:
    """Single-turn text completion (no tools) from streaming deltas."""
    parts: list[str] = []
    async with aclosing(
        cast(
            Any,
            provider.stream(
                [Message(role="user", content=[Text(text=prompt)])],
                [],
                model=model,
            ),
        )
    ) as stream:
        async for ev in stream:
            if isinstance(ev, TextDelta):
                parts.append(ev.text)
            elif isinstance(ev, Done):
                break
    return "".join(parts).strip()


def _path_from_index_line(line: str) -> str | None:
    match = _INDEX_PATH_RE.search(line)
    if not match:
        return None
    path = match.group(1).strip().split("|", 1)[0].strip().split("#", 1)[0].strip()
    path = path.replace("\\", "/").lstrip("./")
    if not path or path.lower().startswith("workspace:"):
        return None
    if not path.endswith((".md", ".txt", ".markdown")):
        path = f"{path}.md"
    return path


def _blurb_from_index_line(line: str) -> str:
    # `- [[path]] | tags: … | summary` → prefer trailing summary
    parts = [p.strip() for p in line.split("|")]
    if len(parts) >= 3:
        return parts[-1][:160]
    return line.strip()[:160]


def _normalize_declared_path(raw: str) -> str:
    text = raw.strip().strip("`").strip()
    if text.lower() in {"", "none", "n/a", "-", "null"}:
        return ""
    if text.startswith("[[") and text.endswith("]]"):
        text = text[2:-2].strip()
    text = text.split("|", 1)[0].strip().split("#", 1)[0].strip()
    text = text.replace("\\", "/").lstrip("./")
    if text and not text.endswith((".md", ".txt", ".markdown")):
        text = f"{text}.md"
    return text


def parse_link_decision(text: str, *, allowed: set[str]) -> _LinkDecision:
    """Parse organizer link LLM output; keep only paths in ``allowed``."""
    related_raw = ""
    supersedes_raw = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("related:"):
            related_raw = stripped.split(":", 1)[1].strip()
        elif low.startswith("supersedes:"):
            supersedes_raw = stripped.split(":", 1)[1].strip()

    related: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\n]", related_raw):
        path = _normalize_declared_path(chunk)
        if not path or path not in allowed or path in seen:
            continue
        seen.add(path)
        related.append(path)

    supersedes = _normalize_declared_path(supersedes_raw)
    if supersedes not in allowed:
        supersedes = ""
    if supersedes:
        related = [p for p in related if p != supersedes]
    related = related[:_MAX_RELATED_LINKS]

    return _LinkDecision(related=tuple(related), supersedes=supersedes or None)


class MemoryOrganizer:
    """Async post-processor that organises raw agent observations into typed memory."""

    def __init__(
        self,
        provider: Provider,
        model: str,
        storage: WorkspaceStorage,
        custom_folders: Sequence[_CustomMemoryFolderLike] | None = None,
        *,
        on_note_written: Callable[[str, str], Awaitable[None]] | None = None,
        pre_run: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._storage = storage
        self._all_folders: list[str] = BUILT_IN_FOLDERS + [
            cf.name for cf in (custom_folders or [])
        ]
        self._custom_folder_descriptions: dict[str, str] = {
            **_BUILT_IN_FOLDER_DESCRIPTIONS,
            **{cf.name: cf.description for cf in (custom_folders or [])},
        }
        self._on_note_written = on_note_written
        self._pre_run = pre_run

    async def run(self) -> MemoryOrganizerResult:
        """Process all unprocessed raw .md files directly under ``raw/``.

        Returns immediately with zeros if ``raw/`` is missing or empty.
        Per-file errors are captured in MemoryOrganizerResult.errors, not propagated.
        """
        await self._run_pre_hook()
        raw_files = await self._list_raw_files()
        if not raw_files:
            return MemoryOrganizerResult(0, 0, False)

        entries: list[IndexEntry] = []
        files_written = 0
        errors: list[str] = []

        for raw_rel in sorted(raw_files):
            try:
                entry = await self._process_raw_file(raw_rel)
            except Exception as e:
                raw_name = Path(raw_rel).name
                logger.warning(
                    "Memory organizer failed to process %s: %s", raw_name, str(e)
                )
                errors.append(raw_name)
                continue
            if entry is not None:
                entries.append(entry)
                files_written += 1

        if entries:
            await self._update_index(entries)

        return MemoryOrganizerResult(
            files_processed=len(raw_files),
            files_written=files_written,
            index_updated=bool(entries),
            errors=errors,
        )

    async def _run_pre_hook(self) -> None:
        if self._pre_run is None:
            return
        try:
            await self._pre_run()
        except Exception as exc:
            logger.warning("memory organizer pre_run failed: %r", exc)

    async def _list_raw_files(self) -> list[str]:
        all_under_raw = await self._storage.list_files("raw/")
        return [
            p
            for p in all_under_raw
            if p.startswith("raw/")
            and p.endswith(".md")
            and not p.startswith("raw/processed/")
            and p.count("/") == 1
        ]

    async def _process_raw_file(self, raw_rel: str) -> IndexEntry | None:
        raw_name = Path(raw_rel).name
        content = await self._storage.read_text(raw_rel)
        summary = await self._summarize(content)
        folder = await self._classify(summary)
        filename = self._generate_filename(raw_name, folder)
        dest_path = f"{folder}/{filename}"
        links = await self._choose_links(
            summary=summary, folder=folder, exclude_path=dest_path
        )

        note_text = format_memory_note(
            note_type=folder,
            status="active",
            body=summary,
            supersedes=links.supersedes,
            related=list(links.related),
        )
        await self._storage.write_text(dest_path, note_text)
        if self._on_note_written is not None:
            await self._on_note_written(dest_path, note_text)

        await self._storage.move(raw_rel, f"raw/processed/{raw_name}")
        return IndexEntry(folder=folder, filename=filename, tags="", summary=summary)

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

    async def _link_candidates(
        self,
        summary: str,
        folder: str,
        *,
        exclude_path: str | None = None,
    ) -> list[_LinkCandidate]:
        """Retrieve INDEX candidates for linking via token overlap only."""
        try:
            index_lines = await async_load_index(self._storage)
        except Exception as exc:
            logger.warning("organizer link candidate load failed: %r", exc)
            return []
        if not index_lines:
            return []

        exclude = (exclude_path or "").replace("\\", "/").lstrip("./")
        by_path: dict[str, str] = {}
        for line in index_lines:
            if not is_index_entry_line(line):
                continue
            path = _path_from_index_line(line)
            if not path or path == exclude:
                continue
            top = path.split("/", 1)[0]
            if top == "working":
                continue
            by_path[path] = _blurb_from_index_line(line)

        if not by_path:
            return []

        ranked: list[str] = []
        seen: set[str] = set()

        def _take(path: str) -> None:
            if path == exclude:
                return
            if path in by_path and path not in seen:
                seen.add(path)
                ranked.append(path)

        terms = _search_query_from_summary(summary, max_terms=12).split()
        if terms:
            scored: list[tuple[int, str]] = []
            for path, blurb in by_path.items():
                hay = f"{path} {blurb}".lower()
                score = sum(1 for t in terms if t in hay)
                if score > 0:
                    scored.append((score, path))
            scored.sort(key=lambda item: (-item[0], item[1]))
            for _, path in scored:
                _take(path)

        # Soft fill: same folder, then semantic, then remaining (newest first).
        for path in reversed([p for p in by_path if p.startswith(f"{folder}/")]):
            _take(path)
        for path in reversed([p for p in by_path if p.startswith("semantic/")]):
            _take(path)
        for path in reversed(list(by_path)):
            _take(path)

        return [
            _LinkCandidate(path=p, blurb=by_path[p])
            for p in ranked[:_MAX_LINK_CANDIDATES]
        ]

    async def _choose_links(
        self,
        *,
        summary: str,
        folder: str,
        exclude_path: str | None = None,
    ) -> _LinkDecision:
        candidates = await self._link_candidates(
            summary, folder, exclude_path=exclude_path
        )
        if not candidates:
            return _LinkDecision(related=(), supersedes=None)
        # Skip link LLM for ephemeral working notes — avoid graph noise.
        if folder == "working":
            return _LinkDecision(related=(), supersedes=None)

        allowed = {c.path for c in candidates}
        candidate_block = "\n".join(f"- {c.path} — {c.blurb}" for c in candidates)
        try:
            raw = await _complete_text(
                self._provider,
                model=self._model,
                prompt=_LINK_PROMPT.format(
                    folder=folder,
                    summary=summary,
                    candidates=candidate_block,
                ),
            )
        except Exception as exc:
            logger.warning("organizer link LLM failed: %r", exc)
            return _LinkDecision(related=(), supersedes=None)
        return parse_link_decision(raw, allowed=allowed)

    async def _update_index(self, entries: list[IndexEntry]) -> None:
        existing_content = await self._read_index_or_default()
        new_lines = await self._format_index_lines(entries)
        if not new_lines:
            return

        merged = append_index_entries(existing_content, new_lines)
        _header, entry_lines = split_index_document(merged)
        cap = index_cap_from_env()
        kept, archived = apply_index_entry_cap(entry_lines, cap)
        if archived:
            await self._append_archive(archived)

        final_content = format_index_document(_header, kept)
        try:
            await self._storage.write_text(INDEX_FILENAME, final_content)
        except Exception as e:
            raise MemoryOrganizerError(f"Failed to write INDEX.md: {e}") from e

    async def _read_index_or_default(self) -> str:
        existing_content = ""
        if await self._storage.exists(INDEX_FILENAME):
            try:
                existing_content = await self._storage.read_text(INDEX_FILENAME)
            except Exception as exc:
                logger.warning("organizer INDEX.md read failed: %r", exc)
                try:
                    report = await repair_memory_tree(self._storage, full_scan=True)
                    if report.quarantined or report.index_rebuilt or report.index_pruned:
                        logger.warning(
                            "organizer memory repair quarantined=%s rebuilt=%s pruned=%s",
                            report.quarantined,
                            report.index_rebuilt,
                            report.index_pruned,
                        )
                    if await self._storage.exists(INDEX_FILENAME):
                        existing_content = await self._storage.read_text(INDEX_FILENAME)
                except Exception as repair_exc:
                    logger.warning("organizer memory repair failed: %r", repair_exc)
                    existing_content = ""
        if not existing_content.strip():
            return f"{DEFAULT_INDEX_HEADER}\n"
        return existing_content

    async def _format_index_lines(self, entries: list[IndexEntry]) -> list[str]:
        new_lines: list[str] = []
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

            new_lines.append(
                f"- [[{entry.folder}/{entry.filename}]]"
                f" | tags: {entry.tags} | {entry.summary}"
            )
        return new_lines

    async def _append_archive(self, archived: list[str]) -> None:
        archive_raw = ""
        if await self._storage.exists(INDEX_ARCHIVE_FILENAME):
            try:
                archive_raw = await self._storage.read_text(INDEX_ARCHIVE_FILENAME)
            except Exception as exc:
                logger.warning("organizer INDEX.archive.md read failed: %r", exc)
                archive_raw = ""
        archive_out = merge_archive_content(archive_raw, archived)
        await self._storage.write_text(INDEX_ARCHIVE_FILENAME, archive_out)
