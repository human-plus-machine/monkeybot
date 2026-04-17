"""Functional Bedrock tool-use roundtrip test (Story 7, MP-C-03).

Exercises the :class:`BedrockProvider` → ``ChatBedrockConverse`` →
``bind_tools`` → ``invoke`` pipeline end-to-end with a synthetic
``langchain_aws`` module so the roundtrip can run under CI without the
optional ``emonk[model-provider-bedrock]`` extra installed. The full
schema pin against a live boto3 ``Converse`` call is gated behind the
integration marker and runs once the real SDK is available.
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from src.core.harness.extensions.model_providers import BedrockProvider
from src.core.harness.specs import AgentSpec


class _FakeConverseChat:
    """Synthetic ``ChatBedrockConverse`` capturing tool bindings and invoke calls."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.bound_tools: list[Any] | None = None
        self.invocations: list[Any] = []

    def bind_tools(self, tools: list[Any]) -> _FakeConverseChat:
        self.bound_tools = list(tools)
        return self

    def invoke(self, messages: Any, **_: Any) -> dict[str, Any]:
        self.invocations.append(messages)
        tool = (self.bound_tools or [{"name": "unknown"}])[0]
        tool_name = tool.get("name") if isinstance(tool, dict) else getattr(tool, "name", "unknown")
        return {
            "role": "assistant",
            "content": [
                {
                    "toolUse": {
                        "name": tool_name,
                        "input": {"msg": "hello"},
                    }
                }
            ],
        }


@pytest.fixture
def fake_bedrock(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    fake = ModuleType("langchain_aws")
    fake.ChatBedrockConverse = _FakeConverseChat  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "langchain_aws", fake)
    return fake


def test_bedrock_tool_use_roundtrip(fake_bedrock: ModuleType) -> None:
    """Bind an ``echo`` tool, invoke, and assert the structured tool-use response."""
    provider = BedrockProvider(
        region="us-east-1",
        model_id="anthropic.claude-3-5-sonnet-20241022-v2:0",
    )
    agent = AgentSpec(name="tool-user", temperature=0.0, max_output_tokens=256)

    chat = provider.build(agent)
    bound = chat.bind_tools(
        [
            {
                "name": "echo",
                "description": "echo the message back",
                "input_schema": {
                    "type": "object",
                    "properties": {"msg": {"type": "string"}},
                    "required": ["msg"],
                },
            }
        ]
    )

    response = bound.invoke([{"role": "user", "content": "call echo with msg=hello"}])

    assert isinstance(response, dict)
    tool_use = response["content"][0]["toolUse"]
    assert tool_use["name"] == "echo"
    assert tool_use["input"] == {"msg": "hello"}
    assert bound.kwargs["model_id"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert bound.kwargs["region_name"] == "us-east-1"
    assert bound.kwargs["temperature"] == 0.0
    assert bound.kwargs["max_tokens"] == 256


def test_bedrock_guardrail_is_forwarded(fake_bedrock: ModuleType) -> None:
    """When the spec carries a guardrail id/version the SDK kwarg is populated."""
    provider = BedrockProvider(
        model_id="anthropic.claude-3-5-sonnet",
        guardrail_id="gr-42",
        guardrail_version="1",
    )
    chat = provider.build(AgentSpec(name="t"))
    assert chat.kwargs["guardrail_config"] == {
        "guardrailIdentifier": "gr-42",
        "guardrailVersion": "1",
    }
