"""Core agent components.

This package contains the core building blocks for monkey-bot:
- build_deep_agent: Factory for creating deep agents (recommended)
- build_agent: Factory for creating LangChain v1 agents (deprecated)
- FirestoreStore: Firestore-backed long-term memory (default)
- GCSStore: GCS-backed long-term memory (legacy)
- DriveFilesystemSync: Google Drive ↔ local filesystem sync
- GCSFilesystemSync: GCS ↔ local filesystem sync (legacy)
- SessionSummaryMiddleware: Per-session memory persistence
- TerminalExecutor: Secure command execution (legacy/optional)
"""

from .agent import build_agent, create_agent_with_mocks, AgentWrapper
from .agent_guard_middleware import (
    DuplicateToolErrorCompactionMiddleware,
    ToolOutputTruncationMiddleware,
    build_default_guard_middleware_stack,
)
from .deepagent import build_deep_agent
from .drive_filesystem_sync import DriveFilesystemSync
from .filesystem_sync import GCSFilesystemSync
from .firestore_store import FirestoreStore
from .interfaces import (
    AgentCoreInterface,
    AgentError,
    EmonkError,
    LLMError,
    Message,
    SecurityError,
    SkillError,
    SkillResult,
    SkillsEngineInterface,
    ExecutionResult,
)
from .store import GCSStore, create_search_memory_tool
from .middleware import SessionSummaryMiddleware
from .terminal import ALLOWED_COMMANDS, ALLOWED_PATHS, TerminalExecutor

__all__ = [
    # Agent
    "build_deep_agent",
    "build_default_guard_middleware_stack",
    "DuplicateToolErrorCompactionMiddleware",
    "ToolOutputTruncationMiddleware",
    "build_agent",
    "AgentWrapper",
    "create_agent_with_mocks",
    # Interfaces
    "AgentCoreInterface",
    "SkillsEngineInterface",
    # Data classes
    "Message",
    "SkillResult",
    "ExecutionResult",
    # Exceptions
    "EmonkError",
    "AgentError",
    "LLMError",
    "SkillError",
    "SecurityError",
    # Store
    "FirestoreStore",
    "GCSStore",
    "create_search_memory_tool",
    # Filesystem sync
    "DriveFilesystemSync",
    "GCSFilesystemSync",
    # Middleware
    "SessionSummaryMiddleware",
    # Terminal (legacy/optional)
    "TerminalExecutor",
    "ALLOWED_COMMANDS",
    "ALLOWED_PATHS",
]
