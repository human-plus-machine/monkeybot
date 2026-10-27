"""Self-checks for the live-eval agent project (regression/t4/agent) and the judge config."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

from monkeybot.core.config.settings import verifier_config_from_section
from monkeybot.core.mcp.mcp_client import interpolate_env_vars
from regression.t4.scenario_loader import agent_workspace_root
from regression.t4.score import resolve_judge_provider_model

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "regression" / "t4" / "agent"
CONFIGS = AGENT / "monkeybot_config"
FIXTURE_SERVER = REPO / "regression" / "fixtures" / "mcp" / "fixture_mcp_server.py"


def _yaml(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CONFIGS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_the_agent_project_moved_out_of_evals() -> None:
    assert not (REPO / "evals" / "smoke_agent").exists()
    assert not (REPO / "evals" / "fixture_mcp_server.py").exists()
    assert FIXTURE_SERVER.is_file()
    assert not (AGENT / "fixture_mcp_server.py").exists()
    for name in ("pyproject.toml", "uv.lock", "skills/browser/SKILL.md"):
        assert (AGENT / name).is_file(), name


def test_the_agent_project_points_back_at_this_checkout() -> None:
    project = tomllib.loads((AGENT / "pyproject.toml").read_text(encoding="utf-8"))
    sources = project["tool"]["uv"]["sources"]
    assert (AGENT / sources["monkeybot"]["path"]).resolve() == REPO
    assert (AGENT / sources["monkeybot-cli"]["path"]).resolve() == REPO / "cli"
    lock = (AGENT / "uv.lock").read_text(encoding="utf-8")
    assert 'editable = "../../../"' in lock
    assert 'editable = "../../../cli"' in lock
    assert 'editable = "../../"' not in lock


@pytest.mark.parametrize(
    ("name", "autoconnect"), [("mcp.json", True), ("mcp.progressive.json", False)]
)
def test_the_agent_mcp_config_reaches_the_fixture_server(name: str, autoconnect: bool) -> None:
    raw = json.loads((CONFIGS / name).read_text(encoding="utf-8"))
    env = {"MONKEYBOT_AGENT_ROOT": str(AGENT), "MONKEYBOT_PYTHON": "python"}
    server = interpolate_env_vars(raw, env)["mcpServers"]["fixture"]
    assert server["enabled"] is True
    assert server["autoConnect"] is autoconnect
    assert server["command"] == "python"
    (arg,) = server["args"]
    assert arg.endswith("/../../fixtures/mcp/fixture_mcp_server.py")
    assert Path(arg).resolve() == FIXTURE_SERVER


def test_only_the_agent_config_files_differ_in_their_verifier_block() -> None:
    base = _yaml("monkeybot.yaml")
    assert "verifier" not in base
    for name in ("monkeybot.verifier-tracker.yaml", "monkeybot.verifier-judge.yaml"):
        config = _yaml(name)
        assert {k: v for k, v in config.items() if k != "verifier"} == base, name
    assert not (CONFIGS / "monkeybot.verifier-on.yaml").exists()


def test_the_tracker_config_is_observe_only_with_the_judge_off() -> None:
    config = verifier_config_from_section(_yaml("monkeybot.verifier-tracker.yaml")["verifier"])
    assert config.enabled and config.tracker.enabled
    assert config.judge.enabled is False
    assert config.escalation.max_severity == "none"


def test_the_judge_config_turns_the_judge_on_and_may_nudge() -> None:
    config = verifier_config_from_section(_yaml("monkeybot.verifier-judge.yaml")["verifier"])
    assert config.enabled and config.tracker.enabled
    assert config.judge.enabled is True
    assert config.judge.model == "glm-5.3-flash"
    assert config.escalation.max_severity == "nudge"


def test_the_agent_runs_on_ollama_cloud_glm() -> None:
    model = _yaml("monkeybot.yaml")["model"]
    assert (model["provider"], model["name"]) == ("ollama-cloud", "glm-5.3-flash")


def test_the_judge_is_ollama_cloud_glm_unless_the_environment_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JUDGE_PROVIDER", raising=False)
    monkeypatch.delenv("JUDGE_MODEL", raising=False)
    assert resolve_judge_provider_model() == ("ollama-cloud", "glm-5.3-flash")
    monkeypatch.setenv("JUDGE_PROVIDER", "fake")
    assert resolve_judge_provider_model() == ("fake", "")


def test_the_default_agent_workspace_is_the_agent_projects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MONKEYBOT_AGENT_WORKSPACE", raising=False)
    assert agent_workspace_root() == AGENT / "workspace"


def test_gitignore_covers_the_agent_projects_local_state() -> None:
    lines = {
        line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    for entry in (
        "/regression/t4/agent/.env",
        "/regression/t4/agent/data/",
        "/regression/t4/agent/workspace/",
        "/regression/t4/runs/",
    ):
        assert entry in lines, entry


def test_live_secrets_come_from_the_repo_root_env_not_the_agent_project() -> None:
    lines = {
        line.strip() for line in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    }
    assert ".env" in lines
    assert not (AGENT / ".env.example").exists()
