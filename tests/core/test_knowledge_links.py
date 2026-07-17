"""Tests for knowledge wiki-link parsing."""

from __future__ import annotations

from monkeybot.core.knowledge.links import format_link_ref, parse_wiki_links


def test_parse_workspace_span_link() -> None:
    links = parse_wiki_links(
        "See [[workspace:research/refunds.md#L40-72]] for policy."
    )
    assert len(links) == 1
    assert links[0].link_type == "workspace"
    assert links[0].target_path == "research/refunds.md"
    assert links[0].start_line == 40
    assert links[0].end_line == 72
    assert format_link_ref(links[0]) == "workspace:research/refunds.md#L40-72"


def test_parse_note_link_normalizes_bare_name() -> None:
    links = parse_wiki_links("Related: [[refund-policy|Refunds]]")
    assert len(links) == 1
    assert links[0].link_type == "note"
    assert links[0].target_path == "notes/refund-policy.md"


def test_parse_multiple_and_workspace_without_span() -> None:
    text = "[[workspace:a/b.ts]] and [[notes/other.md]]"
    links = parse_wiki_links(text)
    assert len(links) == 2
    assert links[0].target_path == "a/b.ts"
    assert links[0].start_line is None
    assert links[1].target_path == "notes/other.md"


def test_relative_link_resolves_against_source_dir() -> None:
    """memory/INDEX.md + [[episodic/x.md]] → memory/episodic/x.md (F2)."""
    links = parse_wiki_links(
        "See [[episodic/tool-echo.md]]",
        source_path="memory/INDEX.md",
    )
    assert len(links) == 1
    assert links[0].target_path == "memory/episodic/tool-echo.md"
    assert links[0].link_type == "note"


def test_absolute_note_roots_unchanged() -> None:
    links = parse_wiki_links(
        "[[memory/episodic/x.md]] and [[notes/tip.md]]",
        source_path="memory/INDEX.md",
    )
    assert [lnk.target_path for lnk in links] == [
        "memory/episodic/x.md",
        "notes/tip.md",
    ]


def test_rejects_parent_escape_outside_known_roots() -> None:
    # Enough .. to leave memory/ entirely
    links = parse_wiki_links(
        "[[../../../etc/passwd]]",
        source_path="memory/INDEX.md",
    )
    assert links == []

    # Popping above notes/ root is also rejected
    links = parse_wiki_links(
        "[[../outside.md]]",
        source_path="notes/tip.md",
    )
    assert links == []


def test_normalizes_dot_segments() -> None:
    links = parse_wiki_links(
        "[[./episodic/../episodic/x.md]]",
        source_path="memory/INDEX.md",
    )
    assert len(links) == 1
    assert links[0].target_path == "memory/episodic/x.md"


def test_workspace_links_ignore_source_path() -> None:
    links = parse_wiki_links(
        "[[workspace:src/app.ts]]",
        source_path="memory/INDEX.md",
    )
    assert links[0].target_path == "src/app.ts"
    assert links[0].link_type == "workspace"
