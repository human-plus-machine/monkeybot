"""Model pricing lookup and cost estimation."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Literal, TypeAlias

from monkeybot.providers.model_capabilities import BEDROCK_GEO_PREFIXES

_log = logging.getLogger(__name__)

Rates: TypeAlias = tuple[float, float, float, float]

# USD per 1M tokens: (input, output, cache_write, cache_read)
# Bedrock/Vertex ids are normalized onto these bare keys (see _normalize).
# Gemini-on-Vertex uses the same gemini-* keys when the model id matches.
# Anthropic cache_write is the 5-minute (1.25× input) rate; estimate_cost
# upgrades Claude-family writes to 2× input when cache_retention is ``long``.
# Built-in defaults only: deployments add models via ``pricing:`` in
# monkeybot.yaml or :func:`register_model_pricing`.
MODEL_PRICING: dict[str, Rates] = {
    "claude-sonnet-4": (3.00, 15.00, 3.75, 0.30),
    "claude-opus-4": (15.00, 75.00, 18.75, 1.50),
    "claude-haiku-4-5": (1.00, 5.00, 1.25, 0.10),
    "gpt-5": (1.25, 10.00, 0.00, 0.125),
    "gemini-2.5-flash": (0.30, 2.50, 0.00, 0.075),
    "gemini-3-flash-preview": (0.30, 2.50, 0.00, 0.075),
    # Bedrock Converse models (USD/M tokens; cache columns unused today).
    # grok-4 prefix-matches grok-4.6, grok-4.1, etc. at current list rates.
    "grok-4": (3.00, 15.00, 0.00, 0.00),
    "nova-pro": (0.80, 3.20, 0.00, 0.00),
    "nova-lite": (0.06, 0.24, 0.00, 0.00),
    "llama3-3-70b-instruct": (0.72, 0.72, 0.00, 0.00),
}

# Anthropic 1-hour ephemeral cache writes are 2× base input (docs: prompt caching).
_ANTHROPIC_LONG_CACHE_WRITE_MULTIPLIER = 2.0

_RATE_FIELDS = ("input", "output", "cache_write", "cache_read")
_REQUIRED_RATE_FIELDS = ("input", "output")

_FileStamp: TypeAlias = tuple[str | None, int | None, int | None]
_IncludeStamp: TypeAlias = tuple[str, int | None, int | None]

_registered_pricing: dict[str, Rates] = {}
_config_pricing_cache: tuple[_FileStamp, tuple[_IncludeStamp, ...], dict[str, Rates]] | None = None
_resolved_key: tuple[str, str, str] | None = None
_resolved_path: Path | None = None
_warned_unknown_models: set[str] = set()

__all__ = [
    "MODEL_PRICING",
    "Rates",
    "estimate_cost",
    "normalize_model_id",
    "parse_pricing_section",
    "pricing_for",
    "register_model_pricing",
    "unregister_model_pricing",
]


def _entry_rates(model: str, entry: Mapping[str, Any]) -> Rates:
    unknown = sorted(set(entry) - set(_RATE_FIELDS) - {"aliases"})
    if unknown:
        raise ValueError(f"pricing.{model}: unknown field(s) {', '.join(unknown)}")
    missing = [f for f in _REQUIRED_RATE_FIELDS if f not in entry or entry[f] is None]
    if missing:
        raise ValueError(f"pricing.{model}: missing {', '.join(missing)}")
    values: list[float] = []
    for field in _RATE_FIELDS:
        if field not in entry:
            values.append(0.0)
            continue
        raw = entry[field]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"pricing.{model}.{field} must be a number, got {raw!r}")
        value = float(raw)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"pricing.{model}.{field} must be a non-negative number, got {raw!r}")
        values.append(value)
    return (values[0], values[1], values[2], values[3])


def _entry_aliases(model: str, raw: object) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes, Mapping)) or not isinstance(raw, Iterable):
        raise ValueError(f"pricing.{model}.aliases must be a list of model ids")
    aliases: list[str] = []
    for alias in raw:
        if not isinstance(alias, str) or not alias.strip():
            raise ValueError(f"pricing.{model}.aliases entries must be non-empty strings")
        aliases.append(alias.strip())
    return aliases


def parse_pricing_section(section: object) -> dict[str, Rates]:
    """Parse ``pricing:`` from monkeybot.yaml into ``{model_id: rates}``.

    Each key is a model id. Values are USD per 1M tokens: ``input`` and
    ``output`` are required, ``cache_write`` and ``cache_read`` default to 0,
    and ``aliases`` lists extra ids priced the same way. Raises ``ValueError``.
    """
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ValueError("pricing must be a mapping of model id to rates")
    parsed: dict[str, Rates] = {}
    for raw_model, entry in section.items():
        model = str(raw_model).strip()
        if not model:
            raise ValueError("pricing keys must be non-empty model ids")
        if not isinstance(entry, Mapping):
            raise ValueError(f"pricing.{model} must be a mapping of rates")
        rates = _entry_rates(model, entry)
        keys: list[str] = []
        for key in (model, *_entry_aliases(model, entry.get("aliases"))):
            if key not in keys:
                keys.append(key)
        for key in keys:
            if key in parsed:
                raise ValueError(f"pricing.{key} is listed more than once")
            parsed[key] = rates
    return parsed


def register_model_pricing(
    model: str,
    *,
    input: float,
    output: float,
    cache_write: float = 0.0,
    cache_read: float = 0.0,
    aliases: Iterable[str] = (),
) -> None:
    """Price ``model`` and ``aliases`` (USD per 1M tokens) for this process.

    Registered rates win over ``pricing:`` in monkeybot.yaml and the built-in
    table for the same id. Subagent and scheduler workers run in their own
    processes and only see monkeybot.yaml, so register there too or use YAML.
    """
    name = model.strip()
    if not name:
        raise ValueError("model id must be non-empty")
    rates = _entry_rates(
        name,
        {"input": input, "output": output, "cache_write": cache_write, "cache_read": cache_read},
    )
    for key in (name, *_entry_aliases(name, aliases)):
        _registered_pricing[key] = rates
        _warned_unknown_models.discard(key)


def unregister_model_pricing(model: str) -> None:
    """Drop a rate added by :func:`register_model_pricing` (aliases are separate ids)."""
    _registered_pricing.pop(model.strip(), None)


def _resolution_key() -> tuple[str, str, str]:
    """Env that selects the config file.

    The working directory is part of the key only when a path is relative or
    nothing is pinned. Absolute pins stay valid after a worker chdirs.
    """
    explicit = os.environ.get("MONKEYBOT_CONFIG", "").strip()
    agent = os.environ.get("MONKEYBOT_AGENT_ROOT", "").strip()
    explicit_abs = bool(explicit) and Path(explicit).expanduser().is_absolute()
    agent_abs = bool(agent) and Path(agent).expanduser().is_absolute()
    # Cwd changes which file we read when nothing is pinned, the agent root is
    # relative, or a relative config path has no agent root to join to.
    needs_cwd = (
        (not explicit and not agent)
        or (bool(agent) and not agent_abs)
        or (bool(explicit) and not explicit_abs and not agent)
    )
    cwd = os.path.abspath(os.getcwd()) if needs_cwd else ""
    return (explicit, agent, cwd)


def _pinned_config_appeared(key: tuple[str, str, str]) -> bool:
    """True when a pinned config file showed up after a cached miss.

    One stat, no directory walk, and no repeat of the missing-path warning.
    Cwd discovery stays cached until the working directory changes.
    """
    explicit, agent, _cwd = key
    # An explicit path wins and does not fall through to the agent-root file.
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute() and agent:
            path = Path(agent).expanduser() / path
        return path.is_file()
    if agent:
        default = Path(agent).expanduser() / "monkeybot_config" / "monkeybot.yaml"
        return default.is_file()
    return False


def _pricing_config_path() -> Path | None:
    """The monkeybot.yaml this process should price from.

    A pinned agent root or ``MONKEYBOT_CONFIG`` wins over the working directory.
    Subagent workers chdir into the workspace after bootstrap, and that
    workspace may contain a different ``monkeybot_config``.
    """
    global _resolved_key, _resolved_path
    key = _resolution_key()
    if _resolved_key == key and _resolved_path is not None:
        return _resolved_path
    if _resolved_key == key and not _pinned_config_appeared(key):
        return None
    if key[1]:
        from monkeybot.core.layout import resolve_config_path  # noqa: PLC0415

        path = resolve_config_path(agent_root=Path(key[1]).expanduser().resolve())
    else:
        from monkeybot.core.config.yaml_loader import (  # noqa: PLC0415
            resolve_monkeybot_config_path,
        )

        path = resolve_monkeybot_config_path()
    _resolved_key = key
    _resolved_path = path
    return path


def _file_stamp(path: Path) -> _IncludeStamp:
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _include_paths(primary: Path, root: Mapping[str, Any]) -> list[Path]:
    raw = root.get("includes")
    if not isinstance(raw, list):
        return []
    parent = primary.parent
    paths: list[Path] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            paths.append((parent / item.strip()).resolve())
    return paths


def _config_pricing() -> dict[str, Rates]:
    """``pricing:`` from monkeybot.yaml, re-read when the file or an include changes.

    Never raises. A bad ``pricing:`` block is ignored until the file changes.
    """
    global _config_pricing_cache
    try:
        path = _pricing_config_path()
    except Exception:
        _log.debug("monkeybot.yaml pricing lookup failed", exc_info=True)
        return {}
    if path is None:
        stamp: _FileStamp = (None, None, None)
        if (
            _config_pricing_cache is not None
            and _config_pricing_cache[0] == stamp
            and _config_pricing_cache[1] == ()
        ):
            return _config_pricing_cache[2]
        _config_pricing_cache = (stamp, (), {})
        return {}

    primary = _file_stamp(path)
    cached = _config_pricing_cache
    if cached is not None and cached[0] == primary:
        fresh_includes = tuple(_file_stamp(Path(item[0])) for item in cached[1])
        if fresh_includes == cached[1]:
            return cached[2]

    parsed: dict[str, Rates] = {}
    include_stamps: tuple[_IncludeStamp, ...] = ()
    try:
        from monkeybot.core.config.runtime_env import (  # noqa: PLC0415
            _load_yaml_file,
            _merge_with_includes,
        )

        root = _load_yaml_file(path)
        include_stamps = tuple(_file_stamp(inc) for inc in _include_paths(path, root))
        merged = _merge_with_includes(path, root)
        parsed = parse_pricing_section(merged.get("pricing"))
    except Exception as exc:
        _log.warning("ignoring pricing in %s: %s", path, exc)
        parsed = {}
    _config_pricing_cache = (primary, include_stamps, parsed)
    return parsed


def normalize_model_id(model: str) -> str:
    """Strip hosting decorations so Bedrock/Vertex ids match :data:`MODEL_PRICING` keys.

    ``us.anthropic.claude-sonnet-4-6-v1:0`` -> ``claude-sonnet-4-6``.
    """
    name = model.strip()
    # ponytail: string surgery, not a model registry. Add a real alias map only if
    # a provider ships an id whose bare name differs from the Anthropic/OpenAI one.
    name = name.split("/")[-1]  # litellm-style "aws_bedrock/us.anthropic.claude-..."
    name = name.split(":", 1)[0]  # bedrock version suffix ":0"
    for region in sorted(BEDROCK_GEO_PREFIXES, key=len, reverse=True):
        prefix = f"{region}."
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    for vendor in ("anthropic.", "meta.", "amazon.", "google.", "xai."):
        if name.startswith(vendor):
            name = name[len(vendor) :]
            break
    if name.endswith("-v1"):
        name = name[: -len("-v1")]
    return name


def _best_rates(table: Mapping[str, Rates], model: str) -> Rates | None:
    """Longest id that matches the raw or normalized model.

    An exact id beats another id of the same length. That keeps ``org/model``
    from sticking on a short prefix of the raw string when the normalized name
    is priced on its own.
    """
    candidates: list[str] = []
    for candidate in (model, normalize_model_id(model)):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    best: tuple[int, bool, str] | None = None
    for candidate in candidates:
        for key in table:
            if not key or not candidate.startswith(key):
                continue
            rank = (len(key), key == candidate)
            if best is None or rank > (best[0], best[1]):
                best = (rank[0], rank[1], key)
    if best is None:
        return None
    return table[best[2]]


def pricing_for(model: str) -> Rates:
    """Return (input, output, cache_write, cache_read) $/M for ``model``.

    Looks in the built-in table, ``pricing:`` in monkeybot.yaml, and
    :func:`register_model_pricing`; for the same id the later source wins.
    The longest matching id wins across the raw id and the normalized hosted
    id; an exact id beats a different id of the same length. Unknown models
    cost zero and warn once per model per process. Never raises.
    """
    table = {**MODEL_PRICING, **_config_pricing(), **_registered_pricing}
    found = _best_rates(table, model)
    if found is not None:
        return found

    if model not in _warned_unknown_models:
        _warned_unknown_models.add(model)
        _log.warning(
            "unknown model for pricing lookup: %s (add it under pricing: in monkeybot.yaml)",
            model,
        )
    return (0.0, 0.0, 0.0, 0.0)


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
    cache_retention: Literal["none", "short", "long"] = "short",
) -> float:
    """Return estimated USD cost using :func:`pricing_for`. Unknown model → 0.0.

    ``cache_retention`` selects the Anthropic cache-write tier for Claude-family
    ids (the normalized name starts with ``claude``) when the write rate is
    non-zero: ``short``/``none`` use the configured 5-minute rate; ``long`` uses
    2× input for 1-hour TTL writes. Other models use ``cache_write`` as configured.
    """
    rin, rout, rcw, rcr = pricing_for(model)
    if (
        cache_retention == "long"
        and rin > 0
        and rcw > 0
        and normalize_model_id(model).startswith("claude")
    ):
        rcw = rin * _ANTHROPIC_LONG_CACHE_WRITE_MULTIPLIER
    return (
        input_tokens * rin
        + output_tokens * rout
        + cache_creation_tokens * rcw
        + cache_read_tokens * rcr
    ) / 1_000_000
