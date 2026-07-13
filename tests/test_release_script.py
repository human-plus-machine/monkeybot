"""Tests for scripts/release.py helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_PY = ROOT / "scripts" / "release.py"


def _load_release():
    spec = importlib.util.spec_from_file_location("monkeybot_release", RELEASE_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def release():
    return _load_release()


def test_packages_order_core_and_browser_before_cli(release) -> None:
    assert list(release.PACKAGES) == ["core", "browser", "cli"]


def test_write_github_output_appends(release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "github_output"
    out.write_text("prior=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    release.write_github_output("packages", "core,cli")
    assert out.read_text(encoding="utf-8") == "prior=1\npackages=core,cli\n"


def test_write_github_output_noop_without_env(release, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    release.write_github_output("packages", "core")  # must not raise


def test_cut_changelog_attributes_notes_to_each_package(
    release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [Unreleased]

### Core

### Added

- Core release.

### Browser MCP

### Added

- Browser release.

### CLI

### Fixed

- CLI release.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "CHANGELOG", changelog)

    release.cut_changelog({"core": "2.2.0", "browser": "0.2.0", "cli": "0.3.0"})

    text = changelog.read_text(encoding="utf-8")
    assert "## [Unreleased]\n\n## [core v2.2.0]" in text
    assert "## [browser v0.2.0]" in text
    assert "## [cli v0.3.0]" in text
    assert text.count("- Core release.") == 1
    assert text.count("- Browser release.") == 1
    assert text.count("- CLI release.") == 1
    assert release.changelog_section("core", "2.2.0") is not None
    assert release.changelog_section("browser", "0.2.0") is not None
    assert release.changelog_section("cli", "0.3.0") is not None


def test_cut_changelog_leaves_other_package_notes_unreleased(
    release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [Unreleased]

### Core

### Added

- Core release.

### CLI

### Fixed

- CLI release.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "CHANGELOG", changelog)

    release.cut_changelog({"core": "2.2.0"})

    text = changelog.read_text(encoding="utf-8")
    assert "## [core v2.2.0]" in text
    assert "- Core release." in release.changelog_section("core", "2.2.0")
    assert "### CLI" in text
    assert "- CLI release." in text


def test_changelog_sections_remain_distinct_for_matching_versions(
    release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        """# Changelog

## [Unreleased]

### Core

### Added

- Core release.

### Browser MCP

### Added

- Browser release.
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "CHANGELOG", changelog)

    release.cut_changelog({"core": "0.3.0", "browser": "0.3.0"})

    assert "- Core release." in release.changelog_section("core", "0.3.0")
    assert "- Browser release." in release.changelog_section("browser", "0.3.0")


def test_publish_force_packages_rethrows_existing_tags(
    release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(
        "## [core v2.2.0] - 2026-07-13\n\n- Core notes.\n\n"
        "## [browser v0.2.0] - 2026-07-13\n\n- Browser notes.\n\n"
        "## [cli v0.3.0] - 2026-07-13\n\n- CLI notes.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(release, "CHANGELOG", changelog)
    monkeypatch.setenv("FORCE_PACKAGES", "browser,cli")
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    versions = {"core": "2.2.0", "browser": "0.2.0", "cli": "0.3.0"}
    calls: list[tuple[str, ...]] = []

    def fake_run(*args: str, **_kw: object) -> str:
        calls.append(args)
        if args[:3] == ("git", "tag", "--list"):
            return "core-v2.2.0\nbrowser-v0.2.0\ncli-v0.3.0"
        raise AssertionError(f"unexpected run call: {args}")

    def read_version(path: Path) -> str:
        for name, pyproject in release.PACKAGES.items():
            if path == pyproject:
                return versions[name]
        raise AssertionError(path)

    monkeypatch.setattr(release, "run", fake_run)
    monkeypatch.setattr(release, "read_version", read_version)
    release.cmd_publish(None)  # type: ignore[arg-type]
    out = capsys.readouterr().out
    assert "Re-publishing browser-v0.2.0" in out
    assert "Re-publishing cli-v0.3.0" in out
    assert "released_packages=browser,cli" in out
    assert not any(c[:2] == ("git", "merge") for c in calls)
