"""Record T3b wire cassettes at the httpx transport (§5.5.2).

Runs only when ``MONKEYBOT_RECORD_CASSETTES=1``. Drives ``OllamaProvider``
(ollama-cloud / glm-5.3-flash) and writes the request plus the raw response
chunks. Secrets are scrubbed before the file is written. This does not start
a gateway and does not use port 8787.

The replay test imports the scenarios below so it sends the same request the
recording sent.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field

from regression.t3.tools.cassette import SCHEMA_VERSION, recording_requested
from testkit.scrub import scrub

WIRE_DIR = Path(__file__).resolve().parents[1] / "wire" / "ollama_cloud"
MODEL = "glm-5.3-flash"
PROVIDER = "ollama-cloud"
SDK_PACKAGES = ("openai",)
_STREAM_TIMEOUT_S = 180.0
_CLOUD_HOSTS = ("ollama.com",)
_REDACTED = "[REDACTED]"
_HEADER_KEEP = frozenset({"content-type"})
_SHAPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("sk-token", re.compile(r"sk-[A-Za-z0-9_\-]{8,}")),
    ("nvapi-token", re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}")),
    ("bearer-token", re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]{8,}")),
    (
        "authorization-value",
        re.compile(
            r"(?i)[\"']?authorization[\"']?\s*[:=]\s*[\"']?(?!\[REDACTED\])[A-Za-z0-9\-._~+/]{8,}"
        ),
    ),
    (
        "x-api-key",
        re.compile(
            r"(?i)[\"']?x-(?:goog-)?api-key[\"']?\s*[:=]\s*[\"']?(?!\[REDACTED\])[A-Za-z0-9\-._~+/]{8,}"
        ),
    ),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)
_PROJECT_ENV = (
    "GOOGLE_CLOUD_PROJECT",
    "FIREBASE_PROJECT_ID",
    "ANTHROPIC_VERTEX_PROJECT_ID",
)


class WireRequest(BaseModel):
    """The HTTP request the adapter sent, with secrets removed from the body."""

    model_config = ConfigDict(extra="forbid")

    method: str
    path: str
    body: dict[str, Any]


class WireResponse(BaseModel):
    """Raw response status, kept headers, and the response body split as it arrived."""

    model_config = ConfigDict(extra="forbid")

    status: int
    headers: dict[str, str]
    chunks: list[str]


class WireCassette(BaseModel):
    """One §5.5.2 provider wire cassette."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    kind: Literal["wire"]
    provider: str
    sdk: dict[str, str]
    recorded_at: str
    request: WireRequest
    response: WireResponse
    expected_events: list[dict[str, Any]] = Field(min_length=1)


@dataclass(frozen=True)
class WireScenario:
    """One live call. The replay test builds the same provider request."""

    filename: str
    prompt: str
    tool_name: str | None
    temperature: float
    max_tokens: int
    thinking_budget: int
    reasoning_effort: str


TOOL_TURN = WireScenario(
    filename="glm_tool_turn.json",
    prompt="Call the add tool once with a=2 and b=3. Do not answer in text.",
    tool_name="add",
    temperature=0.0,
    max_tokens=256,
    thinking_budget=0,
    reasoning_effort="none",
)
THINKING_ONLY = WireScenario(
    filename="thinking_only_completion.json",
    prompt="Respond with no visible text. Think privately about whether 2 is even, then stop.",
    tool_name=None,
    temperature=0.0,
    max_tokens=64,
    thinking_budget=-1,
    reasoning_effort="",
)
SCENARIOS = (TOOL_TURN, THINKING_ONLY)


