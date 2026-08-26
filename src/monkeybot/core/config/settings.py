"""Provider resolution and configuration types for the monkeybot harness."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Literal

from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict
from monkeybot.core.llm.provider import Provider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.huggingface import HuggingFaceProvider
from monkeybot.providers.nvidia import NvidiaProvider
from monkeybot.providers.ollama import OllamaProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.openrouter import OpenRouterProvider
from monkeybot.providers.sampling import resolve_model_sampling
from monkeybot.providers.vertex_claude import VertexClaudeProvider

logger = logging.getLogger(__name__)

_MODEL_PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google_vertexai",
    "vertex": "google_vertexai",
    "google-vertexai": "google_vertexai",
    "vertex-claude": "vertex_anthropic",
    "vertex_claude": "vertex_anthropic",
    # Hyphen form is the YAML / desktop-app id (unlike vertex-claude, which
    # maps onto a different internal id).
    "ollama_cloud": "ollama-cloud",
    "ollama_local": "ollama-local",
}

_OLLAMA_MODES: dict[str, Literal["auto", "cloud", "local"]] = {
    "ollama": "auto",
    "ollama-cloud": "cloud",
    "ollama-local": "local",
}


def normalize_model_provider(provider: str) -> str:
    """Map config aliases (e.g. ``gemini``, ``vertex-claude``) to canonical provider ids."""
    key = provider.strip().lower()
    return _MODEL_PROVIDER_ALIASES.get(key, key)


class ConfigError(Exception):
    """Configuration error with actionable message."""

    pass


@dataclass
class CustomMemoryFolder:
    """User-defined memory folder registered with the memory organizer classifier."""

    name: str
    description: str

    def __post_init__(self) -> None:
        import re

        if not re.match(r"^[a-z0-9-]+$", self.name):
            raise ConfigError(
                f"CustomMemoryFolder.name '{self.name}' is invalid. "
                "Use lowercase letters, digits, and hyphens only."
            )
        reserved = {"episodic", "semantic", "procedural", "working", "raw"}
        if self.name in reserved:
            raise ConfigError(
                f"CustomMemoryFolder.name '{self.name}' conflicts with a built-in folder. "
                f"Reserved names: {sorted(reserved)}"
            )


@dataclass
class SubagentConfig:
    """Configuration for a single named subagent persona."""

    name: str
    description: str
    skills: list[str]
    agent_md: str | None = None
    model: str | None = None
    vertex_location: str | None = None


@dataclass(frozen=True)
class SubagentSettings:
    """Global defaults for ``task`` subagent runs from ``subagents:`` in monkeybot.yaml."""

    # ponytail: 600s killed real implementer/story-writer children mid-work (PRT-5022).
    # A flat hour is the lazy ceiling: parent cancel still stops a runaway sooner, and
    # max_turns bounds the pathological case. Narrow it per-spawn only if an hour of a
    # wedged child actually costs something.
    timeout_sec: float = 3600.0
    max_turns: int = 1000
    vertex_google_search: bool = False


_DEFAULT_SUBAGENT_SETTINGS = SubagentSettings()


@dataclass(frozen=True)
class ProviderConfig:
    """Native streaming Provider plus model id."""

    provider: Provider
    model: str


def _resolve_gcp_project_id() -> str:
    """GCP project for Vertex providers."""
    from monkeybot.core.config.snapshot import current_env

    return (
        (os.getenv("GCP_PROJECT_ID") or "").strip()
        or current_env("VERTEX_AI_PROJECT_ID").strip()
        or current_env("ANTHROPIC_VERTEX_PROJECT_ID").strip()
        or (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    )


def get_provider_config(
    provider: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking_budget: int | None = None,
) -> ProviderConfig:
    """Resolve a Provider and model id from environment or explicit parameters."""
    from monkeybot.core.config.snapshot import current_env

    raw_provider = str(provider or current_env("MODEL_PROVIDER") or "google_vertexai")
    provider_key = normalize_model_provider(raw_provider)
    if provider_key == "fake":
        raise ValueError(
            "MODEL_PROVIDER=fake is for gateway/tests only; inject ScriptedFakeProvider directly "
            "or use the gateway fake provider path."
        )
    resolved_model = str(model_name or current_env("MODEL_NAME") or "gemini-2.5-flash")
    sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens)
    thinking_budget = (
        thinking_budget
        if thinking_budget is not None
        else int(current_env("MODEL_THINKING_BUDGET", "-1"))
    )
    if provider_key == "google_vertexai":
        return ProviderConfig(
            GeminiProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
                thinking_budget=thinking_budget,
            ),
            resolved_model,
        )
    if provider_key == "google_genai":
        api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            raise ValueError(
                "MODEL_PROVIDER=google_genai requires GEMINI_API_KEY. "
                "Set it in your environment or .env file."
            )
        return ProviderConfig(
            GeminiProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
                thinking_budget=thinking_budget,
                api_key=api_key,
            ),
            resolved_model,
        )
    if provider_key == "openai":
        return ProviderConfig(
            OpenAIProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    if provider_key == "anthropic":
        return ProviderConfig(
            ClaudeProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    if provider_key == "vertex_anthropic":
        project = _resolve_gcp_project_id()
        if not project:
            raise ValueError(
                "vertex_anthropic provider requires a GCP project. "
                "Set GCP_PROJECT_ID, VERTEX_AI_PROJECT_ID, ANTHROPIC_VERTEX_PROJECT_ID, "
                "or GOOGLE_CLOUD_PROJECT (or gcp.project_id in monkeybot.yaml)."
            )
        if current_env("VERTEX_AI_LOCATION"):
            logger.warning(
                "VERTEX_AI_LOCATION is no longer read for vertex_anthropic; "
                "set ANTHROPIC_VERTEX_REGION instead"
            )
        region = (current_env("ANTHROPIC_VERTEX_REGION") or "us-east5").strip() or "us-east5"
        return ProviderConfig(
            VertexClaudeProvider(
                project_id=project,
                region=region,
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    if provider_key == "huggingface":
        return ProviderConfig(
            HuggingFaceProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    ollama_mode = _OLLAMA_MODES.get(provider_key)
    if ollama_mode is not None:
        return ProviderConfig(
            OllamaProvider(
                mode=ollama_mode,
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
                thinking_budget=thinking_budget,
            ),
            resolved_model,
        )
    if provider_key == "nvidia":
        return ProviderConfig(
            NvidiaProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    if provider_key == "openrouter":
        return ProviderConfig(
            OpenRouterProvider(
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    if provider_key == "aws_bedrock":
        from monkeybot.providers.bedrock import BedrockProvider  # noqa: PLC0415

        aws_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        return ProviderConfig(
            BedrockProvider(
                aws_region=aws_region,
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
            ),
            resolved_model,
        )
    raise ValueError(
        f"Unsupported model provider: {provider_key}. "
        "Supported providers: google_vertexai, openai, anthropic, vertex_anthropic, "
        "huggingface, ollama, ollama-cloud, ollama-local, nvidia, openrouter, aws_bedrock"
    )


def _parse_subagent_entries(raw_entries: Any) -> list[SubagentConfig]:
    if not raw_entries or not isinstance(raw_entries, list):
        return []
    configs: list[SubagentConfig] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict subagent entry: %s", entry)
            continue
        if "name" not in entry or "description" not in entry:
            logger.warning("Skipping subagent entry missing 'name' or 'description': %s", entry)
            continue
        vloc = entry.get("vertex_location")
        if vloc is None and "location" in entry:
            vloc = entry.get("location")
        raw_skills = entry.get("skills", [])
        skills = list(raw_skills) if isinstance(raw_skills, list) else []
        agent_md = entry.get("agent_md") or entry.get("prompt_file")
        if agent_md is not None and not isinstance(agent_md, str):
            logger.warning("Skipping subagent %s: agent_md must be a string", entry.get("name"))
            continue
        configs.append(
            SubagentConfig(
                name=entry["name"],
                description=entry["description"],
                skills=skills,
                agent_md=agent_md.strip()
                if isinstance(agent_md, str) and agent_md.strip()
                else None,
                model=entry.get("model"),
                vertex_location=vloc,
            )
        )
    return configs


def _subagents_section(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the ``subagents:`` mapping, or empty dict when absent."""
    section = doc.get("subagents")
    if section is None:
        return {}
    if isinstance(section, list):
        raise ConfigError(
            "subagents must be a mapping with optional defaults and a 'personas' list; "
            "a bare list is no longer supported"
        )
    if not isinstance(section, dict):
        raise ConfigError(f"subagents must be a mapping, got {type(section).__name__}")
    return section


