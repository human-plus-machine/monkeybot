"""
Mock implementations for Gateway testing.

This module provides mock implementations that enable Sprint 1 parallel development:
- MockAgentCore: Simple echo implementation for Gateway testing
- MockScheduler: Simple mock scheduler for testing /cron/tick endpoint

Real implementations will be wired in Story 4 (Integration).
"""

from __future__ import annotations

from . import interfaces


class MockScheduler:
    """
    Mock Scheduler for Gateway testing.
    
    This mock implementation enables /cron/tick endpoint testing without
    requiring a real scheduler backend.
    
    Behavior:
        - run_tick() returns empty metrics (no jobs)
        - Never raises exceptions (always succeeds)
        - Useful for testing Gateway's cron endpoint handling
    """
    
    async def run_tick(self) -> dict[str, int]:
        """
        Mock scheduler tick that returns empty metrics.
        
        Returns:
            Dict with execution metrics (all zeros for mock)
        """
        return {
            "jobs_checked": 0,
            "jobs_due": 0,
            "jobs_executed": 0,
            "jobs_succeeded": 0,
            "jobs_failed": 0,
        }


class MockAgentCore(interfaces.AgentCoreInterface):  # type: ignore[misc]
    """
    Mock Agent Core for Gateway testing.

    This mock implementation enables Story 1 (Gateway) to be developed and tested
    independently in Sprint 1, without waiting for Story 2 (Agent Core) to complete.

    Behavior:
        - Returns simple echo response with trace_id
        - Never raises AgentError (always succeeds)
        - Useful for testing Gateway's request/response handling

    Real Agent Core (Story 2) will:
        - Load conversation history from Memory Manager
        - Call LLM with context
        - Execute skills via Skills Engine
        - Save conversation history
        - Return contextual responses

    Story 4 (Integration) will replace this mock with real Agent Core.

    Example:
        >>> mock = MockAgentCore()
        >>> response = await mock.process_message(
        ...     user_id="a1b2c3d4e5f6g7h8",
        ...     content="Hello",
        ...     trace_id="550e8400-e29b-41d4-a716-446655440000"
        ... )
        >>> print(response)
        'Echo: Hello (trace: 550e8400-e29b-41d4-a716-446655440000)'
    """
    
    def __init__(self):
        """Initialize MockAgentCore with mock scheduler."""
        self.scheduler = MockScheduler()

    async def process_message(self, user_id: str, content: str, trace_id: str) -> str:
        """
        Return simple echo response for testing.

        Args:
            user_id: Hashed user identifier (not used by mock)
            content: Message text (echoed back)
            trace_id: Request trace ID (included in response)

        Returns:
            Echo response with content and trace_id
        """
        return f"Echo: {content} (trace: {trace_id})"
