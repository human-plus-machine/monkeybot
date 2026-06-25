"""Provider and deployment configuration validation for monkeybot.yaml."""

from __future__ import annotations

from typing import Any

from monkeybot.core.config.settings import ConfigError, normalize_model_provider

SUPPORTED_MEMORY_BACKENDS = frozenset({"local", "gcs", "drive"})
SUPPORTED_SECRETS_PROVIDERS = frozenset({"env", "gcp_secret_manager"})
SUPPORTED_MODEL_PROVIDERS = frozenset(
    {
        "google_vertexai",
        "openai",
        "anthropic",
        "vertex_anthropic",
        "huggingface",
        "fake",
        "aws_bedrock",
    }
)

SUPPORTED_YAML_MODEL_PROVIDERS = frozenset(
    {
        "gemini",
        "vertex",
        "google_vertexai",
        "openai",
        "anthropic",
        "vertex-claude",
        "vertex_claude",
        "vertex_anthropic",
        "huggingface",
        "fake",
        "aws_bedrock",
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
        supported = ", ".join(sorted(SUPPORTED_MEMORY_BACKENDS))
        if memory_backend == "s3":
            raise ConfigError(
                f"memory.backend is set to 's3' but AWS S3 is not yet supported.\n\n"
                f"Currently supported backends: {supported}"
            )
        if memory_backend == "azure_blob":
            raise ConfigError(
                f"memory.backend is set to 'azure_blob' but Azure Blob Storage is not yet supported.\n\n"
                f"Currently supported backends: {supported}"
            )
        raise ConfigError(
            f"memory.backend is set to '{memory_backend}' which is not supported.\n"
            f"Currently supported backends: {supported}"
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

    if memory_backend == "gcs" and not gcp_project:
        raise ConfigError(
            "memory.backend is set to 'gcs' but gcp.project_id is not configured.\n"
            "Add 'gcp.project_id: your-project-id' to monkeybot.yaml"
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


def validate_monkeybot_yaml_doc(doc: dict[str, Any], *, env: dict[str, str] | None = None) -> None:
    """Validate a parsed monkeybot.yaml document (new schema).

    Args:
        doc: Parsed YAML root mapping.
        env: Optional environment for GCP project resolution.
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

    flat: dict[str, str] = {}
    if isinstance(model, dict) and model.get("provider"):
        flat["MODEL_PROVIDER"] = str(model["provider"])
    memory_uri = ""
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    if isinstance(paths, dict):
        memory_uri = str(paths.get("memory_storage_uri", ""))
    if memory_uri.startswith("gcs://"):
        flat["MEMORY_BACKEND"] = "gcs"
    gcp = doc.get("gcp") if isinstance(doc.get("gcp"), dict) else {}
    if isinstance(gcp, dict) and gcp.get("project_id"):
        flat["GCP_PROJECT_ID"] = str(gcp["project_id"])
    for key in ("GCP_PROJECT_ID", "VERTEX_AI_PROJECT_ID", "ANTHROPIC_VERTEX_PROJECT_ID", "GOOGLE_CLOUD_PROJECT"):
        if key in env and env[key]:
            flat[key] = env[key]
    validate_provider_env(flat)
