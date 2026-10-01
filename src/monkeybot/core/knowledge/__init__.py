"""Unified knowledge layer — local FTS + ANN + link graph + ``search``."""

from monkeybot.core.knowledge.config import (
    knowledge_enabled_from_config,
    knowledge_read_only_from_env,
    resolve_knowledge_settings,
)
from monkeybot.core.knowledge.subsystem import KnowledgeSubsystem, open_knowledge_for_gateway
from monkeybot.core.knowledge.types import KnowledgeSettings, RecallHit

__all__ = [
    "KnowledgeSettings",
    "KnowledgeSubsystem",
    "RecallHit",
    "open_knowledge_for_gateway",
    "knowledge_enabled_from_config",
    "knowledge_read_only_from_env",
    "resolve_knowledge_settings",
]
