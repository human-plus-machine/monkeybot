from __future__ import annotations

from pathlib import Path

from monkeybot.core.tools.gitignore import GitignoreRules


def _rules(tmp_path: Path, text: str, rel_dir: str = "") -> GitignoreRules:
    (tmp_path / ".gitignore").write_text(text, encoding="utf-8")
    return GitignoreRules().with_directory(tmp_path, rel_dir)


def test_unanchored_pattern_matches_at_any_depth(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "*.log\n")
    assert rules.is_ignored("a.log", is_dir=False)
    assert rules.is_ignored("x/y/a.log", is_dir=False)
    assert not rules.is_ignored("a.txt", is_dir=False)


def test_anchored_pattern_matches_only_from_its_directory(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "/gen\nsrc/out\n")
    assert rules.is_ignored("gen", is_dir=True)
    assert not rules.is_ignored("x/gen", is_dir=True)
    assert rules.is_ignored("src/out", is_dir=False)
    assert not rules.is_ignored("a/src/out", is_dir=False)


def test_dir_only_pattern_skips_files(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "cache/\n")
    assert rules.is_ignored("cache", is_dir=True)
    assert not rules.is_ignored("cache", is_dir=False)


def test_negation_last_match_wins(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "*.log\n!keep.log\n")
    assert rules.is_ignored("a.log", is_dir=False)
    assert not rules.is_ignored("keep.log", is_dir=False)


def test_double_star_and_character_classes(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "a/**/b\nout/**\nf[!0-9].txt\n")
    assert rules.is_ignored("a/b", is_dir=False)
    assert rules.is_ignored("a/x/y/b", is_dir=False)
    assert rules.is_ignored("out/deep/file", is_dir=False)
    assert rules.is_ignored("fx.txt", is_dir=False)
    assert not rules.is_ignored("f1.txt", is_dir=False)


def test_comments_blank_lines_and_escapes(tmp_path: Path) -> None:
    rules = _rules(tmp_path, "# comment\n\n\\#hash\n\\!bang\n")
    assert rules.is_ignored("#hash", is_dir=False)
    assert rules.is_ignored("!bang", is_dir=False)
    assert not rules.is_ignored("comment", is_dir=False)


def test_nested_gitignore_is_scoped_to_its_directory(tmp_path: Path) -> None:
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    rules = GitignoreRules().with_directory(sub, "pkg")
    assert rules.is_ignored("pkg/secret.txt", is_dir=False)
    assert not rules.is_ignored("other/secret.txt", is_dir=False)
