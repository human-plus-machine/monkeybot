"""Unit tests for the emonk-harness linter."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.core.harness.linter import lint_config


def _write_cfg(tmp_path: Path, body: dict) -> Path:
    p = tmp_path / "h.yaml"
    p.write_text(yaml.safe_dump(body))
    return p


def test_lint_accepts_minimal_with_rules_disabled(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "SOUL.md").write_text("soul")
    (mem / "IDENTITY.md").write_text("identity")
    path = _write_cfg(
        tmp_path,
        {
            "version": "1",
            "agent": {"name": "demo"},
            "identity": {"dir": str(mem), "enforce_rules": False},
            "skills": {"dirs": []},
        },
    )
    code, findings = lint_config(path)
    assert code == 0, findings


def test_lint_requires_rules_md_when_enforced(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    mem.mkdir()
    path = _write_cfg(
        tmp_path,
        {
            "version": "1",
            "agent": {"name": "demo"},
            "identity": {"dir": str(mem), "enforce_rules": True},
            "skills": {"dirs": []},
        },
    )
    code, findings = lint_config(path)
    assert code == 1
    assert any("RULES.md" in f.message for f in findings)


def test_lint_reports_missing_skill_dir(tmp_path: Path) -> None:
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "RULES.md").write_text("- [R-1] DENY_TOOL: sudo")
    path = _write_cfg(
        tmp_path,
        {
            "version": "1",
            "agent": {"name": "demo"},
            "identity": {"dir": str(mem), "enforce_rules": True},
            "skills": {"dirs": ["./does-not-exist"]},
        },
    )
    code, findings = lint_config(path)
    assert code == 1
    assert any("does not exist" in f.message for f in findings)
