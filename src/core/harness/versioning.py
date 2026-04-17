"""HarnessConfig schema versioning and migration from legacy bot.yaml layouts."""

from __future__ import annotations

from typing import Any

HARNESS_SCHEMA_VERSION = "1"


def migrate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Map legacy ``bot.yaml`` dict onto ``HarnessConfig`` v1 shape.

    Idempotent: if ``raw["version"] == "1"`` the input is returned as-is.
    Unknown keys are preserved in ``extensions``.
    """
    if raw.get("version") == HARNESS_SCHEMA_VERSION:
        return raw

    out: dict[str, Any] = {"version": HARNESS_SCHEMA_VERSION}

    legacy_agent = dict(raw.get("agent") or {})
    legacy_model = dict(raw.get("model") or {})

    agent: dict[str, Any] = {
        "name": legacy_agent.get("name") or raw.get("agent_name") or "emonk-agent",
    }
    if legacy_model:
        if "name" in legacy_model:
            agent["model"] = legacy_model["name"]
        if "provider" in legacy_model:
            agent["provider"] = legacy_model["provider"]
        if "temperature" in legacy_model:
            agent["temperature"] = legacy_model["temperature"]
        if "max_tokens" in legacy_model:
            agent["max_output_tokens"] = legacy_model["max_tokens"]
    out["agent"] = agent

    memory = dict(raw.get("memory") or {})
    identity: dict[str, Any] = {}
    if "dir" in memory:
        identity["dir"] = memory["dir"]
    out["identity"] = identity

    scheduler = dict(raw.get("scheduler") or {})
    if memory.get("dir") and "memory_dir" not in scheduler:
        scheduler["memory_dir"] = memory["dir"]
    if scheduler:
        out["scheduler"] = scheduler

    if "gateway" in raw:
        out["gateway"] = raw["gateway"]

    if "skills_dir" in legacy_agent:
        out["skills"] = {"dirs": [legacy_agent["skills_dir"]]}

    if "subagents" in raw:
        out["subagents"] = raw["subagents"]

    if "secrets" in raw or "gcp" in raw:
        extensions = dict(raw.get("extensions") or {})
        for k in ("secrets", "gcp"):
            if k in raw:
                extensions[f"legacy_{k}"] = raw[k]
        out["extensions"] = extensions

    return out
