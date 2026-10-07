"""Edit, regenerate, rewind, truncate, and fork for a session's branch tree.

Each branch is its own linear history thread. A branch op copies a prefix of
the active thread onto a new branch and makes it active; the caller starts any
replay turn. Truncate shortens the active thread in place, and fork copies a
prefix into a new session. Rows are addressed by ``Message.row_id``, which
copies keep, so a branch finds where it left its parent by id even after
either side compacts.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from collections.abc import Awaitable, Callable, Collection, Iterable, Iterator
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from monkeybot.core.attachments.catalog import referenced_attachment_ids
from monkeybot.core.attachments.store import AttachmentStore
from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.branches import (
    ROOT_BRANCH_ID,
    BranchRecord,
    BranchStore,
    branch_thread_id,
    root_record,
)
from monkeybot.core.persistence.runs import make_run_id
from monkeybot.core.persistence.thread_summary import (
    ChatThreadSummary,
    is_user_text_row,
    last_summary_index,
    preview_from_content_blob,
)
from monkeybot.core.types.content_blocks import ContentBlock, ToolRequest, ToolResponse

logger = logging.getLogger(__name__)

BranchOp = Literal["edit", "regenerate", "rewind", "restore"]


class RewriteEffects(Protocol):
    """Per-thread state outside history (goal ledger, progress tracker) that
    follows a rewrite. Applied best-effort: a failure is logged, never raised.
    """

    async def branched(
        self, source_thread: str, target_thread: str, dropped_row_ids: Collection[str]
    ) -> None: ...

    async def truncated(self, thread_id: str, dropped_row_ids: Collection[str]) -> None: ...

    async def purged(self, thread_ids: Collection[str]) -> None: ...


async def _apply_effects(
    effects: RewriteEffects | None, name: str, apply: Callable[[RewriteEffects], Awaitable[None]]
) -> None:
    if effects is None:
        return
    try:
        await apply(effects)
    except Exception:
        logger.warning("history rewrite %s effects failed", name, exc_info=True)


def _row_ids(messages: Iterable[Message]) -> set[str]:
    return {message.row_id for message in messages if message.row_id}


class HistoryRewriteError(Exception):
    """A rewrite the server will not apply. ``status_code`` is the HTTP status."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RewriteResult:
    """Outcome of a branch op. ``replay_content`` starts a turn when set."""

    op: BranchOp
    branch_id: str
    thread_id: str
    replay_content: list[ContentBlock] | None = None


@dataclass(frozen=True)
class TruncateResult:
    branch_id: str
    thread_id: str
    dropped: int


@dataclass(frozen=True)
class ForkResult:
    session_id: str
    attachments_copied: int


@dataclass(frozen=True)
class ActiveHistory:
    """The branch a session is showing: its newest rows plus navigator points."""

    session_id: str
    branch_id: str
    thread_id: str
    messages: list[Message]
    branch_points: list[dict[str, Any]]


def _active_of(session_id: str, records: list[BranchRecord]) -> BranchRecord:
    for record in records:
        if record.is_active:
            return record
    return root_record(session_id, is_active=True, now=0)


def _row_index(messages: list[Message], row_id: str) -> int:
    for index, message in enumerate(messages):
        if message.row_id == row_id:
            return index
    raise HistoryRewriteError(
        409,
        "ANCHOR_MISMATCH",
        "That message is no longer in this chat. Refresh and try again.",
    )


def _tool_ids(messages: Iterable[Message]) -> tuple[set[str], set[str]]:
    requests: set[str] = set()
    responses: set[str] = set()
    for message in messages:
        for block in message.content:
            if isinstance(block, ToolRequest) and block.id:
                requests.add(block.id)
            elif isinstance(block, ToolResponse) and block.id:
                responses.add(block.id)
    return requests, responses


def assert_tool_pairs(messages: list[Message], prefix_end: int) -> None:
    """Reject a cut that separates a tool request from its response.

    Only pairs that straddle the cut count. Stored history can already hold an
    unpaired call (cancelled tools are repaired in memory, not on disk), and
    that must not block every later rewrite.
    """
    kept_requests, kept_responses = _tool_ids(messages[:prefix_end])
    cut_requests, cut_responses = _tool_ids(messages[prefix_end:])
    if kept_requests & cut_responses or kept_responses & cut_requests:
        raise HistoryRewriteError(
            422,
            "TURN_BOUNDARY",
            "That cut would split a tool call from its result.",
        )


