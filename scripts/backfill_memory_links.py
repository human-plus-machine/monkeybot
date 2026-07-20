#!/usr/bin/env python3
"""One-time backfill: add Obsidian-style related/supersedes links to existing memory notes.

Usage:
  cd /path/to/monkeybot
  set -a && source ~/.monkeybot/agents/default/.env && set +a
  uv run python scripts/backfill_memory_links.py ~/.monkeybot/agents/default/memory
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from monkeybot.core.config.settings import get_provider_config
from monkeybot.core.memory.note_format import (
    TYPED_FOLDERS,
    extract_memory_wiki_links,
    format_memory_note,
    parse_memory_note,
)
from monkeybot.core.memory.organizer import MemoryOrganizer
from monkeybot.core.workspace import create_workspace_storage


def _list_note_paths(memory_root: Path) -> list[str]:
    out: list[str] = []
    for folder in TYPED_FOLDERS:
        d = memory_root / folder
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            if p.is_file():
                out.append(f"{folder}/{p.name}")
    return out


def _body_for_linking(text: str) -> str:
    meta, body = parse_memory_note(text)
    del meta
    # Drop a prior Related: trailer so re-runs don't accumulate noise.
    lines = body.splitlines()
    cut = len(lines)
    for i, line in enumerate(lines):
        if line.strip().lower().startswith("related:"):
            cut = i
            break
    return "\n".join(lines[:cut]).strip()


async def _backfill(memory_root: Path, *, dry_run: bool, limit: int | None) -> int:
    uri = f"local://{memory_root.resolve()}"
    storage = create_workspace_storage(uri)

    # Prefer a cheap flash model for bulk linking; fall back to env/yaml defaults.
    provider_name = os.environ.get("BACKFILL_MODEL_PROVIDER") or os.environ.get(
        "MODEL_PROVIDER", "google_genai"
    )
    model_name = os.environ.get("BACKFILL_MODEL_NAME") or os.environ.get(
        "MODEL_NAME", "gemini-2.5-flash"
    )
    # If default agent is nvidia-only and gemini key exists, prefer gemini for cost.
    if provider_name == "nvidia" and (os.environ.get("GEMINI_API_KEY") or "").strip():
        provider_name = "google_genai"
        model_name = os.environ.get("BACKFILL_MODEL_NAME", "gemini-3-flash-preview")

    cfg = get_provider_config(provider=provider_name, model_name=model_name)
    org = MemoryOrganizer(provider=cfg.provider, model=cfg.model, storage=storage)

    notes = _list_note_paths(memory_root)
    if limit is not None:
        notes = notes[: max(0, limit)]

    linked = 0
    skipped = 0
    errors = 0
    print(f"memory={memory_root} notes={len(notes)} provider={provider_name} model={cfg.model}")

    for i, rel in enumerate(notes, start=1):
        folder = rel.split("/", 1)[0]
        try:
            text = await storage.read_text(rel)
            body = _body_for_linking(text)
            if not body:
                skipped += 1
                continue
            # Skip working/ — ephemeral, avoid graph noise (same as organizer).
            if folder == "working":
                skipped += 1
                continue

            decision = await org._choose_links(
                summary=body[:2500], folder=folder, exclude_path=rel
            )
            related = [p for p in decision.related if p != rel]
            supersedes = (
                decision.supersedes
                if decision.supersedes and decision.supersedes != rel
                else None
            )

            if not related and not supersedes:
                print(f"[{i}/{len(notes)}] {rel}: no links")
                skipped += 1
                continue

            meta, _ = parse_memory_note(text)
            note_type = meta.type if meta is not None else folder
            new_text = format_memory_note(
                note_type=note_type,
                status=meta.status if meta is not None else "active",
                body=body,
                supersedes=supersedes,
                related=related,
            )
            wiki = extract_memory_wiki_links(new_text)
            print(
                f"[{i}/{len(notes)}] {rel}: related={list(related)} "
                f"supersedes={supersedes!r} wiki={wiki}"
            )
            if not dry_run:
                await storage.write_text(rel, new_text)
            linked += 1
        except Exception as exc:
            errors += 1
            print(f"[{i}/{len(notes)}] {rel}: ERROR {exc!r}", file=sys.stderr)

    if not dry_run:
        from monkeybot.core.llm.provider import Done, TextDelta, UsageEvent
        from monkeybot.core.memory.subsystem import MemorySubsystem
        from monkeybot.core.testing.mocks_provider import ScriptedFakeProvider

        fake = ScriptedFakeProvider(
            [
                TextDelta(text="x"),
                UsageEvent(input_tokens=1, output_tokens=1, cached_tokens=0),
                Done(),
            ]
        )
        sub = MemorySubsystem(
            storage=storage,
            provider=fake,
            model="m",
            memory_uri=uri,
        )
        try:
            stats = await sub.rebuild_graph()
            print(f"graph rebuild: {stats}")
        finally:
            await sub.close()

    print(f"done linked={linked} skipped={skipped} errors={errors} dry_run={dry_run}")
    return 0 if errors == 0 else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "memory_root",
        type=Path,
        help="Path to agent memory directory",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N notes")
    args = parser.parse_args()
    root = args.memory_root.expanduser().resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        sys.exit(2)
    raise SystemExit(asyncio.run(_backfill(root, dry_run=args.dry_run, limit=args.limit)))


if __name__ == "__main__":
    main()
