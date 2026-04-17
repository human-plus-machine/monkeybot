"""Structured subagent return values — never leak raw tracebacks to the parent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class SubagentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    ok: bool
    value: Any | None = None
    error: str | None = None
    error_kind: (
        Literal["timeout", "rule_veto", "sandbox_denied", "recursion", "approval_denied", "unexpected"] | None
    ) = None
    depth: int = 0
    parent_run_id: str | None = None
