"""Memory tools — save and search agent memory files."""
from __future__ import annotations

from monkeybot.core.memory import save_memory as _save_memory
from monkeybot.core.memory import search_memory as _search_memory

SAVE_TOOL_DEF = {
    "name": "save_memory",
    "description": (
        "Save a note to agent memory. Use this to persist user preferences, "
        "facts, or context across conversations. The note overwrites any "
        "existing file with the same name."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Short slug for the memory file, e.g. 'user-prefs' or 'project-context'",
            },
            "content": {
                "type": "string",
                "description": "Full markdown content to save",
            },
        },
        "required": ["filename", "content"],
    },
}

SEARCH_TOOL_DEF = {
    "name": "search_memory",
    "description": (
        "Search agent memory files for relevant information. "
        "Returns matching file excerpts. Use before answering questions "
        "that might be covered by past memory."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Keywords to search for"},
            "max_results": {
                "type": "integer",
                "description": "Max files to return (default: 5)",
            },
        },
        "required": ["query"],
    },
}

TOOL_DEF = SEARCH_TOOL_DEF  # backwards-compat alias
TOOL_DEFS = [SAVE_TOOL_DEF, SEARCH_TOOL_DEF]


def save_memory(filename: str, content: str, memory_path: str) -> str:
    """Delegate to core.memory.save_memory."""
    return _save_memory(memory_path, filename, content)


def search_memory(query: str, memory_path: str, max_results: int = 5) -> str:
    """Delegate to core.memory.search_memory."""
    return _search_memory(query, memory_path, max_results)
