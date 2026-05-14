"""read_file + write_file — basic filesystem access."""
from __future__ import annotations

from pathlib import Path

TOOL_DEFS = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. Use for skill files, memory files, config files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to read"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write content to a file. Creates parent directories if needed.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path to write"},
                "content": {"type": "string", "description": "Content to write"},
                "append": {
                    "type": "boolean",
                    "description": "Append instead of overwrite (default: false)",
                },
            },
            "required": ["path", "content"],
        },
    },
]


def read_file(path: str, *, allowed_roots: list[Path] | None = None) -> str:
    """Returns file content, or 'ERROR: ...' string if missing or access denied."""
    resolved = Path(path).resolve()
    if allowed_roots is not None:
        if not any(str(resolved).startswith(str(r.resolve())) for r in allowed_roots):
            return f"ERROR: Access denied: {path}"
    if not resolved.exists():
        return f"ERROR: File not found: {path}"
    return resolved.read_text()


def write_file(
    path: str,
    content: str,
    append: bool = False,
    *,
    allowed_roots: list[Path] | None = None,
) -> str:
    """Creates parent dirs. Returns 'OK: wrote {n} chars to {path}'."""
    resolved = Path(path).resolve()
    if allowed_roots is not None:
        if not any(str(resolved).startswith(str(r.resolve())) for r in allowed_roots):
            return f"ERROR: Access denied: {path}"
    resolved.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with resolved.open(mode) as f:
        f.write(content)
    return f"Success: wrote {len(content)} chars to {path}"
