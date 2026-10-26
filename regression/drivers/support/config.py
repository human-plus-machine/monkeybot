"""Scenario config snapshot and the reloadable live config."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

from monkeybot.core.config.runtime_env import _deep_merge, _flatten_config
from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.snapshot import (
    RuntimeConfig,
    _snapshot_with_env,
    build_runtime_config,
    context_window_tokens,
)
from monkeybot.core.context import TurnContext, refresh_tools_after_mcp_change
from monkeybot.core.mcp.mcp_client import MCPClient
from regression.drivers.bundles.protocol import LoopDriverError
from regression.drivers.support.paths import _BASE_CONFIG
from regression.schema.scenario import Scenario
from regression.schema.steps import ReloadedConfig


def _config_overrides(scenario: Scenario) -> tuple[dict[str, Any], dict[str, str]]:
    if scenario.config is None:
        return {}, {}
    dumped = scenario.config.model_dump()
    dumped.pop("scripted_provider", None)
    raw_env = dumped.pop("env", {})
    env = dict(raw_env) if isinstance(raw_env, dict) else {}
    return dumped, env


def _merge_overrides(base: Mapping[str, Any], overrides: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = _deep_merge({}, dict(base))
    for key, value in overrides.items():
        if "." in key or not isinstance(value, dict):
            _assign_dotted(merged, key, value)
            continue
        current = merged.get(key)
        if isinstance(current, dict):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _assign_dotted(doc: dict[str, Any], dotted: str, value: object) -> None:
    parts = [part for part in dotted.split(".") if part]
    if not parts:
        raise LoopDriverError(f"config override {dotted!r} is empty")
    cursor = doc
    for part in parts[:-1]:
        child = cursor.get(part)
        if not isinstance(child, dict):
            child = {}
            cursor[part] = child
        cursor = child
    cursor[parts[-1]] = value


def _with_gate_enabled(cfg: RuntimeConfig) -> RuntimeConfig:
    gate = replace(cfg.coding.verification_gate, enabled=True)
    return replace(cfg, coding=replace(cfg.coding, verification_gate=gate))


def _snapshot_from_document(
    merged: Mapping[str, Any],
    env: Mapping[str, str],
    *,
    config_path: Path,
    workdir: Path,
    revision: int,
) -> RuntimeConfig:
    """Write ``merged`` and build a snapshot. The file, then ``env``, win over process pins."""
    config_path.write_text(yaml.safe_dump(dict(merged), sort_keys=False), encoding="utf-8")
    try:
        cfg = build_runtime_config(
            config_path=config_path,
            agent_root=workdir,
            revision=revision,
        )
    except (ConfigError, ValueError, OSError, yaml.YAMLError) as exc:
        raise LoopDriverError(f"could not build scenario config: {exc}") from exc
    overlay = dict(cfg.env_values)
    overlay.update(_flatten_config(merged))
    overlay.update(dict(env))
    if overlay != dict(cfg.env_values):
        cfg = _snapshot_with_env(cfg, overlay, base_digest=cfg.digest)
    return cfg


def _scenario_config(
    scenario: Scenario, workdir: Path
) -> tuple[RuntimeConfig, dict[str, str], dict[str, Any], Path]:
    """Merge ``config`` over the base file and return the snapshot, ``env``, and document."""
    if not _BASE_CONFIG.is_file():
        raise LoopDriverError(f"base agent config is missing: {_BASE_CONFIG}")
    loaded = yaml.safe_load(_BASE_CONFIG.read_text(encoding="utf-8"))
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise LoopDriverError("agent.base.yaml root must be a mapping")
    overrides, env = _config_overrides(scenario)
    merged = _merge_overrides(loaded, overrides)
    config_path = workdir / "monkeybot.yaml"
    cfg = _snapshot_from_document(merged, env, config_path=config_path, workdir=workdir, revision=0)
    return cfg, env, merged, config_path


@dataclass
class _LiveConfig:
    """Scenario config the ``reload_config`` step can rebuild for the next message."""

    cfg: RuntimeConfig
    env: dict[str, str]
    merged: dict[str, Any]
    path: Path
    workdir: Path
    gate: bool
    dirty: bool = False
    mcp: MCPClient | None = None
    mcp_path: Path | None = None
    tools: list[Any] | None = None
    catalog: tuple[str, ...] = ()
    turn: TurnContext | None = None
    mcp_pending: bool = False

    def reload(self, updates: Mapping[str, Any] | None) -> ReloadedConfig:
        if updates:
            self.merged = _merge_overrides(self.merged, updates)
        self.cfg = _snapshot_from_document(
            self.merged,
            self.env,
            config_path=self.path,
            workdir=self.workdir,
            revision=self.cfg.revision + 1,
        )
        if self.gate:
            self.cfg = _with_gate_enabled(self.cfg)
        self.dirty = True
        self.mcp_pending = self.mcp is not None
        applied = sorted(str(key) for key in updates) if updates else []
        return ReloadedConfig(
            revision=self.cfg.revision,
            digest=self.cfg.digest,
            applied=applied,
            context_window_tokens=context_window_tokens(self.cfg),
        )

    async def refresh_mcp(self) -> None:
        """Re-read the workspace ``mcp.json`` after ``reload_config``.

        Invalid JSON leaves the live sessions up. A changed spec reconnects
        before the next provider call in this turn.
        """
        client = self.mcp
        path = self.mcp_path
        self.mcp_pending = False
        if client is None or path is None:
            return
        await client.apply_catalog_diff(path)
        if self.turn is not None:
            refresh_tools_after_mcp_change(self.turn, client)
