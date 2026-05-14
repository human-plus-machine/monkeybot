from __future__ import annotations

from pathlib import Path

from monkeybot.core.memory import save_memory, search_memory


def test_save_creates_file(tmp_path: Path) -> None:
    result = save_memory(str(tmp_path), "note", "Hello world")
    assert (tmp_path / "note.md").exists()
    assert (tmp_path / "note.md").read_text() == "Hello world"
    assert "note.md" in result


def test_save_creates_parent_dirs(tmp_path: Path) -> None:
    memory_path = str(tmp_path / "nested" / "deep")
    save_memory(memory_path, "doc", "content")
    assert (Path(memory_path) / "doc.md").exists()


def test_save_returns_ok_string(tmp_path: Path) -> None:
    result = save_memory(str(tmp_path), "myfile", "data")
    assert result.startswith("Success:")
    assert "myfile.md" in result


def test_search_returns_matching_excerpts(tmp_path: Path) -> None:
    (tmp_path / "alpha.md").write_text("Python is great for data science")
    (tmp_path / "beta.md").write_text("Java is a compiled language")
    (tmp_path / "gamma.md").write_text("Python and Java both have strong ecosystems")
    result = search_memory("Python", str(tmp_path))
    assert "### alpha" in result
    assert "### gamma" in result
    assert "### beta" not in result


def test_search_no_matches_returns_sentinel(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("Completely unrelated content")
    result = search_memory("quantum_flux_capacitor", str(tmp_path))
    assert "quantum_flux_capacitor" in result


def test_search_nonexistent_path(tmp_path: Path) -> None:
    result = search_memory("anything", str(tmp_path / "does_not_exist"))
    assert result == "No memory files found."


def test_search_max_results_respected(tmp_path: Path) -> None:
    for i in range(5):
        (tmp_path / f"file{i}.md").write_text("matching keyword here")
    result = search_memory("keyword", str(tmp_path), max_results=2)
    # Count occurrences of "###" headings — each file produces one
    assert result.count("###") == 2


def test_search_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("python is amazing")
    result = search_memory("Python", str(tmp_path))
    assert "note" in result


def test_search_highest_score_first(tmp_path: Path) -> None:
    """File with more keyword matches ranks above file with fewer."""
    (tmp_path / "high.md").write_text("alpha beta content")  # matches 2 keywords
    (tmp_path / "low.md").write_text("alpha only content")   # matches 1 keyword
    result = search_memory("alpha beta", str(tmp_path))
    high_pos = result.index("high")
    low_pos = result.index("low")
    assert high_pos < low_pos


def test_search_multi_keyword_scoring(tmp_path: Path) -> None:
    """Score = number of distinct keywords found (not occurrences)."""
    (tmp_path / "both.md").write_text("alpha beta content")
    (tmp_path / "one.md").write_text("alpha only content")
    result = search_memory("alpha beta", str(tmp_path))
    # 'both' matches 2 keywords, 'one' matches 1 — both should appear
    assert "both" in result
    assert "one" in result
    assert result.index("both") < result.index("one")


def test_search_preview_truncated(tmp_path: Path) -> None:
    """Excerpt is capped at 500 characters."""
    long_content = "keyword " + "x" * 1000
    (tmp_path / "bigfile.md").write_text(long_content)
    result = search_memory("keyword", str(tmp_path))
    # The preview in result should not contain more than ~500 chars from the file
    # Check that the full 1000-char filler isn't present
    assert "x" * 600 not in result
