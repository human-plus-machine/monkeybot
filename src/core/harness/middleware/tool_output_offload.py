"""ToolOutputOffloadMW — moves large tool outputs to a virtual filesystem."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..event_bus import EventBus
from ..events import EventKind, HarnessEvent, Principal, VersionTriple
from ..token_count import count_tokens


@dataclass
class _VirtualFS:
    """Tiny in-memory virtual FS used by the offload middleware."""

    files: dict[str, bytes] = field(default_factory=dict)

    def write(self, path: str, content: bytes) -> None:
        self.files[path] = content

    def read(self, path: str) -> bytes | None:
        return self.files.get(path)

    def paths(self) -> list[str]:
        return sorted(self.files)


class ToolOutputOffloadMW:
    """Replace large tool outputs with a handle + summary.

    ``apply`` takes the raw tool output (string), returns the transformed output
    that replaces the original tool message content.
    """

    name = "ToolOutputOffloadMW"

    def __init__(
        self,
        *,
        threshold_tokens: int,
        event_bus: EventBus,
        model_name: str = "gemini-2.5-flash",
        vfs: _VirtualFS | None = None,
    ) -> None:
        self.threshold_tokens = threshold_tokens
        self.event_bus = event_bus
        self.model_name = model_name
        self.vfs = vfs or _VirtualFS()

    async def maybe_offload(
        self,
        content: str,
        *,
        call_id: str,
        run_id: str,
        session_id: str,
        principal: Principal,
        versions: VersionTriple,
    ) -> str:
        tokens = count_tokens(self.model_name, content)
        if tokens <= self.threshold_tokens:
            return content
        path = f"/.emonk/tool_outputs/{call_id}.txt"
        self.vfs.write(path, content.encode("utf-8"))
        await self.event_bus.publish(
            HarnessEvent(
                run_id=run_id,
                session_id=session_id,
                principal=principal,
                versions=versions,
                ts=datetime.now(UTC),
                kind=EventKind.CONTEXT_OFFLOAD,
                payload={
                    "call_id": call_id,
                    "path": path,
                    "bytes": len(content),
                    "tokens": tokens,
                    "threshold_tokens": self.threshold_tokens,
                },
            )
        )
        head = content[:500]
        tail = content[-500:] if len(content) > 1000 else ""
        return (
            f"[harness] tool output offloaded ({tokens} tokens > {self.threshold_tokens}).\n"
            f"handle: {path}\n"
            f"head: {head!r}\n"
            + (f"tail: {tail!r}\n" if tail else "")
        )
