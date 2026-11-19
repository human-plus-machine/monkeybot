"""Memory capture/recall kill switch.

Read from YAML only (first match wins):

1. ``memory.enabled``
2. ``memory_hook.enabled`` (legacy alias, only when ``memory.enabled`` is absent)
3. Default ``true``

``MONKEYBOT_MEMORY_HOOK_ENABLED`` is ignored. This switch disables capture,
wake-up, and prompt teaching. It is not a sandbox: allowlisted shell commands
can still reach a palace on disk.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from monkeybot.core.config.settings import ConfigError
from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

_FALSE = frozenset({"0", "false", "no", "off"})
_TRUE = frozenset({"1", "true", "yes", "on"})


def _parse_bool(raw: object, *, field: str) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        token = raw.strip().lower()
        if token in _FALSE:
            return False
        if token in _TRUE:
            return True
    raise ConfigError(f"{field} must be true or false, got {raw!r}")


def resolve_memory_enabled(doc: Mapping[str, Any] | None) -> bool:
    """Effective capture switch from a merged YAML document."""
    memory = doc.get("memory") if isinstance(doc, Mapping) else None
    if isinstance(memory, dict) and "enabled" in memory:
        return _parse_bool(memory.get("enabled"), field="memory.enabled")
    hook = doc.get("memory_hook") if isinstance(doc, Mapping) else None
    if isinstance(hook, dict) and "enabled" in hook:
        return _parse_bool(hook.get("enabled"), field="memory_hook.enabled")
    return True


def memory_enabled_flag(raw: str | None) -> bool:
    """Parse a snapshot ``memory.enabled`` token. Missing means on."""
    if raw is None:
        return True
    return _parse_bool(raw, field="memory.enabled")


def memory_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether MemPalace capture, recall, and search teaching are enabled."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    return resolve_memory_enabled(doc)