def event_to_wire(event: Any) -> dict[str, Any]:
    """One provider event as the cassette's ``expected_events`` object."""
    from monkeybot.core.llm.provider import Done, TextDelta, ThinkingDelta, ToolCall, UsageEvent

    if isinstance(event, TextDelta):
        return {"type": "text", "text": event.text}
    if isinstance(event, ThinkingDelta):
        body: dict[str, Any] = {"type": "thinking", "text": event.text}
        if event.signature:
            body["signature"] = event.signature
        return body
    if isinstance(event, ToolCall):
        body = {
            "type": "tool_call",
            "call_id": event.call_id,
            "name": event.name,
            "args": event.args,
        }
        if event.parse_error:
            body["parse_error"] = event.parse_error
        return body
    if isinstance(event, UsageEvent):
        return {
            "type": "usage",
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
            "cached_tokens": event.cached_tokens,
            "cache_read_tokens": event.cache_read_tokens,
            "cache_creation_tokens": event.cache_creation_tokens,
        }
    if isinstance(event, Done):
        return {"type": "done", "truncated": event.truncated}
    raise TypeError(f"unexpected provider event {type(event).__name__}")


def scrub_recorded(text: str) -> str:
    """§5.5.2 scrub, plus a literal remove of the Ollama key if it is set."""
    cleaned = scrub(text)
    key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    if len(key) >= 8:
        cleaned = cleaned.replace(key, _REDACTED)
        quoted = quote(key, safe="")
        if quoted and quoted != key:
            cleaned = cleaned.replace(quoted, _REDACTED)
    return cleaned


def secret_shapes(text: str) -> list[str]:
    """Descriptions of anything in ``text`` that still looks like a secret.

    Lengths and pattern names only. The match text is not returned.
    """
    found: list[str] = []
    key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
    if len(key) >= 8 and key in text:
        found.append(f"env key substring len={len(key)}")
    for name, value in ((name, os.environ.get(name, "")) for name in _PROJECT_ENV):
        if len(value) >= 4 and value in text:
            found.append(f"project id {name} len={len(value)}")
    for name, pattern in _SHAPES:
        for match in pattern.finditer(text):
            found.append(f"{name} len={len(match.group(0))}")
    return found


class _CapturingStream(httpx.AsyncByteStream):
    """Yields the inner stream and keeps every chunk that passed through."""

    def __init__(self, inner: httpx.AsyncByteStream, sink: list[bytes]) -> None:
        self._inner = inner
        self._sink = sink

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._inner:
            self._sink.append(chunk)
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _Exchange:
    def __init__(self) -> None:
        self.method = ""
        self.path = ""
        self.host = ""
        self.body = b""
        self.status = 0
        self.headers: list[tuple[str, str]] = []
        self.chunks: list[bytes] = []


class RecordingTransport(httpx.AsyncBaseTransport):
    """Forwards to the real transport and keeps the request and response bytes."""

    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner
        self.exchanges: list[_Exchange] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        # aread() leaves a ByteStream already consumed. Put the bytes back so
        # the real transport sends the same body.
        request.stream = httpx.ByteStream(body)
        response = await self._inner.handle_async_request(request)
        exchange = _Exchange()
        exchange.method = request.method
        exchange.path = request.url.path
        exchange.host = request.url.host
        exchange.body = body
        exchange.status = response.status_code
        exchange.headers = list(response.headers.items())
        stream = response.stream
        if not isinstance(stream, httpx.AsyncByteStream):
            raise RuntimeError("Ollama response stream is not async")
        response.stream = _CapturingStream(stream, exchange.chunks)
        self.exchanges.append(exchange)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _install_recording_transport() -> list[RecordingTransport]:
    """Wrap the httpx client ``OllamaProvider`` builds. Returns the transports."""
    import monkeybot.providers._openai_compat as compat

    original = compat._keepalive_http_client
    created: list[RecordingTransport] = []

    def wrapped(*, base_url: str | None) -> Any:
        client = original(base_url=base_url)
        if client is None:
            raise RuntimeError("openai.DefaultAsyncHttpxClient is unavailable")
        transport = RecordingTransport(client._transport)
        client._transport = transport
        created.append(transport)
        mounts = getattr(client, "_mounts", None)
        if isinstance(mounts, dict):
            for pattern, mounted in list(mounts.items()):
                if mounted is None:
                    continue
                extra = RecordingTransport(mounted)
                mounts[pattern] = extra
                created.append(extra)
        return client

    compat._keepalive_http_client = wrapped
    return created


