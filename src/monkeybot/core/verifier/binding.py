"""Request-scoped provider/model binding for verifier classifier and judge jobs."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from monkeybot.core.config.settings import effective_verifier_model


@dataclass(frozen=True)
class VerifierBinding:
    """Snapshot of the originating session's provider and model."""

    provider: Any | None = None
    model: str = ""


_current_binding: ContextVar[VerifierBinding | None] = ContextVar(
    "monkeybot_verifier_binding",
    default=None,
)


def bind_verifier_session(provider: Any | None, model: str | None) -> Token[VerifierBinding | None]:
    """Pin verifier jobs to the originating session until ``reset_verifier_session``."""
    return _current_binding.set(VerifierBinding(provider=provider, model=(model or "").strip()))


def reset_verifier_session(token: Token[VerifierBinding | None]) -> None:
    _current_binding.reset(token)


def current_verifier_binding() -> VerifierBinding:
    return _current_binding.get() or VerifierBinding()


def resolve_session_verifier_model(cfg: Any | None, override: str | None) -> str:
    """Explicit YAML override, else the bound session model, else the pinned agent model."""
    if override and override.strip():
        return override.strip()
    session = current_verifier_binding().model
    if session:
        return session
    return effective_verifier_model(cfg, None)
