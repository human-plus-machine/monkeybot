"""Provider and deployment configuration validation for monkeybot.yaml."""

from __future__ import annotations

from typing import Any

from monkeybot.core.config.settings import ConfigError, normalize_model_provider

SUPPORTED_MEMORY_BACKENDS = frozenset({"local"})
SUPPORTED_SECRETS_PROVIDERS = frozenset({"env", "gcp_secret_manager"})
SUPPORTED_MODEL_PROVIDERS = frozenset(
    {
        "google_vertexai",
        "google_genai",
        "openai",
        "anthropic",
        "vertex_anthropic",
        "huggingface",
        "ollama",
        "nvidia",
        "openrouter",
        "fake",
        "aws_bedrock",
    }
)

SUPPORTED_YAML_MODEL_PROVIDERS = frozenset(
    {
        "gemini",
        "vertex",
        "google_vertexai",
        "google_genai",
        "openai",
        "anthropic",
        "vertex-claude",
        "vertex_claude",
        "vertex_anthropic",
        "huggingface",
        "ollama",
        "nvidia",
        "openrouter",
        "fake",
        "aws_bedrock",
    }
)

SUPPORTED_HARNESS_MODES = frozenset({"turn_based", "realtime"})
SUPPORTED_REALTIME_AUDIO_FORMATS = frozenset(
    {
        "pcm_s16le_24khz_mono",
        "pcm_s16le_16khz_mono",
    }
)


def validate_provider_env(config: dict[str, str]) -> None:
    """Validate flattened env-style provider configuration.

    Args:
        config: Flat env var dict (MODEL_PROVIDER, MEMORY_BACKEND, etc.)

    Raises:
        ConfigError: If unsupported provider or missing required configuration.
    """
    memory_backend = config.get("MEMORY_BACKEND", "local")
    if memory_backend not in SUPPORTED_MEMORY_BACKENDS:
        raise ConfigError(
            "MemPalace is local-only. Object-store memory backends (gcs, s3, drive) "
            "are not supported. Set MEMORY_STORAGE_URI to a local:// path or mounted volume."
        )

    secrets_provider = config.get("SECRETS_PROVIDER", "env")
    if secrets_provider not in SUPPORTED_SECRETS_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_SECRETS_PROVIDERS))
        raise ConfigError(
            f"secrets.provider is set to '{secrets_provider}' which is not supported.\n"
            f"Currently supported providers: {supported}"
        )

    model_provider = normalize_model_provider(config.get("MODEL_PROVIDER", "google_vertexai"))
    if model_provider not in SUPPORTED_MODEL_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_MODEL_PROVIDERS))
        if model_provider == "azure_openai":
            raise ConfigError(
                f"model.provider is set to 'azure_openai' but Azure OpenAI is not yet supported.\n\n"
                f"Currently supported providers: {supported}"
            )
        raise ConfigError(
            f"model.provider is set to '{model_provider}' which is not supported.\n"
            f"Currently supported providers: {supported}"
        )

    gcp_project = (
        config.get("GCP_PROJECT_ID")
        or config.get("VERTEX_AI_PROJECT_ID")
        or config.get("ANTHROPIC_VERTEX_PROJECT_ID")
        or config.get("GOOGLE_CLOUD_PROJECT")
        or ""
    )

    if secrets_provider == "gcp_secret_manager" and not gcp_project:
        raise ConfigError(
            "secrets.provider is set to 'gcp_secret_manager' but gcp.project_id is not configured.\n"
            "Add 'gcp.project_id: your-project-id' to monkeybot.yaml"
        )

    if model_provider == "vertex_anthropic" and not gcp_project:
        raise ConfigError(
            "model.provider is set to 'vertex_anthropic' but gcp.project_id is not configured.\n"
            "Add 'gcp.project_id: your-project-id' to monkeybot.yaml"
        )


def _validate_harness_mode(doc: dict[str, Any]) -> None:
    """Validate ``harness.mode`` if present. Defaults to ``turn_based``."""
    harness = doc.get("harness")
    if not isinstance(harness, dict):
        return
    mode = str(harness.get("mode", "turn_based")).strip().lower()
    if mode not in SUPPORTED_HARNESS_MODES:
        supported = ", ".join(sorted(SUPPORTED_HARNESS_MODES))
        raise ConfigError(
            f"harness.mode is set to '{mode}' which is not supported.\n"
            f"Supported modes: {supported}"
        )


