"""Convert a session ``transcript.ndjson`` into a T3a trajectory cassette.

The writer stubs repeated bytes by reference (``schema_seq``, ``text_seq``,
``content_seq``, ``result_seq``, ``base_seq`` + ``diff``). The transcript reader
expands those first, so every record this module reads holds real bytes.

Each ``ProviderResponse`` becomes one cassette call: ``thinking``, then the
``tool_call`` events, then one ``text``, then ``usage`` and ``done``. Each
``VerifierVerdict`` becomes one judge completion. Classifier rows come from
``--ledger``, the session SQLite database, because they are not provider calls.
The transcript keeps no explicit ``truncated`` flag; a ``truncated_batch``
intervention with detail ``stream_truncated`` right after a response says that
response was cut off, and sets ``done.truncated``. A thinking signature is a
separate ``ThinkingBlockComplete`` record just before its response.

Text is scrubbed with :mod:`testkit.scrub` before it is written.

    uv run python -m regression.t3.tools.from_transcript <transcript dir or file> \\
        --scenario coding/fix_failing_test --out regression/t3/trajectories/x.cassette.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from monkeybot.core.persistence.transcript import TRANSCRIPT_FILENAME
from monkeybot.core.persistence.transcript_reader import load_records, resolve_records
from regression.schema.events import DoneEvent
from regression.t3.tools.cassette import (
    SCHEMA_VERSION,
    Cassette,
    CassetteCall,
    CassetteClassifierRow,
    CassetteConstraint,
    CassetteJudgeCompletion,
    CassetteRequest,
    dump_cassette,
    prompt_digest,
    tool_schema_digest,
)
from testkit.scrub import scrub

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRUNCATED_INTERVENTION = "truncated_batch"
_TRUNCATED_DETAIL = "stream_truncated"


class TranscriptConversionError(ValueError):
    """The transcript lacks something a cassette needs."""


def transcript_path(source: Path) -> Path:
    """``source`` is a session directory or the ``transcript.ndjson`` inside it."""
    path = Path(source)
    if path.is_dir():
        path = path / TRANSCRIPT_FILENAME
    if not path.is_file():
        raise TranscriptConversionError(f"no transcript at {path}")
    return path


def from_transcript(
    source: Path,
    *,
    scenario: str,
    harness_commit: str | None = None,
    source_label: str | None = None,
    exclude_request_ids: Collection[str] = (),
    ledger: Path | None = None,
) -> Cassette:
    """Build a cassette from one session transcript.

    ``source_label`` is stored as ``source_transcript`` (a repo-relative path, or
    ``None`` when the transcript is not kept). ``exclude_request_ids`` drops every
    record of those requests, for example a lock-check probe turn.

    Agent replies come from ``ProviderResponse`` records. Judge completions come
    from ``VerifierVerdict`` records. Classifier rows are not in the transcript;
    pass ``ledger`` (the session SQLite database) to read them for the manifest's
    ``session_id``.
    """
    path = transcript_path(source)
    skipped = set(exclude_request_ids)
    records = [
        record
        for record in resolve_records(load_records(path))
        if record.get("request_id") not in skipped
    ]
    build = _Build()
    for record in records:
        build.take(record)
    build.finish()
    if not build.calls:
        raise TranscriptConversionError(
            f"{path} has no completed provider call; was runtime.transcript_enabled on?"
        )
    manifest = _last_manifest(records)
    classifier: list[CassetteClassifierRow] = []
    if ledger is not None:
        session_id = _text(manifest.get("session_id"))
        if not session_id:
            raise TranscriptConversionError(f"{path} has no session_id for the goal ledger")
        classifier = classifier_rows_from_ledger(ledger, session_id)
    cassette = Cassette(
        schema_version=SCHEMA_VERSION,
        kind="trajectory",
        recorded_at=_text(manifest.get("started_at")) or _now(),
        harness_commit=harness_commit if harness_commit is not None else _git_head(),
        provider=_text(manifest.get("provider")),
        model=_text(manifest.get("model")),
        tool_schema_digest=tool_schema_digest(build.first_tools) if build.first_tools else None,
        prompt_digest=prompt_digest(build.first_prompt) if build.first_prompt else None,
        source_transcript=source_label,
        scenario=scenario,
        calls=build.calls,
        tool_results=build.tool_results,
        classifier=classifier,
        judge=build.judge,
    )
    return _scrubbed(cassette)


@dataclass
class _Slot:
    """One provider call in request order. ``exchange_id`` pairs a summary that overlaps chat."""

    request: CassetteRequest
    sequential: bool
    call: CassetteCall | None = None
    dropped: bool = False


class _Build:
    """Single pass over resolved records. Holds the call being assembled.

    Chat calls pair the latest request with the next response. A summary call
    carries ``exchange_id`` on both records, so its response can land after the
    next chat call without taking that chat call's place. Slots stay in request
    order, which is the order replay enters ``stream()``.
    """

    def __init__(self) -> None:
        self.calls: list[CassetteCall] = []
        self.judge: list[CassetteJudgeCompletion] = []
        self.tool_results: dict[str, str] = {}
        self.first_tools: list[dict[str, Any]] = []
        self.first_prompt = ""
        self._tool_names: list[str] = []
        self._slots: list[_Slot] = []
        self._open: _Slot | None = None
        self._by_exchange: dict[str, _Slot] = {}
        self._signature: str | None = None

    def take(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "SystemPromptSnapshot" and not self.first_prompt:
            text = record.get("text")
            if isinstance(text, str):
                self.first_prompt = text
        elif kind == "ProviderRequest":
            self._on_request(record)
        elif kind == "ThinkingBlockComplete":
            signature = record.get("signature")
            self._signature = signature if isinstance(signature, str) and signature else None
        elif kind == "ProviderResponse":
            self._on_response(record)
        elif kind == "ToolCallResult":
            self._on_result(record)
        elif kind == "HarnessIntervention":
            self._on_intervention(record)
        elif kind == "VerifierVerdict":
            completion = _judge_completion(record)
            if completion is not None:
                self.judge.append(completion)

    def finish(self) -> None:
        """Emit filled slots in request order."""
        calls: list[CassetteCall] = []
        for slot in self._slots:
            if slot.dropped or slot.call is None:
                continue
            slot.call.index = len(calls)
            calls.append(slot.call)
        self.calls = calls

    def _on_request(self, record: dict[str, Any]) -> None:
        exchange = record.get("exchange_id")
        if isinstance(exchange, str) and exchange:
            slot = _Slot(request=self._request_for(record, update_cursor=False), sequential=False)
            self._slots.append(slot)
            self._by_exchange[exchange] = slot
            return
        if self._open is not None and self._open.call is None:
            self._open.dropped = True
        slot = _Slot(request=self._request_for(record, update_cursor=True), sequential=True)
        self._slots.append(slot)
        self._open = slot
        self._signature = None

    def _request_for(self, record: dict[str, Any], *, update_cursor: bool) -> CassetteRequest:
        tools = record.get("tools")
        names = list(self._tool_names)
        full: list[dict[str, Any]] | None = None
        if isinstance(tools, list):
            full = [tool for tool in tools if isinstance(tool, dict)]
            names = [str(tool.get("name") or "") for tool in full]
        elif isinstance(tools, dict) and isinstance(tools.get("names"), list):
            # A stub whose source record is not in this file: names survive, schemas do not.
            names = [str(name) for name in tools["names"]]
        if update_cursor:
            self._tool_names = names
            if full and not self.first_tools:
                self.first_tools = full
        messages = record.get("messages")
        offset = record.get("message_offset")
        count = (offset if isinstance(offset, int) else 0) + (
            len(messages) if isinstance(messages, list) else 0
        )
        return CassetteRequest(tool_names=list(names), message_count=count)

    def _on_response(self, record: dict[str, Any]) -> None:
        exchange = record.get("exchange_id")
        if isinstance(exchange, str) and exchange:
            slot = self._by_exchange.get(exchange)
            if slot is None or slot.call is not None:
                return
            self._fill(slot, record, signature=None)
            return
        if self._open is None:
            return
        slot = self._open
        self._open = None
        signature = self._signature
        self._signature = None
        self._fill(slot, record, signature=signature)

    def _fill(self, slot: _Slot, record: dict[str, Any], *, signature: str | None) -> None:
        events: list[dict[str, Any]] = []
        thinking = record.get("thinking")
        thinking_text = thinking if isinstance(thinking, str) else ""
        if thinking_text or signature:
            block: dict[str, Any] = {"type": "thinking", "text": thinking_text}
            if signature:
                block["signature"] = signature
            events.append(block)
        requests = record.get("tool_requests")
        for item in requests if isinstance(requests, list) else []:
            if not isinstance(item, dict):
                continue
            args = item.get("args")
            events.append(
                {
                    "type": "tool_call",
                    "call_id": str(item.get("call_id") or ""),
                    "name": str(item.get("name") or ""),
                    "args": args if isinstance(args, dict) else {},
                }
            )
        text = record.get("text")
        if isinstance(text, str) and text:
            events.append({"type": "text", "text": text})
        events.append({"type": "usage", **_usage(record.get("usage"))})
        events.append({"type": "done", "truncated": False})
        slot.call = CassetteCall.model_validate(
            {"index": 0, "request": slot.request, "events": events}
        )

    def _on_result(self, record: dict[str, Any]) -> None:
        call_id = record.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            return
        body = record.get("result")
        if body is None:
            body = record.get("error")
        if body is None:
            return
        self.tool_results[call_id] = body if isinstance(body, str) else json.dumps(body)

    def _on_intervention(self, record: dict[str, Any]) -> None:
        if record.get("intervention") != _TRUNCATED_INTERVENTION:
            return
        if record.get("detail") != _TRUNCATED_DETAIL:
            return
        for slot in reversed(self._slots):
            if not slot.sequential or slot.dropped or slot.call is None:
                continue
            for event in slot.call.events:
                if isinstance(event, DoneEvent):
                    event.truncated = True
            return


def classifier_rows_from_ledger(db_path: Path, thread_id: str) -> list[CassetteClassifierRow]:
    """Human goal-ledger rows for ``thread_id``, in ``seq`` order.

    Each row is one classifier decision. Constraint ``source_entry_id`` is a
    ledger id assigned at insert time, so it is not part of the replay row.
    """
    path = Path(db_path)
    if not path.is_file():
        raise TranscriptConversionError(f"no goal ledger at {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise TranscriptConversionError(f"cannot open goal ledger {path}: {exc}") from exc
    try:
        try:
            cursor = conn.execute(
                """
                SELECT intent, relates_to, constraints_json, done_when_json
                FROM goal_ledger
                WHERE thread_id = ? AND provenance = 'human'
                ORDER BY seq
                """,
                (thread_id,),
            )
            fetched = cursor.fetchall()
        except sqlite3.Error as exc:
            raise TranscriptConversionError(f"cannot read goal ledger {path}: {exc}") from exc
    finally:
        conn.close()
    rows: list[CassetteClassifierRow] = []
    for intent, relates_to, constraints_json, done_when_json in fetched:
        rows.append(_classifier_row(intent, relates_to, constraints_json, done_when_json))
    return rows


def _classifier_row(
    intent: object,
    relates_to: object,
    constraints_json: object,
    done_when_json: object,
) -> CassetteClassifierRow:
    relates = relates_to if isinstance(relates_to, str) and relates_to.strip() else None
    return CassetteClassifierRow(
        intent=_text(intent) or "noise",
        relates_to=relates,
        constraints=_constraints(constraints_json),
        done_when=_done_when(done_when_json),
    )


def _constraints(raw: object) -> list[CassetteConstraint]:
    payload = _json_list(raw)
    rows: list[CassetteConstraint] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        pattern = _text(item.get("pattern"))
        verbatim = _text(item.get("verbatim"))
        if not pattern and not verbatim:
            continue
        rows.append(
            CassetteConstraint(
                kind=_text(item.get("kind")) or "free_text",
                pattern=pattern or verbatim,
                verbatim=verbatim or pattern,
            )
        )
    return rows


def _done_when(raw: object) -> list[str]:
    return [item.strip() for item in _json_list(raw) if isinstance(item, str) and item.strip()]


def _json_list(raw: object) -> list[Any]:
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _judge_completion(record: dict[str, Any]) -> CassetteJudgeCompletion | None:
    """The judge JSON a ``VerifierVerdict`` was parsed from.

    Model correction text is not stored on the verdict. Status, severity,
    confidence, and rationale are the fields the judge parser keeps.
    """
    status = _text(record.get("status"))
    if not status:
        return None
    body = {
        "status": status,
        "severity": _text(record.get("severity")) or "none",
        "confidence": _confidence(record.get("confidence")),
        "rationale": _text(record.get("rationale")),
    }
    return CassetteJudgeCompletion(
        mode="valid",
        body=json.dumps(body, ensure_ascii=False),
        output_tokens=_count(record.get("judge_tokens")),
    )


def _confidence(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _usage(raw: object) -> dict[str, int]:
    keys = (
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
    )
    source = raw if isinstance(raw, dict) else {}
    return {key: _count(source.get(key)) for key in keys}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _last_manifest(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    for record in records:
        if record.get("type") == "SessionManifest":
            manifest = record
    return manifest


def _scrubbed(cassette: Cassette) -> Cassette:
    body = _scrub_value(cassette.model_dump(mode="json"))
    return Cassette.model_validate(body)


def _scrub_value(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, list):
        return [_scrub_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_value(item) for key, item in value.items()}
    return value


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_head() -> str:
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    sha = done.stdout.strip()
    return sha if done.returncode == 0 and sha else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert a transcript into a T3a cassette.")
    parser.add_argument("source", type=Path, help="session directory or transcript.ndjson")
    parser.add_argument("--scenario", required=True, help="scenario name stored in the cassette")
    parser.add_argument("--out", required=True, type=Path, help="cassette JSON to write")
    parser.add_argument("--source-label", default=None, help="value for source_transcript")
    parser.add_argument(
        "--ledger",
        type=Path,
        default=None,
        help="session SQLite database; classifier rows are read for the transcript session_id",
    )
    args = parser.parse_args(argv)
    try:
        cassette = from_transcript(
            args.source,
            scenario=args.scenario,
            source_label=args.source_label,
            ledger=args.ledger,
        )
    except TranscriptConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    dump_cassette(cassette, args.out)
    print(
        f"wrote {args.out} ({len(cassette.calls)} calls, "
        f"{len(cassette.classifier)} classifier rows, {len(cassette.judge)} judge completions)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
