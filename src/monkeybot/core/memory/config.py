"""YAML/env kill switch for memory capture and recall."""

from __future__ import annotations

import os

from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

_FALSE = frozenset({"0", "false", "no", "off"})
_TRUE = frozenset({"1", "true", "yes", "on"})


def _parse_bool(raw: object, *, label: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
    raise ConfigError(f"{label} must be true or false, got {raw!r}")


def memory_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether memory ingest/recall is enabled.

    Precedence: ``MONKEYBOT_MEMORY_HOOK_ENABLED`` env, then ``memory.enabled``,
    then legacy ``memory_hook.enabled``. Default true.
    """
    env_raw = os.environ.get("MONKEYBOT_MEMORY_HOOK_ENABLED")
    if env_raw is not None and env_raw.strip() != "":
        return _parse_bool(env_raw, label="MONKEYBOT_MEMORY_HOOK_ENABLED")
    _, doc = load_monkeybot_yaml_dict(config_path)
    section = doc.get("memory")
    if isinstance(section, dict) and "enabled" in section:
        return _parse_bool(section.get("enabled"), label="memory.enabled")
    legacy = doc.get("memory_hook")
    if isinstance(legacy, dict) and "enabled" in legacy:
        return _parse_bool(legacy.get("enabled"), label="memory_hook.enabled")
    return True
