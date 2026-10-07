"""Read replay-grade session transcripts into a resolved digest.

Writers stub repeated bytes (``text_seq``, ``result_seq``, ``schema_seq``,
``content_seq``, ``base_seq`` + ``diff``). This module expands those pointers
and renders one condensed timeline per user turn, with struggle signals a
retro can rank without hand-resolving the NDJSON.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.transcript import (
    SUBAGENTS_DIRNAME,
    TRANSCRIPT_FILENAME,
    subagent_transcript_dir,
)

logger = logging.getLogger(__name__)

SLOW_TOOL_MS = 10_000
_TASK_TOOL = "task"
_CHILD_ID_RE = re.compile(r'"child_thread_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
_EXCERPT_USER = 500
_EXCERPT_ARGS = 180
_EXCERPT_FINAL = 400
_EXCERPT_DETAIL = 240
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def load_records(path: Path) -> list[dict[str, Any]]:
    """Load NDJSON records, skipping blank and corrupt lines."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    logger.debug("skipping corrupt NDJSON line %s", kv(line=line_no, path=path))
                    continue
                if isinstance(obj, dict):
                    records.append(obj)
    except OSError:
        logger.warning("transcript read failed %s", kv(path=path), exc_info=True)
        raise
    return records


def apply_unified_diff(base: str, diff_lines: list[str]) -> str:
    """Apply a headerless unified diff produced by the transcript writer.

    Splits on ``\\n``, matching :func:`monkeybot.core.persistence.transcript._text_diff`.

    Raises:
        ValueError: A hunk's body does not match its ``@@`` line counts. Writers
            before the header-slice fix dropped body lines starting with ``--`` /
            ``++``, so those diffs cannot be rebuilt.
    """
    base_lines = base.split("\n")
    out: list[str] = []
    index = 0
    cursor = 0
    while cursor < len(diff_lines):
        line = diff_lines[cursor]
        match = _HUNK_RE.match(line)
        if match is None:
            cursor += 1
            continue
        old_start = int(match.group(1))
        old_len = int(match.group(2)) if match.group(2) is not None else 1
        new_len = int(match.group(4)) if match.group(4) is not None else 1
        # An empty old range names the line *before* the insertion point.
        target = old_start if old_len == 0 else old_start - 1
        target = max(target, index)
        out.extend(base_lines[index:target])
        index = target
        cursor += 1
        seen_old = 0
        seen_new = 0
        while cursor < len(diff_lines) and not diff_lines[cursor].startswith("@@"):
            hunk_line = diff_lines[cursor]
            cursor += 1
            if hunk_line.startswith("+"):
                out.append(hunk_line[1:])
                seen_new += 1
            elif hunk_line.startswith("-"):
                index += 1
                seen_old += 1
            elif hunk_line.startswith(" "):
                out.append(hunk_line[1:])
                index += 1
                seen_old += 1
                seen_new += 1
        if seen_old != old_len or seen_new != new_len:
            raise ValueError(
                f"hunk {line!r} has {seen_old} old / {seen_new} new lines; "
                f"expected {old_len} / {new_len}"
            )
    out.extend(base_lines[index:])
    return "\n".join(out)


