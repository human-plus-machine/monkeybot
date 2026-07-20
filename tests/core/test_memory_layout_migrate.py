"""Tests for data/memory → memory layout migration."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.memory.migrate_layout import (
    discover_local_agent_roots,
    migrate_agent_memory_layout,
    migrate_all_local_agent_memory_layouts,
)


def _write_agent(root: Path, *, uri: str = "local://./data/memory") -> None:
    cfg = root / "monkeybot_config"
    cfg.mkdir(parents=True)
    (cfg / "monkeybot.yaml").write_text(
        "paths:\n"
        f"  memory_storage_uri: {uri}\n"
        "  workspace_root: ./workspace\n",
        encoding="utf-8",
    )
    (cfg / "command_allowlist.yaml").write_text(
        "allowed_path_prefixes:\n"
        "  - ./data/memory/\n"
        "  - ./data/memory\n"
        "  - ./skills/\n",
        encoding="utf-8",
    )
    legacy = root / "data" / "memory"
    legacy.mkdir(parents=True)
    (legacy / "INDEX.md").write_text("# idx\n", encoding="utf-8")
    (legacy / "episodic").mkdir()
    (legacy / "episodic" / "note.md").write_text("hello\n", encoding="utf-8")


def test_migrate_agent_moves_dir_and_rewrites_config(tmp_path: Path) -> None:
    agent = tmp_path / "default"
    _write_agent(agent)

    result = migrate_agent_memory_layout(agent)

    assert result["moved"] is True
    assert result["yaml_updated"] is True
    assert result["allowlist_updated"] is True
    assert (agent / "memory" / "INDEX.md").is_file()
    assert (agent / "memory" / "episodic" / "note.md").read_text() == "hello\n"
    assert not (agent / "data" / "memory").exists()
    yaml_text = (agent / "monkeybot_config" / "monkeybot.yaml").read_text()
    assert "local://./memory" in yaml_text
    assert "data/memory" not in yaml_text
    allow = (agent / "monkeybot_config" / "command_allowlist.yaml").read_text()
    assert "../memory" in allow
    assert "./data/memory" not in allow


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    agent = tmp_path / "default"
    _write_agent(agent)
    first = migrate_agent_memory_layout(agent)
    second = migrate_agent_memory_layout(agent)
    assert first["moved"] is True
    assert second["moved"] is False
    assert second["yaml_updated"] is False
    assert (agent / "memory" / "episodic" / "note.md").is_file()


def test_migrate_skips_when_both_dirs_exist(tmp_path: Path) -> None:
    agent = tmp_path / "default"
    _write_agent(agent)
    (agent / "memory").mkdir()
    (agent / "memory" / "keep.md").write_text("new\n", encoding="utf-8")

    result = migrate_agent_memory_layout(agent)

    assert result["moved"] is False
    assert result["action"] == "skipped_both_exist"
    assert (agent / "memory" / "keep.md").read_text() == "new\n"
    assert (agent / "data" / "memory" / "INDEX.md").is_file()
    # yaml still gets rewritten
    assert result["yaml_updated"] is True


def test_migrate_all_agents_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    agents = home / "agents"
    a = agents / "alpha"
    b = agents / "beta"
    _write_agent(a)
    _write_agent(b)
    # Outside home — only migrated when include= is passed
    outside = tmp_path / "outside-agent"
    _write_agent(outside)

    monkeypatch.setenv("MONKEYBOT_HOME", str(home))

    found = discover_local_agent_roots()
    assert {p.name for p in found} == {"alpha", "beta"}

    results = migrate_all_local_agent_memory_layouts(include=outside)
    by_name = {Path(r["agent_root"]).name: r for r in results}
    assert by_name["alpha"]["moved"] is True
    assert by_name["beta"]["moved"] is True
    assert by_name["outside-agent"]["moved"] is True
    assert (a / "memory" / "INDEX.md").is_file()
    assert (b / "memory" / "INDEX.md").is_file()
    assert (outside / "memory" / "INDEX.md").is_file()
