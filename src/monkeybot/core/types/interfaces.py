"""Shared interfaces for monkeybot agent components.

This module defines interfaces and data structures used across the project.
The default **SSE gateway** path uses native :class:`~monkeybot.core.llm.provider.Provider`
implementations and SQLite-backed history rather than a separate graph-based
orchestration runtime.

Remaining interfaces support backward compatibility.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

# ============================================================================
# Data Classes
# ============================================================================


@dataclass
class Message:
    """Conversation message.

    Attributes:
        role: Message role ("user", "assistant", or "system")
        content: Message text content
        timestamp: ISO8601 timestamp when message was created
        trace_id: Request trace ID for debugging and log correlation
    """

    role: str
    content: str
    timestamp: str
    trace_id: str


@dataclass
class ExecutionResult:
    """Result from terminal command execution.

    Attributes:
        stdout: Standard output from command
        stderr: Standard error from command
        exit_code: Process exit code (0 = success, non-zero = error)
    """

    stdout: str
    stderr: str
    exit_code: int


# ============================================================================
# Exceptions
# ============================================================================


class MonkeybotError(Exception):
    """Base exception for all Monkeybot errors.

    All custom exceptions in Monkeybot inherit from this base class.
    This allows catching all Monkeybot-specific errors with a single except clause.
    """

    pass


class AgentError(MonkeybotError):
    """Raised when agent processing fails.

    Examples:
        - LLM call fails
        - Message processing fails
        - Graph execution error
    """

    pass


class LLMError(MonkeybotError):
    """Raised when LLM API call fails.

    Examples:
        - Vertex AI timeout
        - Rate limit exceeded (429)
        - Model unavailable (503)
        - Invalid API credentials
        
    Note: Provider-level LLM errors are handled by the streaming adapter; this
    exception is kept for explicit error handling when needed.
    """

    pass


class SecurityError(MonkeybotError):
    """Raised when security validation fails.

    Examples:
        - Command not in ALLOWED_COMMANDS
        - Path not in ALLOWED_PATHS
        - Unauthorized user access attempt

    This is a critical security boundary - all SecurityErrors should be logged
    and investigated.
    """

    pass


# ============================================================================
# Agent Core Interface (for Gateway compatibility)
# ============================================================================


class AgentCoreInterface(ABC):
    """Contract that Gateway calls.

    This interface defines how external components (Gateway) interact with
    the agent. Implemented by AgentWrapper in src/core/agent.py.

    Key responsibilities:
        - Process user messages
        - Maintain conversation context (implementation-specific storage)
        - Execute tools/skills
        - Return formatted responses
    """

    @abstractmethod
    async def process_message(self, user_id: str, content: str, trace_id: str) -> str:
        """Process user message and return response.

        This is the main entry point for all user interactions. The Gateway
        filters PII before calling this method, so user_id is already hashed
        and content contains only safe user input.

        Args:
            user_id: Hashed user identifier (NOT email - already filtered by Gateway)
            content: Message text (PII already filtered by Gateway)
            trace_id: Request trace ID for debugging and log correlation

        Returns:
            Response text to send back to user via Gateway

        Raises:
            AgentError: If processing fails (LLM error, tool error, etc.)

        Example:
            >>> agent = build_agent(model, tools)
            >>> response = await agent.process_message(
            ...     user_id="abc123",
            ...     content="What can you help me with?",
            ...     trace_id="trace_xyz"
            ... )
            >>> print(response)
            "I can help you with..."
        """
        pass
