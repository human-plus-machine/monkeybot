"""In-process gateway app for scripted scenarios.

Builds the production ``create_app`` with its lifespan, a temporary agent
root, and ``MODEL_PROVIDER=fake`` pointed at ``MONKEYBOT_FAKE_PROVIDER_SCRIPT``.
HTTP JSON uses ``ASGITransport``. ``GET /events`` uses :mod:`testkit.sse`
because that transport buffers until the body ends.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import yaml
from asgi_lifespan import LifespanManager

from monkeybot.core.config.runtime_env import reset_runtime_env_state_for_tests
from monkeybot.gateway.sse.app import create_app
from testkit.sse import StreamingAsgiResponse

_DEFAULT_CONFIG: dict[str, Any] = {
    "harness": {"mode": "turn_based"},
    "runtime": {"log_level": "WARNING"},
    "model": {
        "provider": "fake",
        "name": "fake",
        "context_window": 200000,
        "max_turns": 10,
    },
    "memory": {"enabled": False},
    "coding": {"verification_gate": {"enabled": False}},
    "web_search": {"backend": "none"},
    "computer": {"enabled": False},
    "scheduler": {"enabled": False},
    "sandbox": {"enabled": False},
}

# Isolation defaults. A caller ``env`` mapping overrides these. Path keys are
# forced again so a scenario cannot point the process at a live database.
_ISOLATION: dict[str, str] = {
    "MONKEYBOT_OTEL_ENABLED": "false",
    "ATTACHMENTS_ENABLED": "false",
    "MONKEYBOT_SCHEDULER_ENABLED": "false",
    "MONKEYBOT_COMPUTER_TOOLS": "false",
    "MONKEYBOT_MEMORY_HOOK_ENABLED": "false",
    "WEB_SEARCH_BACKEND": "none",
    "SANDBOX_ENABLED": "false",
}
_DROP = (
    "MONKEYBOT_WORKSPACE_ROOT_OVERRIDE",
    "MONKEYBOT_FAKE_PROVIDER_EVENTS",
    "MONKEYBOT_AGENT_ID",
    "ARTIFACTS_PATH",
    "MEMORY_STORAGE_URI",
    "MEMORY_PATH",
    "MEMPALACE_PALACE_PATH",
    "MEMPALACE_BACKEND",
    "COMMAND_ALLOWLIST_CONFIG",
    "PERMISSION_CONFIG",
    "MONKEYBOT_APPROVALS_CONFIG",
    "MONKEYBOT_GRANTS_CONFIG",
)


class GatewayHarnessError(RuntimeError):
    """The in-process gateway could not be prepared or called."""


class GatewaySession:
    """One running gateway. JSON routes go through ``client``."""

    def __init__(
        self,
        app: Any,
        client: httpx.AsyncClient,
        *,
        agent_root: Path,
        script_path: Path,
    ) -> None:
        self.app = app
        self.client = client
        self.agent_root = agent_root
        self.script_path = script_path

    async def create_session(self, session_id: str) -> None:
        response = await self.client.post("/sessions", json={"session_id": session_id})
        if response.status_code != 201:
            raise GatewayHarnessError(_http_failure("POST /sessions", response))

    async def post_reply(self, session_id: str, request_id: str, message: str) -> httpx.Response:
        return await self.client.post(
            f"/sessions/{session_id}/reply",
            json={"request_id": request_id, "message": message},
        )

    def stream_events(
        self,
        session_id: str,
        *,
        last_event_id: str | None = None,
    ) -> StreamingAsgiResponse:
        headers = {"accept": "text/event-stream"}
        if last_event_id is not None:
            headers["last-event-id"] = last_event_id
        return StreamingAsgiResponse(
            self.app,
            method="GET",
            path=f"/sessions/{session_id}/events",
            headers=headers,
        )


@asynccontextmanager
async def open_gateway(
    *,
    script: str | Path,
    config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    db_from_config: bool = False,
) -> AsyncIterator[GatewaySession]:
    """Start ``create_app`` inside a temp agent root and close it on exit.

    ``script`` is the provider script file contents, or a path to read. The
    gateway loads it through ``MONKEYBOT_FAKE_PROVIDER_SCRIPT``. No port is
    bound.

    ``db_from_config`` leaves ``DB_URL`` unset so ``paths.db_url`` in the
    temp config is the database setting. The default pins a temp sqlite file
    so a scenario cannot point the process at a live database.
    """
    with tempfile.TemporaryDirectory(prefix="mb-gateway-") as raw:
        root = Path(raw)
        script_path = _install_script(root, script)
        _install_layout(root, _with_fake_provider(config))
        updates = dict(_ISOLATION)
        if env:
            updates.update(env)
        config_dir = root / "monkeybot_config"
        updates["MONKEYBOT_CONFIG"] = str(config_dir / "monkeybot.yaml")
        updates["MONKEYBOT_AGENT_ROOT"] = str(root)
        drop: tuple[str, ...] = _DROP
        if db_from_config:
            updates.pop("DB_URL", None)
            drop = (*_DROP, "DB_URL")
        else:
            updates["DB_URL"] = "sqlite:///data/monkeybot.db"
        updates["MONKEYBOT_FAKE_PROVIDER_SCRIPT"] = str(script_path)
        # Layout export writes absolute paths into the process. Pin this root's
        # files here so a previous run cannot leave AGENT_MD on a deleted temp dir.
        updates["AGENT_MD"] = str(config_dir / "AGENT.md")
        updates["MCP_CONFIG"] = str(config_dir / "mcp.json")
        updates["SKILLS_PATH"] = str(root / "skills")
        updates["MONKEYBOT_WORKSPACE_ROOT"] = str(root / "workspace")
        previous = _push_env(updates, drop)
        reset_runtime_env_state_for_tests()
        app = create_app()
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 123)),
            base_url="http://gateway.test",
            timeout=30,
        )
        manager = LifespanManager(app, startup_timeout=60, shutdown_timeout=45)
        started = False
        try:
            await manager.__aenter__()
            started = True
            async with client:
                yield GatewaySession(
                    app,
                    client,
                    agent_root=root,
                    script_path=script_path,
                )
        finally:
            if started:
                # Shutdown even when the body raised. LifespanManager skips it
                # on an exception, and a live app would leak into the next test.
                await manager.__aexit__(None, None, None)
            _pop_env(previous)
            reset_runtime_env_state_for_tests()


def _with_fake_provider(config: Mapping[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in _DEFAULT_CONFIG.items():
        merged[key] = dict(value) if isinstance(value, dict) else value
    if config:
        for key, value in config.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged[key] = {**current, **value}
            else:
                merged[key] = value
    model = dict(merged.get("model") or {})
    model["provider"] = "fake"
    merged["model"] = model
    return merged


def _install_script(root: Path, script: str | Path) -> Path:
    dest = root / "provider-script.yaml"
    if isinstance(script, Path):
        if not script.is_file():
            raise GatewayHarnessError(f"provider script not found: {script}")
        text = script.read_text(encoding="utf-8")
    else:
        text = script
    if not text.strip():
        raise GatewayHarnessError("provider script is empty")
    dest.write_text(text, encoding="utf-8")
    return dest


def _install_layout(root: Path, config: Mapping[str, Any]) -> None:
    config_dir = root / "monkeybot_config"
    config_dir.mkdir(parents=True)
    (root / "workspace").mkdir()
    (root / "skills").mkdir()
    (root / "data").mkdir()
    (config_dir / "monkeybot.yaml").write_text(
        yaml.safe_dump(dict(config), sort_keys=False),
        encoding="utf-8",
    )
    (config_dir / "AGENT.md").write_text("# Agent\n", encoding="utf-8")
    (config_dir / "mcp.json").write_text('{"mcpServers": {}}\n', encoding="utf-8")


def _push_env(updates: Mapping[str, str], drop: tuple[str, ...]) -> dict[str, str | None]:
    previous: dict[str, str | None] = {}
    keys = set(updates) | set(drop)
    for key in keys:
        previous[key] = os.environ.get(key)
    for key in drop:
        os.environ.pop(key, None)
    for key, value in updates.items():
        os.environ[key] = value
    return previous


def _pop_env(previous: Mapping[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _http_failure(what: str, response: httpx.Response) -> str:
    body = response.text.replace("\n", " ")[:300]
    return f"{what} returned HTTP {response.status_code}: {body}"


__all__ = [
    "GatewayHarnessError",
    "GatewaySession",
    "open_gateway",
]
