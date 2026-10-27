"""Replay a T3a trajectory cassette through the real loop driver.

A ``mode: replay`` scenario names a cassette. The cassette's calls become the
script, and :func:`regression.drivers.loop.run_scenario` runs it, so tools, the
workspace, hooks, invariants and assertions all behave as in any scripted run.
With ``driver: gateway`` the same script goes through the in-process SSE gateway
(:func:`regression.drivers.gateway.run_scenario`), so the framed bytes can be
compared with a golden stream (T3-GW-01). Nothing here touches the network.

Staleness (§5.5.1) is checked against the tools the loop really advertises:

* a tool the cassette calls is gone, or one of its argument keys left the tool's
  schema → ``cassette_stale: tool <name> changed`` (the replay fails);
* a digest differs → :class:`CassetteDigestWarning` (the replay continues).

``run_transcript_roundtrip`` closes the T3 loop for T2-STORE-03: a scripted gateway
run with ``runtime.transcript_enabled: true`` is converted with
``from_transcript`` and the cassette is replayed.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from monkeybot.core.persistence.transcript import TRANSCRIPT_FILENAME
from monkeybot.core.persistence.transcript_reader import iter_session_dirs, load_records
from monkeybot.core.testing.mocks_provider import ScriptedProvider
from regression.drivers import gateway as gateway_driver
from regression.drivers import loop as loop_driver
from regression.drivers.bundles.protocol import LoopDriverError
from regression.drivers.loop import LoopRun
from regression.drivers.support.paths import _REPO
from regression.drivers.support.tokens import _message_text
from regression.schema.assertions import event_field, event_type
from regression.schema.scenario import Scenario, ScenarioConfig
from regression.t3.tools.cassette import (
    Cassette,
    CassetteDigestWarning,
    CassetteStaleError,
    digest_warnings,
    dump_cassette,
    load_cassette,
    stale_message,
    stale_tool,
)
from regression.t3.tools.from_transcript import from_transcript
from testkit.gateway import open_gateway
from testkit.verifier import CLASSIFIER_ROWS_ENV, JUDGE_SCRIPT_ENV

_REPLAY_DRIVERS = frozenset({"loop", "gateway"})
_PROBE_REQUEST_ID = "lock-check"
_TRANSCRIPT_WAIT_S = 5.0


class ReplayDriverError(LoopDriverError):
    """The replay driver cannot run this scenario as written."""


def resolve_cassette(scenario: Scenario) -> Path:
    """Find the cassette a replay scenario names.

    An absolute path is used as is. Otherwise the path is tried from the repo root,
    then from the directory of the scenario YAML (the scenario id is that path).
    """
    if scenario.cassette is None:
        raise ReplayDriverError(f"{scenario.case}: replay scenario needs a cassette")
    named = Path(scenario.cassette)
    candidates = [named] if named.is_absolute() else [_REPO / named]
    if not named.is_absolute():
        candidates.append(_REPO / Path(scenario.id).parent / named)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    tried = ", ".join(str(path) for path in candidates)
    raise ReplayDriverError(f"{scenario.case}: cassette not found (tried {tried})")


def _reject(scenario: Scenario) -> None:
    if scenario.mode != "replay":
        raise ReplayDriverError(f"replay driver runs replay scenarios, not mode {scenario.mode!r}")
    if scenario.driver not in _REPLAY_DRIVERS:
        raise ReplayDriverError(
            f"replay driver runs driver loop or gateway, not {scenario.driver!r}"
        )


def scripted_scenario(scenario: Scenario, cassette: Cassette) -> Scenario:
    """The scripted loop scenario that replays ``cassette`` for ``scenario``.

    With the recording executor each recorded tool result answers its call id.
    The real executor ignores recorded results and runs the tool.
    """
    script = [list(call.events) for call in cassette.calls]
    update: dict[str, Any] = {"mode": "scripted", "script": script, "cassette": None}
    if scenario.executor == "recording" and scenario.tool_results is None and cassette.tool_results:
        update["tool_results"] = {
            call_id: _envelope(body) for call_id, body in cassette.tool_results.items()
        }
    checker = _checker_env(cassette)
    if checker:
        base = scenario.config
        env = dict(base.env) if base is not None else {}
        env.update(checker)
        config = (base if base is not None else ScenarioConfig()).model_copy(update={"env": env})
        update["config"] = config
    return scenario.model_copy(update=update)


def _checker_env(cassette: Cassette) -> dict[str, str]:
    """Classifier rows and judge completions for the scripted verifier.

    Empty lists stay unset so a trajectory with no drift tape does not replace
    a scenario that already set those variables.
    """
    env: dict[str, str] = {}
    if cassette.classifier:
        rows = [row.model_dump(mode="json", exclude_none=True) for row in cassette.classifier]
        env[CLASSIFIER_ROWS_ENV] = json.dumps(rows, ensure_ascii=False)
    if cassette.judge:
        completions = [item.model_dump(mode="json", exclude_none=True) for item in cassette.judge]
        env[JUDGE_SCRIPT_ENV] = json.dumps({"completions": completions}, ensure_ascii=False)
    return env


def _envelope(body: str) -> dict[str, Any]:
    """A recorded result as the dict the recording executor serializes back."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return {"text": body}
    return parsed if isinstance(parsed, dict) else {"text": body}