def _owning_user_index(messages: list[Message], index: int) -> int:
    for cursor in range(index, -1, -1):
        if is_user_text_row(messages[cursor]):
            return cursor
    raise HistoryRewriteError(422, "TURN_BOUNDARY", "No user message found to regenerate.")


def _rewind_end(messages: list[Message], index: int) -> int:
    """Exclusive end that keeps the turn containing ``index``.

    A user-text row keeps that message and drops the reply after it. Any
    other row keeps through the following tool results and verdicts, stopping
    before the next user-text row.
    """
    if is_user_text_row(messages[index]):
        return index + 1
    end = index + 1
    while end < len(messages) and not is_user_text_row(messages[end]):
        end += 1
    return end


_USER_ONLY_MESSAGES = {
    "edit": "Only a user message can be edited.",
    "restore": "Only a user message can be redone.",
}


def _assert_unsummarized(messages: list[Message], index: int) -> None:
    """Reject an op on a row whose turn starts at or before the newest summary.

    Matches the wire ``rewritable`` flag, so every row a client offers is one
    the server takes, and no cut can drop the summary.
    """
    summary = last_summary_index(messages)
    if summary is None:
        return
    turn = next(
        (cursor for cursor in range(index, -1, -1) if is_user_text_row(messages[cursor])), None
    )
    if turn is None or turn <= summary:
        raise HistoryRewriteError(
            422, "SUMMARIZED", "That part of the chat was summarized and can't be changed."
        )


def prefix_end_for(messages: list[Message], index: int, op: BranchOp) -> int:
    """Exclusive end of the prefix ``op`` keeps, after boundary checks."""
    if op == "edit" or op == "restore":
        if not is_user_text_row(messages[index]):
            raise HistoryRewriteError(422, "TURN_BOUNDARY", _USER_ONLY_MESSAGES[op])
        end = index
        if op == "restore" and end == 0:
            # An empty branch has no row to carry a navigator back to the parent.
            raise HistoryRewriteError(
                422, "NOTHING_TO_KEEP", "The first message can't be redone. Edit it instead."
            )
    elif op == "regenerate":
        end = _owning_user_index(messages, index)
    else:
        end = _rewind_end(messages, index)
        if end >= len(messages):
            raise HistoryRewriteError(422, "NOTHING_TO_REWIND", "Nothing after that message.")
    _assert_unsummarized(messages, index)
    assert_tool_pairs(messages, end)
    return end


async def branch_op(
    *,
    history: Any,
    branches: BranchStore,
    session_id: str,
    op: BranchOp,
    anchor_row_id: str,
    effects: RewriteEffects | None = None,
) -> RewriteResult:
    """Copy a prefix of the active branch onto a new active branch."""
    parent = _active_of(session_id, await branches.list(session_id))
    messages: list[Message] = await history.load(parent.thread_id)
    index = _row_index(messages, anchor_row_id)
    end = prefix_end_for(messages, index, op)
    replay: list[ContentBlock] | None = None
    if op == "regenerate":
        replay = list(messages[end].content)
        if not replay:
            raise HistoryRewriteError(
                422, "TURN_BOUNDARY", "Nothing to regenerate from that message."
            )
    branch_id = make_run_id()
    thread_id = branch_thread_id(session_id, branch_id)
    now = int(time.time() * 1000)
    record = BranchRecord(
        branch_id=branch_id,
        session_id=session_id,
        thread_id=thread_id,
        parent_branch_id=parent.branch_id,
        fork_row_id=messages[end - 1].row_id if end > 0 else None,
        op=op,
        created_at=now,
        last_active_at=now,
        is_active=True,
    )
    await history.reset(thread_id, messages[:end])
    try:
        await branches.create(record)
    except BaseException:
        # No branch row points at the copy, so nothing else would ever purge it.
        try:
            await history.reset(thread_id, [])
        except Exception:
            logger.warning(
                "history rewrite cleanup failed %s",
                kv(session_id=session_id, thread_id=thread_id),
                exc_info=True,
            )
        raise
    dropped = _row_ids(messages[end:])
    await _apply_effects(effects, op, lambda fx: fx.branched(parent.thread_id, thread_id, dropped))
    logger.info(
        "history rewrite %s",
        kv(session_id=session_id, op=op, branch_id=branch_id, parent=parent.branch_id, prefix=end),
    )
    return RewriteResult(op=op, branch_id=branch_id, thread_id=thread_id, replay_content=replay)