def subagent_settings_from_section(section: dict[str, Any]) -> SubagentSettings:
    """Parse a ``subagents:`` mapping into :class:`SubagentSettings`.

    Shared by :func:`get_subagent_settings` (reads the YAML file directly) and
    the ``RuntimeConfig`` snapshot builder (parses the already-merged doc), so
    the two never validate the section differently.
    """
    if not section:
        return _DEFAULT_SUBAGENT_SETTINGS

    timeout = _DEFAULT_SUBAGENT_SETTINGS.timeout_sec
    raw_timeout = section.get("timeout_sec")
    if raw_timeout is not None:
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise ConfigError(f"subagents.timeout_sec must be a number, got {raw_timeout!r}")
        timeout = max(1.0, float(raw_timeout))

    max_turns = _DEFAULT_SUBAGENT_SETTINGS.max_turns
    raw_turns = section.get("max_turns")
    if raw_turns is not None:
        if isinstance(raw_turns, bool) or not isinstance(raw_turns, int):
            raise ConfigError(f"subagents.max_turns must be an integer, got {raw_turns!r}")
        max_turns = max(1, raw_turns)

    vertex_raw = section.get("vertex_google_search")
    if vertex_raw is None:
        vertex = False
    elif isinstance(vertex_raw, bool):
        vertex = vertex_raw
    else:
        raise ConfigError(
            f"subagents.vertex_google_search must be true or false, got {vertex_raw!r}"
        )

    if "agent_md" in section:
        raise ConfigError(
            "subagents.agent_md was removed; set agent_md on each entry in "
            "subagents.personas, or omit subagent_type to inherit paths.agent_md"
        )

    return SubagentSettings(
        timeout_sec=timeout,
        max_turns=max_turns,
        vertex_google_search=vertex,
    )


