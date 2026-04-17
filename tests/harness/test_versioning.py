"""Unit tests for the legacy bot.yaml → harness v1 migration."""

from __future__ import annotations

from src.core.harness import HarnessConfig, migrate_config


def test_migrate_passthrough_when_already_v1() -> None:
    raw = {"version": "1", "agent": {"name": "x"}}
    assert migrate_config(raw) == raw


def test_migrate_bot_yaml_shape() -> None:
    legacy = {
        "agent": {"name": "bot", "skills_dir": "./skills"},
        "model": {"name": "gemini-2.5-pro", "provider": "google_vertexai", "temperature": 0.4, "max_tokens": 4096},
        "memory": {"dir": "./mem"},
        "scheduler": {"cadence": "*/5 * * * *"},
        "subagents": [{"name": "impl", "description": "d"}],
    }
    migrated = migrate_config(legacy)
    cfg = HarnessConfig.from_mapping(migrated)
    assert cfg.agent.name == "bot"
    assert cfg.agent.model == "gemini-2.5-pro"
    assert cfg.agent.max_output_tokens == 4096
    assert cfg.identity.dir == "./mem"
    assert cfg.scheduler.cadence == "*/5 * * * *"
    assert cfg.skills.dirs == ["./skills"]
    assert [s.name for s in cfg.subagents] == ["impl"]
