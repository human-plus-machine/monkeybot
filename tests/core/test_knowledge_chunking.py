"""Tests for content-aware knowledge chunking."""

from __future__ import annotations

import pytest

from monkeybot.core.knowledge.chunking import (
    CHUNKER_VERSION,
    chunk_text,
    estimate_tokens,
    index_content_digest,
)


def test_estimate_tokens_rough() -> None:
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 40) == 10


def test_index_content_digest_includes_version() -> None:
    a = index_content_digest("hello")
    b = index_content_digest("hello")
    assert a == b
    assert CHUNKER_VERSION >= 3
    # Different from raw body hash.
    from monkeybot.core.knowledge.extractors import content_hash

    assert a != content_hash("hello")


def test_chunk_empty() -> None:
    assert chunk_text("", path="x", source_type="note") == []
    assert chunk_text("   \n", path="x", source_type="note") == []


def test_chunk_prose_overlap_and_prefix() -> None:
    lines = [f"line {i} content here\n" for i in range(200)]
    text = "".join(lines)
    chunks = chunk_text(
        text,
        path="notes/foo.txt",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.2,
    )
    assert len(chunks) >= 2
    assert chunks[0].path == "notes/foo.txt"
    assert chunks[0].text.startswith("notes/foo.txt")
    assert chunks[0].start_line == 1
    assert chunks[1].start_line < chunks[0].end_line


def _pad(n: int = 40) -> str:
    return " filler about widgets and workflows." * n


def test_markdown_heading_sections() -> None:
    text = (
        "# Title\n\n"
        f"intro paragraph{_pad(5)}\n\n"
        f"## Alpha\n\n"
        f"alpha body with enough text{_pad(8)}\n\n"
        f"## Beta\n\n"
        f"beta body unique keyword ZXQR{_pad(8)}\n"
    )
    chunks = chunk_text(
        text,
        path="docs/guide.md",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.0,
    )
    assert len(chunks) >= 2
    beta = [c for c in chunks if "ZXQR" in c.text]
    assert beta
    assert beta[0].text.startswith("docs/guide.md")
    # Beta-focused chunk should carry the Beta label, not start mid-alpha only.
    assert "Beta" in beta[0].text.split("\n", 1)[0]


def test_markdown_no_mid_section_cut_when_small() -> None:
    text = "## One\n\nshort\n\n## Two\n\nalso short\n"
    chunks = chunk_text(
        text,
        path="a.md",
        source_type="note",
        chunk_tokens=700,
        overlap_ratio=0.0,
    )
    assert all(c.start_line >= 1 for c in chunks)
    joined = "\n".join(c.text for c in chunks)
    assert "short" in joined and "also short" in joined


def test_markdown_oversized_section_subsplits() -> None:
    # Many short lines so line-aligned windowing can cut (not one mega-line).
    body = "\n".join(f"word word word word line {i}" for i in range(200))
    text = f"## Huge\n\n{body}\n"
    chunks = chunk_text(
        text,
        path="big.md",
        source_type="note",
        chunk_tokens=50,
        overlap_ratio=0.1,
    )
    assert len(chunks) >= 2
    assert all(c.text.startswith("big.md") for c in chunks)


def _two_python_defs(*, lines_per_fn: int = 20) -> str:
    pad = "\n".join(f"    x = {i}  # pad content for size" for i in range(lines_per_fn))
    return (
        f"def alpha():\n{pad}\n    return 1\n\n"
        f"def beta():\n{pad}\n    return 2\n"
    )


def test_code_heuristic_does_not_split_inside_def() -> None:
    # Each def ~ under 8x target so atomic units stay whole; two defs exceed
    # target so they land in separate chunks.
    text = _two_python_defs(lines_per_fn=25)
    chunks = chunk_text(
        text,
        path="src/mod.py",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.0,
        use_ast=False,
    )
    assert len(chunks) >= 2
    for c in chunks:
        first_body = c.text.split("\n", 1)[1].lstrip("\n").split("\n", 1)[0]
        assert not first_body.startswith("    x ="), f"mid-def start: {first_body!r}"
    prefixes = [c.text.split("\n", 1)[0] for c in chunks]
    assert any(
        "alpha" in p or "def alpha" in c.text
        for p, c in zip(prefixes, chunks, strict=True)
    )


def test_code_ast_path_when_available() -> None:
    pytest.importorskip("tree_sitter_language_pack")
    text = _two_python_defs(lines_per_fn=25)
    chunks = chunk_text(
        text,
        path="src/mod.py",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.0,
        use_ast=True,
    )
    assert len(chunks) >= 2
    prefixes = [c.text.split("\n", 1)[0] for c in chunks]
    assert any("alpha" in p for p in prefixes) or any("beta" in p for p in prefixes)
    for c in chunks:
        first_body = c.text.split("\n", 1)[1].lstrip("\n").split("\n", 1)[0]
        assert not first_body.startswith("    x =")


def test_json_top_level_keys_grouped() -> None:
    pad = ", ".join(f'"{i}": {i}' for i in range(40))
    text = (
        "{\n"
        f'  "alpha": {{{pad}}},\n'
        f'  "beta": {{{pad}}}\n'
        "}\n"
    )
    chunks = chunk_text(
        text,
        path="cfg/app.json",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.0,
    )
    assert len(chunks) >= 2
    alpha = [c for c in chunks if " · alpha" in c.text.split("\n", 1)[0]]
    beta = [c for c in chunks if " · beta" in c.text.split("\n", 1)[0]]
    assert alpha, f"no alpha chunk in {[c.text.split(chr(10), 1)[0] for c in chunks]}"
    assert beta
    # Alpha-labeled chunk should not also be labeled beta.
    assert all(" · beta" not in c.text.split("\n", 1)[0] for c in alpha)


def test_yaml_top_level_keys() -> None:
    pad = "\n".join(f"  field_{i}: value_{i}_padding_here" for i in range(30))
    text = f"alpha:\n{pad}\nbeta:\n{pad}\n"
    chunks = chunk_text(
        text,
        path="cfg/app.yaml",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.0,
    )
    assert len(chunks) >= 2
    assert any("alpha" in c.text.split("\n", 1)[0] for c in chunks)
    assert any("beta" in c.text for c in chunks)


def test_toml_table_groups() -> None:
    pad = "\n".join(f'key_{i} = "value_{i}_padding"' for i in range(30))
    text = (
        'name = "demo"\n'
        "\n"
        f"[database]\n{pad}\n"
        "\n"
        f"[cache]\n{pad}\n"
    )
    chunks = chunk_text(
        text,
        path="cfg/app.toml",
        source_type="workspace_file",
        chunk_tokens=50,
        overlap_ratio=0.0,
    )
    assert len(chunks) >= 2
    assert any("database" in c.text for c in chunks)
    assert any("cache" in c.text for c in chunks)
