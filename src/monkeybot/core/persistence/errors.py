"""Typed persistence errors shared across storage backends."""

from __future__ import annotations


class AmbiguousCommitError(Exception):
    """Commit acknowledgement was lost; a retry with the same ``message_id`` is safe."""
