"""Preflight for every live run (5.11): the key is there, then one cheap ``stream`` call works.

Runs before the first scenario of a live run, never at collection. The key comes from the
process environment or, failing that, the repo-root ``.env``. On a missing key it returns the
exact message below and writes one ``run_error`` record to the failure ledger, so the run file
shows the run never started. It never prints, copies, or stores the key.

``run_preflight`` is what the CLI calls; ``run_preflight_once`` is the same check, memoised,
for the pytest ``--live`` path (call it from the first T4 item's setup).
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

import yaml

from testkit.failure_ledger import LedgerWriter
from testkit.failures.store import append_jsonl
from testkit.scrub import scrub

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_ROOT = Path(__file__).resolve().parent / "agent"
AGENT_CONFIG = AGENT_ROOT / "monkeybot_config" / "monkeybot.yaml"
# Live secrets come from the repo-root .env (gitignored), not from the agent project.
ENV_FILE = REPO_ROOT / ".env"

KEY_VAR = "OLLAMA_API_KEY"
MISSING_KEY_MESSAGE = (
    "OLLAMA_API_KEY is not set (set it in the environment or in the repo-root .env)"
)
PROVIDER = "ollama-cloud"
DEFAULT_MODEL = "glm-5.3-flash"
STREAM_TIMEOUT_SEC = 60.0
_PROBE_PROMPT = "Reply with the single word: ok"
_MAX_PROBE_TOKENS = 16
_MAX_ERROR_CHARS = 500


class PreflightError(RuntimeError):
    """The live run must not start. ``str(exc)`` is the message to print."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


class _StreamingProvider(Protocol):
    def stream(
        self, messages: Sequence[Any], tools: Sequence[Any], *, model: str
    ) -> AsyncIterator[Any]: ...


ProviderFactory = Callable[[], _StreamingProvider]


def agent_model(config_path: Path | None = None) -> tuple[str, str]:
    """``(provider, model)`` from the agent's ``monkeybot.yaml`` (``MONKEYBOT_AGENT_CONFIG`` wins)."""
    raw = os.environ.get("MONKEYBOT_AGENT_CONFIG", "").strip()
    path = Path(raw) if raw else (config_path or AGENT_CONFIG)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return PROVIDER, DEFAULT_MODEL
    section = data.get("model") if isinstance(data, dict) else None
    section = section if isinstance(section, dict) else {}
    provider = str(section.get("provider") or PROVIDER).strip().lower()
    model = str(section.get("name") or DEFAULT_MODEL).strip()
    return provider, model


def _key_from_file(path: Path) -> str:
    """The key's value in ``path``, matching the name case-insensitively (``ollama_api_key``).

    An exact-case ``OLLAMA_API_KEY`` line wins over other spellings. Blank means not found.
    """
    if not path.is_file():
        return ""
    from dotenv import dotenv_values

    values = dotenv_values(path)
    exact = (values.get(KEY_VAR) or "").strip()
    if exact:
        return exact
    for name, value in values.items():
        if name.strip().upper() == KEY_VAR and (value or "").strip():
            return (value or "").strip()
    return ""


def ensure_key(env_file: Path | None = None) -> bool:
    """True when ``OLLAMA_API_KEY`` is in the process environment or in ``env_file``.

    ``env_file`` defaults to the repo-root ``.env``. The process environment wins. A key found
    only in the file is exported as ``OLLAMA_API_KEY`` for this process, so the stream call
    below and the in-process judge can use it. Nothing else from the file is read into the
    environment.
    """
    if (os.environ.get(KEY_VAR) or "").strip():
        return True
    value = _key_from_file(ENV_FILE if env_file is None else env_file)
    if not value:
        return False
    os.environ[KEY_VAR] = value
    return True


def _default_provider() -> _StreamingProvider:
    from monkeybot.providers.ollama import OllamaProvider

    return OllamaProvider(mode="cloud", max_tokens=_MAX_PROBE_TOKENS)


