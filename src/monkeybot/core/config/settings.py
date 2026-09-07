"""Provider resolution and configuration types for the monkeybot harness."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from monkeybot.core.config.yaml_loader import (
    load_monkeybot_yaml_dict,
    resolve_monkeybot_config_path,
)
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

if TYPE_CHECKING:
    from monkeybot.core.config.snapshot import RuntimeConfig

logger = logging.getLogger(__name__)
_warned_legacy_transcript_env = False
# Stamp is (resolved path, mtime_ns, size). Include-file edits are not in the
# stamp; this flag lives on the primary yaml so a primary mtime change is enough.
_transcript_enabled_cache: tuple[tuple[str | None, int | None, int | None], bool] | None = None

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


# Rank order is the source of truth. `steer` is reserved for an optional later
# phase; Phases 4–6 ship nudge / replan / block.
VERIFIER_SEVERITY_ORDER: tuple[str, ...] = ("none", "nudge", "replan", "steer", "block")
VERIFIER_SEVERITY_RANK: dict[str, int] = {
    name: index for index, name in enumerate(VERIFIER_SEVERITY_ORDER)
}
_VERIFIER_SEVERITIES = frozenset(VERIFIER_SEVERITY_ORDER)


@dataclass(frozen=True)
class VerifierLedgerConfig:
    """``verifier.ledger`` — classifier that maintains the goal ledger."""

    enabled: bool = False
    model: str = "gemini-2.5-flash"
    max_entries_per_thread: int = 64


@dataclass(frozen=True)
class VerifierTrackerConfig:
    """``verifier.tracker`` — deterministic in-loop suspicion signals."""

    enabled: bool = False
    suspicion_threshold: int = 3
    min_turn_before_verdict: int = 3


@dataclass(frozen=True)
class VerifierJudgeConfig:
    """``verifier.judge`` — async LLM verdicts off the critical path."""

    enabled: bool = False
    model: str = "gemini-2.5-flash"
    max_verdicts_per_message: int = 3
    min_turns_between_verdicts: int = 2
    max_spend_ratio: float = 0.25
    tail_grace_s: float = 0.0


@dataclass(frozen=True)
class VerifierEscalationConfig:
    """``verifier.escalation`` — cap on how hard the verifier may intervene."""

    max_severity: str = "nudge"


@dataclass(frozen=True)
class VerifierConfig:
    """YAML-only ``verifier:`` section. Absent or empty → every flag off."""

    enabled: bool = False
    ledger: VerifierLedgerConfig = field(default_factory=VerifierLedgerConfig)
    tracker: VerifierTrackerConfig = field(default_factory=VerifierTrackerConfig)
    judge: VerifierJudgeConfig = field(default_factory=VerifierJudgeConfig)
    escalation: VerifierEscalationConfig = field(default_factory=VerifierEscalationConfig)


_DEFAULT_VERIFIER_CONFIG = VerifierConfig()


@dataclass(frozen=True)
class ProviderConfig:
    """Native streaming Provider plus model id."""

    provider: Provider
    model: str


def _resolve_gcp_project_id(config: RuntimeConfig | None = None) -> str:
    """GCP project for Vertex providers."""
    from monkeybot.core.config.snapshot import env_value_or_current

    return (
        env_value_or_current(config, "GCP_PROJECT_ID").strip()
        or env_value_or_current(config, "VERTEX_AI_PROJECT_ID").strip()
        or env_value_or_current(config, "ANTHROPIC_VERTEX_PROJECT_ID").strip()
        or env_value_or_current(config, "GOOGLE_CLOUD_PROJECT").strip()
    )


def get_provider_config(
    provider: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking_budget: int | None = None,
    config: RuntimeConfig | None = None,
) -> ProviderConfig:
    """Resolve a Provider and model id from a pinned snapshot, environment, or explicit parameters."""
    from monkeybot.core.config.snapshot import env_value_or_current

    raw_provider = str(
        provider or env_value_or_current(config, "MODEL_PROVIDER") or "google_vertexai"
    )
    provider_key = normalize_model_provider(raw_provider)
    if provider_key == "fake":
        raise ValueError(
            "MODEL_PROVIDER=fake is for gateway/tests only; inject ScriptedFakeProvider directly "
            "or use the gateway fake provider path."
        )
    resolved_model = str(
        model_name or env_value_or_current(config, "MODEL_NAME") or "gemini-2.5-flash"
    )
    sampling = resolve_model_sampling(temperature=temperature, max_tokens=max_tokens, config=config)
    thinking_budget = (
        thinking_budget
        if thinking_budget is not None
        else int(env_value_or_current(config, "MODEL_THINKING_BUDGET", "-1"))
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
        project = _resolve_gcp_project_id(config)
        if not project:
            raise ValueError(
                "vertex_anthropic provider requires a GCP project. "
                "Set GCP_PROJECT_ID, VERTEX_AI_PROJECT_ID, ANTHROPIC_VERTEX_PROJECT_ID, "
                "or GOOGLE_CLOUD_PROJECT (or gcp.project_id in monkeybot.yaml)."
            )
        if env_value_or_current(config, "VERTEX_AI_LOCATION"):
            logger.warning(
                "VERTEX_AI_LOCATION is no longer read for vertex_anthropic; "
                "set ANTHROPIC_VERTEX_REGION instead"
            )
        region = (
            env_value_or_current(config, "ANTHROPIC_VERTEX_REGION") or "us-east5"
        ).strip() or "us-east5"
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
        keep_alive, num_ctx = ollama_options_from_config()
        return ProviderConfig(
            OllamaProvider(
                mode=ollama_mode,
                temperature=sampling.temperature,
                max_tokens=sampling.max_tokens,
                thinking_budget=thinking_budget,
                keep_alive=keep_alive,
                num_ctx=num_ctx,
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


def _persona_registry(entries: list[SubagentConfig]) -> dict[str, SubagentConfig]:
    """Key personas by ``name``; raise :class:`ConfigError` on duplicates."""
    registry: dict[str, SubagentConfig] = {}
    for cfg in entries:
        if cfg.name in registry:
            raise ConfigError(f"Duplicate subagent name in monkeybot.yaml: {cfg.name!r}")
        registry[cfg.name] = cfg
    return registry


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


def get_subagent_settings(
    config_path: str | None = None,
    *,
    config: RuntimeConfig | None = None,
) -> SubagentSettings:
    """Global ``task`` defaults from ``subagents:`` in monkeybot.yaml.

    When ``config`` (a pinned ``RuntimeConfig`` snapshot) is given, its
    ``subagent_settings`` field is returned instead of re-reading the YAML
    file, so an in-flight turn spawning a subagent stays on the revision it
    was pinned to rather than picking up a file edit mid-turn.
    """
    if config is not None:
        return config.subagent_settings
    _, doc = load_monkeybot_yaml_dict(config_path)
    return subagent_settings_from_section(_subagents_section(doc))


def get_subagent_configs(config_path: str | None = None) -> list[SubagentConfig]:
    """Return named personas from ``subagents.personas`` in monkeybot.yaml."""
    _, doc = load_monkeybot_yaml_dict(config_path)
    section = _subagents_section(doc)
    return _parse_subagent_entries(section.get("personas"))


def get_subagent_registry(
    config_path: str | None = None,
    *,
    config: RuntimeConfig | None = None,
) -> dict[str, SubagentConfig]:
    """Named subagent personas keyed by ``name``; raises :class:`ConfigError` on duplicates.

    When ``config`` (a pinned ``RuntimeConfig`` snapshot) is given, its
    ``subagents`` field is returned instead of re-reading the YAML file, so
    ``GatewayRuntime.apply`` cannot pick up a third disk state written between
    ``prepare_reload`` and ``apply``.
    """
    if config is not None:
        return dict(config.subagents)
    return _persona_registry(get_subagent_configs(config_path))


def _verifier_section(doc: dict[str, Any]) -> dict[str, Any]:
    """Return the ``verifier:`` mapping, or empty dict when absent."""
    section = doc.get("verifier")
    if section is None:
        return {}
    if not isinstance(section, dict):
        raise ConfigError(f"verifier must be a mapping, got {type(section).__name__}")
    return section


def _verifier_bool(raw: Any, label: str, default: bool) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    raise ConfigError(f"{label} must be true or false, got {raw!r}")


def _verifier_int(raw: Any, label: str, default: int, *, min_value: int = 0) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(f"{label} must be an integer, got {raw!r}")
    if raw < min_value:
        raise ConfigError(f"{label} must be >= {min_value}, got {raw}")
    return raw


def _verifier_float(raw: Any, label: str, default: float, *, min_value: float = 0.0) -> float:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{label} must be a number, got {raw!r}")
    value = float(raw)
    if value < min_value:
        raise ConfigError(f"{label} must be >= {min_value}, got {raw}")
    return value


def _verifier_str(raw: Any, label: str, default: str) -> str:
    if raw is None:
        return default
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{label} must be a non-empty string, got {raw!r}")
    return raw.strip()


def _verifier_nested(section: dict[str, Any], key: str) -> dict[str, Any]:
    raw = section.get(key)
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"verifier.{key} must be a mapping, got {type(raw).__name__}")
    return raw


def verifier_config_from_section(section: dict[str, Any]) -> VerifierConfig:
    """Parse a ``verifier:`` mapping into :class:`VerifierConfig`.

    Shared by :func:`get_verifier_config` and the ``RuntimeConfig`` snapshot
    builder so the two never validate the section differently.
    """
    if not section:
        return _DEFAULT_VERIFIER_CONFIG

    defaults = _DEFAULT_VERIFIER_CONFIG
    ledger_raw = _verifier_nested(section, "ledger")
    tracker_raw = _verifier_nested(section, "tracker")
    judge_raw = _verifier_nested(section, "judge")
    escalation_raw = _verifier_nested(section, "escalation")

    severity = _verifier_str(
        escalation_raw.get("max_severity"),
        "verifier.escalation.max_severity",
        defaults.escalation.max_severity,
    )
    if severity not in _VERIFIER_SEVERITIES:
        allowed = ", ".join(VERIFIER_SEVERITY_ORDER)
        raise ConfigError(
            f"verifier.escalation.max_severity must be one of {allowed}, got {severity!r}"
        )

    return VerifierConfig(
        enabled=_verifier_bool(section.get("enabled"), "verifier.enabled", defaults.enabled),
        ledger=VerifierLedgerConfig(
            enabled=_verifier_bool(
                ledger_raw.get("enabled"), "verifier.ledger.enabled", defaults.ledger.enabled
            ),
            model=_verifier_str(
                ledger_raw.get("model"), "verifier.ledger.model", defaults.ledger.model
            ),
            max_entries_per_thread=_verifier_int(
                ledger_raw.get("max_entries_per_thread"),
                "verifier.ledger.max_entries_per_thread",
                defaults.ledger.max_entries_per_thread,
                min_value=1,
            ),
        ),
        tracker=VerifierTrackerConfig(
            enabled=_verifier_bool(
                tracker_raw.get("enabled"),
                "verifier.tracker.enabled",
                defaults.tracker.enabled,
            ),
            suspicion_threshold=_verifier_int(
                tracker_raw.get("suspicion_threshold"),
                "verifier.tracker.suspicion_threshold",
                defaults.tracker.suspicion_threshold,
                min_value=1,
            ),
            min_turn_before_verdict=_verifier_int(
                tracker_raw.get("min_turn_before_verdict"),
                "verifier.tracker.min_turn_before_verdict",
                defaults.tracker.min_turn_before_verdict,
                min_value=0,
            ),
        ),
        judge=VerifierJudgeConfig(
            enabled=_verifier_bool(
                judge_raw.get("enabled"), "verifier.judge.enabled", defaults.judge.enabled
            ),
            model=_verifier_str(
                judge_raw.get("model"), "verifier.judge.model", defaults.judge.model
            ),
            max_verdicts_per_message=_verifier_int(
                judge_raw.get("max_verdicts_per_message"),
                "verifier.judge.max_verdicts_per_message",
                defaults.judge.max_verdicts_per_message,
                min_value=0,
            ),
            min_turns_between_verdicts=_verifier_int(
                judge_raw.get("min_turns_between_verdicts"),
                "verifier.judge.min_turns_between_verdicts",
                defaults.judge.min_turns_between_verdicts,
                min_value=0,
            ),
            max_spend_ratio=_verifier_float(
                judge_raw.get("max_spend_ratio"),
                "verifier.judge.max_spend_ratio",
                defaults.judge.max_spend_ratio,
                min_value=0.0,
            ),
            tail_grace_s=_verifier_float(
                judge_raw.get("tail_grace_s"),
                "verifier.judge.tail_grace_s",
                defaults.judge.tail_grace_s,
                min_value=0.0,
            ),
        ),
        escalation=VerifierEscalationConfig(max_severity=severity),
    )


def get_verifier_config(
    config_path: str | None = None,
    *,
    config: RuntimeConfig | None = None,
) -> VerifierConfig:
    """``verifier:`` section from monkeybot.yaml.

    When ``config`` (a pinned ``RuntimeConfig`` snapshot) is given, its
    ``verifier`` field is returned instead of re-reading the YAML file.
    Only read from monkeybot.yaml — not from environment variables.
    """
    if config is not None:
        return config.verifier
    _, doc = load_monkeybot_yaml_dict(config_path)
    return verifier_config_from_section(_verifier_section(doc))


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


def _transcript_enabled_stamp(
    config_path: str | None,
) -> tuple[str | None, int | None, int | None]:
    path = resolve_monkeybot_config_path(config_path)
    if path is None:
        return (None, None, None)
    try:
        st = path.stat()
        return (str(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(path), None, None)


def reset_transcript_enabled_cache_for_tests() -> None:
    """Drop the YAML-mtime cache so tests see a freshly written config."""
    global _transcript_enabled_cache
    _transcript_enabled_cache = None


def transcript_enabled_from_config(config_path: str | None = None) -> bool:
    """Whether session NDJSON capture is on (``runtime.transcript_enabled``).

    Defaults to ``False`` when the key is absent. Only read from monkeybot.yaml —
    not from environment variables. Cached on the resolved path + mtime so the
    SSE stream handler and realtime connect do not re-open and merge YAML every
    turn.
    """
    global _warned_legacy_transcript_env, _transcript_enabled_cache
    leftover = os.environ.get("MONKEYBOT_TRANSCRIPT_ENABLED", "").strip()
    if leftover and not _warned_legacy_transcript_env:
        _warned_legacy_transcript_env = True
        logger.warning(
            "MONKEYBOT_TRANSCRIPT_ENABLED is ignored — set runtime.transcript_enabled in monkeybot.yaml"
        )
    stamp = _transcript_enabled_stamp(config_path)
    if _transcript_enabled_cache is not None and _transcript_enabled_cache[0] == stamp:
        return _transcript_enabled_cache[1]
    _, doc = load_monkeybot_yaml_dict(config_path)
    value = _bool_config_flag(
        doc,
        "runtime",
        "transcript_enabled",
        default=False,
        label="runtime.transcript_enabled",
    )
    _transcript_enabled_cache = (stamp, value)
    return value


def ollama_options_from_config(config_path: str | None = None) -> tuple[str | None, int | None]:
    """``model.keep_alive`` and ``model.num_ctx`` from monkeybot.yaml only (not env).

    ``keep_alive`` is ``None`` when absent (provider default ``24h``). Present
    values are stripped strings (including ``"0"`` or empty to omit the request
    field). ``num_ctx`` is ``None`` when absent and must be a positive int when
    set. Never mapped from ``model.context_window``.
    """
    _, doc = load_monkeybot_yaml_dict(config_path)
    model = doc.get("model")
    if not isinstance(model, dict):
        return None, None

    raw_keep_alive = model.get("keep_alive")
    keep_alive: str | None
    if raw_keep_alive is None:
        keep_alive = None
    elif isinstance(raw_keep_alive, bool):
        raise ConfigError(f"model.keep_alive must be a duration string, got {raw_keep_alive!r}")
    else:
        keep_alive = str(raw_keep_alive).strip()

    raw_num_ctx = model.get("num_ctx")
    num_ctx: int | None
    if raw_num_ctx is None:
        num_ctx = None
    elif isinstance(raw_num_ctx, bool) or not isinstance(raw_num_ctx, int):
        raise ConfigError(f"model.num_ctx must be a positive integer, got {raw_num_ctx!r}")
    elif raw_num_ctx < 1:
        raise ConfigError(f"model.num_ctx must be a positive integer, got {raw_num_ctx!r}")
    else:
        num_ctx = raw_num_ctx

    return keep_alive, num_ctx


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
