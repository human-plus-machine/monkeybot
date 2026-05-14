from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.council import (
    MANAGED_CATEGORIES,
    _load_existing_categories,
    _parse_council_sections,
    run_council,
)


@dataclass
class _Chunk:
    text: str


class FakeProvider:
    """Minimal provider stub for council tests."""

    def __init__(self, response: str = "", raise_error: bool = False) -> None:
        self._response = response
        self._raise_error = raise_error
        self.called = False
        self.last_messages: list[Any] = []

    async def stream(
        self,
        messages: list[Any],
        tools: list[Any],
        *,
        model: str = "",
        system: str = "",
    ) -> Any:
        """Yield a single chunk with the configured response text."""
        self.called = True
        self.last_messages = list(messages)
        if self._raise_error:
            raise RuntimeError("provider error")
        yield _Chunk(text=self._response)


FULL_RESPONSE = """\
## Summary
This session covered testing the council.

## user-preferences
- Prefers concise answers

## key-facts
- Python 3.11 in use

## open-questions
- What is the timeout?
"""


async def test_run_council_empty_text(tmp_path: Path) -> None:
    """Empty conversation text → returns [] without calling provider."""
    provider = FakeProvider()
    result = await run_council("", str(tmp_path), provider, "model", "sess")  # type: ignore[arg-type]
    assert result == []
    assert provider.called is False


async def test_run_council_writes_session_file(tmp_path: Path) -> None:
    """After a successful run, a session summary .md file exists under tmp_path."""
    provider = FakeProvider(response=FULL_RESPONSE)
    written = await run_council("user: hello", str(tmp_path), provider, "model", "sess1234")  # type: ignore[arg-type]
    session_files = [w for w in written if "session-" in w]
    assert len(session_files) == 1
    assert (tmp_path / session_files[0]).exists()


async def test_run_council_writes_all_category_files(tmp_path: Path) -> None:
    """All 3 category .md files are written; return list has 4 items."""
    provider = FakeProvider(response=FULL_RESPONSE)
    written = await run_council("user: hello", str(tmp_path), provider, "model", "sess1234")  # type: ignore[arg-type]
    assert len(written) == 4
    for cat in MANAGED_CATEGORIES:
        assert (tmp_path / f"{cat}.md").exists()


async def test_run_council_skips_empty_section(tmp_path: Path) -> None:
    """Response without ## open-questions → open-questions.md not created, and
    any pre-existing open-questions.md is left unchanged."""
    existing_content = "- Is this preserved?"
    (tmp_path / "open-questions.md").write_text(existing_content)

    response_no_open = """\
## Summary
Session summary.

## user-preferences
- Prefers dark mode

## key-facts
- Python 3.11
"""
    provider = FakeProvider(response=response_no_open)
    written = await run_council("user: hello", str(tmp_path), provider, "model", "sess1234")  # type: ignore[arg-type]
    assert "open-questions.md" not in written
    # Pre-existing file must be preserved unchanged
    assert (tmp_path / "open-questions.md").read_text() == existing_content


async def test_run_council_merges_existing(tmp_path: Path) -> None:
    """Pre-existing user-preferences.md is preserved; new content added."""
    (tmp_path / "user-preferences.md").write_text("- Prefers dark mode")
    merged_response = """\
## Summary
Merged session.

## user-preferences
- Prefers dark mode
- Prefers concise answers

## key-facts
- Python 3.11

## open-questions
- TBD
"""
    provider = FakeProvider(response=merged_response)
    await run_council("user: hello", str(tmp_path), provider, "model", "sess1234")  # type: ignore[arg-type]
    content = (tmp_path / "user-preferences.md").read_text()
    assert "Prefers dark mode" in content
    assert "Prefers concise answers" in content


async def test_run_council_provider_error(tmp_path: Path) -> None:
    """Provider error → returns [] without crashing; no files written."""
    provider = FakeProvider(raise_error=True)
    result = await run_council("user: hello", str(tmp_path), provider, "model", "sess1234")  # type: ignore[arg-type]
    assert result == []
    assert list(tmp_path.glob("*.md")) == []


async def test_load_existing_categories_missing_dir(tmp_path: Path) -> None:
    """Nonexistent memory_path → all category values are empty strings."""
    result = _load_existing_categories(str(tmp_path / "nonexistent"))
    assert set(result.keys()) == set(MANAGED_CATEGORIES)
    for val in result.values():
        assert val == ""


async def test_parse_council_sections_all_headers() -> None:
    """FULL_RESPONSE parses into dict with all four expected keys."""
    sections = _parse_council_sections(FULL_RESPONSE)
    assert "summary" in sections
    assert "user-preferences" in sections
    assert "key-facts" in sections
    assert "open-questions" in sections


async def test_parse_council_sections_missing_header() -> None:
    """Response without ## key-facts → that key is absent from result."""
    response = """\
## Summary
Session summary.

## user-preferences
- Prefers dark mode
"""
    sections = _parse_council_sections(response)
    assert sections.get("key-facts", "") == ""


async def test_run_council_does_not_raise_on_save_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OSError from save_memory → run_council returns [] without crashing."""
    monkeypatch.setattr("monkeybot.core.council.save_memory", lambda *a, **kw: (_ for _ in ()).throw(OSError("disk full")))  # noqa: E501
    provider = FakeProvider(response=FULL_RESPONSE)
    result = await run_council("user: hello", str(tmp_path), provider, "model", "sess1234")  # type: ignore[arg-type]
    assert result == []
