"""Request-scoped provider/model binding for verifier classifier and judge jobs."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

from monkeybot.core.config.settings import effective_verifier_model
from monkeybot.core.llm.provider import Provider

ProviderRef = Provider | Callable[[], Provider | None] | None
ModelRef = str | Callable[..., str]


@dataclass(frozen=True)
class VerifierBinding:
    """Snapshot of the originating session's provider and model."""

    provider: Provider | None = None
    model: str = ""


_current_binding: ContextVar[VerifierBinding | None] = ContextVar(
    "monkeybot_verifier_binding",
    default=None,
)


def bind_verifier_session(
    provider: Provider | None, model: str | None
) -> Token[VerifierBinding | None]:
    """Pin verifier jobs to the originating session until ``reset_verifier_session``."""
    return _current_binding.set(VerifierBinding(provider=provider, model=(model or "").strip()))


def reset_verifier_session(token: Token[VerifierBinding | None]) -> None:
    _current_binding.reset(token)


def current_verifier_binding() -> VerifierBinding:
    return _current_binding.get() or VerifierBinding()


def resolve_live_provider(provider: ProviderRef) -> Provider | None:
    if callable(provider):
        return provider()
    return provider


def resolve_live_model(model: ModelRef, session_model: str = "") -> str:
    """Constructor model (YAML/static/callable), else the originating session model."""
    if callable(model):
        try:
            resolved = model(session_model)
        except TypeError:
            resolved = model()
        return (resolved or "").strip()
    static = (model or "").strip()
    return static or session_model.strip()


def resolve_session_verifier_model(
    cfg: Any | None,
    override: str | None,
    *,
    session_model: str | None = None,
) -> str:
    """Explicit YAML override, else session model, else the pinned agent model.

    ``session_model=None`` (default) reads the bound ContextVar. Pass a string
    — including ``""`` — to use the job snapshot instead of the current task.
    """
    if override and override.strip():
        return override.strip()
    session = current_verifier_binding().model if session_model is None else session_model
    session = (session or "").strip()
    if session:
        return session
    return effective_verifier_model(cfg, None)
