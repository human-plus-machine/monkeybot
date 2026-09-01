"""Subagent subprocess protocol: JSON envelope and NDJSON event streaming."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.config.snapshot import current_env
from monkeybot.core.layout import (
    resolve_agent_path,
    resolve_agent_root,
    resolve_config_path,
    resolve_sqlite_url,
)
from monkeybot.core.logging_utils import kv
from monkeybot.core.runtime.events import (
    AgentEvent,
    Error,
    EventDecodeError,
    event_from_json,
    event_to_json,
)

logger = logging.getLogger(__name__)

# asyncio StreamReader defaults to 64 KiB per line. Subagents emit full
# SystemPromptSnapshot NDJSON (AGENT.md + harness + memory INDEX.md) which
# routinely exceeds that and used to fail with:
#   "Separator is not found, and chunk exceed the limit"
# Keep headroom for large tool results / snapshots on the parent←child pipe.
SUBAGENT_STDOUT_LINE_LIMIT = 16 * 1024 * 1024  # 16 MiB

# Prefix of the ``Error`` emitted when the child process itself dies. The parent
# task tool matches on it to report ``exit_reason="crashed"`` — a contract, not
# a log line.
SUBAGENT_EXIT_ERROR_PREFIX = "subagent process exited with code"


def _memory_storage_uri_from_dict(decoded: dict[str, Any]) -> str:
    """Resolve memory URI from envelope fields; empty string means no memory."""
    if "memory_storage_uri" in decoded:
        uri_val = decoded["memory_storage_uri"]
        if not isinstance(uri_val, str):
            raise ValueError("envelope: 'memory_storage_uri' must be a string")
        return uri_val.strip()
    legacy = decoded.get("memory_path")
    if isinstance(legacy, str) and legacy.strip():
        lp = legacy.strip()
        if lp.startswith("gcs://") or lp.startswith("s3://") or lp.startswith("local://"):
            return lp
        return "local://" + lp
    return ""


def default_subagent_script() -> Path:
    """Bundled ``subagent_worker.py`` next to this module."""
    return Path(__file__).resolve().parent / "subagent_worker.py"


def resolve_subagent_script() -> Path:
    """Resolve worker script from ``MONKEYBOT_SUBAGENT_SCRIPT`` or the bundled default."""
    return Path(
        os.environ.get(
            "MONKEYBOT_SUBAGENT_SCRIPT",
            str(default_subagent_script()),
        )
    ).resolve()


def resolve_agent_project_root() -> Path:
    """Bot project root (config, AGENT.md, data/) — not the workspace file-tool sandbox."""
    return resolve_agent_root()


def resolve_project_path(raw: str, agent_root: Path | None = None) -> Path:
    """Resolve a config path relative to ``agent_root`` (default: env or cwd)."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    return resolve_agent_path(raw, root)


def config_path_for_agent_root(agent_root: Path | None = None) -> str | None:
    """Resolve ``monkeybot.yaml`` for ``agent_root`` without consulting cwd."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    cfg = resolve_config_path(agent_root=root)
    return str(cfg) if cfg is not None else None


def resolve_default_agent_md_path(agent_root: Path | None = None) -> Path:
    """Resolve default AGENT.md for a task with no persona: env ``AGENT_MD`` → ``AGENT.md``.

    Returns the first existing file. Raises if none of the candidates exist.
    """
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    raw = current_env("AGENT_MD", "").strip()
    if raw:
        path = resolve_project_path(raw, root)
        if path.is_file():
            return path
    default = resolve_project_path("AGENT.md", root)
    if default.is_file():
        return default
    raise ValueError("No AGENT.md found for subagent (set paths.agent_md or a persona agent_md)")


def resolve_task_agent_md_path(
    *,
    subagent_type: str | None,
    registry: dict[str, SubagentConfig],
    agent_root: Path | None = None,
) -> Path:
    """Resolve AGENT.md for a ``task`` spawn: registry persona, else parent defaults."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    key = (subagent_type or "").strip()
    if key:
        cfg = registry.get(key)
        if cfg is None:
            known = ", ".join(sorted(registry)) if registry else "(none configured)"
            raise ValueError(f"Unknown subagent_type {key!r}. Configured types: {known}")
        if not cfg.agent_md:
            raise ValueError(f"subagent_type {key!r} has no agent_md in monkeybot.yaml")
        path = resolve_project_path(cfg.agent_md, root)
        if not path.is_file():
            raise ValueError(f"agent_md for subagent_type {key!r} not found: {path}")
        return path

    return resolve_default_agent_md_path(root)


