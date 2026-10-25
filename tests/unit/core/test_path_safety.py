"""Tests for ``monkeybot.core.path_safety``."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.path_safety import (
    is_legacy_path_component_safe,
    path_contained_under,
    resolve_legacy_or_sanitized_dir,
)


def test_is_legacy_path_component_safe_rejects_glob_only_names() -> None:
    assert is_legacy_path_component_safe("*") is False
    assert is_legacy_path_component_safe("sess*1") is True


def test_path_contained_under_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "attachments"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "legacy-id").symlink_to(outside)
    assert path_contained_under(root, root / "legacy-id") is None


def test_resolve_legacy_or_sanitized_dir_skips_uncontained_legacy(tmp_path: Path) -> None:
    parent = tmp_path / "attachments"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    legacy_name = "sess*1"
    (parent / legacy_name).symlink_to(outside)
    resolved = resolve_legacy_or_sanitized_dir(parent, legacy_name)
    assert resolved == parent / "sess_1"
    assert path_contained_under(parent.resolve(), resolved) is not None


def test_resolve_legacy_or_sanitized_dir_reuses_contained_legacy(tmp_path: Path) -> None:
    parent = tmp_path / "attachments"
    parent.mkdir()
    legacy_name = "sess*1"
    legacy_dir = parent / legacy_name
    legacy_dir.mkdir()
    resolved = resolve_legacy_or_sanitized_dir(parent, legacy_name)
    assert resolved == legacy_dir


def test_resolve_legacy_or_sanitized_dir_fallback_when_sanitized_is_symlink(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "attachments"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (parent / "safe").symlink_to(outside)
    resolved = resolve_legacy_or_sanitized_dir(parent, "safe")
    assert resolved == parent / "_"
