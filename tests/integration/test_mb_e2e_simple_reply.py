"""Integration: gateway POST flow + agent events on session bus (Story 8).

``httpx.ASGITransport`` buffers until the response body completes, so infinite
SSE streams never yield to the client. We assert the same published frames by
subscribing to the :class:`~monkeybot.gateway.sse.session_bus.SessionBus` after
``POST .../reply`` (the bus is what ``GET .../events`` would stream).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


def _tier_policy_yaml() -> str:
    """Minimal valid run_command policy (defaults + no deny-regex layer)."""
    return "deny_patterns: []\n"


def _parse_sse_data_lines(body: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        for line in block.splitlines():
            if line.startswith("data:"):
                raw = line[len("data:") :].strip()
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
    return events


@pytest_asyncio.fixture
async def gateway_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> AsyncIterator[AsyncClient]:
    """ASGI client with temp DB, tier policy, and deterministic fake provider."""
    db_file = tmp_path / "mb.db"
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir()
    (cfg_dir / "monkeybot.yaml").write_text(
        "model:\n  provider: fake\n  name: fake\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DB_URL", f"sqlite:///{db_file}")
    monkeypatch.setenv("MCP_CONFIG", str(tmp_path / "no_mcp.json"))
    policy_file = tmp_path / "command_allowlist.yaml"
    policy_file.write_text(_tier_policy_yaml(), encoding="utf-8")
    monkeypatch.setenv("COMMAND_ALLOWLIST_CONFIG", str(policy_file))
    agent = tmp_path / "AGENT.md"
    agent.write_text("# Test agent\nYou are a test assistant.\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MD", str(agent))
    memory = tmp_path / "memory"
    memory.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    monkeypatch.setenv("MEMORY_PATH", str(memory))
    monkeypatch.setenv("SKILLS_PATH", str(skills))

    from asgi_lifespan import LifespanManager

    from monkeybot.gateway.sse.app import app

    async with LifespanManager(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


@pytest.mark.integration
@pytest.mark.asyncio
async def test_mb_e2e_simple_reply(gateway_client: AsyncClient) -> None:
    """POST session + reply; bus receives AssistantDelta and TurnComplete (SSE wire shape)."""
    from monkeybot.gateway.sse.app import app

    r1 = await gateway_client.post("/sessions", json={})
    assert r1.status_code == 201
    sid = r1.json()["session_id"]

    bus = app.state.registry.get(sid)
    assert bus is not None
    replay, q = await bus.subscribe(None)

    body = {"request_id": "req-e2e-1", "message": "Hello"}
    r2 = await gateway_client.post(f"/sessions/{sid}/reply", json=body)
    assert r2.status_code == 200

    frames: list[str] = list(replay)
    joined = ""
    deadline = asyncio.get_running_loop().time() + 10.0
    while '"type":"TurnComplete"' not in joined and asyncio.get_running_loop().time() < deadline:
        frames.append(await asyncio.wait_for(q.get(), timeout=1.0))
        joined = "".join(frames)

    parsed = _parse_sse_data_lines(joined)
    types = [str(p.get("type")) for p in parsed]
    assert "AssistantDelta" in types
    assert "TurnComplete" in types
    tc = next(p for p in parsed if p.get("type") == "TurnComplete")
    usage = tc.get("usage")
    assert isinstance(usage, dict)

    uresp = await gateway_client.get(f"/sessions/{sid}/usage")
    assert uresp.status_code == 200
    uj = uresp.json()
    assert uj["session_id"] == sid
    assert uj["turns"] >= 1
