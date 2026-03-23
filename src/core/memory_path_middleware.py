"""Rewrite workspace_* tool paths using ``config['configurable']['memory_context_dir']``.

``campaign_dir`` is accepted as an alias (HTTP / transition). See also ``resolve_virtual_path``.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

# Paths that must never get ``memory_context_dir`` prepended (repo-root-relative, POSIX slashes).
_SKIP_CONTEXT_PREFIX: tuple[str, ...] = (
    "data/memory/global/",
    "data/memory/campaigns/",  # already scoped; avoid doubling
    "skills/",
    "config/",
    "src/",
    "prompts/",
    "tests/",
    ".venv/",
    "static/",
    "subagents/",
)

_FS_TOOLS_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "workspace_read_file": ("path",),
    "workspace_write_file": ("path",),
    "workspace_replace_in_file": ("path",),
    "workspace_glob": ("root",),
    "workspace_grep": ("root",),
}


def normalize_context_dir(context_dir: str | None) -> str | None:
    if context_dir is None:
        return None
    s = str(context_dir).strip().replace("\\", "/").strip("/")
    return s or None


def resolve_virtual_path(raw: str, *, context_dir: str | None, repo_root: Path) -> str:
    """Rewrite a tool path for scoped memory sessions.

    - Host-absolute paths that lie under ``repo_root`` become repo-relative POSIX paths
      (what deepagents then normalizes to a leading ``/`` virtual path).
    - If ``context_dir`` is set, short relative paths (e.g. ``STRATEGY.md``,
      ``research/x.md``) are prefixed with ``context_dir/``.
    - Leaves paths unchanged when they match global/skill roots or already include
      ``data/memory/campaigns/``.

    Does not expand ``..`` or ``~``; returns ``raw`` unchanged so existing validation errors
    still apply.
    """
    if raw is None:
        return raw
    s = raw.strip()
    if not s:
        return raw
    if ".." in s or s.startswith("~"):
        return raw
    if re.match(r"^[a-zA-Z]:", s):
        return raw

    repo = repo_root.resolve()
    cd = normalize_context_dir(context_dir)

    # 1) Host-absolute under repo -> relative to repo (POSIX, no leading slash)
    p = Path(s)
    if p.is_absolute():
        try:
            resolved = p.resolve()
            rel = resolved.relative_to(repo)
            return rel.as_posix()
        except (ValueError, OSError):
            pass

    # Normalize for prefix checks (virtual ``/data/...`` -> ``data/...``)
    rel_s = s.replace("\\", "/").lstrip("./")
    key = rel_s.lstrip("/")

    for prefix in _SKIP_CONTEXT_PREFIX:
        if key.startswith(prefix) or key == prefix.rstrip("/"):
            return raw

    if not cd:
        return raw

    # Already full repo-relative ``data/...`` outside campaign folder — do not prepend
    if key.startswith("data/") and not key.startswith("data/memory/campaigns/"):
        return raw

    return f"{cd}/{key}".replace("//", "/")


def _memory_context_dir_from_config(config: Any) -> str | None:
    if not isinstance(config, dict):
        return None
    conf = config.get("configurable")
    if not isinstance(conf, dict):
        return None
    raw = conf.get("memory_context_dir")
    if raw is None:
        raw = conf.get("campaign_dir")
    if raw is None:
        return None
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s or None


def _maybe_rewrite_request(request: ToolCallRequest, repo_root: Path) -> ToolCallRequest:
    call = request.tool_call
    name = call.get("name")
    if name not in _FS_TOOLS_PATH_KEYS:
        return request

    cfg = getattr(request.runtime, "config", None)
    context_dir = _memory_context_dir_from_config(cfg if isinstance(cfg, dict) else {})

    args = dict(call.get("args") or {})
    changed = False

    if name == "workspace_write_file" and ("content" not in args or args.get("content") is None):
        args["content"] = ""
        changed = True

    if name == "workspace_replace_in_file":
        if "old_string" not in args or args.get("old_string") is None:
            args["old_string"] = ""
            changed = True
        if "new_string" not in args or args.get("new_string") is None:
            args["new_string"] = ""
            changed = True

    for key in _FS_TOOLS_PATH_KEYS[name]:
        if key not in args or args[key] is None:
            continue
        val = args[key]
        if not isinstance(val, str):
            continue
        new_val = resolve_virtual_path(val, context_dir=context_dir, repo_root=repo_root)
        if new_val != val:
            args[key] = new_val
            changed = True

    if not changed:
        return request
    new_call = {**call, "args": args}
    return request.override(tool_call=new_call)


class MemoryPathMiddleware(AgentMiddleware[AgentState[Any], None, Any]):
    """Normalizes host paths and applies ``memory_context_dir`` prefix to workspace_* tools."""

    def __init__(self, *, repo_root: Path) -> None:
        super().__init__()
        self._repo_root = Path(repo_root).resolve()
        self.tools = []

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command[Any]],
    ) -> ToolMessage | Command[Any]:
        req = _maybe_rewrite_request(request, self._repo_root)
        return handler(req)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        req = _maybe_rewrite_request(request, self._repo_root)
        return await handler(req)


# Backward-compatible alias
CampaignPathMiddleware = MemoryPathMiddleware
