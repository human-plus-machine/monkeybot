"""Edit, regenerate, rewind, truncate, and fork for a session's branch tree.

Each branch is stored as its own linear history thread. This module only
copies or replaces rows between turns. Compaction and the verifier keep
operating on whichever thread is active; their code is not involved here.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal, Protocol

from monkeybot.core.llm.provider import Message
from monkeybot.core.logging_utils import kv
from monkeybot.core.persistence.branches import (
    ROOT_BRANCH_ID,
    BranchRecord,
    BranchStore,
    branch_thread_id,
)
from monkeybot.core.persistence.runs import make_run_id
from monkeybot.core.persistence.thread_summary import (
    ChatThreadSummary,
    is_user_text_row,
    last_summary_index,
    message_fingerprint,
    preview_from_content_blob,
    text_from_message,
)
from monkeybot.core.types.content_blocks import ContentBlock, ToolRequest, ToolResponse

logger = logging.getLogger(__name__)

RewriteOp = Literal["edit", "regenerate", "rewind", "truncate", "fork"]


class HistoryRewriteError(Exception):
    """A rewrite the server will not apply. ``status_code`` is the HTTP status."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class HistoryAnchor:
    """Client address of one stored history row."""

    row_index: int
    fingerprint: str


class _ThreadEffects(Protocol):
    def fork_thread(self, src_thread: str, dst_thread: str) -> None: ...

    def reset_conversation_state(self, thread_id: str) -> None: ...


class _LedgerEffects(Protocol):
    async def copy_branch_prefix(
        self,
        src_thread: str,
        dst_thread: str,
        dropped_texts: list[str],
    ) -> None: ...

    async def drop_truncated(self, thread_id: str, dropped_texts: list[str]) -> None: ...

    async def clear_thread(self, thread_id: str) -> None: ...


@dataclass
class RewriteEffects:
    """Optional verifier hooks. Both no-op when the feature is disabled."""

    tracker: _ThreadEffects | None = None
    ledger: _LedgerEffects | None = None


@dataclass(frozen=True)
class RewriteResult:
    """Outcome of a rewrite. ``replay_content`` starts a turn when set."""

    op: str
    branch_id: str
    thread_id: str
    replay_content: list[ContentBlock] | None = None
    forked_session_id: str | None = None


@dataclass(frozen=True)
class ActiveHistory:
    """The branch a session is currently showing.

    ``messages`` may be the newest tail of the thread; ``offset`` is the
    absolute index of ``messages[0]`` and ``summary_index`` is absolute.
    """

    session_id: str
    branch_id: str
    thread_id: str
    messages: list[Message]
    branch_points: list[dict[str, Any]]
    offset: int = 0
    summary_index: int | None = None


def locate_anchor(messages: list[Message], anchor: HistoryAnchor) -> int | None:
    """Resolve ``anchor`` against ``messages``. ``None`` if the row is gone.

    The hinted index wins when its fingerprint still matches. Otherwise the
    closest earlier row with that fingerprint is used: compaction and
    truncation only move surviving rows to lower indexes, so a later row with
    identical content is never the same message.
    """
    hinted = anchor.row_index
    if 0 <= hinted < len(messages) and message_fingerprint(messages[hinted]) == anchor.fingerprint:
        return hinted
    for index in range(min(hinted, len(messages) - 1), -1, -1):
        if message_fingerprint(messages[index]) == anchor.fingerprint:
            return index
    return None


def resolve_anchor(messages: list[Message], anchor: HistoryAnchor) -> int:
    """Like :func:`locate_anchor`, but a miss is a 409."""
    found = locate_anchor(messages, anchor)
    if found is None:
        raise HistoryRewriteError(
            409,
            "ANCHOR_MISMATCH",
            "That message changed or was removed. Refresh the chat and try again.",
        )
    return found


def _tool_ids(messages: list[Message]) -> tuple[set[str], set[str]]:
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