def _turn_cut(
    messages: list[Message], anchor_row_id: str, *, must_drop: bool, keep_summary: bool
) -> int:
    """Exclusive end keeping the turn that holds ``anchor_row_id``.

    ``keep_summary`` refuses summarized turns; an in-place cut must set it.
    """
    index = _row_index(messages, anchor_row_id)
    end = _rewind_end(messages, index)
    if keep_summary:
        _assert_unsummarized(messages, index)
    if must_drop and end >= len(messages):
        raise HistoryRewriteError(422, "NOTHING_TO_TRUNCATE", "Nothing after that message.")
    assert_tool_pairs(messages, end)
    return end


def _strands_a_branch(
    records: list[BranchRecord],
    active_branch_id: str,
    messages: list[Message],
    end: int,
    dropped: set[str],
) -> bool:
    """Whether cutting ``messages`` at ``end`` drops a fork row or any offered option.

    The second check covers compacted forks, whose navigator sits on a
    summary row rather than the fork row itself.
    """
    if not records:
        return False
    before = {
        key: set(options) for key, _, options in _key_points(records, active_branch_id, messages)
    }
    if any(key in dropped for key in before):
        return True
    after = {
        key: set(options)
        for key, _, options in _key_points(records, active_branch_id, messages[:end])
    }
    return any(not options <= after.get(key, set()) for key, options in before.items())


async def truncate_active(
    *,
    history: Any,
    branches: BranchStore,
    session_id: str,
    anchor_row_id: str,
    effects: RewriteEffects | None = None,
) -> TruncateResult:
    """Delete the active branch's rows after the anchor's turn, in place.

    Refused while another branch forks from a row that would go: its
    navigator lives on that row, so the branch would become unreachable.
    """
    records = await branches.list(session_id)
    active = _active_of(session_id, records)
    messages: list[Message] = await history.load(active.thread_id)
    end = _turn_cut(messages, anchor_row_id, must_drop=True, keep_summary=True)
    dropped = {message.row_id for message in messages[end:] if message.row_id}
    if _strands_a_branch(records, active.branch_id, messages, end, dropped):
        raise HistoryRewriteError(
            409,
            "BRANCHES_IN_TAIL",
            "Other versions of this chat branch off after that message.",
        )
    kept = {message.row_id for message in messages[:end]}
    if dropped & kept:
        # A duplicated id would take a kept row with it; rewrite instead.
        await history.reset(active.thread_id, messages[:end])
    else:
        await history.delete_rows(active.thread_id, dropped)
    await _apply_effects(effects, "truncate", lambda fx: fx.truncated(active.thread_id, dropped))
    result = TruncateResult(
        branch_id=active.branch_id, thread_id=active.thread_id, dropped=len(messages) - end
    )
    logger.info(
        "history truncate %s",
        kv(session_id=session_id, branch_id=active.branch_id, kept=end, dropped=result.dropped),
    )
    return result


async def fork_session(
    *,
    history: Any,
    branches: BranchStore,
    attachments: AttachmentStore | None,
    session_id: str,
    anchor_row_id: str,
    effects: RewriteEffects | None = None,
) -> ForkResult:
    """Start a new session holding the active branch through the anchor's turn.

    Attachment files are copied first: the new session resolves them under
    its own id, and a failed copy must not leave a fork with dead references.
    """
    active = _active_of(session_id, await branches.list(session_id))
    messages: list[Message] = await history.load(active.thread_id)
    # A fork is a new session, so it may start inside the summarized part.
    end = _turn_cut(messages, anchor_row_id, must_drop=False, keep_summary=False)
    prefix = messages[:end]
    forked_session_id = str(uuid.uuid4())
    copied = 0
    if attachments is not None:
        for attachment_id in referenced_attachment_ids(prefix):
            if await asyncio.to_thread(
                attachments.copy, session_id, forked_session_id, attachment_id
            ):
                copied += 1
    await history.reset(forked_session_id, prefix)
    dropped = _row_ids(messages[end:])
    await _apply_effects(
        effects, "fork", lambda fx: fx.branched(active.thread_id, forked_session_id, dropped)
    )
    logger.info(
        "history fork %s",
        kv(
            session_id=session_id,
            forked_session_id=forked_session_id,
            rows=len(prefix),
            attachments=copied,
        ),
    )
    return ForkResult(session_id=forked_session_id, attachments_copied=copied)