def _copy_record(record: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = json.loads(json.dumps(record, ensure_ascii=False, default=str))
    return copied


def _by_seq(records: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    indexed: dict[int, dict[str, Any]] = {}
    for record in records:
        seq = record.get("seq")
        if isinstance(seq, int):
            indexed[seq] = record
    return indexed


def _resolve_text_anchors(records: list[dict[str, Any]], by_seq: dict[int, dict[str, Any]]) -> None:
    """Expand ``base_seq`` / ``diff`` onto ``text``. Diffs never chain."""
    for record in records:
        base_seq = record.get("base_seq")
        if not isinstance(base_seq, int):
            continue
        base = by_seq.get(base_seq)
        if base is None:
            continue
        base_text = base.get("text")
        if not isinstance(base_text, str):
            continue
        if record.get("changed") is False:
            record["text"] = base_text
            continue
        diff = record.get("diff")
        if record.get("changed") is True and isinstance(diff, list):
            lines = [line for line in diff if isinstance(line, str)]
            try:
                record["text"] = apply_unified_diff(base_text, lines)
            except ValueError as exc:
                logger.debug(
                    "transcript diff does not apply %s", kv(seq=record.get("seq"), error=exc)
                )
                record["text_error"] = f"diff_mismatch: {exc}"


def _resolve_schema(record: dict[str, Any], by_seq: dict[int, dict[str, Any]]) -> None:
    tools = record.get("tools")
    if not isinstance(tools, dict):
        return
    schema_seq = tools.get("schema_seq")
    if not isinstance(schema_seq, int):
        return
    source = by_seq.get(schema_seq)
    if source is None:
        return
    full = source.get("tools")
    if isinstance(full, list):
        record["tools"] = _copy_record({"tools": full})["tools"]


def _text_at(by_seq: dict[int, dict[str, Any]], seq: object) -> str | None:
    if not isinstance(seq, int):
        return None
    source = by_seq.get(seq)
    if source is None:
        return None
    text = source.get("text")
    return text if isinstance(text, str) else None


def _resolve_message_pointers(messages: object, by_seq: dict[int, dict[str, Any]]) -> None:
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        content_seq = message.get("content_seq")
        content_index = message.get("content_index")
        if isinstance(content_seq, int) and isinstance(content_index, int):
            source = by_seq.get(content_seq)
            source_messages = source.get("messages") if isinstance(source, dict) else None
            if isinstance(source_messages, list) and 0 <= content_index < len(source_messages):
                source_message = source_messages[content_index]
                if isinstance(source_message, dict) and "content" in source_message:
                    message["content"] = _copy_record({"content": source_message["content"]})[
                        "content"
                    ]
        elif "text_seq" in message and "content" not in message:
            text = _text_at(by_seq, message.get("text_seq"))
            if text is not None:
                message["content"] = text
        _resolve_blocks(message.get("content"), by_seq)


def _resolve_blocks(content: object, by_seq: dict[int, dict[str, Any]]) -> None:
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict):
            continue
        text = _text_at(by_seq, block.get("text_seq"))
        if text is not None:
            block["text"] = text
        result_seq = block.get("result_seq")
        if not isinstance(result_seq, int):
            continue
        source = by_seq.get(result_seq)
        if isinstance(source, dict) and "result" in source:
            block["result"] = _copy_record({"result": source["result"]})["result"]


def resolve_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of ``records`` with stub pointers expanded to real bytes."""
    resolved = [_copy_record(record) for record in records]
    by_seq = _by_seq(resolved)
    _resolve_text_anchors(resolved, by_seq)
    for record in resolved:
        _resolve_schema(record, by_seq)
    for record in resolved:
        _resolve_message_pointers(record.get("messages"), by_seq)
    return resolved


def _seq_of(record: dict[str, Any]) -> int | None:
    seq = record.get("seq")
    return seq if isinstance(seq, int) else None


def _clip(text: str, limit: int, *, full: bool) -> str:
    if full or len(text) <= limit:
        return text
    if limit <= 1:
        return "…"
    return text[: limit - 1] + "…"


def _preview_args(args: object, *, full: bool) -> str:
    try:
        rendered = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        rendered = str(args)
    return _clip(rendered, _EXCERPT_ARGS, full=full)


def _args_key(args: object) -> str:
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(args)


def _result_chars(result: object) -> int:
    if isinstance(result, str):
        return len(result)
    try:
        return len(json.dumps(result, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return len(str(result))


@dataclass
class ToolStep:
    seq: int
    name: str
    args_preview: str
    ok: bool
    error_kind: str | None
    duration_ms: int | None
    result_chars: int


@dataclass
class TurnNote:
    seq: int
    kind: str
    text: str


@dataclass
class SubagentBlock:
    seq: int
    child_thread_id: str
    subagent_type: str | None
    digest: SessionDigest | None


@dataclass
class TurnDigest:
    index: int
    user_seq: int | None
    user_text: str
    items: list[ToolStep | TurnNote | SubagentBlock] = field(default_factory=list)
    final_seq: int | None = None
    final_text: str = ""


@dataclass
class SlowTool:
    seq: int
    tool: str
    duration_ms: int


@dataclass
class RepeatedCall:
    tool: str
    count: int
    first_seq: int
    args_preview: str


@dataclass
class StruggleSignals:
    tool_errors: dict[str, int] = field(default_factory=dict)
    interventions: dict[str, int] = field(default_factory=dict)
    steers: int = 0
    verdicts: int = 0
    empty_assistant: int = 0
    summaries: int = 0
    slow_tools: list[SlowTool] = field(default_factory=list)
    repeated_calls: list[RepeatedCall] = field(default_factory=list)

    def score(self) -> int:
        """How hard the session looked. Higher means more worth a retro."""
        return (
            sum(self.tool_errors.values())
            + sum(self.interventions.values())
            + self.steers
            + self.verdicts
            + self.empty_assistant
            + self.summaries
            + len(self.slow_tools)
            + len(self.repeated_calls)
        )


@dataclass
class UnlinkedSubagent:
    """A child transcript under ``subagents/`` that no parent turn points at."""

    dir_name: str
    digest: SessionDigest


@dataclass
class SessionDigest:
    session_dir: str
    manifest: dict[str, Any]
    signals: StruggleSignals
    turns: list[TurnDigest]
    unlinked_subagents: list[UnlinkedSubagent] = field(default_factory=list)


def merge_signals(left: StruggleSignals, right: StruggleSignals) -> StruggleSignals:
    """Add ``right`` into a copy of ``left`` (child runs roll into the parent)."""
    errors = dict(left.tool_errors)
    for key, count in right.tool_errors.items():
        errors[key] = errors.get(key, 0) + count
    interventions = dict(left.interventions)
    for key, count in right.interventions.items():
        interventions[key] = interventions.get(key, 0) + count
    return StruggleSignals(
        tool_errors=errors,
        interventions=interventions,
        steers=left.steers + right.steers,
        verdicts=left.verdicts + right.verdicts,
        empty_assistant=left.empty_assistant + right.empty_assistant,
        summaries=left.summaries + right.summaries,
        slow_tools=[*left.slow_tools, *right.slow_tools],
        repeated_calls=[*left.repeated_calls, *right.repeated_calls],
    )


def _bump(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _result_failed(result: dict[str, Any]) -> bool:
    error = result.get("error")
    error_kind = result.get("error_kind")
    return (isinstance(error, str) and bool(error)) or (
        isinstance(error_kind, str) and bool(error_kind)
    )


def signals_from_records(records: list[dict[str, Any]]) -> StruggleSignals:
    """Struggle counts from one file. Does not expand stubs.

    Repeated calls are identical tool + args within one user turn. Calls rejected
    right after a ``truncated_batch`` intervention count once, as that intervention.
    """
    signals = StruggleSignals()
    groups: dict[tuple[int, str, str], list[int]] = {}
    turn = 0
    in_rejected_batch = False
    for record in records:
        kind = record.get("type")
        seq = _seq_of(record) or 0
        if kind not in ("ToolCallStarted", "ToolCallResult"):
            in_rejected_batch = False
        if kind == "UserMessage":
            turn += 1
        elif kind == "ToolCallResult":
            if not in_rejected_batch and _result_failed(record):
                error_kind = record.get("error_kind")
                label = error_kind if isinstance(error_kind, str) and error_kind else "error"
                _bump(signals.tool_errors, label)
            duration = record.get("duration_ms")
            if isinstance(duration, int) and duration >= SLOW_TOOL_MS:
                tool = record.get("tool")
                signals.slow_tools.append(
                    SlowTool(
                        seq=seq,
                        tool=tool if isinstance(tool, str) else "",
                        duration_ms=duration,
                    )
                )
        elif kind == "ToolCallStarted":
            if in_rejected_batch:
                continue
            tool = record.get("tool")
            name = tool if isinstance(tool, str) else ""
            key = (turn, name, _args_key(record.get("args")))
            groups.setdefault(key, []).append(seq)
        elif kind == "HarnessIntervention":
            intervention = record.get("intervention")
            label = intervention if isinstance(intervention, str) and intervention else "unknown"
            _bump(signals.interventions, label)
            in_rejected_batch = label == "truncated_batch"
        elif kind == "UserSteered":
            signals.steers += 1
        elif kind == "VerifierVerdict":
            signals.verdicts += 1
        elif kind == "ContextSummarized":
            signals.summaries += 1
        elif kind == "ProviderResponse" and record.get("assistant_text_empty") is True:
            signals.empty_assistant += 1
    for (_turn, tool, args_key), seqs in groups.items():
        if len(seqs) < 2:
            continue
        signals.repeated_calls.append(
            RepeatedCall(
                tool=tool,
                count=len(seqs),
                first_seq=seqs[0],
                args_preview=_clip(args_key, _EXCERPT_ARGS, full=False),
            )
        )
    return signals


def _last_manifest(records: list[dict[str, Any]]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    for record in records:
        if record.get("type") == "SessionManifest":
            manifest = record
    return manifest


def _note(record: dict[str, Any], kind: str, text: str, *, full: bool) -> TurnNote | None:
    seq = _seq_of(record)
    if seq is None:
        return None
    return TurnNote(seq=seq, kind=kind, text=_clip(text, _EXCERPT_DETAIL, full=full))


def _tool_step(
    started: dict[str, Any] | None,
    result: dict[str, Any],
    *,
    full: bool,
) -> ToolStep | None:
    seq = _seq_of(result) or (_seq_of(started) if started else None)
    if seq is None:
        return None
    name = ""
    if started is not None and isinstance(started.get("tool"), str):
        name = started["tool"]
    elif isinstance(result.get("tool"), str):
        name = result["tool"]
    error_kind = result.get("error_kind")
    failed = _result_failed(result)
    duration = result.get("duration_ms")
    args = started.get("args") if started is not None else {}
    return ToolStep(
        seq=seq,
        name=name,
        args_preview=_preview_args(args, full=full),
        ok=not failed,
        error_kind=error_kind if isinstance(error_kind, str) else None,
        duration_ms=duration if isinstance(duration, int) else None,
        result_chars=_result_chars(result.get("result")),
    )


def _remember_final(turn: TurnDigest, record: dict[str, Any], *, full: bool) -> None:
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        return
    seq = _seq_of(record)
    turn.final_seq = seq
    turn.final_text = _clip(text.strip(), _EXCERPT_FINAL, full=full)


class _TurnBuilder:
    def __init__(self, index: int) -> None:
        self.turn = TurnDigest(index=index, user_seq=None, user_text="")
        self.pending: dict[str, dict[str, Any]] = {}

    def flush_pending(self, *, full: bool) -> None:
        for started in self.pending.values():
            seq = _seq_of(started)
            if seq is None:
                continue
            tool = started.get("tool")
            self.turn.items.append(
                ToolStep(
                    seq=seq,
                    name=tool if isinstance(tool, str) else "",
                    args_preview=_preview_args(started.get("args"), full=full),
                    ok=False,
                    error_kind="unfinished",
                    duration_ms=None,
                    result_chars=0,
                )
            )
        self.pending.clear()
        self.turn.items.sort(key=lambda item: item.seq)


def _child_dirs(session_dir: Path) -> list[Path]:
    root = session_dir / SUBAGENTS_DIRNAME
    if not root.is_dir():
        return []
    return sorted(
        path for path in root.iterdir() if path.is_dir() and (path / TRANSCRIPT_FILENAME).is_file()
    )


def _child_digest(
    session_dir: Path,
    child_thread_id: str,
    *,
    full: bool,
) -> SessionDigest | None:
    child_dir = subagent_transcript_dir(session_dir, child_thread_id)
    if not (child_dir / TRANSCRIPT_FILENAME).is_file():
        return None
    return build_digest(child_dir, full=full)


def _task_child(
    started: dict[str, Any] | None, result: dict[str, Any]
) -> tuple[str, str | None] | None:
    """``(child_thread_id, subagent_type)`` named by a ``task`` tool result."""
    name = started.get("tool") if started is not None else result.get("tool")
    raw = result.get("result")
    if name != _TASK_TOOL or not isinstance(raw, str) or not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        child = payload.get("child_thread_id")
        sub_type = payload.get("subagent_type")
        if not isinstance(child, str) or not child:
            return None
        return child, sub_type if isinstance(sub_type, str) else None
    # A clipped result no longer parses; the id may still be present.
    match = _CHILD_ID_RE.search(raw)
    if match is None:
        return None
    try:
        child = json.loads(f'"{match.group(1)}"')
    except ValueError:
        return None
    return (child, None) if isinstance(child, str) and child else None


def _add_subagent(
    turn: TurnDigest,
    *,
    seq: int,
    child_thread_id: str,
    subagent_type: str | None,
    session_dir: Path,
    linked: set[str],
    full: bool,
) -> None:
    dir_name = subagent_transcript_dir(session_dir, child_thread_id).name
    if dir_name in linked:
        return
    linked.add(dir_name)
    turn.items.append(
        SubagentBlock(
            seq=seq,
            child_thread_id=child_thread_id,
            subagent_type=subagent_type,
            digest=_child_digest(session_dir, child_thread_id, full=full),
        )
    )


def _apply_record(
    builder: _TurnBuilder,
    record: dict[str, Any],
    *,
    session_dir: Path,
    linked: set[str],
    full: bool,
) -> None:
    kind = record.get("type")
    turn = builder.turn
    if kind == "ToolCallStarted":
        call_id = record.get("call_id")
        if isinstance(call_id, str) and call_id:
            builder.pending[call_id] = record
        return
    if kind == "ToolCallResult":
        call_id = record.get("call_id")
        started = builder.pending.pop(call_id, None) if isinstance(call_id, str) else None
        step = _tool_step(started, record, full=full)
        if step is None:
            return
        turn.items.append(step)
        child = _task_child(started, record)
        if child is not None:
            _add_subagent(
                turn,
                seq=step.seq,
                child_thread_id=child[0],
                subagent_type=child[1],
                session_dir=session_dir,
                linked=linked,
                full=full,
            )
        return
    if kind == "HarnessIntervention":
        name = record.get("intervention")
        detail = record.get("detail")
        label = name if isinstance(name, str) else "intervention"
        body = detail if isinstance(detail, str) else ""
        note = _note(record, "intervention", f"{label}: {body}".strip(), full=full)
        if note is not None:
            turn.items.append(note)
        return
    if kind == "UserSteered":
        text = record.get("text")
        note = _note(record, "steer", text if isinstance(text, str) else "", full=full)
        if note is not None:
            turn.items.append(note)
        return
    if kind == "VerifierVerdict":
        status = record.get("status")
        severity = record.get("severity")
        note = _note(
            record,
            "verdict",
            f"{status if isinstance(status, str) else ''} {severity if isinstance(severity, str) else ''}".strip(),
            full=full,
        )
        if note is not None:
            turn.items.append(note)
        return
    if kind == "ContextSummarized":
        note = _note(record, "summary", "context summarized", full=full)
        if note is not None:
            turn.items.append(note)
        return
    if kind == "Error":
        error = record.get("error")
        note = _note(record, "error", error if isinstance(error, str) else "", full=full)
        if note is not None:
            turn.items.append(note)
        return
    if kind in ("ProviderResponse", "AssistantTextEnded"):
        _remember_final(turn, record, full=full)
        return
    if kind == "SubagentStarted":
        child_id = record.get("child_thread_id")
        if not isinstance(child_id, str) or not child_id:
            return
        seq = _seq_of(record)
        if seq is None:
            return
        sub_type = record.get("subagent_type")
        _add_subagent(
            turn,
            seq=seq,
            child_thread_id=child_id,
            subagent_type=sub_type if isinstance(sub_type, str) else None,
            session_dir=session_dir,
            linked=linked,
            full=full,
        )


def _turns_from_records(
    records: list[dict[str, Any]],
    *,
    session_dir: Path,
    linked: set[str],
    full: bool,
) -> list[TurnDigest]:
    builders: list[_TurnBuilder] = []
    current: _TurnBuilder | None = None

    def open_turn() -> _TurnBuilder:
        nonlocal current
        if current is not None:
            current.flush_pending(full=full)
        current = _TurnBuilder(len(builders) + 1)
        builders.append(current)
        return current

    for record in records:
        kind = record.get("type")
        if kind == "UserMessage":
            opened = open_turn()
            content = record.get("content")
            opened.turn.user_seq = _seq_of(record)
            opened.turn.user_text = _clip(
                content if isinstance(content, str) else "",
                _EXCERPT_USER,
                full=full,
            )
            continue
        if kind in (
            "SessionManifest",
            "SystemPromptSnapshot",
            "SystemContextUpdated",
            "ProviderRequest",
            "ContextUsage",
        ):
            continue
        if current is None:
            if kind in ("ToolCallStarted", "ToolCallResult", "HarnessIntervention", "Error"):
                open_turn()
            else:
                continue
        if current is None:
            continue
        _apply_record(current, record, session_dir=session_dir, linked=linked, full=full)
    if current is not None:
        current.flush_pending(full=full)
    return [builder.turn for builder in builders if _turn_has_content(builder.turn)]


def _turn_has_content(turn: TurnDigest) -> bool:
    return bool(turn.user_text or turn.items or turn.final_text)


def _collect_child_signals(
    turns: list[TurnDigest], unlinked: list[UnlinkedSubagent]
) -> StruggleSignals:
    extra = StruggleSignals()
    for turn in turns:
        for item in turn.items:
            if isinstance(item, SubagentBlock) and item.digest is not None:
                extra = merge_signals(extra, item.digest.signals)
    for orphan in unlinked:
        extra = merge_signals(extra, orphan.digest.signals)
    return extra


def build_digest(session_dir: Path, *, full: bool = False) -> SessionDigest:
    """Resolved per-turn digest for one session directory, including child runs.

    Children attach to the ``task`` call that started them. Any other transcript
    under ``subagents/`` is listed as unlinked, so the score matches ``trace list``.
    """
    path = session_dir / TRANSCRIPT_FILENAME
    records = resolve_records(load_records(path))
    linked: set[str] = set()
    turns = _turns_from_records(records, session_dir=session_dir, linked=linked, full=full)
    unlinked = [
        UnlinkedSubagent(dir_name=child.name, digest=build_digest(child, full=full))
        for child in _child_dirs(session_dir)
        if child.name not in linked
    ]
    signals = merge_signals(signals_from_records(records), _collect_child_signals(turns, unlinked))
    return SessionDigest(
        session_dir=str(session_dir),
        manifest=_last_manifest(records),
        signals=signals,
        turns=turns,
        unlinked_subagents=unlinked,
    )


def iter_session_dirs(transcripts_root: Path) -> list[Path]:
    """Session folders under ``.monkeybot/transcripts``, newest first."""
    if not transcripts_root.is_dir():
        return []
    dirs = [
        path
        for path in transcripts_root.iterdir()
        if path.is_dir() and (path / TRANSCRIPT_FILENAME).is_file()
    ]
    return sorted(dirs, key=lambda path: path.name, reverse=True)


def scan_session_signals(session_dir: Path) -> StruggleSignals:
    """Signals for ``trace list``, including nested subagent transcripts."""
    signals = signals_from_records(load_records(session_dir / TRANSCRIPT_FILENAME))
    for child in _child_dirs(session_dir):
        signals = merge_signals(signals, scan_session_signals(child))
    return signals


def records_by_seq(session_dir: Path, seqs: Iterable[int]) -> dict[int, dict[str, Any]]:
    """Resolved records keyed by ``seq``, reading the file once. Absent seqs are omitted."""
    wanted = set(seqs)
    found: dict[int, dict[str, Any]] = {}
    for record in resolve_records(load_records(session_dir / TRANSCRIPT_FILENAME)):
        seq = _seq_of(record)
        if seq is not None and seq in wanted:
            found[seq] = record
    return found


def _fmt_signals(signals: StruggleSignals) -> list[str]:
    lines: list[str] = []
    if signals.score() == 0:
        return ["No struggle signals."]
    if signals.tool_errors:
        parts = [f"{name} {count}" for name, count in sorted(signals.tool_errors.items())]
        lines.append("tool errors: " + ", ".join(parts))
    if signals.interventions:
        parts = [f"{name} {count}" for name, count in sorted(signals.interventions.items())]
        lines.append("interventions: " + ", ".join(parts))
    lines.append(f"steers: {signals.steers}")
    lines.append(f"verifier verdicts: {signals.verdicts}")
    lines.append(f"empty assistant replies: {signals.empty_assistant}")
    lines.append(f"context summaries: {signals.summaries}")
    if signals.slow_tools:
        lines.append(f"slow tools (>{SLOW_TOOL_MS // 1000}s):")
        for slow in signals.slow_tools:
            lines.append(f"  - {slow.tool} {slow.duration_ms}ms @seq {slow.seq}")
    if signals.repeated_calls:
        lines.append("repeated calls:")
        for repeated in signals.repeated_calls:
            lines.append(
                f"  - {repeated.tool} x{repeated.count} first @seq {repeated.first_seq} "
                f"args=`{repeated.args_preview}`"
            )
    return lines


def _fmt_tool(step: ToolStep) -> str:
    status = "ok" if step.ok else "err"
    kind = f" {step.error_kind}" if step.error_kind else ""
    duration = f" {step.duration_ms}ms" if step.duration_ms is not None else ""
    return (
        f"@seq {step.seq} `{step.name}` {status}{kind}{duration} "
        f"result_chars={step.result_chars} args=`{step.args_preview}`"
    )


def _fmt_turns(turns: list[TurnDigest], *, nested: bool) -> list[str]:
    lines: list[str] = []
    heading = "###" if nested else "##"
    for turn in turns:
        user_at = f" @seq {turn.user_seq}" if turn.user_seq is not None else ""
        lines.append(f"{heading} Turn {turn.index}{user_at}")
        lines.append("")
        if turn.user_text:
            lines.append(f"**You:** {turn.user_text}")
            lines.append("")
        for item in turn.items:
            if isinstance(item, ToolStep):
                lines.append(f"- {_fmt_tool(item)}")
            elif isinstance(item, TurnNote):
                lines.append(f"- @seq {item.seq} {item.kind}: {item.text}")
            elif isinstance(item, SubagentBlock):
                label = item.subagent_type or "subagent"
                lines.append(f"- @seq {item.seq} subagent `{label}` `{item.child_thread_id}`")
                if item.digest is None:
                    lines.append("  - no child transcript")
                else:
                    for child_line in _fmt_turns(item.digest.turns, nested=True):
                        lines.append(f"  {child_line}" if child_line else "")
        if turn.final_text:
            at = f" @seq {turn.final_seq}" if turn.final_seq is not None else ""
            lines.append("")
            lines.append(f"**Assistant**{at}: {turn.final_text}")
        lines.append("")
    return lines


def render_digest_markdown(digest: SessionDigest, *, max_chars: int = 0) -> str:
    """Markdown timeline. ``max_chars`` 0 means no cap."""
    manifest = digest.manifest
    session_id = manifest.get("session_id") or Path(digest.session_dir).name
    lines = [f"# Transcript {session_id}", ""]
    for key in ("model", "provider", "started_at", "harness_version", "workspace_root"):
        value = manifest.get(key)
        if value is not None:
            lines.append(f"- {key}: {value}")
    if manifest.get("subagent_type"):
        lines.append(f"- subagent_type: {manifest['subagent_type']}")
    if manifest.get("parent_session_id"):
        lines.append(f"- parent_session_id: {manifest['parent_session_id']}")
    lines.append("")
    lines.append("## Signals")
    lines.append("")
    for line in _fmt_signals(digest.signals):
        if line.startswith("  "):
            lines.append(line)
        else:
            lines.append(f"- {line}")
    lines.append("")
    lines.extend(_fmt_turns(digest.turns, nested=False))
    if digest.unlinked_subagents:
        lines.append("## Unlinked subagent runs")
        lines.append("")
        lines.append("No parent `task` call in this transcript names these runs.")
        lines.append("")
        for orphan in digest.unlinked_subagents:
            label = orphan.digest.manifest.get("subagent_type") or "subagent"
            lines.append(f"- subagent `{label}` `{orphan.dir_name}`")
            for child_line in _fmt_turns(orphan.digest.turns, nested=True):
                lines.append(f"  {child_line}" if child_line else "")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    if max_chars > 0 and len(text) > max_chars:
        note = "\n\n… truncated\n"
        keep = max(0, max_chars - len(note))
        text = text[:keep].rstrip() + note
    return text


def digest_to_json(digest: SessionDigest) -> dict[str, Any]:
    """JSON-ready digest, including nested subagent timelines."""
    payload = asdict(digest)
    payload["signals"]["score"] = digest.signals.score()
    return payload
