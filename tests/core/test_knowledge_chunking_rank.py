"""Offline rank gate: heading-boundary markdown chunking keeps late-section hits ≤ rank 4."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.knowledge.chunking import chunk_text, index_content_digest
from monkeybot.core.knowledge.fusion import search as recall
from monkeybot.core.knowledge.sqlite_index import KnowledgeIndex


@pytest.mark.asyncio
async def test_markdown_heading_chunking_rank_leq_4(tmp_path: Path) -> None:
    """A unique term in a late H2 section should surface the file at rank ≤ 4.

    Heading-boundary chunks keep the term in a focused section chunk instead of
    diluting it across a large line-window with earlier noise.
    """
    index = KnowledgeIndex(tmp_path / "rank.sqlite")
    await index.open()
    try:
        # Target: many early sections of filler, then a late section with UNIQUE_TOKEN.
        early = "\n\n".join(
            f"## Section {i}\n\n"
            f"Generic filler about widgets and workflows number {i}. " * 20
            for i in range(1, 12)
        )
        target_body = (
            "# Product handbook\n\n"
            f"{early}\n\n"
            "## Refund escalation\n\n"
            "When annual plans need UNIQUE_TOKEN_ZXQR approval from finance, "
            "route via the refunds playbook.\n"
        )

        distractors = [
            (
                f"noise/noise_{i}.md",
                f"# Noise {i}\n\n"
                + "\n".join(
                    f"## Part {j}\n\nwallets widgets workflows filler {i}-{j}\n"
                    for j in range(8)
                ),
            )
            for i in range(6)
        ]

        await index.upsert_file(
            path="docs/handbook.md",
            source_type="workspace_file",
            content_hash=index_content_digest(target_body),
            mtime=1.0,
            chunks=chunk_text(
                target_body,
                path="docs/handbook.md",
                source_type="workspace_file",
                chunk_tokens=80,
                overlap_ratio=0.0,
            ),
            links=[],
        )
        for path, body in distractors:
            await index.upsert_file(
                path=path,
                source_type="workspace_file",
                content_hash=index_content_digest(body),
                mtime=1.0,
                chunks=chunk_text(
                    body,
                    path=path,
                    source_type="workspace_file",
                    chunk_tokens=80,
                    overlap_ratio=0.0,
                ),
                links=[],
            )

        hits = await recall(index, "UNIQUE_TOKEN_ZXQR refund escalation", limit=10)
        paths = [h.path for h in hits]
        assert "docs/handbook.md" in paths, f"target missing from hits: {paths}"
        rank = paths.index("docs/handbook.md") + 1
        assert rank <= 4, f"expected rank ≤ 4, got {rank} in {paths}"
    finally:
        await index.close()