def _tools(scenario: WireScenario) -> list[Any]:
    from monkeybot.core.types.types_tools import ToolDef

    if scenario.tool_name is None:
        return []
    return [
        ToolDef(
            name=scenario.tool_name,
            description="Add two integers.",
            input_schema={
                "type": "object",
                "properties": {
                    "a": {"type": "integer"},
                    "b": {"type": "integer"},
                },
                "required": ["a", "b"],
            },
        )
    ]


def _provider(scenario: WireScenario) -> Any:
    from monkeybot.providers.ollama import OllamaProvider

    return OllamaProvider(
        mode="cloud",
        temperature=scenario.temperature,
        max_tokens=scenario.max_tokens,
        thinking_budget=scenario.thinking_budget,
        reasoning_effort=scenario.reasoning_effort,
    )


def _chunks_as_text(raw_chunks: list[bytes]) -> list[str]:
    texts: list[str] = []
    for chunk in raw_chunks:
        try:
            texts.append(chunk.decode("utf-8"))
        except UnicodeDecodeError:
            return [b"".join(raw_chunks).decode("utf-8")]
    return texts


def _kept_headers(headers: list[tuple[str, str]]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name, value in headers:
        if name.lower() not in _HEADER_KEEP:
            continue
        kept[name.lower()] = scrub_recorded(value)
    return kept


def _shape_problem(scenario: WireScenario, events: list[dict[str, Any]]) -> str | None:
    if not events or events[-1].get("type") != "done":
        return "stream did not end with done"
    types = [event.get("type") for event in events]
    if scenario.tool_name is not None:
        calls = [
            event
            for event in events
            if event.get("type") == "tool_call" and event.get("name") == scenario.tool_name
        ]
        if not calls:
            return f"no {scenario.tool_name} tool_call in {dict(Counter(str(t) for t in types))}"
        return None
    if "thinking" not in types:
        return f"no thinking event in {dict(Counter(str(t) for t in types))}"
    if "text" in types or "tool_call" in types:
        return f"thinking-only stream also has {dict(Counter(str(t) for t in types))}"
    return None


def _cassette_text(
    scenario: WireScenario, exchange: _Exchange, events: list[dict[str, Any]]
) -> str:
    from importlib.metadata import version

    body_text = scrub_recorded(exchange.body.decode("utf-8"))
    body = json.loads(body_text)
    if not isinstance(body, dict):
        raise RuntimeError(f"{scenario.filename}: request body was not a JSON object")
    chunks = [scrub_recorded(chunk) for chunk in _chunks_as_text(exchange.chunks)]
    # A secret split across two chunks survives per-chunk scrub. Scrub the join
    # and, when that changes it, store one chunk so the secret is gone.
    joined = scrub_recorded("".join(chunks))
    if joined != "".join(chunks):
        chunks = [joined]
    cassette = WireCassette(
        schema_version=SCHEMA_VERSION,
        kind="wire",
        provider=PROVIDER,
        sdk={name: version(name) for name in SDK_PACKAGES},
        recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        request=WireRequest(method=exchange.method, path=exchange.path, body=body),
        response=WireResponse(
            status=exchange.status,
            headers=_kept_headers(exchange.headers),
            chunks=chunks,
        ),
        expected_events=events,
    )
    payload = cassette.model_dump(mode="json")
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write_if_clean(path: Path, text: str) -> None:
    shapes = secret_shapes(text)
    if shapes:
        raise SystemExit(
            f"refusing to write {path.name}: secret-shaped text after scrub ({'; '.join(shapes)})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    written = path.read_text(encoding="utf-8")
    again = secret_shapes(written)
    if again:
        path.unlink()
        raise SystemExit(
            f"removed {path.name}: secret-shaped text was still in the file ({'; '.join(again)})"
        )


async def _record_one(scenario: WireScenario, transports: list[RecordingTransport]) -> None:
    from monkeybot.core.llm.provider import Message
    from monkeybot.providers._sdk_client import aclose_sdk_clients

    await aclose_sdk_clients()
    before = sum(len(transport.exchanges) for transport in transports)
    provider = _provider(scenario)
    messages = [Message.text("user", scenario.prompt)]

    async def collect() -> list[dict[str, Any]]:
        return [
            event_to_wire(event)
            async for event in provider.stream(messages, _tools(scenario), model=MODEL)
        ]

    events = await asyncio.wait_for(collect(), _STREAM_TIMEOUT_S)
    exchanges = [item for transport in transports for item in transport.exchanges]
    fresh = exchanges[before:]
    if not fresh:
        raise RuntimeError(f"{scenario.filename}: the transport saw no request")
    exchange = fresh[-1]
    counts = dict(Counter(str(event.get("type")) for event in events))
    prefix = exchange.chunks[0][:1] if exchange.chunks else b""
    kind = "text" if prefix in (b"d", b"e", b":", b"\n", b"{") else f"binary {prefix.hex()}"
    print(
        f"{scenario.filename}: host={exchange.host} path={exchange.path} "
        f"status={exchange.status} requests={len(fresh)} chunks={len(exchange.chunks)} "
        f"bytes={sum(len(chunk) for chunk in exchange.chunks)} events={counts} body={kind}",
        flush=True,
    )
    if exchange.host != _CLOUD_HOSTS[0] and not exchange.host.endswith(".ollama.com"):
        raise RuntimeError(f"{scenario.filename}: host {exchange.host} is not Ollama Cloud")
    if exchange.status != 200:
        raise RuntimeError(f"{scenario.filename}: HTTP {exchange.status}")
    problem = _shape_problem(scenario, events)
    if problem is not None:
        blob = b"".join(exchange.chunks)
        reasoning_key = blob.count(b'"reasoning"')
        thinking_key = blob.count(b'"thinking"')
        print(
            f"{scenario.filename}: not saved ({problem}); "
            f"reasoning_key={reasoning_key} thinking_key={thinking_key}",
            flush=True,
        )
        raise RuntimeError(f"{scenario.filename}: {problem}")
    text = _cassette_text(scenario, exchange, events)
    _write_if_clean(WIRE_DIR / scenario.filename, text)


async def record_all(names: tuple[str, ...] = ()) -> None:
    """Record the Ollama cassettes. The key must already be in the environment.

    ``names`` limits the run to those filenames. An empty tuple records both.
    """
    chosen = [scenario for scenario in SCENARIOS if not names or scenario.filename in names]
    if names and len(chosen) != len(names):
        known = ", ".join(scenario.filename for scenario in SCENARIOS)
        raise SystemExit(f"unknown cassette; expected one of: {known}")
    transports = _install_recording_transport()
    failures: list[str] = []
    for scenario in chosen:
        try:
            await _record_one(scenario, transports)
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            detail = scrub_recorded(f"{type(exc).__name__}: {exc}")
            key = (os.environ.get("OLLAMA_API_KEY") or "").strip()
            if len(key) >= 8 and key in detail:
                detail = f"{type(exc).__name__}: error text still contained the key; omitted"
            if status is not None:
                detail = f"{detail} status={status}"
            print(f"{scenario.filename}: FAILED {detail[:400]}", flush=True)
            failures.append(scenario.filename)
    if failures:
        raise SystemExit(f"recording failed for {', '.join(failures)}")


def main() -> None:
    if not recording_requested():
        raise SystemExit("refusing to record: set MONKEYBOT_RECORD_CASSETTES=1")
    os.environ.pop("OLLAMA_BASE_URL", None)
    from regression.t4.preflight import MISSING_KEY_MESSAGE, ensure_key

    if not ensure_key():
        raise SystemExit(MISSING_KEY_MESSAGE)
    asyncio.run(record_all(tuple(sys.argv[1:])))


if __name__ == "__main__":
    main()
