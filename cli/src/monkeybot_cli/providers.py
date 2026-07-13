"""Provider → extra → credential mapping (owned by CLI)."""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass

from monkeybot.core.config import normalize_model_provider


@dataclass(frozen=True)
class ProviderSpec:
    yaml_aliases: tuple[str, ...]
    extra: str | None
    credential_env_vars: tuple[str, ...]
    gcp_adc: bool = False
    # True when the provider needs no API key (e.g. a local server reachable
    # without auth, such as Ollama). Skips the credentials check in `doctor`.
    credentials_optional: bool = False


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "google_vertexai": ProviderSpec(
        ("gemini", "vertex", "google_vertexai"),
        "gemini",
        (
            "GEMINI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "GCP_PROJECT_ID",
            "GOOGLE_CLOUD_PROJECT",
        ),
        gcp_adc=True,
    ),
    "openai": ProviderSpec(("openai",), "openai", ("OPENAI_API_KEY",)),
    "anthropic": ProviderSpec(("anthropic",), "claude", ("ANTHROPIC_API_KEY",)),
    "vertex_anthropic": ProviderSpec(
        ("vertex-claude", "vertex_claude", "vertex_anthropic"),
        "vertex-claude",
        ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "ANTHROPIC_VERTEX_PROJECT_ID"),
        gcp_adc=True,
    ),
    "aws_bedrock": ProviderSpec(
        ("aws_bedrock",),
        "bedrock",
        ("AWS_ACCESS_KEY_ID", "AWS_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION"),
    ),
    "huggingface": ProviderSpec(
        ("huggingface",), "huggingface", ("HF_TOKEN", "HUGGINGFACE_API_KEY")
    ),
    "nvidia": ProviderSpec(("nvidia",), "nvidia", ("NVIDIA_API_KEY",)),
    "ollama": ProviderSpec(
        # credential_env_vars is empty: credentials_optional=True short-circuits
        # credentials_present() before these are ever read, and OLLAMA_BASE_URL
        # isn't a credential anyway (it's just the server address).
        ("ollama",),
        "ollama",
        (),
        credentials_optional=True,
    ),
    "fake": ProviderSpec(("fake",), None, (), credentials_optional=True),
}


def canonical_provider(yaml_provider: str) -> str:
    return normalize_model_provider(yaml_provider.strip().lower())


def spec_for_provider(yaml_provider: str) -> ProviderSpec | None:
    key = canonical_provider(yaml_provider)
    return PROVIDER_SPECS.get(key)


def extra_module(extra: str) -> str:
    """Return the importable module name used to detect ``extra``."""
    mapping = {
        "gemini": "google.genai",
        "vertex": "google.auth",
        "claude": "anthropic",
        "vertex-claude": "anthropic",
        "openai": "openai",
        "bedrock": "boto3",
        "huggingface": "openai",
        "ollama": "openai",
        "nvidia": "openai",
    }
    return mapping.get(extra, extra)


def extra_installed(extra: str) -> bool:
    """Check ``extra`` in the *current* process (the CLI env).

    Prefer running the check in the interpreter that will host the gateway
    (see ``monkeybot_cli.runtime_python``) so provider/storage extras declared
    on the agent project are detected, not the CLI's globals.
    """
    return importlib.util.find_spec(extra_module(extra)) is not None


def credentials_present(spec: ProviderSpec) -> bool:
    if spec.credentials_optional:
        return True
    if spec.gcp_adc:
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
            return True
        for var in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "VERTEX_AI_PROJECT_ID"):
            if os.environ.get(var, "").strip():
                return True
        if os.environ.get("GEMINI_API_KEY", "").strip():
            return True
        return False
    return any(os.environ.get(v, "").strip() for v in spec.credential_env_vars)