def assert_editable(messages: list[Message], index: int) -> None:
    """Rows at or before the newest compaction summary are read-only."""
    boundary = last_summary_index(messages)
    if boundary is not None and index <= boundary:
        raise HistoryRewriteError(
            422,
            "COMPACTED_REGION",
            "That message was summarized and can no longer be edited or rewound.",
        )


def _owning_user_index(messages: list[Message], index: int) -> int:
    if is_user_text_row(messages[index]):
        return index
    for cursor in range(index, -1, -1):
        if is_user_text_row(messages[cursor]):
            return cursor
    raise HistoryRewriteError(
        422,
        "TURN_BOUNDARY",
        "No user message found to regenerate.",
    )


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


def prefix_end_for(messages: list[Message], index: int, op: RewriteOp) -> int:
    """Exclusive end index of the prefix ``op`` keeps, after boundary checks."""
    if op == "edit":
        if not is_user_text_row(messages[index]):
            raise HistoryRewriteError(
                422,
                "TURN_BOUNDARY",
                "Only a user message can be edited.",
            )
        assert_editable(messages, index)
        end = index
    elif op == "regenerate":
        user_index = _owning_user_index(messages, index)
        assert_editable(messages, user_index)
        end = user_index
    else:
        assert_editable(messages, index)
        end = _rewind_end(messages, index)
    assert_tool_pairs(messages, end)
    return end


def _fork_anchor(messages: list[Message], prefix_end: int) -> tuple[int, str] | None:
    """Last shared row, present on both the parent and the new branch."""
    if prefix_end <= 0:
        return None
    row = prefix_end - 1
    return row, message_fingerprint(messages[row])


def _user_texts(messages: list[Message]) -> list[str]:
    return [text_from_message(message) for message in messages if is_user_text_row(message)]


async def _copy_side_effects(
    effects: RewriteEffects | None,
    src_thread: str,
    dst_thread: str,
    dropped: list[Message],
) -> None:
    """Carry verifier state onto the new thread. Best effort: the history copy
    already committed, so a verifier failure must not fail the rewrite.
    """
    if effects is None:
        return
    try:
        if effects.tracker is not None:
            effects.tracker.fork_thread(src_thread, dst_thread)
        if effects.ledger is not None:
            await effects.ledger.copy_branch_prefix(src_thread, dst_thread, _user_texts(dropped))
    except Exception:
        logger.warning(
            "history rewrite verifier copy failed %s",
            kv(src_thread=src_thread, dst_thread=dst_thread),
            exc_info=True,
        )


async def _truncate_side_effects(
    effects: RewriteEffects | None,
    thread_id: str,
    dropped: list[Message],
) -> None:
    if effects is None:
        return
    try:
        if effects.tracker is not None:
            effects.tracker.reset_conversation_state(thread_id)
        if effects.ledger is not None:
            await effects.ledger.drop_truncated(thread_id, _user_texts(dropped))
    except Exception:
        logger.warning(
            "history truncate verifier reset failed %s",
            kv(thread_id=thread_id),
            exc_info=True,
        )


def _replay_content(messages: list[Message], index: int) -> list[ContentBlock]:
    replay = list(messages[_owning_user_index(messages, index)].content)
    if not replay:
        raise HistoryRewriteError(
            422,
            "TURN_BOUNDARY",
            "Nothing to regenerate from that message.",
        )
    return replay


def _assert_fork_points_kept(
    records: list[BranchRecord],
    branch_id: str,
    messages: list[Message],
    end: int,
) -> None:
    """Truncate must not cut away a row where this branch meets another version.

    That is a child's fork row, or this branch's own fork row from its parent.
    The other version would survive but its navigator point would vanish,
    leaving it unreachable from the chat. Rewind keeps both versions instead.
    """
    for record in records:
        is_child = record.parent_branch_id == branch_id
        is_own_fork = record.branch_id == branch_id and record.parent_branch_id is not None
        if not (is_child or is_own_fork):
            continue
        if record.fork_row_index is None or record.fork_fingerprint is None:
            continue
        anchor = HistoryAnchor(record.fork_row_index, record.fork_fingerprint)
        located = locate_anchor(messages, anchor)
        if located is not None and located >= end:
            raise HistoryRewriteError(
                409,
                "BRANCHES_IN_TAIL",
                "Another version of this chat branches off at a message this would remove. "
                "Rewind instead to keep both.",
            )