def _validate_realtime_config(doc: dict[str, Any]) -> None:
    """Validate ``realtime.*`` settings when ``harness.mode`` is ``realtime``."""
    harness = doc.get("harness")
    mode = "turn_based"
    if isinstance(harness, dict):
        mode = str(harness.get("mode", "turn_based")).strip().lower()

    realtime_raw = doc.get("realtime")
    if not isinstance(realtime_raw, dict):
        realtime_raw = {}

    def _require_positive(path: str, default: int) -> None:
        parts = path.split(".")
        value: Any = realtime_raw
        for part in parts:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
        if value is None:
            return
        if not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(f"realtime.{path} must be a positive number, got {value!r}")

    # Validate audio format strings even when mode is turn_based so misconfigurations
    # are caught before the mode is flipped.
    audio_raw = realtime_raw.get("audio")
    audio: dict[str, Any] = audio_raw if isinstance(audio_raw, dict) else {}
    for fmt_key in ("input_format", "output_format"):
        fmt = str(audio.get(fmt_key, "pcm_s16le_24khz_mono")).strip().lower()
        if fmt and fmt not in SUPPORTED_REALTIME_AUDIO_FORMATS:
            supported = ", ".join(sorted(SUPPORTED_REALTIME_AUDIO_FORMATS))
            raise ConfigError(
                f"realtime.audio.{fmt_key} is set to '{fmt}' which is not supported.\n"
                f"Supported formats: {supported}"
            )

    chunk_ms = audio.get("chunk_ms")
    if chunk_ms is not None and (not isinstance(chunk_ms, int) or chunk_ms <= 0):
        raise ConfigError(f"realtime.audio.chunk_ms must be a positive integer, got {chunk_ms!r}")

    _require_positive("audio.max_utterance_sec", 60)
    _require_positive("session.max_duration_sec", 1800)
    _require_positive("session.idle_timeout_sec", 120)
    _require_positive("session.max_response_turn_sec", 300)

    max_concurrent = realtime_raw.get("session", {}).get("max_concurrent_sessions")
    if max_concurrent is not None and (not isinstance(max_concurrent, int) or max_concurrent <= 0):
        raise ConfigError(
            f"realtime.session.max_concurrent_sessions must be a positive integer, "
            f"got {max_concurrent!r}"
        )

    if mode != "realtime":
        return

    # Realtime mode requires the provider to be from the Gemini family for v1.
    model_raw = doc.get("model")
    model: dict[str, Any] = model_raw if isinstance(model_raw, dict) else {}
    provider = str(model.get("provider", "gemini")).strip().lower()
    if provider not in {"gemini", "vertex", "google_vertexai", "google_genai"}:
        raise ConfigError(
            "harness.mode is 'realtime' but model.provider is not a Gemini family provider. "
            "Realtime v1 only supports google_vertexai or google_genai (Gemini Live)."
        )


def validate_monkeybot_yaml_doc(doc: dict[str, Any], *, env: dict[str, str] | None = None) -> None:
    """Validate a parsed monkeybot.yaml document (new schema).

    Args:
        doc: Parsed YAML root mapping.
        env: Optional environment for GCP project resolution.

    Raises:
        ConfigError: If unsupported provider or missing required configuration.
    """
    env = env or {}
    model_raw = doc.get("model")
    model: dict[str, Any] = model_raw if isinstance(model_raw, dict) else {}
    provider_raw = str(model.get("provider", "gemini")).strip().lower()
    if provider_raw and provider_raw not in SUPPORTED_YAML_MODEL_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_YAML_MODEL_PROVIDERS))
        raise ConfigError(
            f"model.provider is set to '{provider_raw}' which is not supported.\n"
            f"Currently supported providers: {supported}"
        )

    _validate_harness_mode(doc)
    _validate_realtime_config(doc)

    flat: dict[str, str] = {}
    if isinstance(model, dict) and model.get("provider"):
        flat["MODEL_PROVIDER"] = str(model["provider"])
    memory_uri = ""
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    if isinstance(paths, dict):
        memory_uri = str(paths.get("memory_storage_uri", ""))
    if memory_uri.lower().startswith(("gcs://", "s3://", "gs://")):
        raise ConfigError(
            "MemPalace requires a local:// memory URI. "
            f"Object-store URIs are not supported, got: {memory_uri}"
        )
    gcp = doc.get("gcp") if isinstance(doc.get("gcp"), dict) else {}
    if isinstance(gcp, dict) and gcp.get("project_id"):
        flat["GCP_PROJECT_ID"] = str(gcp["project_id"])
    for key in ("GCP_PROJECT_ID", "VERTEX_AI_PROJECT_ID", "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"):
        if key in env and env[key]:
            flat[key] = env[key]
    validate_provider_env(flat)

    from monkeybot.core.config.runtime_env import warn_retired_tools_keys

    warn_retired_tools_keys(doc)
