"""Per-session hiding of connected MCP servers that go unused.

The process keeps one :class:`~monkeybot.core.mcp.mcp_client.MCPClient`. Hiding
drops a server's tools from one chat's tool list. The connection stays up for
every other session.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from monkeybot.core.types.types_tools import ToolDef

# Inner turns (model calls) a server's tools may be offered without use.
IDLE_INNER_TURNS = 10

# Meta-tools whose ``server`` argument names one MCP server.
_SERVER_ARG_TOOLS = frozenset(
    {
        "read_mcp_resource",
        "get_mcp_prompt",
        "list_mcp_resources",
        "list_mcp_prompts",
    }
)


def idle_note(servers: Sequence[str]) -> str:
    """Short note telling the model how to bring hidden servers back.

    The hide can land on a later compaction, so the note does not name the
    idle-turn count.
    """
    ordered = [name for name in servers if name]
    if not ordered:
        return ""
    if len(ordered) == 1:
        name = ordered[0]
        return (
            f"MCP server `{name}` was hidden because it went unused. "
            f"Call `enable_mcp` with name `{name}` to restore it."
        )
    names = ", ".join(f"`{name}`" for name in ordered)
    return (
        f"MCP servers {names} were hidden because they went unused. "
        "Call `enable_mcp` with each name to restore them."
    )


def advertised_servers(tools: Sequence[ToolDef], known_servers: Iterable[str]) -> set[str]:
    """Servers that have a ``server__tool`` entry in ``tools``.

    Longest server name wins so ``a_b__tool`` is ``a_b``, not ``a``.
    """
    prefixes = sorted({name for name in known_servers if name}, key=len, reverse=True)
    found: set[str] = set()
    for tool in tools:
        for prefix in prefixes:
            if tool.name.startswith(f"{prefix}__"):
                found.add(prefix)
                break
    return found


def servers_used_by_calls(
    calls: Sequence[tuple[str, Mapping[str, object]]],
    known_servers: Iterable[str],
) -> set[str]:
    """Servers targeted by this inner turn's tool calls.

    A prefixed ``server__tool`` name, a meta-tool ``server`` argument, or
    ``enable_mcp``'s ``name`` all count.
    """
    prefixes = sorted({name for name in known_servers if name}, key=len, reverse=True)
    used: set[str] = set()
    for name, args in calls:
        if name == "enable_mcp":
            target = args.get("name")
            if isinstance(target, str) and target:
                used.add(target)
            continue
        if name in _SERVER_ARG_TOOLS:
            target = args.get("server")
            if isinstance(target, str) and target:
                used.add(target)
            continue
        for prefix in prefixes:
            if name.startswith(f"{prefix}__"):
                used.add(prefix)
                break
    return used


@dataclass
class MCPSessionVisibility:
    """Idle counts and the set of MCP servers hidden from one chat session.

    Reaching the idle limit does not hide by itself. The caller hides on a
    history rewrite, which is already rewriting the cached message prefix.
    A server used before that rewrite goes back to zero and is not hidden.
    """

    hidden: set[str] = field(default_factory=set)
    _idle: dict[str, int] = field(default_factory=dict)
    _pending_note: str = ""

    def idle_count(self, server: str) -> int:
        """Inner turns this server has been offered without use. Hidden servers read 0."""
        return self._idle.get(server, 0)

    def is_hidden(self, server: str) -> bool:
        """True when this session must not see or call ``server``."""
        return server in self.hidden

    def note_inner_turn(self, advertised: set[str], used: set[str]) -> None:
        """Count one model call.

        A server that was offered and not used gains one idle turn. A server
        the model called, and that is not hidden, goes back to zero. Hidden
        servers stay hidden: a remembered name is rejected by the executor and
        is not use. An empty ``advertised`` set (a no-tools recovery call)
        changes nothing except resetting servers in ``used``.
        """
        for server in used:
            if server and server not in self.hidden:
                self._idle[server] = 0
        for server in advertised:
            if not server or server in used or server in self.hidden:
                continue
            self._idle[server] = self._idle.get(server, 0) + 1

    def record_use(self, server: str) -> None:
        """Reset ``server`` and show it again. ``enable_mcp`` and voice calls do this."""
        if not server:
            return
        self._idle[server] = 0
        self.hidden.discard(server)

    def servers_due(self) -> list[str]:
        """Servers at the idle limit that are not hidden yet. Does not hide them."""
        return sorted(
            name
            for name, count in self._idle.items()
            if count >= IDLE_INNER_TURNS and name not in self.hidden
        )

    def take_newly_idle(self) -> list[str]:
        """Hide servers that reached the idle limit. Returns those names, once.

        Call this only when history is being rewritten. A due server stays
        listed, and keeps counting, until then.
        """
        newly = self.servers_due()
        for name in newly:
            self.hidden.add(name)
            self._idle[name] = 0
        return newly

    def queue_note(self, servers: Sequence[str]) -> None:
        """Remember the idle note until a later tool result can carry it."""
        note = idle_note(servers)
        if not note:
            return
        if self._pending_note:
            self._pending_note = f"{self._pending_note}\n{note}"
        else:
            self._pending_note = note

    def take_note(self) -> str:
        """Return the queued idle note, once. Empty when nothing is waiting."""
        note = self._pending_note
        self._pending_note = ""
        return note

    def filter_tools(self, tools: Sequence[ToolDef], known_servers: Sequence[str]) -> list[ToolDef]:
        """Drop ``server__tool`` entries for servers hidden in this session."""
        prefixes = sorted(
            {name for name in known_servers if name in self.hidden},
            key=len,
            reverse=True,
        )
        if not prefixes:
            return list(tools)
        return [
            tool
            for tool in tools
            if not any(tool.name.startswith(f"{prefix}__") for prefix in prefixes)
        ]
