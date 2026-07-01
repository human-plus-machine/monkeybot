"""Tests for playbook path confinement and host slug normalization."""

from __future__ import annotations

from pathlib import Path

import pytest

from browser_mcp import playbooks


@pytest.fixture(autouse=True)
def _isolated_playbooks_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Each test uses a fresh temp playbooks root."""
    root = tmp_path / "playbooks"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROWSER_MCP_PLAYBOOKS_DIR", str(root))
    return root


@pytest.mark.parametrize(
    ("host_or_url", "expected"),
    [
        ("example.com", "example.com"),
        ("https://example.com/path?q=1", "example.com"),
        ("https://www.example.com", "example.com"),
        ("www.example.com", "example.com"),
        ("sub.example.co.uk", "sub.example.co.uk"),
        ("my-site.io", "my-site.io"),
    ],
)
def test_host_slug_normalizes_valid_hosts(host_or_url: str, expected: str) -> None:
    assert playbooks.host_slug(host_or_url) == expected


@pytest.mark.parametrize(
    "host_or_url",
    [
        "",
        "   ",
        "..",
        "....",
        "../etc",
        "https://../",
        "host..traversal.com",
    ],
)
def test_host_slug_rejects_traversal_and_invalid_hosts(host_or_url: str) -> None:
    with pytest.raises(playbooks.PlaybookError):
        playbooks.host_slug(host_or_url)


def test_host_slug_ignores_url_path_segments(_isolated_playbooks_dir: Path) -> None:
    """Path segments after the host must not influence the on-disk filename."""
    assert playbooks.host_slug("evil/../../../secret") == "evil"
    assert playbooks.playbook_path("evil/../../../secret") == _isolated_playbooks_dir / "evil.md"


def test_playbook_path_stays_under_configured_root(_isolated_playbooks_dir: Path) -> None:
    path = playbooks.playbook_path("https://example.com/docs")
    root = _isolated_playbooks_dir.resolve()
    assert path == root / "example.com.md"
    assert path.resolve().relative_to(root)


def test_assert_under_root_rejects_escape(_isolated_playbooks_dir: Path) -> None:
    root = _isolated_playbooks_dir.resolve()
    outside = (root.parent / "outside.md").resolve()
    with pytest.raises(playbooks.PlaybookError, match="escapes playbooks directory"):
        playbooks._assert_under_root(outside, root)


def test_write_and_read_playbook_round_trip(_isolated_playbooks_dir: Path) -> None:
    result = playbooks.write_playbook("example.com", "# Notes\n")
    assert result == {"ok": True, "path": str(_isolated_playbooks_dir / "example.com.md"), "host": "example.com"}
    assert playbooks.read_playbook("https://www.example.com/page") == "# Notes\n"


def test_write_playbook_append_adds_separator(_isolated_playbooks_dir: Path) -> None:
    playbooks.write_playbook("example.com", "first")
    playbooks.write_playbook("example.com", "second", append=True)
    content = playbooks.read_playbook("example.com")
    assert content == "first\n\n---\n\nsecond"


def test_list_playbook_names_all_and_by_host(_isolated_playbooks_dir: Path) -> None:
    playbooks.write_playbook("alpha.com", "a")
    playbooks.write_playbook("alpha.io", "b")
    playbooks.write_playbook("beta.com", "c")
    assert playbooks.list_playbook_names() == ["alpha.com.md", "alpha.io.md", "beta.com.md"]
    assert playbooks.list_playbook_names("alpha.com") == ["alpha.com.md"]


def test_read_playbook_missing_raises(_isolated_playbooks_dir: Path) -> None:
    with pytest.raises(playbooks.PlaybookError, match="no playbook"):
        playbooks.read_playbook("missing.example")


def test_list_playbook_names_empty_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "not-created"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BROWSER_MCP_PLAYBOOKS_DIR", str(missing))
    assert playbooks.list_playbook_names() == []