async def run_scenario(scenario: Scenario) -> LoopRun | gateway_driver.GatewayRun:
    """Replay one scenario. Staleness fails it; a digest difference only warns.

    ``driver: loop`` replays through the loop driver. ``driver: gateway`` replays
    through the in-process SSE gateway, so the frames on the wire come from the
    recorded model output. The gateway always runs the real tools: recorded tool
    results are not fed back.
    """
    _reject(scenario)
    cassette = load_cassette(resolve_cassette(scenario))
    scripted = scripted_scenario(scenario, cassette)
    stale: list[str] = []
    run: LoopRun | gateway_driver.GatewayRun
    through_gateway = scenario.driver == "gateway"
    checked = _checked_gateway_provider if through_gateway else _checked_provider
    with checked(cassette, stale):
        try:
            if through_gateway:
                run = await gateway_driver.run_scenario(scripted)
            else:
                run = await loop_driver.run_scenario(scripted)
        except (AssertionError, gateway_driver.GatewayDriverError):
            # A stale cassette makes the run fail in other ways. Say why first.
            if stale:
                raise CassetteStaleError(stale_message(stale[0])) from None
            raise
    if stale:
        raise CassetteStaleError(stale_message(stale[0]))
    return run


@contextmanager
def _checked_provider(cassette: Cassette, stale: list[str]) -> Iterator[None]:
    """Make the loop driver's scripted provider check each call against live tools."""
    original = loop_driver._ResultTokenProvider

    class _Checked(original):  # type: ignore[valid-type, misc]
        async def stream(
            self,
            messages: Sequence[Any],
            tools: Sequence[Any],
            **kwargs: Any,
        ) -> Any:
            _check_call(cassette, self.stream_calls, messages, tools, stale)
            async for event in super().stream(messages, tools, **kwargs):
                yield event

    loop_driver._ResultTokenProvider = _Checked
    try:
        yield
    finally:
        loop_driver._ResultTokenProvider = original


@contextmanager
def _checked_gateway_provider(cassette: Cassette, stale: list[str]) -> Iterator[None]:
    """Make the gateway's scripted provider check each call against the live tools.

    The gateway builds its provider from the script text, so the check sits on the
    class for the length of one run. Probe calls past the recording are not checked.
    """
    original = ScriptedProvider.stream

    async def checked(
        self: ScriptedProvider,
        messages: Sequence[Any],
        tools: Sequence[Any],
        **kwargs: Any,
    ) -> Any:
        _check_call(cassette, self.stream_calls, messages, tools, stale)
        async for event in original(self, messages, tools, **kwargs):
            yield event

    ScriptedProvider.stream = checked  # type: ignore[method-assign, assignment]
    try:
        yield
    finally:
        ScriptedProvider.stream = original  # type: ignore[method-assign]


def _check_call(
    cassette: Cassette,
    index: int,
    messages: Sequence[Any],
    tools: Sequence[Any],
    stale: list[str],
) -> None:
    """Warn on digest drift at the first call. Fail when a recorded tool call is stale."""
    if index == 0:
        _warn_digests(cassette, messages, tools)
    if index < len(cassette.calls):
        name = stale_tool(cassette.calls[index], tools)
        if name is not None:
            stale.append(name)
            raise CassetteStaleError(stale_message(name))


