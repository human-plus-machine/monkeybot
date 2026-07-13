"""Catalog of agent-selectable monkeybot extras for ``monkeybot new``."""

from __future__ import annotations

from dataclasses import dataclass

from monkeybot_cli.providers import PROVIDER_SPECS, spec_for_provider


@dataclass(frozen=True)
class ExtraChoice:
    """One selectable optional-dependency row."""

    key: str  # package extra name or YAML provider id (for provider menu)
    label: str


# Primary provider menu — keys are YAML ``model.provider`` values.
# ``fake`` is intentionally omitted: it remains valid via ``--provider fake`` for CI/smoke.
PROVIDER_CHOICES: tuple[ExtraChoice, ...] = (
    ExtraChoice("gemini", "Gemini / Vertex AI"),
    ExtraChoice("openai", "OpenAI"),
    ExtraChoice("anthropic", "Anthropic (Claude API)"),
    ExtraChoice("vertex-claude", "Claude on Vertex AI"),
    ExtraChoice("aws_bedrock", "AWS Bedrock"),
    ExtraChoice("huggingface", "Hugging Face"),
    ExtraChoice("ollama", "Ollama (local)"),
    ExtraChoice("nvidia", "NVIDIA NIM"),
)

# Non-provider agent features (root ``[project.optional-dependencies]`` names).
FEATURE_CHOICES: tuple[ExtraChoice, ...] = (
    ExtraChoice("postgres", "Postgres conversation store (parallel subagents)"),
    ExtraChoice("firestore", "Firestore storage"),
    ExtraChoice("gcs", "Google Cloud Storage"),
    ExtraChoice("sandbox", "OpenSandbox code execution"),
    ExtraChoice("web-search", "DuckDuckGo web search (ddgs)"),
    ExtraChoice("observability", "OpenTelemetry tracing"),
    ExtraChoice("scheduler", "Cron scheduler"),
    ExtraChoice("council", "Council / multi-agent GCS helpers"),
    ExtraChoice("aws", "AWS helpers (boto3)"),
    ExtraChoice("realtime", "Realtime WebSocket gateway support"),
    ExtraChoice("realtime-gemini", "Gemini Live realtime"),
    ExtraChoice("cli-realtime", "CLI talk audio (PortAudio / push-to-talk)"),
)

_FEATURE_KEYS = frozenset(c.key for c in FEATURE_CHOICES)
_KNOWN_PACKAGE_EXTRAS = frozenset(
    {s.extra for s in PROVIDER_SPECS.values() if s.extra} | _FEATURE_KEYS
)


def provider_extra_name(yaml_provider: str | None) -> str | None:
    """Map a YAML provider id to its package extra (None for fake / unknown)."""
    if not yaml_provider:
        return None
    spec = spec_for_provider(yaml_provider)
    return spec.extra if spec is not None else None


def normalize_extra_token(raw: str) -> str | None:
    """Accept ``--with`` tokens: feature keys, package extras, or YAML provider aliases."""
    token = raw.strip()
    if not token:
        return None
    if token in _FEATURE_KEYS:
        return token
    if token in _KNOWN_PACKAGE_EXTRAS:
        return token
    spec = spec_for_provider(token)
    if spec is not None:
        return spec.extra  # may be None for fake — caller should skip
    # hyphen/underscore variants for features
    alt = token.replace("_", "-")
    if alt in _FEATURE_KEYS:
        return alt
    alt2 = token.replace("-", "_")
    spec = spec_for_provider(alt2)
    if spec is not None:
        return spec.extra
    return None


def additional_provider_extra_choices(primary_yaml: str | None) -> tuple[ExtraChoice, ...]:
    """Other provider package extras (beyond the primary) for multi-select."""
    primary_extra = provider_extra_name(primary_yaml)
    seen: set[str] = set()
    rows: list[ExtraChoice] = []
    for choice in PROVIDER_CHOICES:
        extra = provider_extra_name(choice.key)
        if extra is None or extra == primary_extra or extra in seen:
            continue
        seen.add(extra)
        rows.append(ExtraChoice(extra, choice.label))
    return tuple(rows)
