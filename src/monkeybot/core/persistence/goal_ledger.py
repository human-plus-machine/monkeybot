"""Goal ledger types and stores (protocol impls). SQLite is durable; in-memory is for tests."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

import aiosqlite

from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.sqlite import TaskReentrantLock, with_conn_lock

logger = logging.getLogger(__name__)


class Provenance(StrEnum):
    HUMAN = "human"
    VERIFIER_STEER = "verifier_steer"
    CONTEXT_SUMMARY = "context_summary"
    TOOL_RESULT = "tool_result"


class Channel(StrEnum):
    MESSAGE = "message"
    STEER = "steer"
    FOLLOW_UP = "follow_up"


class Intent(StrEnum):
    NEW_GOAL = "new_goal"
    REFINEMENT = "refinement"
    SCOPE_CHANGE = "scope_change"
    CORRECTION = "correction"
    PREEMPT = "preempt"
    ANSWER = "answer"
    NOISE = "noise"


class Status(StrEnum):
    ACTIVE = "active"
    DEFERRED = "deferred"
    SATISFIED = "satisfied"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class ConstraintKind(StrEnum):
    PATH_GLOB = "path_glob"
    TOOL_NAME = "tool_name"
    COMMAND_REGEX = "command_regex"
    FREE_TEXT = "free_text"


@dataclass(frozen=True)
class Constraint:
    kind: ConstraintKind
    pattern: str
    source_entry_id: str
    verbatim: str

    @property
    def match_key(self) -> tuple[ConstraintKind, str]:
        return (self.kind, self.pattern)


@dataclass(frozen=True)
class GoalEntry:
    entry_id: str
    thread_id: str
    seq: int
    verbatim: str
    provenance: Provenance
    channel: Channel | None
    intent: Intent
    status: Status
    relates_to: str | None
    constraints: tuple[Constraint, ...]
    done_when: tuple[str, ...]
    created_at_ms: int


@dataclass(frozen=True)
class ResolvedIntent:
    active_goal: GoalEntry | None
    refinement_chain: tuple[GoalEntry, ...]
    deferred_stack: tuple[GoalEntry, ...]
    superseded: tuple[str, ...]
    standing_constraints: tuple[Constraint, ...]
    correction_history: Mapping[Constraint, int]
    pending_classification: bool


@dataclass(frozen=True)
class ConstraintDraft:
    kind: ConstraintKind
    pattern: str
    verbatim: str


@dataclass(frozen=True)
class Classification:
    intent: Intent
    relates_to: str | None
    constraints: tuple[ConstraintDraft, ...]
    done_when: tuple[str, ...]


def new_entry_id() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    return int(time.time() * 1000)


def empty_resolved(*, pending: bool) -> ResolvedIntent:
    return ResolvedIntent(
        active_goal=None,
        refinement_chain=(),
        deferred_stack=(),
        superseded=(),
        standing_constraints=(),
        correction_history={},
        pending_classification=pending,
    )


def resolve_intent(
    entries: list[GoalEntry],
    *,
    pending_classification: bool,
) -> ResolvedIntent:
    """Derive the verifier's view. Only HUMAN provenance contributes intent."""
    human = [e for e in entries if e.provenance == Provenance.HUMAN]
    by_id = {e.entry_id: e for e in human}
    active = next((e for e in reversed(human) if e.status == Status.ACTIVE), None)
    chain: list[GoalEntry] = []
    cursor = active
    seen: set[str] = set()
    while cursor is not None and cursor.entry_id not in seen:
        chain.append(cursor)
        seen.add(cursor.entry_id)
        if not cursor.relates_to:
            break
        parent = by_id.get(cursor.relates_to)
        if parent is None or parent.intent not in (Intent.NEW_GOAL, Intent.REFINEMENT, Intent.SCOPE_CHANGE):
            break
        cursor = parent
    chain.reverse()
    deferred = tuple(e for e in human if e.status == Status.DEFERRED)
    superseded = tuple(_one_liner(e) for e in human if e.status == Status.SUPERSEDED)
    standing: list[Constraint] = []
    standing_seen: set[tuple[ConstraintKind, str]] = set()
    for entry in human:
        for constraint in entry.constraints:
            key = constraint.match_key
            if key in standing_seen:
                continue
            standing_seen.add(key)
            standing.append(constraint)
    corr_counts: dict[tuple[ConstraintKind, str], tuple[Constraint, int]] = {}
    for entry in human:
        if entry.intent != Intent.CORRECTION:
            continue
        for constraint in entry.constraints:
            key = constraint.match_key
            prev = corr_counts.get(key)
            corr_counts[key] = (constraint, (prev[1] if prev else 0) + 1)
    history = dict(corr_counts.values())
    return ResolvedIntent(
        active_goal=active,
        refinement_chain=tuple(chain),
        deferred_stack=deferred,
        superseded=superseded,
        standing_constraints=tuple(standing),
        correction_history=history,
        pending_classification=pending_classification,
    )


