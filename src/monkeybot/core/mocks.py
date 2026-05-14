"""Mock implementations for parallel development and testing.

These mocks allow Story 2 (Agent Core) to be developed and tested independently
while Story 1 (Gateway) and Story 3 (Skills/Memory) are being built in parallel.

Story 4 (Integration) replaces these mocks with real implementations.
"""

import logging
from datetime import datetime
from typing import Any

from .interfaces import Message, SkillResult, SkillsEngineInterface

logger = logging.getLogger(__name__)


class MockSkillsEngine(SkillsEngineInterface):
    """Mock Skills Engine for Agent Core testing.

    Returns canned responses for skill execution without real terminal execution.
    Useful for:
        - Unit testing Agent Core in isolation
        - Parallel development (Agent Core doesn't wait for Skills Engine)
        - Fast test execution (no subprocess overhead)

    Story 4 replaces this with real SkillsEngine implementation from Story 3.
    """

    async def execute_skill(self, skill_name: str, args: dict[str, Any]) -> SkillResult:
        """Return mock success response for any skill.

        Args:
            skill_name: Skill identifier (e.g., "file-ops", "memory-remember")
            args: Skill arguments (e.g., {"path": "./data/memory/"})

        Returns:
            SkillResult with mock output
        """
        logger.info(
            f"[MOCK] Executing skill: {skill_name}",
            extra={"component": "mock_skills_engine", "skill": skill_name, "args": args},
        )

        # Return context-appropriate responses for common skills
        if skill_name == "memory-remember":
            key = args.get("key", "unknown")
            value = args.get("value", "unknown")
            return SkillResult(success=True, output=f"✅ Stored: {key} = {value}")
        elif skill_name == "memory-recall":
            key = args.get("key", "unknown")
            return SkillResult(success=True, output=f"Retrieved: {key} = Python")
        elif skill_name == "file-ops":
            action = args.get("action", "list")
            return SkillResult(
                success=True,
                output=f"Mock file operation: {action} completed successfully",
            )
        else:
            # Generic mock response
            return SkillResult(
                success=True, output=f"Mock execution of '{skill_name}' with args: {args}"
            )

    def list_skills(self) -> list[str]:
        """Return list of mock skills.

        Returns:
            List of mock skill names
        """
        return ["memory-remember", "memory-recall", "file-ops"]


class MockMemoryManager:
    """Mock Memory Manager for Agent Core testing.

    Stores data in-memory (dict) instead of filesystem/GCS.
    Properly isolates data by user_id for realistic testing.

    Useful for:
        - Unit testing without filesystem I/O
        - Fast test execution
        - No cleanup required (data disappears after test)

    Story 4 replaces this with real MemoryManager implementation from Story 3.
    """

    def __init__(self) -> None:
        """Initialize in-memory storage with per-user isolation."""
        # Store conversation history per user: {user_id: [Message, ...]}
        self.conversation_histories: dict[str, list[Message]] = {}
        # Store facts per user: {user_id: {key: value}}
        self.user_facts: dict[str, dict[str, str]] = {}
        # Mock memory directory (ephemeral temp for tests)
        from pathlib import Path
        import tempfile
        self.memory_dir = Path(tempfile.mkdtemp())

    async def read_conversation_history(self, user_id: str, limit: int = 10) -> list[Message]:
        """Return last N messages from in-memory history for specific user.

        Args:
            user_id: User identifier (properly isolated per user)
            limit: Max messages to return

        Returns:
            Last N messages for this user (oldest first)
        """
        if user_id not in self.conversation_histories:
            self.conversation_histories[user_id] = []

        messages = self.conversation_histories[user_id][-limit:]
        logger.info(
            f"[MOCK] Read conversation history: {len(messages)} messages",
            extra={"component": "mock_memory_manager", "user_id": user_id, "count": len(messages)},
        )
        return messages

    async def write_conversation(
        self, user_id: str, role: str, content: str, trace_id: str
    ) -> None:
        """Append message to in-memory history for specific user.

        Args:
            user_id: User identifier
            role: "user" or "assistant"
            content: Message content
            trace_id: Request trace ID
        """
        if user_id not in self.conversation_histories:
            self.conversation_histories[user_id] = []

        self.conversation_histories[user_id].append(
            Message(
                role=role,
                content=content,
                timestamp=datetime.now().isoformat(),
                trace_id=trace_id,
            )
        )
        logger.info(
            f"[MOCK] Wrote conversation: role={role}",
            extra={
                "component": "mock_memory_manager",
                "user_id": user_id,
                "role": role,
                "content_length": len(content),
                "trace_id": trace_id,
            },
        )

    async def read_fact(self, user_id: str, key: str) -> str | None:
        """Read fact from in-memory dict for specific user.

        Args:
            user_id: User identifier
            key: Fact key

        Returns:
            Fact value if exists, None otherwise
        """
        if user_id not in self.user_facts:
            return None

        value = self.user_facts[user_id].get(key)
        logger.info(
            f"[MOCK] Read fact: {key}={'found' if value else 'not found'}",
            extra={
                "component": "mock_memory_manager",
                "user_id": user_id,
                "key": key,
                "found": value is not None,
            },
        )
        return value

    async def write_fact(self, user_id: str, key: str, value: str) -> None:
        """Write fact to in-memory dict for specific user.

        Args:
            user_id: User identifier
            key: Fact key
            value: Fact value
        """
        if user_id not in self.user_facts:
            self.user_facts[user_id] = {}

        self.user_facts[user_id][key] = value
        logger.info(
            f"[MOCK] Wrote fact: {key}={value[:50]}...",
            extra={
                "component": "mock_memory_manager",
                "user_id": user_id,
                "key": key,
                "value_length": len(value),
            },
        )


class MockVertexAI:
    """Mock Vertex AI client for LLM testing.

    Returns canned responses without real API calls.
    Useful for:
        - Unit testing without API costs
        - Fast test execution (no network calls)
        - Predictable responses for testing

    Story 4 replaces this with real ChatVertexAI from langchain-google-vertexai.

    Note:
        This mock mimics the interface of LangChain's ChatVertexAI but returns
        simple string responses instead of AIMessage objects. This is intentional
        to keep the mock simple while still testing the core agent logic.
    """

    async def ainvoke(self, messages: list[dict[str, str]]) -> str:
        """Return mock LLM response based on last user message.

        Args:
            messages: List of message dicts [{"role": "user", "content": "..."}]

        Returns:
            Mock response text
        """
        if not messages:
            return "Mock LLM: Empty conversation"

        last_message = messages[-1]["content"]

        # Return context-appropriate responses for common patterns
        if "remember" in last_message.lower() and "python" in last_message.lower():
            return "I'll remember that you prefer Python for all code examples."
        elif "recall" in last_message.lower() or "what" in last_message.lower():
            return "According to my memory, you prefer Python for code examples."
        elif "list" in last_message.lower() and "file" in last_message.lower():
            return "Here are the files in ./data/memory/:\n- SYSTEM_PROMPT.md\n- CONVERSATION_HISTORY/\n- KNOWLEDGE_BASE/"
        elif "hello" in last_message.lower() or "hi" in last_message.lower():
            return "Hello! I'm Monkeybot, your AI assistant. How can I help you today?"
        else:
            # Generic mock response
            return f"Mock LLM response to: {last_message[:50]}{'...' if len(last_message) > 50 else ''}"