async def _probe(provider: _StreamingProvider, model: str) -> None:
    from monkeybot.core.llm.provider import Done, Message

    saw_done = False
    messages = [Message.text("user", _PROBE_PROMPT)]
    async for event in provider.stream(messages, [], model=model):
        if isinstance(event, Done):
            saw_done = True
    if not saw_done:
        raise RuntimeError("the stream ended without a Done event")


def _error_text(exc: BaseException) -> str:
    text = scrub(f"{type(exc).__name__}: {exc}".strip())
    return text if len(text) <= _MAX_ERROR_CHARS else text[:_MAX_ERROR_CHARS] + "..."


def _write_run_error(
    *,
    reason: str,
    message: str,
    suite: str,
    agent_url: str,
    provider: str,
    model: str,
    ledger_dir: Path | None,
    argv: list[str] | None,
) -> str:
    """Write a run file holding one ``run_error`` record. Returns its repo-relative path."""
    live = {"provider": provider, "model": model, "agent_url": agent_url, "suite": suite}
    writer = LedgerWriter(ledger_dir)
    writer.start(argv=argv if argv is not None else sys.argv, live=live)
    assert writer.path is not None
    append_jsonl(
        writer.path,
        {
            "kind": "run_error",
            "run_id": writer.run_id,
            "tier": "t4",
            "reason": reason,
            "message": scrub(message),
            "live": live,
            "repro": f"uv run python -m regression.t4 --suite {suite}",
        },
    )
    writer.finish(
        exit_status=2,
        counts={
            "passed": 0,
            "failed": 0,
            "errors": 1,
            "skipped": 0,
            "xfailed": 0,
            "xpassed_strict": 0,
        },
        outcomes=[],
    )
    return writer.shown_path


def run_preflight(
    *,
    suite: str,
    agent_url: str,
    ledger_dir: Path | None = None,
    ledger: bool = True,
    env_file: Path | None = None,
    config_path: Path | None = None,
    provider_factory: ProviderFactory | None = None,
    timeout_sec: float = STREAM_TIMEOUT_SEC,
    argv: list[str] | None = None,
) -> None:
    """Check the key and make one cheap ``stream`` call; raise :class:`PreflightError` on failure.

    A failure also writes one ``run_error`` ledger record (unless ``ledger`` is false), and the
    error message then ends with the run file's path. The key is never part of any message.
    """
    provider, model = agent_model(config_path)

    def fail(reason: str, message: str) -> PreflightError:
        if ledger:
            try:
                shown = _write_run_error(
                    reason=reason,
                    message=message,
                    suite=suite,
                    agent_url=agent_url,
                    provider=provider,
                    model=model,
                    ledger_dir=ledger_dir,
                    argv=argv,
                )
            except Exception as exc:  # the ledger is a side channel; never mask the failure
                message = f"{message}\n(ledger not written: {type(exc).__name__})"
            else:
                message = f"{message}\nrun_error recorded to {shown}"
        return PreflightError(message, reason=reason)

    if not ensure_key(env_file):
        raise fail("missing_key", MISSING_KEY_MESSAGE)
    if provider != PROVIDER and provider_factory is None:
        raise fail(
            "unsupported_provider",
            f"the agent config names model.provider {provider!r}; live runs use {PROVIDER}",
        )

    factory = provider_factory or _default_provider

    async def check() -> None:
        await asyncio.wait_for(_probe(factory(), model), timeout_sec)

    try:
        asyncio.run(check())
    except Exception as exc:
        text = _error_text(exc)
        raise fail(
            "stream_failed",
            f"preflight stream call to {provider}/{model} failed: {text}",
        ) from None


_passed: set[tuple[str, str]] = set()


def run_preflight_once(**kwargs: Any) -> None:
    """:func:`run_preflight`, skipped once it has passed for this suite and agent URL."""
    key = (str(kwargs.get("suite", "")), str(kwargs.get("agent_url", "")))
    if key in _passed:
        return
    run_preflight(**kwargs)
    _passed.add(key)
