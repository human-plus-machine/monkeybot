"""Tests for Provider protocol wiring and scripted fakes."""

# mypy: disable-error-code=untyped-decorator

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from monkeybot.core.content_blocks import Text
from monkeybot.core.interfaces import LLMError
from monkeybot.core.mocks_provider import ScriptedFakeProvider
from monkeybot.core.provider import Done, Message, ProviderEvent, TextDelta
from monkeybot.core.providers import gemini as gemini_mod
from monkeybot.core.providers.gemini import GeminiProvider
from monkeybot.core.types_tools import ToolDef


async def _collect_fake(scripted: ScriptedFakeProvider) -> list[ProviderEvent]:
    """Materialize scripted provider output for assertions."""
    events: list[ProviderEvent] = []
    async for ev in scripted.stream([], [], model="x"):
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_fake_provider_stream_order() -> None:
    p = ScriptedFakeProvider([TextDelta(text="a"), TextDelta(text="b"), Done()])
    out = await _collect_fake(p)
    assert len(out) == 3
    assert isinstance(out[0], TextDelta) and out[0].text == "a"
    assert isinstance(out[1], TextDelta) and out[1].text == "b"
    assert isinstance(out[2], Done)


@pytest.mark.asyncio
async def test_fake_provider_supports_streaming_false() -> None:
    p = ScriptedFakeProvider(
        [TextDelta(text="a"), TextDelta(text="b"), Done()],
        supports_streaming=False,
    )
    assert p.supports_streaming is False
    out = await _collect_fake(p)
    assert len(out) == 3


def test_gemini_module_import_does_not_load_google_genai() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    code = f"""
import importlib.util
import sys
from pathlib import Path

_repo = Path({repo_root.as_posix()!r})
_pkg = _repo / "src" / "monkeybot"
_spec = importlib.util.spec_from_file_location(
    "monkeybot",
    _pkg / "__init__.py",
    submodule_search_locations=[str(_pkg)],
)
assert _spec is not None and _spec.loader is not None
_monkeybot = importlib.util.module_from_spec(_spec)
sys.modules["monkeybot"] = _monkeybot
_spec.loader.exec_module(_monkeybot)

import monkeybot.core.providers.gemini  # noqa: E402 — after manual package bootstrap

assert "google.genai" not in sys.modules
assert "google.generativeai" not in sys.modules
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(repo_root),
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout


def test_normalize_vertex_model_strips_models_prefix() -> None:
    assert gemini_mod._normalize_vertex_model("models/gemini-2.0-flash") == "gemini-2.0-flash"


def test_suppress_thinking_curator_and_summarize_shapes() -> None:
    cur_sys = Message(
        role="system",
        content=[Text(text="You narrow context for another assistant. Reply with ONLY a JSON object.")],
    )
    cur_user = Message(role="user", content=[Text(text="x")])
    assert gemini_mod._suppress_thinking_for_auxiliary_call([cur_sys, cur_user], []) is True

    sum_sys = Message(
        role="system",
        content=[Text(text="You compress prior agent conversation turns into one dense factual summary.")],
    )
    sum_user = Message(role="user", content=[Text(text="blob")])
    assert gemini_mod._suppress_thinking_for_auxiliary_call([sum_sys, sum_user], []) is True

    tool = ToolDef(name="t", description="d", input_schema={})
    assert gemini_mod._suppress_thinking_for_auxiliary_call([cur_sys, cur_user], [tool]) is False

    assert gemini_mod._suppress_thinking_for_auxiliary_call([cur_user], []) is False


def test_vertex_location_preview_defaults_to_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "my-proj")
    project, loc = gemini_mod._vertex_project_and_location("gemini-3-flash-preview")
    assert project == "my-proj"
    assert loc == "global"


def test_vertex_location_explicit_env_overrides_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GCP_PROJECT_ID", "my-proj")
    monkeypatch.setenv("VERTEX_AI_LOCATION", "europe-west4")
    _project, loc = gemini_mod._vertex_project_and_location("gemini-3-flash-preview")
    assert loc == "europe-west4"


def test_vertex_location_embedded_in_full_resource_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VERTEX_AI_LOCATION", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_LOCATION", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "p")
    full = (
        "projects/p/locations/us-east1/publishers/google/models/gemini-2.5-flash"
    )
    _project, loc = gemini_mod._vertex_project_and_location(full)
    assert loc == "us-east1"


@pytest.mark.asyncio
async def test_gemini_stream_requires_vertex_project_env() -> None:
    """GeminiProvider needs a GCP project id unless integration explicitly runs live."""
    p = GeminiProvider()
    tool = ToolDef(name="noop", description="noop", input_schema={})
    keys = ("GCP_PROJECT_ID", "VERTEX_AI_PROJECT_ID", "GOOGLE_CLOUD_PROJECT")
    saved = {k: os.environ.pop(k, None) for k in keys}
    try:
        with pytest.raises(LLMError, match="VERTEX_AI_PROJECT_ID|GOOGLE_CLOUD_PROJECT|GCP_PROJECT_ID"):
            async for _ in p.stream(
                [Message(role="user", content=[Text(text="hi")])],
                [tool],
                model="gemini-2.5-flash",
            ):
                pass
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("MONKEYBOT_GEMINI_INTEGRATION") != "1",
    reason="Set MONKEYBOT_GEMINI_INTEGRATION=1 and GCP credentials to hit Vertex (costs apply).",
)
async def test_gemini_stream_smoke_integration() -> None:
    p = GeminiProvider()
    tool = ToolDef(name="noop", description="noop", input_schema={})
    chunks: list[ProviderEvent] = []
    async for ev in p.stream([Message(role="user", content="Say only: ok.")], [tool], model="gemini-2.5-flash"):
        chunks.append(ev)
    assert chunks
    assert isinstance(chunks[-1], Done)
