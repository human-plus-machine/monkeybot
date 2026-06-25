"""Provider resolution and configuration types for the MonkeyBot harness."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict
from monkeybot.core.llm.provider import Provider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.huggingface import HuggingFaceProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider

logger = logging.getLogger(__name__)

_MODEL_PROVIDER_ALIASES: dict[str, str] = {
    "gemini": "google_vertexai",
    "vertex": "google_vertexai",
    "google-vertexai": "google_vertexai",
    "vertex-claude": "vertex_anthropic",
    "vertex_claude": "vertex_anthropic",
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
    """Configuration for a single subagent."""

    name: str
    description: str
    skills: list[str]
    prompt_file: str | None = None
    model: str | None = None
    vertex_location: str | None = None


@dataclass(frozen=True)
class ProviderConfig:
    """Native streaming Provider plus model id."""

    provider: Provider
    model: str


def cache_enabled_from_env() -> bool:
    """True unless MODEL_ENABLE_CACHING is a falsey string."""
    raw = os.getenv("MODEL_ENABLE_CACHING", "true").strip().lower()
    return raw not in {"0", "false", "no", "off", ""}


def _resolve_gcp_project_id() -> str:
    """GCP project for Vertex providers."""
    return (
        (os.getenv("GCP_PROJECT_ID") or "").strip()
        or (os.getenv("VERTEX_AI_PROJECT_ID") or "").strip()
        or (os.getenv("ANTHROPIC_VERTEX_PROJECT_ID") or "").strip()
        or (os.getenv("GOOGLE_CLOUD_PROJECT") or "").strip()
    )


def get_provider_config(
    provider: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking_budget: int | None = None,
    cache_enabled: bool | None = None,
) -> ProviderConfig:
    """Resolve a Provider and model id from environment or explicit parameters."""
    raw_provider = str(provider or os.getenv("MODEL_PROVIDER") or "google_vertexai")
    provider_key = normalize_model_provider(raw_provider)
    if provider_key == "fake":
        raise ValueError(
            "MODEL_PROVIDER=fake is for gateway/tests only; inject ScriptedFakeProvider directly "
            "or use the gateway fake provider path."
        )
    resolved_model = str(model_name or os.getenv("MODEL_NAME") or "gemini-2.5-flash")
    temperature = temperature if temperature is not None else float(os.getenv("MODEL_TEMPERATURE", "0.7"))
    max_tokens = max_tokens if max_tokens is not None else int(os.getenv("MODEL_MAX_TOKENS", "60000"))
    thinking_budget = (
        thinking_budget if thinking_budget is not None else int(os.getenv("MODEL_THINKING_BUDGET", "-1"))
    )
    resolved_cache = cache_enabled_from_env() if cache_enabled is None else cache_enabled

    if provider_key == "google_vertexai":
        return ProviderConfig(
            GeminiProvider(
                temperature=temperature,
                max_output_tokens=max_tokens,
                thinking_budget=thinking_budget,
                cache_enabled=resolved_cache,
            ),
            resolved_model,
        )
    if provider_key == "openai":
        return ProviderConfig(OpenAIProvider(cache_enabled=resolved_cache), resolved_model)
    if provider_key == "anthropic":
        return ProviderConfig(ClaudeProvider(cache_enabled=resolved_cache), resolved_model)
    if provider_key == "vertex_anthropic":
        project = _resolve_gcp_project_id()
        if not project:
            raise ValueError(
                "vertex_anthropic provider requires a GCP project. "
                "Set GCP_PROJECT_ID, VERTEX_AI_PROJECT_ID, ANTHROPIC_VERTEX_PROJECT_ID, "
                "or GOOGLE_CLOUD_PROJECT (or gcp.project_id in monkeybot.yaml)."
            )
        if os.getenv("VERTEX_AI_LOCATION"):
            logger.warning(
                "VERTEX_AI_LOCATION is no longer read for vertex_anthropic; "
                "set ANTHROPIC_VERTEX_REGION instead"
            )
        region = (os.getenv("ANTHROPIC_VERTEX_REGION") or "us-east5").strip() or "us-east5"
        return ProviderConfig(
            VertexClaudeProvider(project_id=project, region=region, cache_enabled=resolved_cache),
            resolved_model,
        )
    if provider_key == "huggingface":
        return ProviderConfig(HuggingFaceProvider(cache_enabled=resolved_cache), resolved_model)
    if provider_key == "aws_bedrock":
        from monkeybot.providers.bedrock import BedrockClaudeProvider  # noqa: PLC0415

        aws_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        return ProviderConfig(
            BedrockClaudeProvider(aws_region=aws_region, cache_enabled=resolved_cache),
            resolved_model,
        )
    raise ValueError(
        f"Unsupported model provider: {provider_key}. "
        "Supported providers: google_vertexai, openai, anthropic, vertex_anthropic, "
        "huggingface, aws_bedrock"
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
        configs.append(
            SubagentConfig(
                name=entry["name"],
                description=entry["description"],
                skills=entry.get("skills", []),
                prompt_file=entry.get("prompt_file"),
                model=entry.get("model"),
                vertex_location=vloc,
            )
        )
    return configs


def get_subagent_configs(config_path: str | None = None) -> list[SubagentConfig]:
    """Return subagent configurations from ``subagents:`` in monkeybot.yaml."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    return _parse_subagent_entries(doc.get("subagents"))
