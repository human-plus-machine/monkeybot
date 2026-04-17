"""Discriminated-union spec for the ``ModelProvider`` extension surface.

See 1b-contracts.md §4.2.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from pydantic_core import CoreSchema


class _ModelProviderBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    import_path: str | None = None


class ModelProviderVertexSpec(_ModelProviderBase):
    """Google Cloud Vertex AI model provider."""

    backend: Literal["vertex"] = "vertex"
    project_id: str | None = None
    location: str = "us-central1"


class ModelProviderBedrockSpec(_ModelProviderBase):
    """AWS Bedrock model provider."""

    backend: Literal["bedrock"] = "bedrock"
    region: str = "us-east-1"
    model_id: str = ""
    guardrail_id: str | None = None
    guardrail_version: str | None = None


class ModelProviderOpenAISpec(_ModelProviderBase):
    """OpenAI chat model provider."""

    backend: Literal["openai"] = "openai"
    api_key_handle: str = "OPENAI_API_KEY"


class ModelProviderAnthropicSpec(_ModelProviderBase):
    """Anthropic chat model provider."""

    backend: Literal["anthropic"] = "anthropic"
    api_key_handle: str = "ANTHROPIC_API_KEY"


class ModelProviderOllamaSpec(_ModelProviderBase):
    """Local Ollama chat model provider."""

    backend: Literal["ollama"] = "ollama"
    base_url: str = "http://localhost:11434"


_MODEL_PROVIDER_UNION = Annotated[
    ModelProviderVertexSpec | ModelProviderBedrockSpec | ModelProviderOpenAISpec | ModelProviderAnthropicSpec | ModelProviderOllamaSpec,
    Field(discriminator="backend"),
]

_ADAPTER: TypeAdapter[Any] = TypeAdapter(_MODEL_PROVIDER_UNION)


class ModelProviderSpec:
    """Discriminated-union wrapper for the model-provider surface."""

    @classmethod
    def model_validate(cls, data: Any) -> _ModelProviderBase:
        return _ADAPTER.validate_python(data)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> CoreSchema:
        return _ADAPTER.core_schema


__all__ = [
    "ModelProviderAnthropicSpec",
    "ModelProviderBedrockSpec",
    "ModelProviderOllamaSpec",
    "ModelProviderOpenAISpec",
    "ModelProviderSpec",
    "ModelProviderVertexSpec",
]