async def activate_branch(
    *, branches: BranchStore, session_id: str, branch_id: str
) -> BranchRecord:
    """Point the session at an existing branch. A session never branched is its root."""
    if branch_id == ROOT_BRANCH_ID and not await branches.list(session_id):
        return root_record(session_id, is_active=True, now=0)
    updated = await branches.set_active(session_id, branch_id)
    if updated is None:
        raise HistoryRewriteError(404, "BRANCH_NOT_FOUND", "Unknown branch")
    return updated


def _split_position(
    a: str,
    b: str,
    by_id: dict[str, BranchRecord],
    position: Callable[[str | None], float],
) -> float:
    """Active-thread position of the last row branches ``a`` and ``b`` share:
    the minimum, over the tree path between them, of each hop's fork row.
    """
    if a == b:
        return math.inf

    def lineage(branch_id: str) -> list[str]:
        path = [branch_id]
        seen = {branch_id}
        record = by_id.get(branch_id)
        while record is not None and record.parent_branch_id is not None:
            if record.parent_branch_id in seen:
                break
            path.append(record.parent_branch_id)
            seen.add(record.parent_branch_id)
            record = by_id.get(record.parent_branch_id)
        return path

    up_a, up_b = lineage(a), lineage(b)
    common = set(up_b)
    lca_index = next((i for i, node in enumerate(up_a) if node in common), None)
    if lca_index is None:
        return -1.0
    lca = up_a[lca_index]
    hops = up_a[:lca_index] + up_b[: up_b.index(lca)]
    split = math.inf
    for hop in hops:
        record = by_id.get(hop)
        fork = record.fork_row_id if record is not None else None
        split = min(split, position(fork))
    return split


def divergence_points(
    records: list[BranchRecord],
    active_branch_id: str,
    messages: list[Message],
) -> list[dict[str, Any]]:
    """Navigator points on the active thread ``messages`` (the whole thread).

    A point exists for each fork key (a row id, or ``None`` for a fork before
    the first row) where the key's branches and their parents fall into two
    or more continuations, and the active branch is in one. Each continuation
    is offered once, as the active branch or else its most recently active
    member, ordered by its oldest member.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for _key, anchor_row, options in _key_points(records, active_branch_id, messages):
        point = merged.get(anchor_row)
        if point is None:
            merged[anchor_row] = {"anchor": {"row_id": anchor_row}, "options": options}
            order.append(anchor_row)
        else:
            point["options"] = list(dict.fromkeys([*point["options"], *options]))

    row_position = {message.row_id: index for index, message in enumerate(messages)}
    points: list[dict[str, Any]] = []
    for row_id in sorted(order, key=lambda rid: row_position.get(rid, 0)):
        point = merged[row_id]
        point["active_index"] = point["options"].index(active_branch_id)
        points.append(point)
    return points


def _key_points(
    records: list[BranchRecord],
    active_branch_id: str,
    messages: list[Message],
) -> Iterator[tuple[str | None, str, list[str]]]:
    """``(fork key, anchor row id, options)`` for each fork key with a point."""
    by_id = {record.branch_id: record for record in records}
    row_position: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.row_id is not None:
            row_position[message.row_id] = index
    summary_index = last_summary_index(messages)
    members_by_key: dict[str | None, set[str]] = {}
    for record in records:
        if record.parent_branch_id is None:
            continue
        members = members_by_key.setdefault(record.fork_row_id, set())
        members.update((record.branch_id, record.parent_branch_id))

    for key, members in members_by_key.items():
        if key is None:
            r: float = -1
            anchor_index = 0 if messages else None
        elif key in row_position:
            r = row_position[key]
            after = int(r) + 1
            anchor_index = after if after < len(messages) else int(r)
        elif active_branch_id in members and summary_index is not None:
            r = summary_index
            anchor_index = summary_index
        else:
            continue
        if anchor_index is None:
            continue

        def position(fork: str | None, key: str | None = key, r: float = r) -> float:
            # Every branch here holds the key row, so a fork row the active
            # thread lacks lies past it.
            if fork == key:
                return r
            if fork is None:
                return -1.0
            return float(row_position.get(fork, math.inf))

        candidates = sorted(
            members | {active_branch_id},
            key=lambda bid: (by_id[bid].created_at, bid) if bid in by_id else (0, bid),
        )
        groups: list[list[str]] = []
        for candidate in candidates:
            for group in groups:
                if _split_position(candidate, group[0], by_id, position) > r:
                    group.append(candidate)
                    break
            else:
                groups.append([candidate])
        if len(groups) < 2:
            continue

        def representative(group: list[str]) -> str:
            if active_branch_id in group:
                return active_branch_id
            return max(
                group,
                key=lambda bid: (by_id[bid].last_active_at, bid) if bid in by_id else (0, bid),
            )

        anchor_row = messages[anchor_index].row_id
        if anchor_row is None:
            continue
        yield key, anchor_row, [representative(group) for group in groups]


async def load_active_history(
    history: Any,
    branches: BranchStore,
    session_id: str,
    *,
    limit: int | None = None,
) -> ActiveHistory:
    """The active branch's newest ``limit`` rows, plus navigator points on them."""
    records = await branches.list(session_id)
    active = _active_of(session_id, records)
    if not records:
        messages = await history.load(active.thread_id, limit=limit)
        return ActiveHistory(session_id, active.branch_id, active.thread_id, messages, [])
    # Points are computed on the whole thread so a fork row before the
    # returned tail still groups branches correctly.
    full: list[Message] = await history.load(active.thread_id)
    shown = full if limit is None else full[-limit:]
    shown_ids = {message.row_id for message in shown}
    points = [
        point
        for point in divergence_points(records, active.branch_id, full)
        if point["anchor"]["row_id"] in shown_ids
    ]
    return ActiveHistory(session_id, active.branch_id, active.thread_id, shown, points)