def _warn_digests(cassette: Cassette, messages: Sequence[Any], tools: Sequence[Any]) -> None:
    system = "\n".join(_message_text(m) for m in messages if getattr(m, "role", "") == "system")
    schemas = [tool.to_model_schema() for tool in tools]
    for problem in digest_warnings(cassette, tools=schemas, system_text=system):
        warnings.warn(f"{cassette.scenario or 'cassette'}: {problem}", CassetteDigestWarning, 2)


def transcript_roundtrip_requested(scenario: Scenario) -> bool:
    """A scripted gateway scenario that turns the transcript on is also replayed."""
    if scenario.mode != "scripted" or scenario.driver != "gateway" or scenario.config is None:
        return False
    runtime = scenario.config.model_dump().get("runtime")
    return isinstance(runtime, dict) and runtime.get("transcript_enabled") is True


async def run_transcript_roundtrip(scenario: Scenario) -> LoopRun | gateway_driver.GatewayRun:
    """Run a gateway scenario, convert its transcript, and replay the cassette green.

    The scenario's own assertions hold for the gateway run first. They then hold
    again for the replay, which must also make the same number of provider calls
    and say the same thing.
    """
    gateway_driver._reject(scenario)
    config, env = gateway_driver._scenario_document(scenario)
    script = gateway_driver._script_text(scenario)
    async with open_gateway(script=script, config=config, env=env) as gateway:
        ran = await gateway_driver._drive(scenario, gateway)
        session = await _await_transcript(gateway.agent_root / "workspace", len(scenario.messages))
        cassette = from_transcript(
            session,
            scenario=scenario.id,
            harness_commit="roundtrip",
            exclude_request_ids=(_PROBE_REQUEST_ID,),
        )
    expected = len(scenario.script or [])
    if len(cassette.calls) != expected:
        raise AssertionError(
            f"{scenario.case}\ntranscript has {len(cassette.calls)} provider calls, "
            f"the script has {expected}"
        )
    with tempfile.TemporaryDirectory(prefix="mb-roundtrip-") as raw:
        path = Path(raw) / "roundtrip.cassette.json"
        dump_cassette(cassette, path)
        replay = Scenario.model_validate(
            {
                "id": scenario.id,
                "case": scenario.case,
                "mode": "replay",
                "driver": "loop",
                "messages": list(scenario.messages),
                "cassette": str(path),
                "assertions": scenario.assertions,
            }
        )
        # The gateway and the loop driver advertise different tool sets by design, so the
        # digests of a roundtrip cassette always differ. That is not a finding here.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CassetteDigestWarning)
            replayed = await run_scenario(replay)
    _same_reply(scenario, ran.events, replayed.events)
    return replayed


async def _await_transcript(workspace: Path, turns: int) -> Path:
    """The session directory once it holds a ``TurnComplete`` for every message."""
    root = workspace / ".monkeybot" / "transcripts"
    deadline = time.monotonic() + _TRANSCRIPT_WAIT_S
    while True:
        for session in iter_session_dirs(root):
            records = load_records(session / TRANSCRIPT_FILENAME)
            done = sum(
                1
                for record in records
                if record.get("type") == "TurnComplete"
                and record.get("request_id") != _PROBE_REQUEST_ID
            )
            if done >= turns:
                return session
        if time.monotonic() >= deadline:
            raise AssertionError(f"no transcript with {turns} completed turn(s) under {root}")
        await asyncio.sleep(0.05)


def _reply_text(events: Sequence[object]) -> str:
    parts = [
        str(event_field(event, "delta") or "")
        for event in events
        if event_type(event) == "AssistantDelta"
    ]
    return "".join(parts)


def _same_reply(scenario: Scenario, gateway: Sequence[object], replay: Sequence[object]) -> None:
    before = _reply_text(gateway)
    after = _reply_text(replay)
    if before != after:
        raise AssertionError(
            f"{scenario.case}\nreplayed reply {after!r} differs from the gateway reply {before!r}"
        )


__all__ = [
    "ReplayDriverError",
    "resolve_cassette",
    "run_scenario",
    "run_transcript_roundtrip",
    "scripted_scenario",
    "transcript_roundtrip_requested",
]
