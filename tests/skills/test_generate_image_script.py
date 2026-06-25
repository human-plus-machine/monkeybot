"""Tests for image-generator skill script (no live Vertex calls)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "playground"
    / "agent"
    / "workspace"
    / "skills"
    / "image-generator"
    / "generate_image.py"
)

_MINIMAL_PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
    b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_image_skill", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _vertex_response(png: bytes) -> MagicMock:
    inline = MagicMock()
    inline.data = png
    part = MagicMock()
    part.inline_data = inline
    content = MagicMock()
    content.parts = [part]
    candidate = MagicMock()
    candidate.content = content
    response = MagicMock()
    response.candidates = [candidate]
    return response


@pytest.fixture
def mock_vertex_genai(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Install fake google.genai modules matching the pinned SDK response shape."""

    def _install(*, client_factory: Callable[..., MagicMock] | None = None) -> dict[str, Any]:
        response = _vertex_response(_MINIMAL_PNG)
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = response

        fake_types = types.ModuleType("google.genai.types")
        fake_types.GenerateContentConfig = MagicMock
        fake_types.ImageConfig = MagicMock

        fake_genai = types.ModuleType("google.genai")
        fake_genai.Client = client_factory or MagicMock(return_value=mock_client)
        fake_genai.types = fake_types

        fake_google = types.ModuleType("google")
        fake_google.genai = fake_genai

        monkeypatch.setitem(sys.modules, "google", fake_google)
        monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
        monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
        return {"client": mock_client, "response": response}

    yield {"install": _install}


def test_image_bytes_from_response_reads_candidate_inline_data() -> None:
    mod = _load_module()
    png = b"\x89PNG\r\n\x1a\n"
    assert mod._image_bytes_from_response(_vertex_response(png)) == png
    assert mod._image_bytes_from_response(_vertex_response(b"")) is None
    assert mod._image_bytes_from_response(MagicMock(candidates=[])) is None


def test_generate_image_script_requires_prompt(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0


def test_generate_image_script_empty_prompt_json(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--prompt", "   "],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is False
    assert "empty" in payload["error"].lower()


@pytest.mark.parametrize("aspect", ["99:1", "bad"])
def test_generate_image_script_invalid_aspect(tmp_path: Path, aspect: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--prompt", "test", "--aspect-ratio", aspect],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["ok"] is False


def test_generate_image_script_success_mocked_vertex(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_vertex_genai: dict[str, Any],
) -> None:
    mock_vertex_genai["install"]()
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_AI_LOCATION", "global")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["generate_image.py", "--prompt", "a red circle"])

    mod = _load_module()
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 0
    out_files = list((tmp_path / "generated-media" / "images").glob("*.png"))
    assert len(out_files) == 1


def test_generate_image_script_writes_under_workspace_root_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_vertex_genai: dict[str, Any],
) -> None:
    agent_dir = tmp_path / "agent"
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.chdir(agent_dir)
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_AI_LOCATION", "global")

    mock_vertex_genai["install"]()
    monkeypatch.setattr(sys, "argv", ["generate_image.py", "--prompt", "circle"])

    mod = _load_module()
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 0
    out_files = list((workspace / "generated-media" / "images").glob("*.png"))
    assert len(out_files) == 1
    assert not (agent_dir / "generated-media").exists()


def test_generate_image_script_uses_credentials_file_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_vertex_genai: dict[str, Any],
) -> None:
    creds_file = tmp_path / "gcp-sa.json"
    creds_file.write_text(
        json.dumps({"type": "service_account", "project_id": "test-project"}),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(creds_file))
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_AI_LOCATION", "global")

    captured_client_kwargs: dict[str, object] = {}

    def _client_factory(**kwargs: object) -> MagicMock:
        captured_client_kwargs.update(kwargs)
        client = MagicMock()
        client.models.generate_content.return_value = _vertex_response(_MINIMAL_PNG)
        return client

    mock_vertex_genai["install"](client_factory=_client_factory)
    monkeypatch.setattr(sys, "argv", ["generate_image.py", "--prompt", "circle"])

    mod = _load_module()
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 0
    assert captured_client_kwargs == {
        "vertexai": True,
        "project": "test-project",
        "location": "global",
    }
    assert os.environ["GOOGLE_APPLICATION_CREDENTIALS"] == str(creds_file.resolve())