async def _active_parent(
    branches: BranchStore,
    session_id: str,
) -> BranchRecord:
    active = await branches.get_active(session_id)
    if active is not None:
        if active.branch_id != ROOT_BRANCH_ID:
            await branches.ensure_root(session_id)
        return active
    return await branches.ensure_root(session_id)


async def branch_op(
    *,
    history: Any,
    branches: BranchStore,
    session_id: str,
    op: Literal["edit", "regenerate", "rewind"],
    anchor: HistoryAnchor,
    effects: RewriteEffects | None = None,
) -> RewriteResult:
    """Copy a prefix onto a new branch and make it active. Does not start a turn."""
    parent = await _active_parent(branches, session_id)
    messages = await history.load(parent.thread_id)
    index = resolve_anchor(messages, anchor)
    end = prefix_end_for(messages, index, op)
    replay = _replay_content(messages, index) if op == "regenerate" else None
    prefix = list(messages[:end])
    branch_id = make_run_id()
    thread_id = branch_thread_id(session_id, branch_id)
    shared = _fork_anchor(messages, end)
    now = int(time.time() * 1000)
    created = BranchRecord(
        branch_id=branch_id,
        session_id=session_id,
        thread_id=thread_id,
        parent_branch_id=parent.branch_id,
        fork_row_index=shared[0] if shared is not None else None,
        fork_fingerprint=shared[1] if shared is not None else None,
        op=op,
        created_at=now,
        last_active_at=now,
        is_active=False,
    )
    await history.reset(thread_id, prefix)
    try:
        await branches.create(created, make_active=True)
    except Exception:
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
    await _copy_side_effects(effects, parent.thread_id, thread_id, messages[end:])
    logger.info(
        "history rewrite %s",
        kv(session_id=session_id, op=op, branch_id=branch_id, prefix=end),
    )
    return RewriteResult(op=op, branch_id=branch_id, thread_id=thread_id, replay_content=replay)


async def truncate_active(
    *,
    history: Any,
    branches: BranchStore,
    session_id: str,
    anchor: HistoryAnchor,
    effects: RewriteEffects | None = None,
) -> RewriteResult:
    """Destructively drop the tail of the active branch. Siblings are kept."""
    active = await branches.get_active(session_id)
    thread_id = active.thread_id if active is not None else session_id
    branch_id = active.branch_id if active is not None else ROOT_BRANCH_ID
    messages = await history.load(thread_id)
    index = resolve_anchor(messages, anchor)
    end = prefix_end_for(messages, index, "truncate")
    _assert_fork_points_kept(await branches.list(session_id), branch_id, messages, end)
    await history.truncate_tail(thread_id, end)
    await _truncate_side_effects(effects, thread_id, messages[end:])
    logger.info(
        "history truncate %s",
        kv(session_id=session_id, branch_id=branch_id, prefix=end),
    )
    return RewriteResult(op="truncate", branch_id=branch_id, thread_id=thread_id)


async def fork_session(
    *,
    history: Any,
    branches: BranchStore,
    session_id: str,
    anchor: HistoryAnchor,
    effects: RewriteEffects | None = None,
) -> RewriteResult:
    """Seed a new session with the prefix. Does not change the source branch."""
    active = await branches.get_active(session_id)
    source_thread = active.thread_id if active is not None else session_id
    messages = await history.load(source_thread)
    index = resolve_anchor(messages, anchor)
    end = prefix_end_for(messages, index, "fork")
    prefix = list(messages[:end])
    new_session_id = str(uuid.uuid4())
    await history.reset(new_session_id, prefix)
    await _copy_side_effects(effects, source_thread, new_session_id, messages[end:])
    logger.info(
        "history fork %s",
        kv(session_id=session_id, forked_session_id=new_session_id, prefix=end),
    )
    return RewriteResult(
        op="fork",
        branch_id=ROOT_BRANCH_ID,
        thread_id=new_session_id,
        forked_session_id=new_session_id,
    )