def normalize_sqlite_db_url(db_url: str, agent_root: Path | None = None) -> str:
    """Compatibility wrapper for the central layout resolver."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    return resolve_sqlite_url(db_url, root)


@dataclass(frozen=True)
class SubagentEnvelope:
    """Inputs forwarded to a child Python worker via stdin JSON."""

    task: str
    context: str
    memory_storage_uri: str
    parent_run_id: str
    model: str = "gemini-2.5-flash"
    traceparent: str | None = None
    agent_md: str | None = None
    subagent_type: str | None = None
    parent_session_id: str | None = None
    child_thread_id: str | None = None

    def to_json(self) -> str:
        """Serialize to a compact JSON object for stdin (UTF-8)."""
        payload: dict[str, str] = {
            "task": self.task,
            "context": self.context,
            "memory_storage_uri": self.memory_storage_uri,
            "parent_run_id": self.parent_run_id,
            "model": self.model,
        }
        if self.traceparent is not None:
            payload["traceparent"] = self.traceparent
        if self.agent_md is not None:
            payload["agent_md"] = self.agent_md
        if self.subagent_type is not None:
            payload["subagent_type"] = self.subagent_type
        if self.parent_session_id is not None:
            payload["parent_session_id"] = self.parent_session_id
        if self.child_thread_id is not None:
            payload["child_thread_id"] = self.child_thread_id
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> SubagentEnvelope:
        """Parse and validate envelope JSON."""
        try:
            decoded: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("envelope: invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("envelope: root must be an object")
        uri = _memory_storage_uri_from_dict(decoded)
        return cls(
            task=_req_str(decoded, "task"),
            context=_req_str(decoded, "context"),
            memory_storage_uri=uri,
            parent_run_id=_req_str(decoded, "parent_run_id"),
            model=_opt_model(decoded),
            traceparent=_opt_traceparent(decoded),
            agent_md=_opt_str_field(decoded, "agent_md"),
            subagent_type=_opt_str_field(decoded, "subagent_type"),
            parent_session_id=_opt_str_field(decoded, "parent_session_id"),
            child_thread_id=_opt_str_field(decoded, "child_thread_id"),
        )


def _req_str(data: dict[str, Any], key: str) -> str:
    val = data.get(key)
    if not isinstance(val, str):
        raise ValueError(f"envelope: {key!r} must be a string")
    return val


def _opt_traceparent(data: dict[str, Any]) -> str | None:
    if "traceparent" not in data:
        return None
    val = data.get("traceparent")
    if not isinstance(val, str):
        raise ValueError("envelope: 'traceparent' must be a string")
    stripped = val.strip()
    return stripped or None


def _opt_str_field(data: dict[str, Any], key: str) -> str | None:
    if key not in data:
        return None
    val = data.get(key)
    if not isinstance(val, str):
        raise ValueError(f"envelope: {key!r} must be a string")
    stripped = val.strip()
    return stripped or None


def _opt_model(data: dict[str, Any]) -> str:
    if "model" not in data:
        return "gemini-2.5-flash"
    val = data.get("model")
    if not isinstance(val, str) or not val:
        raise ValueError("envelope: 'model' must be a non-empty string")
    return val


async def spawn_subagent(
    script: str,
    envelope: SubagentEnvelope,
    *,
    scratch_dir: Path,
    on_event: Callable[[AgentEvent], Awaitable[None]] | None = None,
    subprocess_exec: Callable[..., Awaitable[asyncio.subprocess.Process]] | None = None,
    extra_env: dict[str, str] | None = None,
) -> AsyncIterator[AgentEvent]:
    """Run ``script`` under ``python -u``, stream NDJSON stdout as ``AgentEvent`` values.

    Stdout lines are UTF-8 NDJSON. Each line is appended to ``progress.jsonl`` under
    ``scratch_dir``. Parse failures yield :class:`Error` and continue.

    The stdout ``StreamReader`` uses :data:`SUBAGENT_STDOUT_LINE_LIMIT` (not asyncio's
    default 64 KiB) so large NDJSON events (e.g. ``SystemPromptSnapshot``) do not fail
    with ``Separator is not found, and chunk exceed the limit``.

    After the process exits with code 0, writes ``output.json`` with
    ``event_to_json`` of the last successfully parsed event.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    progress_path = scratch_dir / "progress.jsonl"

    if subprocess_exec is not None:
        exec_fn = subprocess_exec
    else:
        merged_env = dict(extra_env) if extra_env else None

        async def exec_fn(*cmd: str | bytes) -> asyncio.subprocess.Process:
            return await _default_subprocess_exec(*cmd, extra_env=merged_env)

    proc = await exec_fn(sys.executable, "-u", script)

    stdin = proc.stdin
    stdout = proc.stdout
    if stdin is None or stdout is None:
        logger.warning(
            "subagent subprocess missing pipes %s",
            kv(script=script, parent_run_id=envelope.parent_run_id),
        )
        yield Error(request_id="", error="subagent: subprocess missing stdin/stdout pipes")
        await proc.wait()
        return

    stdin.write(envelope.to_json().encode("utf-8"))
    await stdin.drain()
    stdin.close()

    last_evt: AgentEvent | None = None

    while True:
        try:
            line_b = await stdout.readline()
        except ValueError as exc:
            # LimitOverrunError is surfaced as ValueError with this message when a
            # single NDJSON line has no newline within the StreamReader limit.
            msg = str(exc)
            logger.warning(
                "subagent NDJSON line exceeded pipe limit %s",
                kv(
                    script=script,
                    parent_run_id=envelope.parent_run_id,
                    limit=SUBAGENT_STDOUT_LINE_LIMIT,
                    error=msg,
                ),
            )
            yield Error(
                request_id="",
                error=(
                    f"subagent NDJSON line exceeded StreamReader limit "
                    f"({SUBAGENT_STDOUT_LINE_LIMIT} bytes): {msg}"
                ),
            )
            if proc.returncode is None:
                proc.kill()
            break
        if not line_b:
            break
        raw_line = line_b.decode("utf-8").rstrip("\r\n")
        _append_progress_line(progress_path, raw_line)
        if not raw_line.strip():
            continue
        try:
            evt = event_from_json(raw_line)
        except EventDecodeError as exc:
            logger.warning(
                "subagent NDJSON parse error %s",
                kv(script=script, parent_run_id=envelope.parent_run_id, error=exc),
            )
            yield Error(request_id="", error=f"NDJSON parse error: {exc}")
            continue

        last_evt = evt
        if on_event is not None:
            await on_event(evt)
        yield evt

    code = await proc.wait()
    if code != 0:
        logger.warning(
            "subagent process exited nonzero %s",
            kv(script=script, parent_run_id=envelope.parent_run_id, exit_code=code),
        )
        yield Error(request_id="", error=f"{SUBAGENT_EXIT_ERROR_PREFIX} {code}")

    if code == 0 and last_evt is not None:
        out_path = scratch_dir / "output.json"
        out_path.write_text(event_to_json(last_evt) + "\n", encoding="utf-8")


async def _default_subprocess_exec(
    *cmd: str | bytes,
    extra_env: dict[str, str] | None = None,
) -> asyncio.subprocess.Process:
    """Spawn with ``PYTHONUNBUFFERED=1``; CLI also uses ``python -u``."""
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env["PYTHONUNBUFFERED"] = "1"
    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
        limit=SUBAGENT_STDOUT_LINE_LIMIT,
    )


def _append_progress_line(progress_path: Path, line: str) -> None:
    with progress_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
