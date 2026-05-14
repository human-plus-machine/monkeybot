"""MonkeyBot v2 — lightweight agent framework."""
from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["AgentLoop", "ConversationHistory", "Provider", "TurnContext", "WebhookGateway"]


def __getattr__(name: str) -> object:
    if name == "AgentLoop":
        from monkeybot.core.loop import AgentLoop
        return AgentLoop
    if name == "ConversationHistory":
        from monkeybot.core.history import ConversationHistory
        return ConversationHistory
    if name == "Provider":
        from monkeybot.core.provider import Provider
        return Provider
    if name == "TurnContext":
        from monkeybot.core.context import TurnContext
        return TurnContext
    if name == "WebhookGateway":
        from monkeybot.gateway.webhook import WebhookGateway
        return WebhookGateway
    raise AttributeError(f"module 'monkeybot' has no attribute {name!r}")
