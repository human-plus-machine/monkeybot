"""Tests for image-generator skill script (no live Vertex calls)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _load_module():
    spec = importlib.util.spec_from_file_location("generate_image_skill", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


def test_generate_image_script_success_mocked_vertex(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
        b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    inline = MagicMock()
    inline.data = fake_png
    part = MagicMock()
    part.inline_data = inline
    response = MagicMock()
    response.parts = [part]
    response.candidates = None

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = response

    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = MagicMock
    fake_types.ImageConfig = MagicMock

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = MagicMock(return_value=mock_client)
    fake_genai.types = fake_types

    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
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
) -> None:
    agent_dir = tmp_path / "agent"
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True)
    monkeypatch.chdir(agent_dir)
    monkeypatch.setenv("MONKEYBOT_WORKSPACE_ROOT", str(workspace))
    monkeypatch.setenv("VERTEX_AI_PROJECT_ID", "test-project")
    monkeypatch.setenv("VERTEX_AI_LOCATION", "global")

    fake_png = b"\x89PNG\r\n\x1a\n"
    inline = MagicMock()
    inline.data = fake_png
    part = MagicMock()
    part.inline_data = inline
    response = MagicMock()
    response.parts = [part]
    response.candidates = None

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = response

    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = MagicMock
    fake_types.ImageConfig = MagicMock

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = MagicMock(return_value=mock_client)
    fake_genai.types = fake_types

    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
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

    fake_png = b"\x89PNG\r\n\x1a\n"
    inline = MagicMock()
    inline.data = fake_png
    part = MagicMock()
    part.inline_data = inline
    response = MagicMock()
    response.parts = [part]
    response.candidates = None

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = response
    captured_client_kwargs: dict[str, object] = {}

    def _client_factory(**kwargs: object) -> MagicMock:
        captured_client_kwargs.update(kwargs)
        return mock_client

    fake_types = types.ModuleType("google.genai.types")
    fake_types.GenerateContentConfig = MagicMock
    fake_types.ImageConfig = MagicMock

    fake_genai = types.ModuleType("google.genai")
    fake_genai.Client = _client_factory
    fake_genai.types = fake_types

    fake_google = types.ModuleType("google")
    fake_google.genai = fake_genai

    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
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