def _one_liner(entry: GoalEntry) -> str:
    text = " ".join(entry.verbatim.split())
    if len(text) <= 120:
        return text
    return text[:119] + "…"


def _constraints_to_json(constraints: tuple[Constraint, ...]) -> str:
    return json.dumps(
        [
            {
                "kind": c.kind.value,
                "pattern": c.pattern,
                "source_entry_id": c.source_entry_id,
                "verbatim": c.verbatim,
            }
            for c in constraints
        ],
        ensure_ascii=False,
    )


def _constraints_from_json(raw: str, fallback_entry_id: str) -> tuple[Constraint, ...]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        logger.warning("goal_ledger constraints json invalid %s", kv(raw=raw[:80]), exc_info=True)
        return ()
    if not isinstance(payload, list):
        return ()
    out: list[Constraint] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            kind = ConstraintKind(str(item.get("kind") or ""))
        except ValueError:
            kind = ConstraintKind.FREE_TEXT
        out.append(
            Constraint(
                kind=kind,
                pattern=str(item.get("pattern") or ""),
                source_entry_id=str(item.get("source_entry_id") or fallback_entry_id),
                verbatim=str(item.get("verbatim") or ""),
            )
        )
    return tuple(out)


def _done_when_from_json(raw: str) -> tuple[str, ...]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError:
        logger.warning("goal_ledger done_when json invalid %s", kv(raw=raw[:80]), exc_info=True)
        return ()
    if not isinstance(payload, list):
        return ()
    return tuple(str(x) for x in payload if str(x).strip())


def entry_from_row(row: tuple[Any, ...]) -> GoalEntry:
    (
        entry_id,
        thread_id,
        seq,
        verbatim,
        provenance,
        channel,
        intent,
        status,
        relates_to,
        constraints_json,
        done_when_json,
        created_at_ms,
    ) = row
    ch = Channel(channel) if channel else None
    return GoalEntry(
        entry_id=str(entry_id),
        thread_id=str(thread_id),
        seq=int(seq),
        verbatim=str(verbatim),
        provenance=Provenance(str(provenance)),
        channel=ch,
        intent=Intent(str(intent)),
        status=Status(str(status)),
        relates_to=str(relates_to) if relates_to else None,
        constraints=_constraints_from_json(str(constraints_json or "[]"), str(entry_id)),
        done_when=_done_when_from_json(str(done_when_json or "[]")),
        created_at_ms=int(created_at_ms),
    )


@runtime_checkable
class GoalLedgerStore(Protocol):
    """Durable goal-ledger rows, independent of HistoryStore."""

    async def next_seq(self, thread_id: str) -> int: ...

    async def append(self, entry: GoalEntry) -> None: ...

    async def list_entries(self, thread_id: str) -> list[GoalEntry]: ...

    async def update_status(self, entry_id: str, status: Status) -> None: ...

    async def delete_entry(self, entry_id: str) -> None: ...


