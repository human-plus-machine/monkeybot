"""Goal ledger types and stores (protocol impls). SQLite is durable; in-memory is for tests."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
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


def new_entry_id() -> str:
    return str(uuid.uuid4())


def match_verbatim_seq(entries: Sequence[GoalEntry], user_texts: Sequence[str]) -> int | None:
    """Seq of the ledger row that lines up with the last kept user message.

    Walks ``entries`` in seq order and consumes ``user_texts`` in order. Entries
    whose verbatim does not match the next kept user text are skipped, so
    classifier noise between two user messages stays inside the copied prefix
    when it was recorded before the last match.
    """
    if not user_texts:
        return None
    index = 0
    last: int | None = None
    wanted = [text.strip() for text in user_texts]
    for entry in entries:
        if index >= len(wanted):
            break
        if entry.verbatim.strip() == wanted[index]:
            last = entry.seq
            index += 1
    return last


def retarget_entry(
    entry: GoalEntry,
    *,
    thread_id: str,
    seq: int,
    id_map: Mapping[str, str],
) -> GoalEntry:
    """Copy ``entry`` onto ``thread_id`` with new ids. Drops relates_to that
    point at a row outside the copied prefix.
    """
    new_id = id_map[entry.entry_id]
    constraints = tuple(
        replace(
            constraint,
            source_entry_id=id_map.get(constraint.source_entry_id, constraint.source_entry_id),
        )
        for constraint in entry.constraints
    )
    relates = id_map.get(entry.relates_to) if entry.relates_to else None
    return replace(
        entry,
        entry_id=new_id,
        thread_id=thread_id,
        seq=seq,
        relates_to=relates,
        constraints=constraints,
    )


def now_ms() -> int:
    return int(time.time() * 1000)


def empty_resolved(*, pending: bool) -> ResolvedIntent:
    return ResolvedIntent(
        active_goal=None,
        deferred_stack=(),
        standing_constraints=(),
        correction_history={},
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
    corr_counts: dict[tuple[ConstraintKind, str], tuple[Constraint, int]] = {}
    for entry in human:
        if entry.intent != Intent.CORRECTION:
            continue
        for constraint in entry.constraints:
            key = constraint.match_key
            prev = corr_counts.get(key)
            corr_counts[key] = (constraint, (prev[1] if prev else 0) + 1)
    return ResolvedIntent(
        active_goal=active,
        deferred_stack=deferred,
        standing_constraints=tuple(standing_by_key.values()),
        correction_history=dict(corr_counts.values()),
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

    async def copy_prefix(self, src_thread: str, dst_thread: str, upto_seq: int) -> int: ...

    async def drop_after(self, thread_id: str, upto_seq: int) -> int: ...


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

    async def copy_prefix(self, src_thread: str, dst_thread: str, upto_seq: int) -> int:
        source = [row for row in self._entries.get(src_thread) or [] if row.seq <= upto_seq]
        id_map = {row.entry_id: new_entry_id() for row in source}
        copied: list[GoalEntry] = []
        for seq, row in enumerate(source, start=1):
            copied.append(retarget_entry(row, thread_id=dst_thread, seq=seq, id_map=id_map))
        for row in copied:
            self._entries[dst_thread].append(row)
            self._by_id[row.entry_id] = row
        return len(copied)

    async def drop_after(self, thread_id: str, upto_seq: int) -> int:
        kept: list[GoalEntry] = []
        dropped = 0
        for row in self._entries.get(thread_id) or []:
            if row.seq > upto_seq:
                self._by_id.pop(row.entry_id, None)
                dropped += 1
            else:
                kept.append(row)
        if thread_id in self._entries:
            self._entries[thread_id] = kept
        return dropped


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
        return await self._list_entries_unlocked(thread_id)

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

    @with_conn_lock
    async def copy_prefix(self, src_thread: str, dst_thread: str, upto_seq: int) -> int:
        source = [
            row for row in await self._list_entries_unlocked(src_thread) if row.seq <= upto_seq
        ]
        id_map = {row.entry_id: new_entry_id() for row in source}
        await self._conn.execute("BEGIN IMMEDIATE")
        try:
            for seq, row in enumerate(source, start=1):
                await self._insert_entry(
                    retarget_entry(row, thread_id=dst_thread, seq=seq, id_map=id_map)
                )
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
        return len(source)

    @with_conn_lock
    async def drop_after(self, thread_id: str, upto_seq: int) -> int:
        cursor = await self._conn.execute(
            "DELETE FROM goal_ledger WHERE thread_id = ? AND seq > ?",
            (thread_id, upto_seq),
        )
        await self._conn.commit()
        return int(cursor.rowcount)

    async def _list_entries_unlocked(self, thread_id: str) -> list[GoalEntry]:
        columns = ", ".join(GOAL_LEDGER_COLUMNS)
        cursor = await self._conn.execute(
            f"SELECT {columns} FROM goal_ledger WHERE thread_id = ? ORDER BY seq ASC",
            (thread_id,),
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return [entry_from_row(tuple(r)) for r in rows]
