"""AWS Bedrock :class:`ModelProvider` (Story 7).

Wraps :class:`langchain_aws.ChatBedrockConverse` (the Converse-API variant,
**not** the deprecated ``ChatBedrock``). The ``langchain_aws`` import is
lazy; just importing this module is safe even when the optional
dependency is not installed.

The MP-C-03 contract (1B §11.5) pins the Bedrock tool-use schema against
the published ``boto3`` Converse shape — see
``tests/harness/extensions/model_providers/test_bedrock.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..base import ModelProvider
from ..values import ModelCapabilities

if TYPE_CHECKING:
    from langchain_core.language_models import BaseChatModel

    from ...specs import AgentSpec


class BedrockProvider(ModelProvider):
    """Resolve AWS Bedrock chat models via the Converse API.

    Args:
        region: AWS region for the Bedrock endpoint. Defaults to ``us-east-1``.
        model_id: Bedrock model identifier (e.g.
            ``anthropic.claude-3-5-sonnet-20241022-v2:0``). Empty string
            defers to ``agent_spec.model`` at build time.
        guardrail_id: Optional Bedrock guardrail id to enforce on every
            call. When set, ``guardrail_config`` is forwarded to the SDK.
        guardrail_version: Guardrail version paired with ``guardrail_id``
            (defaults to ``"DRAFT"`` when only the id is supplied).
    """

    def __init__(
        self,
        *,
        region: str = "us-east-1",
        model_id: str = "",
        guardrail_id: str | None = None,
        guardrail_version: str | None = None,
    ) -> None:
        self.region = region
        self.model_id = model_id
        self.guardrail_id = guardrail_id
        self.guardrail_version = guardrail_version

    def build(self, spec: AgentSpec) -> BaseChatModel:
        """Return a configured :class:`ChatBedrockConverse` for ``spec``.

        The Converse client is preferred over the deprecated ``ChatBedrock``
        because it exposes a stable tool-use schema that MP-C-03 pins in
        CI. When ``self.guardrail_id`` is set, the Bedrock guardrail
        config is forwarded to the SDK.
        """
        from langchain_aws import ChatBedrockConverse

        model_id = self.model_id or spec.model
        kwargs: dict[str, Any] = {
            "model_id": model_id,
            "region_name": self.region,
            "temperature": spec.temperature,
            "max_tokens": spec.max_output_tokens,
        }
        if self.guardrail_id:
            kwargs["guardrail_config"] = {
                "guardrailIdentifier": self.guardrail_id,
                "guardrailVersion": self.guardrail_version or "DRAFT",
            }
        kwargs.update(spec.extra_model_kwargs or {})
        return ChatBedrockConverse(**kwargs)

    def capabilities(self) -> ModelCapabilities:
        """Capabilities reflect Anthropic-on-Bedrock defaults (tool calling, 200K ctx)."""
        return ModelCapabilities(
            tool_calling=True,
            streaming=True,
            thinking=False,
            vision=False,
            max_context_tokens=200_000,
        )


__all__ = ["BedrockProvider"]
