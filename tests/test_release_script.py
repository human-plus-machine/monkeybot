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


def test_packages_order_core_before_cli(release) -> None:
    assert list(release.PACKAGES) == ["core", "cli"]


def test_write_github_output_appends(release, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    out = tmp_path / "github_output"
    out.write_text("prior=1\n", encoding="utf-8")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    release.write_github_output("packages", "core,cli")
    assert out.read_text(encoding="utf-8") == "prior=1\npackages=core,cli\n"


def test_write_github_output_noop_without_env(release, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    release.write_github_output("packages", "core")  # must not raise