async def activate_branch(
    *,
    branches: BranchStore,
    session_id: str,
    branch_id: str,
) -> BranchRecord:
    """Point the session at an existing branch. No-op for an implicit root."""
    if branch_id == ROOT_BRANCH_ID:
        existing = await branches.get(session_id, ROOT_BRANCH_ID)
        if existing is None and not await branches.list(session_id):
            now = 0
            return BranchRecord(
                branch_id=ROOT_BRANCH_ID,
                session_id=session_id,
                thread_id=session_id,
                parent_branch_id=None,
                fork_row_index=None,
                fork_fingerprint=None,
                op=None,
                created_at=now,
                last_active_at=now,
                is_active=True,
            )
    updated = await branches.set_active(session_id, branch_id)
    if updated is None:
        raise HistoryRewriteError(404, "BRANCH_NOT_FOUND", "Unknown branch")
    return updated


def _inherited_limit(
    branch_id: str,
    ancestors: set[str],
    by_id: dict[str, BranchRecord],
) -> int | None:
    """Last row ``branch_id`` inherited from the nearest branch in ``ancestors``.

    Each hop keeps only the rows up to its fork row, so the limit is the
    smallest fork row on the path. ``None`` when no branch in ``ancestors``
    is an ancestor; ``-1`` when a hop forked before any row.
    """
    limit: int | None = None
    seen: set[str] = set()
    current = by_id.get(branch_id)
    while current is not None and current.branch_id not in seen:
        seen.add(current.branch_id)
        parent = current.parent_branch_id
        if parent is None:
            return None
        fork = current.fork_row_index if current.fork_row_index is not None else -1
        limit = fork if limit is None else min(limit, fork)
        if parent in ancestors:
            return limit
        current = by_id.get(parent)
    return None


def _active_option_index(
    options: list[str],
    active_branch_id: str,
    by_id: dict[str, BranchRecord],
) -> int:
    if active_branch_id in options:
        return options.index(active_branch_id)
    seen: set[str] = set()
    current: str | None = active_branch_id
    while current is not None and current not in seen:
        seen.add(current)
        if current in options:
            return options.index(current)
        record = by_id.get(current)
        current = record.parent_branch_id if record is not None else None
    return 0


