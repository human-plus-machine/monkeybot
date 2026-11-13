"""Firestore collection paths for flat vs nested ``layout`` DB URLs."""

from __future__ import annotations

from typing import Any, Literal, cast

FirestoreLayout = Literal["flat", "nested"]
_VALID_LAYOUTS = frozenset({"flat", "nested"})

_MONKEYBOT_DOC = "monkeybot"


def parse_firestore_layout(raw: str) -> FirestoreLayout:
    """Normalize ``layout`` from a DB URL query string."""
    value = (raw or "flat").strip().lower()
    if value not in _VALID_LAYOUTS:
        raise ValueError(f"invalid Firestore layout {raw!r} (expected flat or nested)")
    return cast(FirestoreLayout, value)


def flat_collection_id(prefix: str, base: str) -> str:
    """Collection id under the default (flat) layout — stable for tests and flat callers."""
    if base == "session_turn_locks":
        if not prefix:
            return "session_turn_locks"
        return f"{prefix}_session_turn_locks"
    if not prefix:
        return base
    return f"{prefix}_{base}"


def flat_scheduled_loops_goal_sessions_id(prefix: str) -> str:
    return f"{flat_collection_id(prefix, 'scheduled_loops')}_goal_sessions"


def firestore_monkeybot_collection(
    client: Any,
    *,
    prefix: str,
    layout: FirestoreLayout,
    base: str,
) -> Any:
    """Resolve a store collection reference for ``layout``."""
    if layout == "nested":
        parent = prefix.strip() or "_default"
        return client.collection(parent).document(_MONKEYBOT_DOC).collection(base)
    return client.collection(flat_collection_id(prefix, base))


def firestore_scheduled_loops_goal_sessions(
    client: Any,
    *,
    prefix: str,
    layout: FirestoreLayout,
) -> Any:
    if layout == "nested":
        parent = prefix.strip() or "_default"
        return (
            client.collection(parent)
            .document(_MONKEYBOT_DOC)
            .collection("scheduled_loops_goal_sessions")
        )
    return client.collection(flat_scheduled_loops_goal_sessions_id(prefix))


__all__ = [
    "FirestoreLayout",
    "flat_collection_id",
    "flat_scheduled_loops_goal_sessions_id",
    "firestore_monkeybot_collection",
    "firestore_scheduled_loops_goal_sessions",
    "parse_firestore_layout",
]
