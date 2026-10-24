"""Model pricing lookup and cost estimation."""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Mapping
from typing import Any, Literal, TypeAlias

from monkeybot.providers.model_capabilities import BEDROCK_GEO_PREFIXES

_log = logging.getLogger(__name__)

Rates: TypeAlias = tuple[float, float, float, float]

# USD per 1M tokens: (input, output, cache_write, cache_read)
# Bedrock/Vertex ids are normalized onto these bare keys (see _normalize).
# Gemini-on-Vertex uses the same gemini-* keys when the model id matches.
# Anthropic cache_write is the 5-minute (1.25× input) rate; estimate_cost
# upgrades writes to 2× input when cache_retention is ``long`` (1h TTL).
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

_registered_pricing: dict[str, Rates] = {}
_config_pricing_cache: tuple[tuple[str | None, int | None, int | None], dict[str, Rates]] | None = (
    None
)
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
    missing = [f for f in _REQUIRED_RATE_FIELDS if entry.get(f) is None]
    if missing:
        raise ValueError(f"pricing.{model}: missing {', '.join(missing)}")
    values: list[float] = []
    for field in _RATE_FIELDS:
        raw = entry.get(field, 0.0)
        if raw is None:
            raw = 0.0
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
    if isinstance(raw, str) or not isinstance(raw, Iterable):
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
        for key in (model, *_entry_aliases(model, entry.get("aliases"))):
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
    for key in (name, *_entry_aliases(name, list(aliases))):
        _registered_pricing[key] = rates
        _warned_unknown_models.discard(key)


def unregister_model_pricing(model: str) -> None:
    """Drop a rate added by :func:`register_model_pricing` (aliases are separate ids)."""
    _registered_pricing.pop(model.strip(), None)


def _config_stamp() -> tuple[str | None, int | None, int | None]:
    from monkeybot.core.config.yaml_loader import resolve_monkeybot_config_path  # noqa: PLC0415

    path = resolve_monkeybot_config_path()
    if path is None:
        return (None, None, None)
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def _config_pricing() -> dict[str, Rates]:
    """``pricing:`` from monkeybot.yaml, re-read when the file changes. Never raises."""
    global _config_pricing_cache
    try:
        stamp = _config_stamp()
    except Exception:
        _log.debug("monkeybot.yaml pricing lookup failed", exc_info=True)
        return {}
    if _config_pricing_cache is not None and _config_pricing_cache[0] == stamp:
        return _config_pricing_cache[1]
    parsed: dict[str, Rates] = {}
    if stamp[0] is not None:
        try:
            from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict  # noqa: PLC0415

            _, doc = load_monkeybot_yaml_dict(stamp[0])
            parsed = parse_pricing_section(doc.get("pricing"))
        except Exception as exc:
            _log.warning("ignoring pricing in %s: %s", stamp[0], exc)
            parsed = {}
    _config_pricing_cache = (stamp, parsed)
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


def pricing_for(model: str) -> Rates:
    """Return (input, output, cache_write, cache_read) $/M for ``model``.

    Looks in the built-in table, ``pricing:`` in monkeybot.yaml, and
    :func:`register_model_pricing`; for the same id the later source wins.
    Exact match first, then longest known-prefix match, else zeros (warned once
    per model per process). Hosted ids (Bedrock, Vertex) are normalized too.
    Never raises.
    """
    table = {**MODEL_PRICING, **_config_pricing(), **_registered_pricing}
    for candidate in (model, normalize_model_id(model)):
        exact = table.get(candidate)
        if exact is not None:
            return exact

        best_key: str | None = None
        for key in table:
            if candidate.startswith(key) and (best_key is None or len(key) > len(best_key)):
                best_key = key

        if best_key is not None:
            return table[best_key]

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

    ``cache_retention`` selects the Anthropic cache-write tier when the model
    has a non-zero write rate (Claude family): ``short``/``none`` use the table's
    5-minute 1.25× rate; ``long`` uses 2× input for 1-hour TTL writes. OpenAI /
    Gemini keep ``cache_write=0`` so retention is a no-op for them.
    """
    rin, rout, rcw, rcr = pricing_for(model)
    if cache_retention == "long" and rin > 0 and rcw > 0:
        rcw = rin * _ANTHROPIC_LONG_CACHE_WRITE_MULTIPLIER
    return (
        input_tokens * rin
        + output_tokens * rout
        + cache_creation_tokens * rcw
        + cache_read_tokens * rcr
    ) / 1_000_000
