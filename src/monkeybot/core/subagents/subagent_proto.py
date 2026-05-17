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

from monkeybot.core.runtime.events import AgentEvent, Error, EventDecodeError, event_from_json, event_to_json


@dataclass(frozen=True)
class SubagentEnvelope:
    """Inputs forwarded to a child Python worker via stdin JSON."""

    task: str
    context: str
    memory_storage_uri: str
    parent_run_id: str
    model: str = "gemini-2.5-flash"

    def to_json(self) -> str:
        """Serialize to a compact JSON object for stdin (UTF-8)."""
        payload = {
            "task": self.task,
            "context": self.context,
            "memory_storage_uri": self.memory_storage_uri,
            "parent_run_id": self.parent_run_id,
            "model": self.model,
        }
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
        uri = decoded.get("memory_storage_uri")
        if not isinstance(uri, str) or not uri.strip():
            legacy = decoded.get("memory_path")
            if isinstance(legacy, str) and legacy.strip():
                lp = legacy.strip()
                if lp.startswith("gcs://") or lp.startswith("s3://") or lp.startswith("local://"):
                    uri = lp
                else:
                    uri = "local://" + lp
            else:
                raise ValueError("envelope: 'memory_storage_uri' must be a non-empty string")
        return cls(
            task=_req_str(decoded, "task"),
            context=_req_str(decoded, "context"),
            memory_storage_uri=uri.strip(),
            parent_run_id=_req_str(decoded, "parent_run_id"),
            model=_opt_model(decoded),
        )


def _req_str(data: dict[str, Any], key: str) -> str:
    val = data.get(key)
    if not isinstance(val, str):
        raise ValueError(f"envelope: {key!r} must be a string")
    return val


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
) -> AsyncIterator[AgentEvent]:
    """Run ``script`` under ``python -u``, stream NDJSON stdout as ``AgentEvent`` values.

    Stdout lines are UTF-8 NDJSON. Each line is appended to ``progress.jsonl`` under
    ``scratch_dir``. Parse failures yield :class:`Error` and continue.

    After the process exits with code 0, writes ``output.json`` with
    ``event_to_json`` of the last successfully parsed event.
    """
    scratch_dir.mkdir(parents=True, exist_ok=True)
    progress_path = scratch_dir / "progress.jsonl"

    exec_fn = subprocess_exec or _default_subprocess_exec
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
) -> asyncio.subprocess.Process:
    """Spawn with ``PYTHONUNBUFFERED=1``; CLI also uses ``python -u``."""
    env = dict(os.environ)
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