def divergence_points(
    records: list[BranchRecord],
    active_branch_id: str,
    messages: list[Message],
    *,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Navigator points whose fork row still exists on ``messages``.

    A point whose anchor was compacted away (or sits before a sliced tail
    starting at absolute ``offset``) is omitted.     Options are the parent
    branch plus the children that forked there, and only when the active
    branch is one of them, or a descendant that inherited the fork row.
    """
    by_id = {record.branch_id: record for record in records}
    groups: dict[tuple[str, str], list[BranchRecord]] = {}
    for record in records:
        if record.parent_branch_id is None or record.fork_fingerprint is None:
            continue
        if record.fork_row_index is None:
            continue
        key = (record.parent_branch_id, record.fork_fingerprint)
        groups.setdefault(key, []).append(record)

    points: list[tuple[int, dict[str, Any]]] = []
    for (parent_id, fingerprint), children in groups.items():
        children.sort(key=lambda record: (record.created_at, record.branch_id))
        hint = children[0].fork_row_index
        if hint is None:
            continue
        relevant = {parent_id, *(child.branch_id for child in children)}
        if active_branch_id not in relevant:
            limit = _inherited_limit(active_branch_id, relevant, by_id)
            if limit is None or hint > limit:
                continue
        local = locate_anchor(messages, HistoryAnchor(hint - offset, fingerprint))
        if local is None:
            continue
        index = offset + local
        options = [parent_id, *[child.branch_id for child in children]]
        points.append(
            (
                index,
                {
                    "anchor": {"row_index": index, "fingerprint": fingerprint},
                    "options": options,
                    "active_index": _active_option_index(options, active_branch_id, by_id),
                },
            )
        )
    points.sort(key=lambda item: item[0])
    return [point for _index, point in points]


async def _load_tail(
    history: Any,
    thread_id: str,
    limit: int | None,
) -> tuple[list[Message], int]:
    """Newest ``limit`` rows plus the absolute index of the first one."""
    if limit is None:
        return await history.load(thread_id), 0
    messages = await history.load(thread_id, limit=limit)
    if len(messages) < limit:
        return messages, 0
    tail = await history.last_row(thread_id)
    total = tail[0] if tail is not None else len(messages)
    return messages, max(0, total - len(messages))


async def load_active_history(
    history: Any,
    branches: BranchStore,
    session_id: str,
    *,
    limit: int | None = None,
) -> ActiveHistory:
    """The active branch's transcript (newest ``limit`` rows), plus navigators."""
    active = await branches.get_active(session_id)
    thread_id = active.thread_id if active is not None else session_id
    branch_id = active.branch_id if active is not None else ROOT_BRANCH_ID
    messages, offset = await _load_tail(history, thread_id, limit)
    local_summary = last_summary_index(messages)
    points: list[dict[str, Any]] = []
    if active is not None:
        records = await branches.list(session_id)
        points = divergence_points(records, active.branch_id, messages, offset=offset)
    return ActiveHistory(
        session_id=session_id,
        branch_id=branch_id,
        thread_id=thread_id,
        messages=messages,
        branch_points=points,
        offset=offset,
        summary_index=None if local_summary is None else offset + local_summary,
    )


async def overlay_active_previews(
    history: Any,
    branches: BranchStore,
    summaries: list[ChatThreadSummary],
) -> list[ChatThreadSummary]:
    """Sidebar rows with the preview of each session's active non-root branch.

    One branch query covers every row. A failure keeps the root preview for
    the affected rows instead of hiding them.
    """
    try:
        active_by_session = await branches.active_non_root(
            [summary.thread_id for summary in summaries]
        )
    except Exception:
        logger.warning("active branch preview lookup failed", exc_info=True)
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
                "active branch preview failed %s",
                kv(session_id=summary.thread_id),
                exc_info=True,
            )
            out.append(summary)
            continue
        preview = summary.preview
        count = summary.message_count
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
    """Delete the session transcript, every branch thread with its goal-ledger
    rows, and the branch rows.

    Threads go first so a failure leaves branch rows pointing at whatever
    survived, and a retry can finish the job. The caller must hold the
    session idle, or a running turn could write to a thread after its wipe.
    """
    ledger = effects.ledger if effects is not None else None
    wiped: set[str] = set()

    async def _wipe(thread_id: str) -> None:
        if thread_id in wiped:
            return
        await history.reset(thread_id, [])
        if ledger is not None:
            await ledger.clear_thread(thread_id)
        wiped.add(thread_id)

    await _wipe(session_id)
    for record in await branches.list(session_id):
        await _wipe(record.thread_id)
    for record in await branches.delete_session(session_id):
        await _wipe(record.thread_id)


async def resolve_active_thread_id(backend: Any, session_id: str) -> str:
    """Thread id the next turn should read and write.

    A backend with no ``branches`` attribute stays on ``session_id`` (test
    doubles). Any other branch-store failure propagates so the turn does not
    silently write the root thread. An active branch has ``last_active_at``
    bumped so the sidebar can sort the session.
    """
    try:
        store = backend.branches()
    except AttributeError:
        return session_id
    active = await store.get_active(session_id)
    if active is None:
        return session_id
    thread_id = active.thread_id
    if not isinstance(thread_id, str) or not thread_id:
        return session_id
    await store.touch(session_id, active.branch_id)
    return thread_id
