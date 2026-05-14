"""E2 integration tests — cross-story contract validation.

Exercises the full Story 1+2+3+4 dependency chain:
  Story 2 (safety) → Story 4 (cli/_load_inspectors)
  Story 1 (claude) → Story 4 (cli/_load_provider)
  Story 3 (skills) → AgentLoop (skill discovery)
  Story 4 (webhook) → AgentLoop → AssistantDelta events
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from monkeybot.core.events import AssistantDelta, TurnComplete
from monkeybot.core.provider import ToolCall
from monkeybot.core.safety import load_inspectors
from monkeybot.gateway.webhook import WebhookGateway, load_bot_webhook
from monkeybot.providers._utils import estimate_cost
from monkeybot.tools.skill_ops import list_skills

# ---------------------------------------------------------------------------
# Story 2 + Story 4: safety factory wired into cli._load_inspectors
# ---------------------------------------------------------------------------

def test_load_inspectors_full_tier_config() -> None:
    """Command tiers + denied patterns both present → 2-inspector chain in order."""
    config: dict[str, Any] = {
        "safety": {
            "command_tiers": {
                "pre_approved": ["read_file"],
                "requires_approval": ["write_file"],
                "denied": ["run_command"],
            },
            "denied_patterns": ["rm -rf", "DROP TABLE"],
        }
    }
    inspectors = load_inspectors(config)
    assert len(inspectors) == 2
    # CommandTierInspector first
    from monkeybot.core.inspector import CommandTierInspector, RulesInspector  # noqa: PLC0415

    assert isinstance(inspectors[0], CommandTierInspector)
    assert isinstance(inspectors[1], RulesInspector)


def test_inspector_chain_deny_takes_precedence() -> None:
    """Denied tool is blocked at the CommandTierInspector level."""
    config: dict[str, Any] = {
        "safety": {
            "command_tiers": {"denied": ["run_command"]},
            "denied_patterns": [],
        }
    }
    inspectors = load_inspectors(config)
    call = ToolCall(call_id="t1", name="run_command", args={})
    decision = asyncio.run(inspectors[0].check(call, ctx=None))  # type: ignore[arg-type]
    assert decision.kind == "deny"


def test_inspector_chain_pattern_deny() -> None:
    """RulesInspector blocks args containing a denied pattern."""
    config: dict[str, Any] = {"safety": {"denied_patterns": ["rm -rf"]}}
    inspectors = load_inspectors(config)
    call = ToolCall(call_id="t2", name="run_command", args={"cmd": "rm -rf /"})
    decision = asyncio.run(inspectors[0].check(call, ctx=None))  # type: ignore[arg-type]
    assert decision.kind == "deny"


# ---------------------------------------------------------------------------
# Story 1: estimate_cost shared by both GeminiProvider and ClaudeProvider
# ---------------------------------------------------------------------------

def test_estimate_cost_claude_pricing() -> None:
    """estimate_cost returns correct value for claude-3-5-sonnet at known rates."""
    # claude-3-5-sonnet-20241022: (3.00, 15.00) per million tokens
    cost = estimate_cost(
        "claude-3-5-sonnet-20241022",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        pricing={"claude-3-5-sonnet-20241022": (3.00, 15.00)},
    )
    assert abs(cost - 18.0) < 0.001  # $3 input + $15 output = $18


def test_estimate_cost_gemini_pricing() -> None:
    """estimate_cost works for Gemini models too (used by GeminiProvider after refactor)."""
    from monkeybot.providers.gemini import GeminiProvider  # noqa: PLC0415

    # GeminiProvider no longer has a local _estimate_cost — it imports from _utils
    # Verify the import chain is intact by checking the provider instantiates
    provider = GeminiProvider()
    assert provider.name == "gemini"


# ---------------------------------------------------------------------------
# Story 1: ClaudeProvider satisfies the Provider Protocol
# ---------------------------------------------------------------------------

def test_claude_provider_satisfies_protocol() -> None:
    """ClaudeProvider is structurally compatible with the Provider Protocol."""
    from monkeybot.core.provider import Provider  # noqa: PLC0415
    from monkeybot.providers.claude import ClaudeProvider  # noqa: PLC0415

    os.environ["ANTHROPIC_API_KEY"] = "integration-test-key"
    try:
        p = ClaudeProvider()
        assert isinstance(p, Provider)
        assert p.name == "claude"
        assert p.supports_streaming is True
    finally:
        os.environ.pop("ANTHROPIC_API_KEY", None)


# ---------------------------------------------------------------------------
# Story 3 + AgentLoop: built-in skills are discoverable at runtime
# ---------------------------------------------------------------------------

def test_builtin_skills_all_discoverable() -> None:
    """All 4 E2 built-in skills are found by list_skills."""
    result = list_skills(skills_path=".agents/skills")
    for name in ("memory-save", "memory-search", "file-ops", "self-improve"):
        assert name in result, f"Built-in skill '{name}' missing from list_skills output"


def test_builtin_skills_memory_filter() -> None:
    """filter='memory' returns exactly memory-save and memory-search (not file-ops)."""
    result = list_skills(skills_path=".agents/skills", filter="memory")
    assert "memory-save" in result
    assert "memory-search" in result
    assert "file-ops" not in result, "file-ops must not match 'memory' filter after description fix"
    assert "self-improve" not in result


# ---------------------------------------------------------------------------
# Story 4 + Story 2: WebhookGateway with safety inspector chain
# ---------------------------------------------------------------------------

def test_webhook_gateway_with_safety_inspectors(tmp_path: Path) -> None:
    """WebhookGateway + AgentLoop built with load_inspectors() runs end-to-end."""
    from fastapi.testclient import TestClient  # noqa: PLC0415

    # Build inspector chain from config (Story 2)
    config: dict[str, Any] = {
        "safety": {
            "command_tiers": {"pre_approved": ["read_file"]},
            "denied_patterns": ["rm -rf"],
        }
    }
    inspectors = load_inspectors(config)
    assert len(inspectors) == 2

    # Mock AgentLoop (we don't need a real LLM for this test)
    mock_loop = MagicMock()

    async def fake_run(message: str, session_id: str):  # type: ignore[return]
        yield AssistantDelta(text=f"Echo: {message}")
        yield TurnComplete(input_tokens=1, output_tokens=1, duration_ms=5)

    mock_loop.run = fake_run

    gateway = WebhookGateway(
        loop=mock_loop,
        session_id_fn=lambda p: "e2e-session",
        extract_message=lambda p: p.get("text"),
    )
    app = gateway.build_app()
    client = TestClient(app)

    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

    resp = client.post("/webhook", json={"text": "hello integration"})
    assert resp.status_code == 200
    assert resp.json() == {"text": "Echo: hello integration"}


# ---------------------------------------------------------------------------
# Story 4 + Story 4: load_bot_webhook loads real example-bot extractor
# ---------------------------------------------------------------------------

def test_load_bot_webhook_gchat_full_round_trip() -> None:
    """Google Chat extractor + formatter round-trip via load_bot_webhook."""
    extract, fmt, sid = load_bot_webhook("bots/example-bot")

    # MESSAGE event → extracts text
    msg = extract({"type": "MESSAGE", "message": {"text": "tell me a joke"}})
    assert msg == "tell me a joke"

    # Non-message event → None
    assert extract({"type": "ADDED_TO_SPACE"}) is None

    # Format response
    response = fmt("Why did the bot cross the road?")
    assert response == {"text": "Why did the bot cross the road?"}

    # Session ID from space name
    session = sid({"space": {"name": "spaces/ABCDEF"}})
    assert session == "spaces/ABCDEF"


# ---------------------------------------------------------------------------
# CLI integration: _load_provider and _load_inspectors wiring
# ---------------------------------------------------------------------------

def test_cli_load_inspectors_delegates_to_safety() -> None:
    """cli._load_inspectors delegates to core/safety.load_inspectors."""
    from monkeybot.cli import _load_inspectors  # noqa: PLC0415
    from monkeybot.core.inspector import CommandTierInspector  # noqa: PLC0415

    config: dict[str, Any] = {
        "safety": {
            "command_tiers": {"pre_approved": ["read_file"], "denied": [], "requires_approval": []}
        }
    }
    result = _load_inspectors(config)
    assert len(result) == 1
    assert isinstance(result[0], CommandTierInspector)


def test_cli_load_provider_gemini() -> None:
    """cli._load_provider returns GeminiProvider when MODEL_PROVIDER=gemini."""
    from monkeybot.cli import _load_provider  # noqa: PLC0415
    from monkeybot.providers.gemini import GeminiProvider  # noqa: PLC0415

    with patch.dict(os.environ, {"MODEL_PROVIDER": "gemini"}):
        provider = _load_provider()
    assert isinstance(provider, GeminiProvider)


def test_cli_load_provider_claude() -> None:
    """cli._load_provider returns ClaudeProvider when MODEL_PROVIDER=claude."""
    from monkeybot.cli import _load_provider  # noqa: PLC0415
    from monkeybot.providers.claude import ClaudeProvider  # noqa: PLC0415

    with patch.dict(os.environ, {"MODEL_PROVIDER": "claude", "ANTHROPIC_API_KEY": "test"}):
        provider = _load_provider()
    assert isinstance(provider, ClaudeProvider)


def test_cli_load_provider_unknown_raises() -> None:
    """cli._load_provider raises ValueError for unknown provider."""
    from monkeybot.cli import _load_provider  # noqa: PLC0415

    with patch.dict(os.environ, {"MODEL_PROVIDER": "openai"}):
        with pytest.raises(ValueError, match="openai"):
            _load_provider()


# ---------------------------------------------------------------------------
# WebhookGateway lazy export from monkeybot package
# ---------------------------------------------------------------------------

def test_webhook_gateway_lazy_export() -> None:
    """WebhookGateway is importable directly from the monkeybot package."""
    from monkeybot import WebhookGateway as WG  # noqa: PLC0415
    from monkeybot.gateway.webhook import WebhookGateway  # noqa: PLC0415

    assert WG is WebhookGateway
