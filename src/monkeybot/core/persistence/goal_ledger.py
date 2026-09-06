"""Goal ledger types and stores (protocol impls). SQLite is durable; in-memory is for tests."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
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
    deferred_stack: tuple[GoalEntry, ...]
    standing_constraints: tuple[Constraint, ...]
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


def new_entry_id() -> str:
    return str(uuid.uuid4())


def now_ms() -> int:
    return int(time.time() * 1000)


def empty_resolved(*, pending: bool) -> ResolvedIntent:
    return ResolvedIntent(
        active_goal=None,
        deferred_stack=(),
        standing_constraints=(),
        pending_classification=pending,
    )


def resolve_intent(
    entries: list[GoalEntry],
    *,
    pending_classification: bool,
) -> ResolvedIntent:
    """Derive the verifier's view. Only HUMAN provenance contributes intent.

    ``standing_constraints`` accumulate from every human entry regardless of
    status. Constraints are sticky across scope changes: a path glob attached
    to a later-superseded goal stays standing until a later correction
    replaces that same ``(kind, pattern)`` key.
    """
    human = [e for e in entries if e.provenance == Provenance.HUMAN]
    active = next((e for e in reversed(human) if e.status == Status.ACTIVE), None)
    deferred = tuple(e for e in human if e.status == Status.DEFERRED)
    standing_by_key: dict[tuple[ConstraintKind, str], Constraint] = {}
    for entry in human:
        for constraint in entry.constraints:
            standing_by_key[constraint.match_key] = constraint
    return ResolvedIntent(
        active_goal=active,
        deferred_stack=deferred,
        standing_constraints=tuple(standing_by_key.values()),
        pending_classification=pending_classification,
    )


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

    async def commit_classified(
        self,
        entry: GoalEntry,
        *,
        status_updates: Sequence[tuple[str, Status]] = (),
    ) -> GoalEntry: ...

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
    """Process-local store for tests."""

    def __init__(self) -> None:
        self._entries: dict[str, list[GoalEntry]] = defaultdict(list)
        self._by_id: dict[str, GoalEntry] = {}

    async def next_seq(self, thread_id: str) -> int:
        rows = self._entries.get(thread_id) or []
        return (rows[-1].seq + 1) if rows else 1

    async def append(self, entry: GoalEntry) -> None:
        self._entries[entry.thread_id].append(entry)
        self._by_id[entry.entry_id] = entry

    def _apply_status(self, entry_id: str, status: Status) -> None:
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

    async def commit_classified(
        self,
        entry: GoalEntry,
        *,
        status_updates: Sequence[tuple[str, Status]] = (),
    ) -> GoalEntry:
        for entry_id, status in status_updates:
            self._apply_status(entry_id, status)
        seq = await self.next_seq(entry.thread_id)
        stamped = replace(entry, seq=seq)
        await self.append(stamped)
        return stamped

    async def list_entries(self, thread_id: str) -> list[GoalEntry]:
        return list(self._entries.get(thread_id) or [])

    async def update_status(self, entry_id: str, status: Status) -> None:
        self._apply_status(entry_id, status)

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

    async def _insert_entry(self, entry: GoalEntry) -> None:
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

    @with_conn_lock
    async def append(self, entry: GoalEntry) -> None:
        await self._insert_entry(entry)
        await self._conn.commit()

    @with_conn_lock
    async def commit_classified(
        self,
        entry: GoalEntry,
        *,
        status_updates: Sequence[tuple[str, Status]] = (),
    ) -> GoalEntry:
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            cursor = await self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM goal_ledger WHERE thread_id = ?",
                (entry.thread_id,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            seq = (int(row[0]) if row is not None else 0) + 1
            stamped = replace(entry, seq=seq)
            for entry_id, status in status_updates:
                await self._conn.execute(
                    "UPDATE goal_ledger SET status = ? WHERE entry_id = ?",
                    (status.value, entry_id),
                )
            await self._insert_entry(stamped)
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return stamped

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
