"""Session transcript capture for harness debugging (internal use only).

Writes one append-only NDJSON file per session under
``{workspace_root}/.monkeybot/transcripts/{UTC_compact}_{session_id}/transcript.ndjson``:
a ``SessionManifest`` (readers take the **last** one), followed by durable
:class:`~monkeybot.core.runtime.events.AgentEvent` boundaries (OpenCode V2-style),
plus raw provider request/response records.

Live-only streaming deltas (``AssistantDelta``, ``ToolInputDelta``, …) are
skipped so the transcript is a replay-grade durable log. Repeated bytes are
stubbed by reference — tool schemas (``schema_seq``), ``toolResponse`` bodies
(``result_seq``), replayed provider messages (``content_seq`` / ``text_seq``),
and drifting system prompts (``base_seq`` + unified ``diff``) — always pointing
at the record in this file that holds the real bytes.

Not surfaced to the agent or any tool; gated by YAML ``runtime.transcript_enabled``
(default off; config-file only) and wired from the SSE gateway loop and realtime
WebSocket route.
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from monkeybot.core.path_safety import (
    is_legacy_path_component_safe,
    path_contained_under,
    sanitize_path_component,
)
from monkeybot.core.runtime.events import (
    AgentEvent,
    AssistantTextEnded,
    ThinkingBlockComplete,
    event_to_json,
    is_durable_event,
)

logger = logging.getLogger(__name__)

_TRANSCRIPT_REL_DIR = Path(".monkeybot") / "transcripts"
_TRANSCRIPT_FILENAME = "transcript.ndjson"
_TRANSCRIPT_EXTRA_KINDS: frozenset[str] = frozenset({"SystemPromptSnapshot", "ContextUsage"})
_HASH_TEXT_FIELDS: dict[str, str] = {
    "SystemPromptSnapshot": "text",
    "SystemContextUpdated": "text",
}
# Below this, a ``content_seq`` pointer costs more bytes than the message it replaces.
_MSG_STUB_MIN_CHARS = 200
# Re-anchor (write full text again) once a diff against the anchor stops paying for itself.
_DIFF_MAX_RATIO = 0.5
# One line of context keeps hunks readable while staying applicable against the anchor.
_DIFF_CONTEXT_LINES = 1
# Written into every manifest so a reviewing agent can resolve pointers without
# prior knowledge of this module.
_STUB_FORMAT: dict[str, str] = {
    "doc": "Repeated bytes are written once and referenced by seq. Look up the cited record.",
    "text_seq": "text is the `text` of that record",
    "result_seq": "result is the `result` of that ToolCallResult",
    "schema_seq": "full tool schema is the `tools` of that ProviderRequest",
    "content_seq": "content is messages[content_index].content of that record",
    "base_seq": "apply this record's `diff` to that record's `text`; diffs never chain",
    "diff": "unified diff, no ---/+++ header, lines split on \\n (not splitlines)",
    "changed": "false: same as base_seq. true: see `diff`. absent with `text`: full body",
}
_MANIFEST_FINGERPRINT_KEYS: tuple[str, ...] = (
    "harness_version",
    "model",
    "provider",
    "workspace_root",
    "agent_md",
    "context_window_tokens",
    "memory_on",
    "memory_storage_uri",
    "sandbox_enabled",
    "computer_tools",
    "mcp_catalog",
)


def now_iso() -> str:
    """UTC timestamp with millisecond precision, e.g. ``2026-07-16T20:35:48.123Z``."""
    return (
        time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
        + f".{int(time.time() * 1000) % 1000:03d}Z"
    )


def _utc_compact_from_iso(started_at: str) -> str:
    """Derive ``YYYYMMDDTHHMMSSZ`` from an ISO-ish UTC timestamp."""
    bare = started_at.rstrip("Z")
    date_part, _, time_part = bare.partition("T")
    hhmmss = time_part.split(".", 1)[0] if time_part else "000000"
    return f"{date_part.replace('-', '')}T{hhmmss.replace(':', '')}Z"


def _find_existing_session_dir(transcripts_root: Path, session_id: str) -> Path | None:
    """Reuse an existing ``*_{session_id}`` session folder when present."""
    if not transcripts_root.is_dir():
        return None
    safe_id = sanitize_path_component(session_id)
    suffixes = [f"_{safe_id}"]
    if safe_id != session_id and is_legacy_path_component_safe(session_id):
        suffixes.append(f"_{session_id}")
    matches = sorted(
        (
            p
            for p in transcripts_root.iterdir()
            if p.is_dir() and any(p.name.endswith(sfx) for sfx in suffixes)
        ),
        key=lambda p: p.name,
        reverse=True,
    )
    root = transcripts_root.resolve()
    for match in matches:
        if path_contained_under(root, match) is not None:
            return match
    return None


def resolve_session_artifact_dir(
    workspace_root: Path,
    session_id: str,
    *,
    started_at: str | None = None,
) -> Path:
    """Return the per-session debug artifact directory under ``.monkeybot/transcripts/``.

    Reuses an existing ``*_{session_id}`` folder when present; otherwise returns a new
    ``{UTC_compact}_{session_id}`` path. The directory is not created here — callers
    mkdir on first write (transcript NDJSON, ``todos.json``, etc.).
    """
    safe_id = sanitize_path_component(session_id)
    transcripts_root = workspace_root.resolve() / _TRANSCRIPT_REL_DIR
    existing = _find_existing_session_dir(transcripts_root, session_id)
    if existing is not None:
        return existing
    folder = f"{_utc_compact_from_iso(started_at or now_iso())}_{safe_id}"
    return transcripts_root / folder


def _json_hash(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _json_chars(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(value))


def _text_diff(base: str, text: str) -> list[str] | None:
    """Unified diff of ``base`` → ``text``, or None when full text is cheaper.

    The ``---``/``+++`` header is dropped: the record already names its base via
    ``base_seq``.

    Splits on ``\\n`` rather than :meth:`str.splitlines` so reconstruction is
    exact for any body — ``splitlines`` drops a trailing newline and treats
    ``\\x0b`` / ``\\u2028`` as breaks that a ``\\n`` join would not restore.
    """
    hunks = list(
        difflib.unified_diff(
            base.split("\n"),
            text.split("\n"),
            lineterm="",
            n=_DIFF_CONTEXT_LINES,
        )
    )
    body = [line for line in hunks if not line.startswith(("---", "+++"))]
    if not body:
        return None
    if sum(len(line) + 1 for line in body) >= len(text) * _DIFF_MAX_RATIO:
        return None
    return body


@dataclasses.dataclass
class _TextAnchor:
    """Last full-text record for one hashed-text kind; diffs are relative to it."""

    hash: str
    text: str
    seq: int | None


def _manifest_fingerprint(fields: dict[str, Any]) -> str:
    sliced = {k: fields.get(k) for k in _MANIFEST_FINGERPRINT_KEYS}
    return _json_hash(sliced)


def runtime_manifest_fields(
    *,
    model: str,
    provider: str,
    workspace_root: str,
    agent_md: str,
    context_window_tokens: int | None = None,
    memory_on: bool | None = None,
    memory_storage_uri: str | None = None,
    mcp_catalog: list[str] | None = None,
) -> dict[str, Any]:
    """Stable config snapshot for ``SessionManifest`` (no allowlist dump)."""
    from monkeybot import __version__ as monkeybot_version
    from monkeybot.computer import should_enable_computer_tools
    from monkeybot.core.tools.sandbox_executor import SandboxConfig

    uri = memory_storage_uri
    if uri is None:
        uri = os.environ.get("MEMORY_STORAGE_URI", "") or None
    on = memory_on
    if on is None:
        on = bool(uri)
    window = context_window_tokens
    if window is None:
        raw = os.environ.get("MODEL_CONTEXT_WINDOW", "").strip()
        window = int(raw) if raw.isdigit() else None
    fields: dict[str, Any] = {
        "harness_version": monkeybot_version,
        "model": model,
        "provider": provider,
        "workspace_root": workspace_root,
        "agent_md": agent_md,
        "memory_on": on,
        "sandbox_enabled": SandboxConfig.from_env().enabled,
        "computer_tools": should_enable_computer_tools(),
    }
    if window is not None:
        fields["context_window_tokens"] = window
    if uri:
        fields["memory_storage_uri"] = uri
    if mcp_catalog is not None:
        fields["mcp_catalog"] = list(mcp_catalog)
    return fields


class _ScanState:
    """Replay-grade indexes rebuilt from an existing NDJSON file on resume."""

    def __init__(self) -> None:
        self.max_seq = 0
        self.last_manifest: dict[str, Any] | None = None
        self.last_schema_hash: str | None = None
        self.last_schema_seq: int | None = None
        self.result_seq_by_call_id: dict[str, int] = {}
        self.anchor_by_kind: dict[str, _TextAnchor] = {}
        self.msg_ref_by_hash: dict[str, tuple[int, int]] = {}


def _index_scanned_messages(state: _ScanState, messages: Any, seq: int) -> None:
    """Record where each full message body lives so a resume can keep stubbing."""
    if not isinstance(messages, list):
        return
    for index, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if content is None:
            continue
        state.msg_ref_by_hash[_json_hash(content)] = (seq, index)


def _scan_transcript(path: Path) -> _ScanState:
    state = _ScanState()
    if not path.is_file():
        return state
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                seq = obj.get("seq")
                if isinstance(seq, int) and seq > state.max_seq:
                    state.max_seq = seq
                rtype = obj.get("type")
                if rtype == "SessionManifest":
                    state.last_manifest = obj
                    continue
                if rtype == "ProviderRequest":
                    tools = obj.get("tools")
                    if isinstance(tools, list):
                        state.last_schema_hash = _json_hash(tools)
                        state.last_schema_seq = seq if isinstance(seq, int) else None
                    elif isinstance(tools, dict):
                        h = tools.get("schema_hash")
                        s = tools.get("schema_seq")
                        if isinstance(h, str):
                            state.last_schema_hash = h
                        if isinstance(s, int):
                            state.last_schema_seq = s
                    if isinstance(seq, int):
                        _index_scanned_messages(state, obj.get("messages"), seq)
                elif rtype == "ToolCallResult":
                    cid = obj.get("call_id")
                    if isinstance(cid, str) and cid and isinstance(seq, int):
                        state.result_seq_by_call_id[cid] = seq
                field = _HASH_TEXT_FIELDS.get(str(rtype or ""))
                if field:
                    # Only full-text records re-anchor; diff records point back at one.
                    text = obj.get(field)
                    if isinstance(text, str) and text:
                        digest = obj.get("hash")
                        state.anchor_by_kind[str(rtype)] = _TextAnchor(
                            hash=digest if isinstance(digest, str) and digest else _json_hash(text),
                            text=text,
                            seq=seq if isinstance(seq, int) else None,
                        )
    except OSError:
        return _ScanState()
    return state


class TranscriptWriter:
    """Append-only NDJSON transcript writer for one gateway session.

    One instance per ``session_id``; safe for concurrent turns on the same
    session via an internal lock. All file I/O runs in ``asyncio.to_thread``.

    ``provider_records`` is True for the SSE text loop (which writes
    ``ProviderRequest`` / ``ProviderResponse``) and False for realtime, where
    ``AssistantTextEnded`` is the only durable assistant text.
    """

    def __init__(
        self,
        session_id: str,
        *,
        workspace_root: Path,
        provider_records: bool = True,
    ) -> None:
        self._session_id = session_id
        self._started_at = now_iso()
        self._session_dir = resolve_session_artifact_dir(
            workspace_root, session_id, started_at=self._started_at
        )
        self._path = self._session_dir / _TRANSCRIPT_FILENAME
        self._lock = asyncio.Lock()
        self._provider_records = provider_records
        scanned = _scan_transcript(self._path) if self._path.is_file() else _ScanState()
        self._seq = scanned.max_seq
        self._last_manifest = scanned.last_manifest
        self._manifest_written = scanned.last_manifest is not None
        self._last_schema_hash = scanned.last_schema_hash
        self._last_schema_seq = scanned.last_schema_seq
        self._result_seq_by_call_id = dict(scanned.result_seq_by_call_id)
        self._anchor_by_kind = dict(scanned.anchor_by_kind)
        # Newest record per kind, anchor or diff. On resume only full-text records
        # can seed this, so a stub may be skipped once until the next snapshot.
        self._last_text_by_kind: dict[str, tuple[str, int]] = {
            kind: (anchor.text, anchor.seq)
            for kind, anchor in scanned.anchor_by_kind.items()
            if anchor.seq is not None
        }
        self._msg_ref_by_hash = dict(scanned.msg_ref_by_hash)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def session_dir(self) -> Path:
        return self._session_dir

    async def ensure_manifest(self, **manifest_fields: Any) -> None:
        """Write or refresh the session manifest (idempotent when fingerprint matches).

        A reused session dir appends a new ``SessionManifest`` with ``resumed: true``
        when the config fingerprint differs. Readers take the last manifest.
        """
        record = {
            "type": "SessionManifest",
            "session_id": self._session_id,
            "started_at": self._started_at,
            **manifest_fields,
            "format": _STUB_FORMAT,
        }
        new_fp = _manifest_fingerprint(record)
        async with self._lock:
            if self._manifest_written and self._last_manifest is not None:
                if _manifest_fingerprint(self._last_manifest) == new_fp:
                    return
                record["resumed"] = True

            def _write() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")

            try:
                await asyncio.to_thread(_write)
            except OSError:
                logger.warning(
                    "transcript manifest write failed for %s", self._session_id, exc_info=True
                )
                return
            self._manifest_written = True
            self._last_manifest = record

    async def drain(self) -> None:
        """Wait until any in-flight append holds the lock (then release).

        Used by session removal so a late write cannot race teardown.
        """
        async with self._lock:
            return

    def _stub_blocks(
        self, msg: dict[str, object], text_refs: dict[str, int]
    ) -> dict[str, object]:
        """Point tool-result and injected-context blocks at the records holding them."""
        content = msg.get("content")
        if not isinstance(content, list):
            return msg
        new_blocks: list[object] = []
        changed = False
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            kind = block.get("type")
            if kind == "toolResponse":
                call_id = block.get("id")
                seq = self._result_seq_by_call_id.get(str(call_id)) if call_id else None
                if seq is None:
                    new_blocks.append(block)
                    continue
                new_blocks.append(
                    {
                        "type": "toolResponse",
                        "id": call_id,
                        "toolName": block.get("toolName"),
                        "isError": block.get("isError"),
                        "result_seq": seq,
                        "result_chars": _json_chars(block.get("result")),
                    }
                )
                changed = True
                continue
            # The system prompt and each mid-epoch context update are injected into
            # the next message verbatim, so they land in the file twice otherwise.
            text = block.get("text") if kind == "text" else None
            seq = text_refs.get(text) if isinstance(text, str) else None
            if seq is None or not isinstance(text, str) or len(text) < _MSG_STUB_MIN_CHARS:
                new_blocks.append(block)
                continue
            new_blocks.append({"type": "text", "text_seq": seq, "chars": len(text)})
            changed = True
        return {**msg, "content": new_blocks} if changed else msg

    def _stub_messages(
        self, messages: list[dict[str, object]]
    ) -> tuple[list[dict[str, object]], dict[str, int]]:
        """Replace already-captured message bodies with pointers into this file.

        Every new user turn replays the whole history at ``message_offset`` 0, so
        without this the system prompt and every prior turn are re-embedded each
        time. Returns the stubbed messages plus the content hashes this record
        will own, which the caller registers once the real ``seq`` is known.
        """
        out: list[dict[str, object]] = []
        owned: dict[str, int] = {}
        text_refs = dict(self._last_text_by_kind.values())
        for index, msg in enumerate(messages):
            content = msg.get("content")
            role = msg.get("role")
            if isinstance(content, str) and content in text_refs:
                out.append({"role": role, "text_seq": text_refs[content], "chars": len(content)})
                continue
            chars = _json_chars(content)
            # Small bodies stay inline, but their blocks still get stubbed below.
            if content is not None and chars >= _MSG_STUB_MIN_CHARS:
                digest = _json_hash(content)
                ref = self._msg_ref_by_hash.get(digest)
                if ref is not None:
                    ref_seq, ref_index = ref
                    out.append(
                        {
                            "role": role,
                            "content_seq": ref_seq,
                            "content_index": ref_index,
                            "chars": chars,
                        }
                    )
                    continue
                owned[digest] = index
            out.append(self._stub_blocks(msg, text_refs))
        return out, owned

    def _tools_payload(
        self, tools: list[dict[str, object]], seq: int
    ) -> tuple[list[dict[str, object]] | dict[str, object], str, int]:
        digest = _json_hash(tools)
        names = [str(t.get("name") or "") for t in tools]
        if digest == self._last_schema_hash and self._last_schema_seq is not None:
            stub: dict[str, object] = {
                "schema_hash": digest,
                "schema_seq": self._last_schema_seq,
                "tool_count": len(tools),
                "names": names,
            }
            return stub, digest, self._last_schema_seq
        return tools, digest, seq

    def _enrich_hashed_text(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Collapse a repeated or drifting text body against its anchor.

        Returns the payload and whether it carries full text (making it the new
        anchor). ``changed`` and ``base_seq`` are relative to the anchor — the
        most recent record of this kind that holds the whole body — so diffs
        never chain: a reader applies one ``diff`` to one ``base_seq``.

        The system prompt embeds per-turn volatile blocks (conversation recall,
        the restated current request), so a whole-body hash misses on every turn
        and plain hash dedup never fires. Diffing is what actually pays here.
        """
        rtype = str(payload.get("type") or "")
        field = _HASH_TEXT_FIELDS.get(rtype)
        if not field:
            return payload, False
        text = payload.get(field)
        if not isinstance(text, str) or not text:
            return payload, False
        digest = _json_hash(text)
        payload["hash"] = digest
        payload["chars"] = len(text)
        anchor = self._anchor_by_kind.get(rtype)
        if anchor is None or anchor.seq is None:
            return payload, True
        if anchor.hash == digest:
            payload.pop(field, None)
            payload["changed"] = False
            payload["base_seq"] = anchor.seq
            return payload, False
        diff = _text_diff(anchor.text, text)
        if diff is None:
            return payload, True
        payload.pop(field, None)
        payload["changed"] = True
        payload["base_seq"] = anchor.seq
        payload["diff"] = diff
        return payload, False

    async def _append_line(self, record: dict[str, Any]) -> int:
        async with self._lock:
            self._seq += 1
            seq = self._seq
            line = json.dumps({"seq": seq, **record}, ensure_ascii=False, separators=(",", ":"))

            def _append() -> None:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")

            try:
                await asyncio.to_thread(_append)
            except OSError:
                logger.warning("transcript append failed for %s", self._session_id, exc_info=True)
            return seq

    async def write_user_message(self, *, request_id: str, content: str) -> None:
        """Append the incoming user turn (not an ``AgentEvent``; harness-internal)."""
        await self._append_line(
            {"ts": now_iso(), "type": "UserMessage", "request_id": request_id, "content": content}
        )

    def _skip_hollow(self, event: AgentEvent) -> bool:
        if not self._provider_records:
            return False
        if isinstance(event, AssistantTextEnded):
            return True
        return isinstance(event, ThinkingBlockComplete) and not event.signature

    async def write_event(self, event: AgentEvent) -> None:
        """Append one harness ``AgentEvent`` (durable + transcript-extra kinds)."""
        if self._skip_hollow(event):
            return
        extra = event.kind in _TRANSCRIPT_EXTRA_KINDS
        if not extra and not is_durable_event(event):
            return
        payload = json.loads(event_to_json(event))
        rtype = str(payload.get("type") or "")
        field = _HASH_TEXT_FIELDS.get(rtype)
        text = payload.get(field) if field else None
        payload, is_anchor = self._enrich_hashed_text(payload)
        seq = await self._append_line({"ts": now_iso(), **payload})
        if isinstance(text, str) and text:
            self._last_text_by_kind[rtype] = (text, seq)
            if is_anchor:
                self._anchor_by_kind[rtype] = _TextAnchor(
                    hash=str(payload["hash"]), text=text, seq=seq
                )
        if rtype == "ToolCallResult":
            cid = payload.get("call_id")
            if isinstance(cid, str) and cid:
                self._result_seq_by_call_id[cid] = seq

    async def write_provider_request(
        self,
        *,
        request_id: str,
        inner_turn: int,
        model: str,
        messages: list[dict[str, object]],
        message_offset: int = 0,
        messages_reset: bool = False,
        tools: list[dict[str, object]] | None = None,
        thinking_budget: int | None,
        tools_include_reason: str | None = None,
        tools_dirty_reason: str | None = None,
    ) -> None:
        stubbed, owned = self._stub_messages(messages)
        record: dict[str, Any] = {
            "ts": now_iso(),
            "type": "ProviderRequest",
            "request_id": request_id,
            "inner_turn": inner_turn,
            "model": model,
            "message_offset": message_offset,
            "messages": stubbed,
            "thinking_budget": thinking_budget,
        }
        if messages_reset:
            record["messages_reset"] = True
        payload: list[dict[str, object]] | dict[str, object] | None = None
        digest = ""
        schema_seq = 0
        if tools is not None:
            payload, digest, schema_seq = self._tools_payload(tools, self._seq + 1)
            record["tools"] = payload
            if tools_include_reason:
                record["tools_include_reason"] = tools_include_reason
            if tools_dirty_reason:
                record["tools_dirty_reason"] = tools_dirty_reason
        seq = await self._append_line(record)
        for content_hash, index in owned.items():
            self._msg_ref_by_hash[content_hash] = (seq, index)
        if payload is not None:
            self._last_schema_hash = digest
            self._last_schema_seq = seq if isinstance(payload, list) else schema_seq

    async def write_provider_response(
        self,
        *,
        request_id: str,
        inner_turn: int,
        model: str,
        text: str,
        thinking: str,
        tool_requests: list[dict[str, object]],
        usage: dict[str, object],
    ) -> None:
        record: dict[str, Any] = {
            "ts": now_iso(),
            "type": "ProviderResponse",
            "request_id": request_id,
            "inner_turn": inner_turn,
            "model": model,
            "text": text,
            "thinking": thinking,
            "tool_requests": tool_requests,
            "usage": usage,
        }
        # A tool step legitimately has no prose; only a reply with neither text nor
        # tool calls is the pathology worth flagging.
        if not (text or "").strip() and not tool_requests:
            record["assistant_text_empty"] = True
        await self._append_line(record)


__all__ = [
    "TranscriptWriter",
    "now_iso",
    "resolve_session_artifact_dir",
    "runtime_manifest_fields",
]
