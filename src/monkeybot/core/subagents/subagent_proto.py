"""Subagent subprocess protocol: JSON envelope and NDJSON event streaming."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.runtime.events import (
    AgentEvent,
    Error,
    EventDecodeError,
    event_from_json,
    event_to_json,
)


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
    raw = os.environ.get("MONKEYBOT_AGENT_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_project_path(raw: str, agent_root: Path | None = None) -> Path:
    """Resolve a config path relative to ``agent_root`` (default: env or cwd)."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    p = Path(raw).expanduser()
    if p.is_absolute():
        return p.resolve()
    return (root / p).resolve()


def resolve_subagent_agent_md_path(agent_root: Path | None = None) -> Path | None:
    """Effective subagent AGENT.md path; ``MONKEYBOT_SUBAGENT_AGENT_MD`` wins over ``AGENT_MD``."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    raw = os.environ.get("MONKEYBOT_SUBAGENT_AGENT_MD", "").strip()
    if not raw:
        raw = os.environ.get("AGENT_MD", "").strip()
    if not raw:
        return None
    return resolve_project_path(raw, root)


def resolve_task_agent_md_path(
    *,
    subagent_type: str | None,
    registry: dict[str, SubagentConfig],
    agent_root: Path | None = None,
) -> Path:
    """Resolve AGENT.md for a ``task`` spawn: registry type, then global subagent/parent defaults."""
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

    fallback = resolve_subagent_agent_md_path(root)
    if fallback is not None and fallback.is_file():
        return fallback
    raw = os.environ.get("AGENT_MD", "").strip()
    if raw:
        path = resolve_project_path(raw, root)
        if path.is_file():
            return path
    default = (root / "AGENT.md").resolve()
    if default.is_file():
        return default
    raise ValueError("No AGENT.md found for subagent (set subagent.agent_md or paths.agent_md)")


def normalize_sqlite_db_url(db_url: str, agent_root: Path | None = None) -> str:
    """Rewrite relative ``sqlite:///`` paths against project root (not workspace cwd)."""
    root = agent_root if agent_root is not None else resolve_agent_project_root()
    stripped = db_url.strip()
    prefix = "sqlite:///"
    if not stripped.lower().startswith(prefix):
        return db_url
    remainder = stripped[len(prefix) :]
    if not remainder or remainder == ":memory:":
        return db_url
    p = Path(remainder)
    if p.is_absolute():
        return db_url
    abs_path = (root / p).resolve()
    return f"sqlite:///{abs_path}"


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
        yield Error(request_id="", error="subagent: subprocess missing stdin/stdout pipes")
        await proc.wait()
        return

    stdin.write(envelope.to_json().encode("utf-8"))
    await stdin.drain()
    stdin.close()

    last_evt: AgentEvent | None = None

    while True:
        line_b = await stdout.readline()
        if not line_b:
            break
        raw_line = line_b.decode("utf-8").rstrip("\r\n")
        _append_progress_line(progress_path, raw_line)
        if not raw_line.strip():
            continue
        try:
            evt = event_from_json(raw_line)
        except EventDecodeError as exc:
            yield Error(request_id="", error=f"NDJSON parse error: {exc}")
            continue

        last_evt = evt
        if on_event is not None:
            await on_event(evt)
        yield evt

    code = await proc.wait()
    if code != 0:
        yield Error(request_id="", error=f"subagent process exited with code {code}")

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
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


def _append_progress_line(progress_path: Path, line: str) -> None:
    with progress_path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
