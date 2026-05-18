"""Configuration and secrets management for monkey-bot framework.

Provides utilities for loading secrets and configuring models:
- load_bot_config(): Load bot.yaml config file with defaults
- load_secrets(): Load from GCP Secret Manager (prod) or .env (dev)
- get_provider_config(): Resolve native :class:`~monkeybot.core.llm.provider.Provider` + model id

This module handles environment detection and secret loading for deployments.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml
from dotenv import load_dotenv

from monkeybot.core.llm.provider import Provider
from monkeybot.providers.gemini import GeminiProvider
from monkeybot.providers.claude import ClaudeProvider
from monkeybot.providers.openai import OpenAIProvider
from monkeybot.providers.vertex_claude import VertexClaudeProvider

logger = logging.getLogger(__name__)


# Framework defaults (zero-config local dev)
DEFAULTS = {
    "AGENT_NAME": "monkey-bot",
    "SKILLS_DIR": "./skills",
    "MODEL_PROVIDER": "google_vertexai",
    "MODEL_NAME": "gemini-2.5-flash",
    "MODEL_TEMPERATURE": "0.7",
    "MODEL_MAX_TOKENS": "60000",
    "MODEL_THINKING_BUDGET": "-1",
    "PORT": "8080",
    "LOG_LEVEL": "INFO",
    "ENVIRONMENT": "development",
    "MEMORY_DIR": "./data/memory",
    "MEMORY_BACKEND": "local",
    "GCS_MEMORY_SYNC_ENABLED": "true",
    "DRIVE_MEMORY_SYNC_ENABLED": "true",
    "DRIVE_MEMORY_FOLDER_ID": "",
    "GCS_ENABLED": "false",
    "SECRETS_PROVIDER": "env",
    "GOOGLE_CHAT_FORMAT": "workspace_addon",
    "SANDBOX_ENABLED":    "false",
    "SANDBOX_SERVER_URL": "http://localhost:8080",
    "SANDBOX_IMAGE":      "python:3.12",
}


# Mapping from YAML paths to environment variable names
CONFIG_MAPPING = {
    # Agent
    "agent.name": "AGENT_NAME",
    "agent.skills_dir": "SKILLS_DIR",
    # Model
    "model.provider": "MODEL_PROVIDER",
    "model.name": "MODEL_NAME",
    "model.temperature": "MODEL_TEMPERATURE",
    "model.max_tokens": "MODEL_MAX_TOKENS",
    "model.thinking_budget": "MODEL_THINKING_BUDGET",
    # Server
    "server.port": "PORT",
    "server.log_level": "LOG_LEVEL",
    # Gateway
    "gateway.allowed_users": "ALLOWED_USERS",
    "gateway.chat_format": "GOOGLE_CHAT_FORMAT",
    "gateway.webhook_url": "GOOGLE_CHAT_WEBHOOK",
    # Memory
    "memory.dir": "MEMORY_DIR",
    "memory.backend": "MEMORY_BACKEND",
    "memory.bucket": "GCS_MEMORY_BUCKET",
    "memory.gcs_sync_enabled": "GCS_MEMORY_SYNC_ENABLED",
    "memory.drive_folder_id": "DRIVE_MEMORY_FOLDER_ID",
    "memory.drive_sync_enabled": "DRIVE_MEMORY_SYNC_ENABLED",
    # Secrets
    "secrets.provider": "SECRETS_PROVIDER",
    # GCP-specific
    "gcp.project_id": "GCP_PROJECT_ID",
    "gcp.location": "VERTEX_AI_LOCATION",
    # Orchestrator LLM Vertex region — when set, overrides ``gcp.location`` for the same env var
    "model.location": "VERTEX_AI_LOCATION",
    # Sandbox execution (optional — requires [sandbox] extra)
    "sandbox.enabled":    "SANDBOX_ENABLED",
    "sandbox.server_url": "SANDBOX_SERVER_URL",
    "sandbox.image":      "SANDBOX_IMAGE",
    # AWS-specific (future)
    "aws.region": "AWS_REGION",
    "aws.account_id": "AWS_ACCOUNT_ID",
    # Azure-specific (future)
    "azure.subscription_id": "AZURE_SUBSCRIPTION_ID",
    "azure.resource_group": "AZURE_RESOURCE_GROUP",
    # Genkit (bundled flows — Vertex media region, often us-central1)
    "genkit.vertex_location": "GENKIT_VERTEX_AI_LOCATION",
}


class ConfigError(Exception):
    """Configuration error with actionable message."""
    pass


@dataclass
class CustomMemoryFolder:
    """User-defined memory folder registered with the memory organizer classifier.

    Args:
        name: Folder name under MEMORY_DIR. Lowercase letters, digits, hyphens only.
        description: One sentence telling the classifier when to route content here.

    Example:
        CustomMemoryFolder("campaigns", "Active and completed marketing campaign plans.")
    """
    name: str
    description: str

    def __post_init__(self) -> None:
        import re
        if not re.match(r'^[a-z0-9-]+$', self.name):
            raise ConfigError(
                f"CustomMemoryFolder.name '{self.name}' is invalid. "
                "Use lowercase letters, digits, and hyphens only."
            )
        _RESERVED = {"episodic", "semantic", "procedural", "working", "raw"}
        if self.name in _RESERVED:
            raise ConfigError(
                f"CustomMemoryFolder.name '{self.name}' conflicts with a built-in folder. "
                f"Reserved names: {sorted(_RESERVED)}"
            )


@dataclass
class SubagentConfig:
    """Configuration for a single subagent.

    Subagents are specialised agents with their own skills directory and
    system prompt.  They share the same backend (filesystem + shell) as
    the orchestrator and are used purely for context management.

    Attributes:
        name: Unique identifier for the subagent (used as the ``task`` tool
            target name).
        description: One-sentence description used by the orchestrator to
            decide when to delegate.
        skills: List of skill directory paths exposed to this subagent via
            ``SkillsMiddleware``.
        prompt_file: Optional path to a Markdown file used as the subagent's
            system prompt.  When omitted a sensible default is generated.
        model: Optional model override in ``provider:model-name`` format.
            Defaults to the orchestrator's model when not set.
        vertex_location: Optional Vertex region for this subagent's LLM (e.g. ``us-east5``).
            YAML key may be ``vertex_location`` or ``location``. When omitted, the host
            app should use the orchestrator default (``VERTEX_AI_LOCATION``).
    """
    name: str
    description: str
    skills: list[str]
    prompt_file: str | None = None
    model: str | None = None
    vertex_location: str | None = None


def _flatten_yaml_to_env(yaml_dict: Dict[str, Any]) -> Dict[str, str]:
    """Flatten nested YAML dict to flat env var dict using CONFIG_MAPPING.
    
    Args:
        yaml_dict: Nested dict from YAML parsing
        
    Returns:
        Flat dict with env var names as keys and string values
        
    Example:
        >>> yaml_dict = {"agent": {"name": "test"}, "gateway": {"allowed_users": ["a@b.com"]}}
        >>> _flatten_yaml_to_env(yaml_dict)
        {"AGENT_NAME": "test", "ALLOWED_USERS": "a@b.com"}
    """
    result: dict[str, str] = {}

    # Walk through CONFIG_MAPPING to extract values from nested yaml_dict
    for yaml_path, env_var in CONFIG_MAPPING.items():
        # Split path into parts (e.g., "agent.name" -> ["agent", "name"])
        parts = yaml_path.split(".")

        # Navigate nested dict
        value: Any = yaml_dict
        for part in parts:
            if isinstance(value, dict) and part in value:
                value = value[part]
            else:
                value = None
                break
        
        if value is not None:
            # Handle list values (join with commas)
            if isinstance(value, list):
                result[env_var] = ",".join(str(v) for v in value)
            else:
                # Convert all values to strings
                result[env_var] = str(value)
    
    return result


def _validate_provider_config(config: Dict[str, str]) -> None:
    """Validate cloud provider configuration and give actionable errors.
    
    Args:
        config: Flat env var dict from load_bot_config
        
    Raises:
        ConfigError: If unsupported provider is specified or required config is missing
    """
    # Supported providers for each backend type
    SUPPORTED_MEMORY_BACKENDS = {"local", "gcs", "drive"}
    SUPPORTED_SECRETS_PROVIDERS = {"env", "gcp_secret_manager"}
    SUPPORTED_MODEL_PROVIDERS = {"google_vertexai", "openai", "anthropic", "vertex_anthropic"}
    
    # Validate memory backend
    memory_backend = config.get("MEMORY_BACKEND", "local")
    if memory_backend not in SUPPORTED_MEMORY_BACKENDS:
        supported = ", ".join(SUPPORTED_MEMORY_BACKENDS)
        if memory_backend == "s3":
            raise ConfigError(
                f"memory.backend is set to 's3' but AWS S3 is not yet supported.\n\n"
                f"To add AWS S3 support, the following are needed:\n"
                f"  - Implement S3Store (subclass BaseStore) in monkeybot/core/store.py\n"
                f"  - Add aws.region and aws.account_id to bot.yaml\n"
                f"  - pip install boto3\n\n"
                f"Currently supported backends: {supported}"
            )
        elif memory_backend == "azure_blob":
            raise ConfigError(
                f"memory.backend is set to 'azure_blob' but Azure Blob Storage is not yet supported.\n\n"
                f"To add Azure Blob support, the following are needed:\n"
                f"  - Implement AzureBlobStore (subclass BaseStore) in monkeybot/core/store.py\n"
                f"  - Add azure.subscription_id and azure.resource_group to bot.yaml\n"
                f"  - pip install azure-storage-blob\n\n"
                f"Currently supported backends: {supported}"
            )
        else:
            raise ConfigError(
                f"memory.backend is set to '{memory_backend}' which is not supported.\n"
                f"Currently supported backends: {supported}"
            )
    
    # Validate secrets provider
    secrets_provider = config.get("SECRETS_PROVIDER", "env")
    if secrets_provider not in SUPPORTED_SECRETS_PROVIDERS:
        supported = ", ".join(SUPPORTED_SECRETS_PROVIDERS)
        if secrets_provider == "aws_secrets_manager":
            raise ConfigError(
                f"secrets.provider is set to 'aws_secrets_manager' but AWS Secrets Manager is not yet supported.\n\n"
                f"To add AWS Secrets Manager support, the following are needed:\n"
                f"  - Implement _load_secrets_from_aws() in monkeybot/core/config.py\n"
                f"  - Add aws.region and aws.account_id to bot.yaml\n"
                f"  - pip install boto3\n\n"
                f"Currently supported providers: {supported}"
            )
        elif secrets_provider == "azure_key_vault":
            raise ConfigError(
                f"secrets.provider is set to 'azure_key_vault' but Azure Key Vault is not yet supported.\n\n"
                f"To add Azure Key Vault support, the following are needed:\n"
                f"  - Implement _load_secrets_from_azure() in monkeybot/core/config.py\n"
                f"  - Add azure.subscription_id and azure.resource_group to bot.yaml\n"
                f"  - pip install azure-keyvault-secrets azure-identity\n\n"
                f"Currently supported providers: {supported}"
            )
        else:
            raise ConfigError(
                f"secrets.provider is set to '{secrets_provider}' which is not supported.\n"
                f"Currently supported providers: {supported}"
            )
    
    # Validate model provider
    model_provider = config.get("MODEL_PROVIDER", "google_vertexai")
    if model_provider not in SUPPORTED_MODEL_PROVIDERS:
        supported = ", ".join(SUPPORTED_MODEL_PROVIDERS)
        if model_provider == "aws_bedrock":
            raise ConfigError(
                f"model.provider is set to 'aws_bedrock' but AWS Bedrock is not yet supported.\n\n"
                f"To add AWS Bedrock support, the following are needed:\n"
                f"  - Add aws_bedrock case to get_provider_config() in monkeybot/core/config.py\n"
                f"  - Add aws.region to bot.yaml\n"
                f"  - pip install boto3\n\n"
                f"Currently supported providers: {supported}"
            )
        elif model_provider == "azure_openai":
            raise ConfigError(
                f"model.provider is set to 'azure_openai' but Azure OpenAI is not yet supported.\n\n"
                f"To add Azure OpenAI support, the following are needed:\n"
                f"  - Add azure_openai case to get_provider_config() in monkeybot/core/config.py\n"
                f"  - Add azure.subscription_id to bot.yaml\n"
                f"  - pip install openai\n\n"
                f"Currently supported providers: {supported}"
            )
        else:
            raise ConfigError(
                f"model.provider is set to '{model_provider}' which is not supported.\n"
                f"Currently supported providers: {supported}"
            )
    
    # Validate GCP-specific dependencies
    if memory_backend == "gcs" and not config.get("GCP_PROJECT_ID"):
        raise ConfigError(
            "memory.backend is set to 'gcs' but gcp.project_id is not configured.\n"
            "Add 'gcp.project_id: your-project-id' to bot.yaml"
        )
    
    if secrets_provider == "gcp_secret_manager" and not config.get("GCP_PROJECT_ID"):
        raise ConfigError(
            "secrets.provider is set to 'gcp_secret_manager' but gcp.project_id is not configured.\n"
            "Add 'gcp.project_id: your-project-id' to bot.yaml"
        )
    
    if model_provider == "vertex_anthropic" and not config.get("GCP_PROJECT_ID"):
        raise ConfigError(
            "model.provider is set to 'vertex_anthropic' but gcp.project_id is not configured.\n"
            "Add 'gcp.project_id: your-project-id' to bot.yaml"
        )


# Track if config has been loaded to avoid duplicate work
_config_loaded = False

# Raw YAML dict stored after the last successful load_bot_config() call.
# Used by get_subagent_configs() to read the top-level `subagents:` section
# without requiring a second YAML parse.
_raw_yaml: dict[str, Any] | None = None


def load_bot_config(config_path: str | None = None) -> Dict[str, str]:
    """Load bot configuration from bot.yaml file with framework defaults.
    
    This is the main entry point for loading bot configuration. It:
    1. Looks for bot.yaml in current directory (or explicit path)
    2. Parses YAML and flattens to env var format
    3. Derives computed values (e.g., GCS_ENABLED from memory.backend)
    4. Sets bot.yaml values as os.environ (only if not already set by caller)
    5. Fills remaining gaps with framework DEFAULTS
    6. Validates provider configuration
    7. Returns the loaded config dict
    
    Loading priority: framework DEFAULTS < bot.yaml < .env < secrets provider
    
    This function can be called multiple times safely - it will only load once.
    
    After the first successful load, config is cached in ``_config_loaded``;
    subsequent calls with no ``config_path`` return the cached env state.
    Warm Lambda/Cloud Functions containers reuse this as intended. In tests,
    reset with ``monkeybot.core.config.settings._config_loaded = False`` before
    reloading from a different path.
    
    Args:
        config_path: Optional explicit path to bot.yaml file.
            If not provided, looks for bot.yaml in current directory.
            
    Returns:
        Dictionary of loaded config values (env var name -> value)
        
    Raises:
        ConfigError: If configuration validation fails
        
    Example:
        >>> # Load from default location (./bot.yaml)
        >>> config = load_bot_config()
        
        >>> # Load from explicit path
        >>> config = load_bot_config("/path/to/bot.yaml")
    """
    global _config_loaded, _raw_yaml
    
    # If already loaded, just return current env state
    if _config_loaded and not config_path:  # Allow explicit path to override
        return {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
    
    # Step 1: Look for bot.yaml
    if config_path:
        yaml_path = Path(config_path)
    else:
        yaml_path = Path("bot.yaml")
    
    if not yaml_path.exists():
        logger.info(f"No bot.yaml found at {yaml_path.absolute()}, using defaults and env vars only")
        for key, default_value in DEFAULTS.items():
            if key not in os.environ:
                os.environ[key] = default_value
                logger.debug(f"Applied default: {key}={default_value}")
        _config_loaded = True
        return {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
    
    # Step 3: Parse YAML
    try:
        with open(yaml_path) as f:
            yaml_dict = yaml.safe_load(f)
        
        if not yaml_dict:
            logger.warning(f"bot.yaml at {yaml_path} is empty, using defaults only")
            return {k: os.environ.get(k, v) for k, v in DEFAULTS.items()}
        
        logger.info(f"Loaded bot.yaml from {yaml_path.absolute()}")
        _raw_yaml = yaml_dict
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse bot.yaml: {e}")
    except Exception as e:
        raise ConfigError(f"Failed to read bot.yaml: {e}")
    
    # Step 4: Flatten YAML to env var format
    config = _flatten_yaml_to_env(yaml_dict)
    
    # Step 5: Derive computed values
    # GCS_ENABLED is derived from memory.backend for backward compatibility
    if config.get("MEMORY_BACKEND") == "gcs":
        config["GCS_ENABLED"] = "true"
    
    # VERTEX_AI_PROJECT_ID can be set from gcp.project_id for backward compatibility
    if "GCP_PROJECT_ID" in config and "VERTEX_AI_PROJECT_ID" not in config:
        config["VERTEX_AI_PROJECT_ID"] = config["GCP_PROJECT_ID"]
    
    # Step 6: Set bot.yaml values as environment variables (only if not already set by caller).
    # Must happen BEFORE applying defaults so that bot.yaml values take priority over defaults.
    # Priority chain: env vars (caller) > bot.yaml > framework defaults
    for key, value in config.items():
        if key not in os.environ:
            os.environ[key] = value
            logger.debug(f"Set from bot.yaml: {key}={value}")
    
    # Step 7: Apply framework defaults for any keys not covered by bot.yaml or caller env vars
    for key, default_value in DEFAULTS.items():
        if key not in os.environ:
            os.environ[key] = default_value
            logger.debug(f"Applied default: {key}={default_value}")
    
    # Step 8: Validate provider configuration
    # Use current environment state (includes yaml + defaults + any existing env vars)
    current_config = {k: os.environ.get(k, "") for k in config.keys()}
    _validate_provider_config(current_config)
    
    # Mark as loaded
    _config_loaded = True
    
    logger.info(f"✅ Bot config loaded from {yaml_path.absolute()}")
    return config


def load_secrets(required_secrets: list[str] | None = None) -> Dict[str, str]:
    """Load secrets using configured secrets provider.
    
    This function first loads bot.yaml config (which sets defaults and SECRETS_PROVIDER),
    then dispatches to the appropriate secrets backend based on configuration.

    Secrets providers:
        - env: Load from .env file using python-dotenv (development)
        - gcp_secret_manager: Load from GCP Secret Manager (production)
        - aws_secrets_manager: AWS Secrets Manager (future)
        - azure_key_vault: Azure Key Vault (future)

    The provider is determined by:
    1. bot.yaml `secrets.provider` field (if present)
    2. SECRETS_PROVIDER env var (if set)
    3. ENVIRONMENT env var: "production" -> gcp_secret_manager, else -> env (legacy)

    Args:
        required_secrets: Optional list of secret names to validate in production.
            Secret names should use kebab-case (e.g., "vertex-ai-project-id").
            If not provided, loads all available secrets without validation.

    Returns:
        Dictionary of secret key-value pairs (env var name → value).
        In development mode (env provider), returns empty dict (secrets loaded into os.environ).

    Raises:
        RuntimeError: If required secrets are missing in production
        RuntimeError: If secrets provider is not available
        ConfigError: If secrets provider is not supported

    Environment Variables (inputs):
        SECRETS_PROVIDER: Provider to use (env, gcp_secret_manager, etc.)
        ENVIRONMENT: "production" or "development" (legacy, for backward compat)
        GCP_PROJECT_ID: GCP project ID for Secret Manager (if using gcp_secret_manager)

    Environment Variables (outputs):
        Sets all loaded secrets as environment variables with env var naming.

    Example:
        >>> # Development mode - load from .env
        >>> load_secrets()
        {}  # Secrets loaded into os.environ

        >>> # Production mode with GCP - load from Secret Manager
        >>> os.environ["SECRETS_PROVIDER"] = "gcp_secret_manager"
        >>> load_secrets(required_secrets=["vertex-ai-project-id", "allowed-users"])
        {"VERTEX_AI_PROJECT_ID": "...", "ALLOWED_USERS": "..."}
    """
    # Load bot config first (sets SECRETS_PROVIDER and other config)
    load_bot_config()
    
    # Determine secrets provider
    # Priority: SECRETS_PROVIDER env var > ENVIRONMENT env var (legacy)
    secrets_provider = os.getenv("SECRETS_PROVIDER", "")
    
    # Legacy support: ENVIRONMENT=production -> gcp_secret_manager
    if not secrets_provider:
        environment = os.getenv("ENVIRONMENT", "development")
        if environment == "production":
            secrets_provider = "gcp_secret_manager"
            logger.info("Using gcp_secret_manager (inferred from ENVIRONMENT=production)")
        else:
            secrets_provider = "env"
    
    # Dispatch to appropriate secrets backend
    if secrets_provider == "env":
        return _load_secrets_from_env()
    elif secrets_provider == "gcp_secret_manager":
        return _load_secrets_from_gcp(required_secrets)
    else:
        # This should have been caught by _validate_provider_config, but check again
        raise ConfigError(
            f"Unsupported secrets provider: {secrets_provider}\n"
            f"Currently supported: env, gcp_secret_manager"
        )


def _load_secrets_from_gcp(required_secrets: list[str] | None = None) -> Dict[str, str]:
    """Load secrets from GCP Secret Manager.

    Args:
        required_secrets: Optional list of secret names to validate

    Returns:
        Dictionary of secret key-value pairs (env var name → value)

    Raises:
        RuntimeError: If Secret Manager client fails or required secrets are missing
    """
    try:
        from google.cloud import secretmanager
    except ImportError:
        raise RuntimeError(
            "google-cloud-secret-manager is required for production. "
            "Install with: pip install google-cloud-secret-manager"
        )

    project_id = os.getenv("GCP_PROJECT_ID", "")
    if not project_id:
        raise RuntimeError(
            "GCP_PROJECT_ID environment variable is required for Secret Manager"
        )
    client = secretmanager.SecretManagerServiceClient()

    # If no required secrets specified, just log warning and return empty dict
    if not required_secrets:
        logger.warning(
            "No required_secrets specified for GCP Secret Manager. "
            "Recommend passing required secrets list for validation."
        )
        return {}

    loaded_secrets: Dict[str, str] = {}
    missing_secrets = []

    for secret_name in required_secrets:
        try:
            name = f"projects/{project_id}/secrets/{secret_name}/versions/latest"
            response = client.access_secret_version(request={"name": name})
            secret_value = response.payload.data.decode("UTF-8")

            # Convert secret name to env var format (e.g., x-api-key → X_API_KEY)
            env_var_name = secret_name.upper().replace("-", "_")
            loaded_secrets[env_var_name] = secret_value

            # Set as environment variable for backward compatibility
            os.environ[env_var_name] = secret_value

            logger.info(f"✅ Loaded secret: {secret_name}")
        except Exception as e:
            logger.error(f"❌ Failed to load secret {secret_name}: {e}")
            missing_secrets.append(secret_name)

    if missing_secrets:
        raise RuntimeError(
            f"Failed to load required secrets: {', '.join(missing_secrets)}\n"
            f"See docs/getting-started.md and monkeybot_config/monkeybot.example.yaml for setup instructions."
        )

    logger.info(f"✅ Loaded {len(loaded_secrets)} secrets from GCP Secret Manager")
    return loaded_secrets


def _load_secrets_from_env() -> Dict[str, str]:
    """Load secrets from .env file for development.

    Returns:
        Empty dict (secrets loaded directly into os.environ by dotenv)
    """
    load_dotenv()
    logger.info("✅ Loaded secrets from .env file (development mode)")
    return {}


@dataclass(frozen=True)
class ProviderConfig:
    """Native streaming :class:`~monkeybot.core.llm.provider.Provider` plus model id."""

    provider: Provider
    model: str


def get_provider_config(
    provider: str | None = None,
    model_name: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    thinking_budget: int | None = None,
) -> ProviderConfig:
    """Resolve a :class:`Provider` and model id from environment or explicit parameters.

    Supported ``MODEL_PROVIDER`` / ``provider`` values:
    - ``google_vertexai`` — :class:`~monkeybot.providers.gemini.GeminiProvider` (Vertex / ``google-genai``)
    - ``openai`` — :class:`~monkeybot.providers.openai.OpenAIProvider`
    - ``anthropic`` — :class:`~monkeybot.providers.claude.ClaudeProvider`
    - ``vertex_anthropic`` — :class:`~monkeybot.providers.vertex_claude.VertexClaudeProvider` on Vertex (ADC)

    ``temperature``, ``max_tokens``, and ``thinking_budget`` override env for
    :class:`~monkeybot.providers.gemini.GeminiProvider` only; other providers read
    generation limits from environment at :meth:`~monkeybot.core.llm.provider.Provider.stream` time
    where applicable.

    Raises:
        ValueError: Unsupported provider or missing required configuration
    """
    provider_key = provider or os.getenv("MODEL_PROVIDER", "google_vertexai")
    resolved_model = str(model_name or os.getenv("MODEL_NAME") or "gemini-2.5-flash")
    temperature = temperature if temperature is not None else float(
        os.getenv("MODEL_TEMPERATURE", "0.7")
    )
    max_tokens = max_tokens if max_tokens is not None else int(
        os.getenv("MODEL_MAX_TOKENS", "60000")
    )
    thinking_budget = thinking_budget if thinking_budget is not None else int(
        os.getenv("MODEL_THINKING_BUDGET", "-1")
    )

    logger.info(
        "Initializing provider: provider=%s model=%s temp=%s max_tokens=%s thinking_budget=%s",
        provider_key,
        resolved_model,
        temperature,
        max_tokens,
        thinking_budget,
    )

    if provider_key == "google_vertexai":
        return ProviderConfig(
            GeminiProvider(
                temperature=temperature,
                max_output_tokens=max_tokens,
                thinking_budget=thinking_budget,
            ),
            resolved_model,
        )

    if provider_key == "openai":
        return ProviderConfig(OpenAIProvider(), resolved_model)

    if provider_key == "anthropic":
        return ProviderConfig(ClaudeProvider(), resolved_model)

    if provider_key == "vertex_anthropic":
        project = os.getenv("GCP_PROJECT_ID") or os.getenv("VERTEX_AI_PROJECT_ID")
        if not project:
            raise ValueError(
                "vertex_anthropic provider requires GCP_PROJECT_ID or VERTEX_AI_PROJECT_ID. "
                "Set gcp.project_id in bot.yaml or GCP_PROJECT_ID env var."
            )
        region = (
            os.getenv("VERTEX_AI_LOCATION")
            or os.getenv("ANTHROPIC_VERTEX_REGION")
            or "us-east5"
        )
        return ProviderConfig(
            VertexClaudeProvider(project_id=project, region=region),
            resolved_model,
        )

    raise ValueError(
        f"Unsupported model provider: {provider_key}. "
        "Supported providers: google_vertexai, openai, anthropic, vertex_anthropic"
    )


def get_subagent_configs() -> list[SubagentConfig]:
    """Return subagent configurations parsed from the ``subagents:`` section of bot.yaml.

    Relies on ``_raw_yaml`` being populated by a prior call to
    ``load_bot_config()``.  Returns an empty list when no subagents are
    configured or when ``load_bot_config()`` has not yet been called.

    Invalid entries (missing ``name`` or ``description``) are skipped with a
    warning log rather than raising an error, so a single malformed entry does
    not prevent the rest from loading.

    Returns:
        List of :class:`SubagentConfig` instances in declaration order.

    Example:
        >>> load_bot_config()
        >>> for sa in get_subagent_configs():
        ...     print(sa.name, sa.skills)
    """
    raw_entries = (_raw_yaml or {}).get("subagents", [])
    if not raw_entries:
        return []

    configs: list[SubagentConfig] = []
    for entry in raw_entries:
        if not isinstance(entry, dict):
            logger.warning("Skipping non-dict subagent entry: %s", entry)
            continue
        if "name" not in entry or "description" not in entry:
            logger.warning(
                "Skipping subagent entry missing 'name' or 'description': %s", entry
            )
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