def get_subagent_settings(config_path: str | None = None) -> SubagentSettings:
    """Global ``task`` defaults from ``subagents:`` in monkeybot.yaml (config-file only)."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    return subagent_settings_from_section(_subagents_section(doc))


def get_subagent_configs(config_path: str | None = None) -> list[SubagentConfig]:
    """Return named personas from ``subagents.personas`` in monkeybot.yaml."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    section = _subagents_section(doc)
    return _parse_subagent_entries(section.get("personas"))


def get_subagent_registry(config_path: str | None = None) -> dict[str, SubagentConfig]:
    """Named subagent personas keyed by ``name``; raises :class:`ConfigError` on duplicates."""
    registry: dict[str, SubagentConfig] = {}
    for cfg in get_subagent_configs(config_path):
        if cfg.name in registry:
            raise ConfigError(f"Duplicate subagent name in monkeybot.yaml: {cfg.name!r}")
        registry[cfg.name] = cfg
    return registry


def _bool_config_flag(
    doc: dict[str, Any],
    section: str,
    key: str,
    *,
    default: bool,
    label: str,
) -> bool:
    section_obj = doc.get(section)
    if not isinstance(section_obj, dict):
        return default
    raw = section_obj.get(key)
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"{label} must be true or false, got {raw!r}")


def auto_schema_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether storage backends should apply DDL on ``open()`` (``paths.auto_schema``).

    Defaults to ``True`` when the key is absent. Only read from monkeybot.yaml —
    not from environment variables.
    """
    _, doc = load_monkeybot_yaml_dict(config_path)
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return True
    raw = paths.get("auto_schema")
    if raw is None:
        return True
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"paths.auto_schema must be true or false, got {raw!r}")


def vertex_google_search_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether the main agent enables Gemini's native ``google_search`` grounding tool.

    Read from ``web_search.vertex_google_search`` in monkeybot.yaml. Additive to, and
    independent of, ``web_search.backend`` (the harness's pluggable DuckDuckGo/Tavily/
    Firecrawl custom tool). Defaults to ``False`` when absent. Config-file only.
    """
    _, doc = load_monkeybot_yaml_dict(config_path)
    return _bool_config_flag(
        doc,
        "web_search",
        "vertex_google_search",
        default=False,
        label="web_search.vertex_google_search",
    )


def subagent_vertex_google_search_from_config(config_path: str | None = None) -> bool:
    """Whether subagent runs enable Gemini's native ``google_search`` grounding tool.

    Read from ``subagents.vertex_google_search`` in monkeybot.yaml. Defaults to ``False``
    when absent. Config-file only — not exposed via environment variables.
    """
    return get_subagent_settings(config_path).vertex_google_search
