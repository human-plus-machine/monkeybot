"""Shared interfaces for monkey-bot agent components.

This module defines interfaces and data structures used across the project.
The default **SSE gateway** path uses native :class:`~monkeybot.core.llm.provider.Provider`
implementations and SQLite-backed history rather than a separate graph-based
orchestration runtime.

Remaining interfaces support backward compatibility and the skills system.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

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
class SkillResult:
    """Result from skill execution.

    Attributes:
        success: True if skill executed successfully, False otherwise
        output: Skill output text (stdout or result data)
        error: Error message if success is False, None otherwise
        data: Optional structured data (for skills that return JSON/dicts)
    """

    success: bool
    output: str
    error: str | None = None
    data: dict[str, Any] | None = None


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


class SkillError(MonkeybotError):
    """Raised when skill execution fails.

    Examples:
        - Skill not found
        - Skill execution timeout
        - Terminal executor failure
        - Invalid skill arguments
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


# ============================================================================
# Skills Engine Interface
# ============================================================================


class SkillsEngineInterface(ABC):
    """Contract for Skills Engine.

    This interface defines how the agent interacts with the skills system.
    Implemented by SkillsEngine in src/skills/executor.py.
    
    Note: Skills are exposed as MCP or harness tools; this interface remains
    for backward compatibility with the subprocess-based skill execution model.

    Key responsibilities:
        - Execute skills by name
        - List available skills
        - Manage skill lifecycle
    """

    @abstractmethod
    async def execute_skill(self, skill_name: str, args: dict[str, Any]) -> SkillResult:
        """Execute a skill by name with arguments.

        Skills are discovered from ./skills/ directory at startup. Each skill
        has a SKILL.md file with metadata and a Python entry point.

        Args:
            skill_name: Skill identifier from SKILL.md (e.g., "file-ops", "memory")
            args: Skill arguments from tool call (e.g., {"path": "./data/memory/"})

        Returns:
            SkillResult with success status and output

        Raises:
            SkillError: If skill not found or execution fails

        Example:
            >>> skills = SkillsEngine(terminal_executor)
            >>> result = await skills.execute_skill(
            ...     skill_name="file-ops",
            ...     args={"action": "list", "path": "./data/memory/"}
            ... )
            >>> print(result.success)  # True
            >>> print(result.output)   # "file1.txt\nfile2.txt\n..."
        """
        pass

    @abstractmethod
    def list_skills(self) -> list[str]:
        """Return list of available skill names.

        Returns:
            List of skill names (e.g., ["file-ops", "memory", "shell"])

        Example:
            >>> skills = SkillsEngine(terminal_executor)
            >>> print(skills.list_skills())
            ["file-ops", "search-web", "post-content"]
        """
        pass
