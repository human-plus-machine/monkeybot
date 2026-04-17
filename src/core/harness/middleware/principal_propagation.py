"""PrincipalPropagationMW — carries principal + run_id through the pipeline via ContextVars.

Because LangGraph state and middleware composition varies across deep agents versions,
we do not try to inject into state dicts directly. Instead the assembler uses these
ContextVars so every event emission has a valid principal/run_id/session_id.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

from ..events import Principal

_PRINCIPAL: ContextVar[Principal] = ContextVar("harness_principal", default=Principal())
_RUN_ID: ContextVar[str] = ContextVar("harness_run_id", default="unset")
_SESSION_ID: ContextVar[str] = ContextVar("harness_session_id", default="unset")


class PrincipalPropagationMW:
    name = "PrincipalPropagationMW"

    def __init__(self, *, principal_required: bool = True) -> None:
        self.principal_required = principal_required

    @contextmanager
    def scope(
        self, *, principal: Principal | None, run_id: str, session_id: str
    ) -> Iterator[Principal]:
        from ..errors import HarnessConfigError

        p = principal or Principal()
        if self.principal_required and p.kind == "anonymous" and p.id == "anonymous":
            raise HarnessConfigError(
                "SecuritySpec.principal_required=True but anonymous principal supplied"
            )
        p_tok = _PRINCIPAL.set(p)
        r_tok = _RUN_ID.set(run_id)
        s_tok = _SESSION_ID.set(session_id)
        try:
            yield p
        finally:
            _PRINCIPAL.reset(p_tok)
            _RUN_ID.reset(r_tok)
            _SESSION_ID.reset(s_tok)


def current_principal() -> Principal:
    return _PRINCIPAL.get()


def current_run_id() -> str:
    return _RUN_ID.get()


def current_session_id() -> str:
    return _SESSION_ID.get()