async def overlay_active_previews(
    history: Any,
    branches: BranchStore,
    summaries: list[ChatThreadSummary],
) -> list[ChatThreadSummary]:
    """Sidebar rows showing each session's active branch rather than its root.

    One branch query covers every row. A failure keeps the root preview for
    the affected rows instead of hiding them.
    """
    try:
        active_by_session = await branches.active_non_root([s.thread_id for s in summaries])
    except Exception:
        logger.warning("active branch preview lookup failed", exc_info=True)
        return summaries
    if not active_by_session:
        return summaries
    out: list[ChatThreadSummary] = []
    for summary in summaries:
        active = active_by_session.get(summary.thread_id)
        if active is None:
            out.append(summary)
            continue
        try:
            tail = await history.last_row(active.thread_id)
        except Exception:
            logger.warning(
                "active branch preview failed %s", kv(session_id=summary.thread_id), exc_info=True
            )
            out.append(summary)
            continue
        preview, count = summary.preview, summary.message_count
        if tail is not None:
            count, blob = tail
            preview = preview_from_content_blob(blob) or summary.preview
        out.append(
            replace(
                summary,
                message_count=count,
                preview=preview,
                last_message_at=max(summary.last_message_at, active.last_active_at),
            )
        )
    out.sort(key=lambda row: row.last_message_at, reverse=True)
    return out


async def purge_session_branches(
    history: Any,
    branches: BranchStore,
    session_id: str,
    effects: RewriteEffects | None = None,
) -> None:
    """Delete the session transcript, every branch thread, and the branch rows.

    Threads go first so a failure leaves branch rows pointing at whatever
    survived, and a retry finishes the job. The caller must hold the session
    idle, or a running turn could write to a thread after its wipe.
    """
    wiped: set[str] = set()

    async def _wipe(thread_id: str) -> None:
        if thread_id not in wiped:
            await history.reset(thread_id, [])
            wiped.add(thread_id)

    await _wipe(session_id)
    for record in await branches.list(session_id):
        await _wipe(record.thread_id)
    for record in await branches.delete_session(session_id):
        await _wipe(record.thread_id)
    await _apply_effects(effects, "purge", lambda fx: fx.purged(wiped))


async def resolve_active_thread_id(backend: Any, session_id: str) -> str:
    """History thread the session's next turn reads and writes.

    A backend without branch support stays on ``session_id``. A branch-store
    failure propagates so the turn never silently writes the root thread. The
    active branch's ``last_active_at`` is bumped so the sidebar sorts by it.
    """
    store_factory = getattr(backend, "branches", None)
    if store_factory is None:
        return session_id
    store: BranchStore = store_factory()
    active = await store.get_active(session_id)
    if active is None or not active.thread_id:
        return session_id
    await store.touch(session_id, active.branch_id)
    return active.thread_id
