"""
Interface contracts for the Gateway module.

This module defines the contract between the Gateway (HTTP interface) and the
Agent Core (LLM orchestration). The AgentCoreInterface enables parallel development
of Gateway and Agent Core in Sprint 1.
"""

from abc import ABC, abstractmethod


class AgentCoreInterface(ABC):
    """
    Contract for Agent Core that Gateway will call.

    The Gateway is responsible for:
    - Receiving Google Chat webhooks
    - Filtering PII (hashing emails, stripping metadata)
    - Validating user authorization

    The Agent Core (implementing this interface) is responsible for:
    - Loading conversation history
    - Calling LLM with context
    - Executing skills
    - Saving conversation history
    - Returning response text

    This interface enables Sprint 1 parallel development:
    - Story 1 (Gateway) uses MockAgentCore
    - Story 2 (Agent Core) implements real AgentCore
    - Story 4 (Integration) wires them together
    """

    @abstractmethod
    async def process_message(self, user_id: str, content: str, trace_id: str) -> str:
        """
        Process a user message and return response text.

        This is the main entry point for agent processing. The Gateway has already:
        - Validated the user is authorized (checked ALLOWED_USERS)
        - Filtered PII (hashed email to user_id, stripped Google Chat metadata)
        - Generated a trace_id for debugging

        Args:
            user_id: Hashed user identifier (SHA-256, 16 chars).
                     NOT the email address (PII already filtered).
                     Same user_id for same email across requests (stable hashing).
            content: Message text from user.
                     PII already filtered, only the message content.
            trace_id: Request trace ID for debugging and logging.
                     UUID v4 format, unique per request.

        Returns:
            Response text to send back to user via Google Chat.
            Should be < 4000 characters (Gateway will truncate if needed).

        Raises:
            AgentError: If processing fails (LLM error, skill error, etc.)
                       Gateway will catch this and return 500 to user.

        Example:
            >>> agent = AgentCore(...)
            >>> response = await agent.process_message(
            ...     user_id="a1b2c3d4e5f6g7h8",
            ...     content="Remember that I prefer Python",
            ...     trace_id="550e8400-e29b-41d4-a716-446655440000"
            ... )
            >>> print(response)
            "✅ I'll remember that. Stored: code_language_preference = Python"
        """
        pass


class AgentError(Exception):
    """
    Raised when Agent Core processing fails.

    This is the base exception for all Agent Core errors. Specific error types
    (LLMError, SkillError, MemoryError) will inherit from this in Story 2.

    The Gateway catches AgentError and returns a 500 Internal Server Error to the user.

    Attributes:
        message: Human-readable error message (safe to show to user)
        trace_id: Request trace ID for debugging
        cause: Original exception that caused the error (optional)

    Example:
        >>> try:
        ...     result = await agent.process_message(...)
        ... except AgentError as e:
        ...     logger.error(f"Agent processing failed: {e.message}", extra={"trace_id": e.trace_id})
        ...     return {"error": e.message}
    """

    def __init__(self, message: str, trace_id: str | None = None, cause: Exception | None = None):
        """
        Initialize AgentError.

        Args:
            message: Human-readable error message
            trace_id: Request trace ID for debugging (optional)
            cause: Original exception that caused the error (optional)
        """
        self.message = message
        self.trace_id = trace_id
        self.cause = cause
        super().__init__(message)

    def __str__(self) -> str:
        """Return string representation of error."""
        if self.trace_id:
            return f"{self.message} (trace_id: {self.trace_id})"
        return self.message
