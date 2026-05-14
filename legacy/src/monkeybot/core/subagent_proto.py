from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
from asyncio import subprocess as asyncio_subprocess
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

import ulid

from monkeybot.core.events import (
    AgentEvent,
    ErrorEvent,
    SubagentCompleted,
    SubagentStarted,
    TurnComplete,
    event_from_json,
    event_to_json,
)
from monkeybot.core.runs import create_scratch_dir

log = logging.getLogger("monkeybot.subagent")


@dataclass(frozen=True)
class SubagentDefinition:
    """A registered named subagent. Returned by SubagentRegistry.resolve()."""

    name: str
    script: str
    description: str
    skills_path: str
    model: str
    timeout_seconds: int


@dataclass
class SubagentEnvelope:
    """Payload written to child stdin as one JSON line."""

    run_id: str
    parent_run_id: str | None
    agent_name: str | None
    task: str
    context: dict[str, Any]
    skills_path: str
    model: str
    scratch_dir: str


def read_envelope_from_stdin() -> SubagentEnvelope:
    """Read and deserialize one JSON line from sys.stdin.

    Called at the top of every subagent script.

    Raises:
        ValueError: If stdin is empty or JSON is malformed.
    """
    line = sys.stdin.readline()
    if not line:
        raise ValueError("stdin is empty — no SubagentEnvelope received")
    data = json.loads(line)
    return SubagentEnvelope(**data)


def emit_event(event: AgentEvent) -> None:
    """Write event as a JSON line to sys.stdout and flush.

    Called by child scripts to stream events back to the parent.
    stdout is exclusively for event lines — use stderr for debug output.
    """
    sys.stdout.write(event_to_json(event) + "\n")
    sys.stdout.flush()


async def spawn_subagent(
    definition_or_script: SubagentDefinition | str,
    task: str,
    context: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
    timeout_seconds: int = 300,
) -> AsyncGenerator[AgentEvent, None]:
    """Spawn a subagent subprocess and yield its AgentEvent stream.

    Accepts either a SubagentDefinition (named, from registry) or a raw
    script path string (ad-hoc). Yields SubagentStarted, forwarded events
    from child stdout, and SubagentCompleted when done.

    Args:
        definition_or_script: A SubagentDefinition or path to a Python script.
        task: The task description sent to the subagent.
        context: Optional key/value context dict for the subagent.
        parent_run_id: Optional run ID of the spawning agent.
        timeout_seconds: Per-line read timeout in seconds (overridden by definition).

    Yields:
        AgentEvent instances from SubagentStarted through SubagentCompleted.
    """
    if isinstance(definition_or_script, SubagentDefinition):
        script = definition_or_script.script
        effective_timeout = definition_or_script.timeout_seconds
        agent_name: str | None = definition_or_script.name
        skills_path = definition_or_script.skills_path
        model = definition_or_script.model
    else:
        script = definition_or_script
        effective_timeout = timeout_seconds
        agent_name = None
        skills_path = ""
        model = ""

    run_id = str(ulid.new())
    scratch_dir = create_scratch_dir(run_id)

    envelope = SubagentEnvelope(
        run_id=run_id,
        parent_run_id=parent_run_id,
        agent_name=agent_name,
        task=task,
        context=context or {},
        skills_path=skills_path,
        model=model,
        scratch_dir=scratch_dir,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            script,
            stdin=asyncio_subprocess.PIPE,
            stdout=asyncio_subprocess.PIPE,
            stderr=asyncio_subprocess.PIPE,
        )
    except FileNotFoundError:
        yield ErrorEvent(message=f"Script not found: {script}", recoverable=False)
        return

    assert proc.stdin is not None
    assert proc.stdout is not None
    assert proc.stderr is not None

    envelope_line = json.dumps(dataclasses.asdict(envelope)) + "\n"
    proc.stdin.write(envelope_line.encode())
    await proc.stdin.drain()
    proc.stdin.close()

    yield SubagentStarted(run_id=run_id, script=script, parent_run_id=parent_run_id)

    turn_complete_received = False
    while True:
        try:
            raw = await asyncio.wait_for(
                proc.stdout.readline(), timeout=float(effective_timeout)
            )
        except TimeoutError:
            proc.terminate()
            yield ErrorEvent(message="Subagent timeout", recoverable=True)
            break

        if not raw:
            break

        line = raw.decode().strip()
        if not line:
            continue

        try:
            event = event_from_json(line)
        except (ValueError, json.JSONDecodeError):
            log.warning("subagent malformed line: %r", line)
            yield ErrorEvent(
                message=f"Malformed event from subagent: {line[:80]}", recoverable=True
            )
            continue

        yield event
        if isinstance(event, TurnComplete):
            turn_complete_received = True
            break

    stderr_bytes = await proc.stderr.read()
    if stderr_bytes:
        for stderr_line in stderr_bytes.decode().splitlines():
            log.debug("subagent stderr: %s", stderr_line)

    await proc.wait()

    if proc.returncode != 0 and not turn_complete_received:
        yield ErrorEvent(
            message=f"Subagent exited with code {proc.returncode}",
            recoverable=True,
        )

    yield SubagentCompleted(run_id=run_id, scratch_dir=scratch_dir)