GOAL_LEDGER_COLUMNS = (
    "entry_id",
    "thread_id",
    "seq",
    "verbatim",
    "provenance",
    "channel",
    "intent",
    "status",
    "relates_to",
    "constraints_json",
    "done_when_json",
    "created_at_ms",
)


class InMemoryGoalLedgerStore:
    """Process-local store for tests and non-SQLite backends (Phase 1)."""

    def __init__(self) -> None:
        self._entries: dict[str, list[GoalEntry]] = defaultdict(list)
        self._by_id: dict[str, GoalEntry] = {}

    async def next_seq(self, thread_id: str) -> int:
        rows = self._entries.get(thread_id) or []
        return (rows[-1].seq + 1) if rows else 1

    async def append(self, entry: GoalEntry) -> None:
        self._entries[entry.thread_id].append(entry)
        self._by_id[entry.entry_id] = entry

    async def list_entries(self, thread_id: str) -> list[GoalEntry]:
        return list(self._entries.get(thread_id) or [])

    async def update_status(self, entry_id: str, status: Status) -> None:
        old = self._by_id.get(entry_id)
        if old is None:
            return
        updated = replace(old, status=status)
        self._by_id[entry_id] = updated
        rows = self._entries[old.thread_id]
        for i, row in enumerate(rows):
            if row.entry_id == entry_id:
                rows[i] = updated
                break

    async def delete_entry(self, entry_id: str) -> None:
        old = self._by_id.pop(entry_id, None)
        if old is None:
            return
        self._entries[old.thread_id] = [
            row for row in self._entries[old.thread_id] if row.entry_id != entry_id
        ]


class SQLiteGoalLedgerStore:
    """SQLite persistence for goal-ledger entries."""

    def __init__(
        self,
        conn: aiosqlite.Connection,
        *,
        lock: TaskReentrantLock | None = None,
    ) -> None:
        self._conn = conn
        self._lock = lock or TaskReentrantLock()

    @with_conn_lock
    async def next_seq(self, thread_id: str) -> int:
        cursor = await self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM goal_ledger WHERE thread_id = ?",
            (thread_id,),
        )
        row = await cursor.fetchone()
        await cursor.close()
        current = int(row[0]) if row is not None else 0
        return current + 1

    @with_conn_lock
    async def append(self, entry: GoalEntry) -> None:
        await self._conn.execute(
            """
            INSERT INTO goal_ledger(
                entry_id, thread_id, seq, verbatim, provenance, channel,
                intent, status, relates_to, constraints_json, done_when_json, created_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id,
                entry.thread_id,
                entry.seq,
                entry.verbatim,
                entry.provenance.value,
                entry.channel.value if entry.channel is not None else None,
                entry.intent.value,
                entry.status.value,
                entry.relates_to,
                _constraints_to_json(entry.constraints),
                json.dumps(list(entry.done_when), ensure_ascii=False),
                entry.created_at_ms,
            ),
        )
        await self._conn.commit()

    @with_conn_lock
    async def list_entries(self, thread_id: str) -> list[GoalEntry]:
        columns = ", ".join(GOAL_LEDGER_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM goal_ledger WHERE thread_id = ? ORDER BY seq ASC",
            (thread_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [entry_from_row(tuple(r)) for r in rows]

    @with_conn_lock
    async def update_status(self, entry_id: str, status: Status) -> None:
        await self._conn.execute(
            "UPDATE goal_ledger SET status = ? WHERE entry_id = ?",
            (status.value, entry_id),
        )
        await self._conn.commit()

    @with_conn_lock
    async def delete_entry(self, entry_id: str) -> None:
        await self._conn.execute("DELETE FROM goal_ledger WHERE entry_id = ?", (entry_id,))
        await self._conn.commit()
