"""Trivial echo skill used by the Story 8 smoke tests.

A smoke skill must have no network or disk side-effects so it can exercise
the tool-invocation code path without dragging AWS credentials into unit
tests. :func:`echo` simply returns its input verbatim.
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def echo(message: str) -> str:
    """Return ``message`` unchanged.

    Used by the AWS enterprise reference stack's smoke tests to verify
    tool wiring without touching any external service.
    """
    return message


__all__ = ["echo"]
