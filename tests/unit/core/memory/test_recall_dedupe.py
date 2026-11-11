"""Recall drops drawers the wake-up block already shows."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.memory.palace import (
    WAKE_UP_SNIPPET_CHARS,
    DrawerRecord,
    drawers_not_in_wake_up,
    format_thread_wake_up,
)
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.persistence.sqlite import apply_schema, open_connection
from testkit.palace import InMemoryPalace

_SNAPSHOT = (
    Path(__file__).resolve().parents[4] / "regression" / "t3" / "memory" / "wake_up_snapshot.json"
)
_RECALL_HEADING = "Workspace conversation recall"


def _drawer(
    drawer_id: str,
    content: str,
    *,
    room: str = "conversation",
    filed_at: str = "2026-01-01T00:00:00",
    thread_id: str = "t1",
) -> DrawerRecord:
    return DrawerRecord(
        drawer_id=drawer_id,
        content=content,
        wing="main",
        room=room,
        filed_at=filed_at,
        metadata={"thread_id": thread_id, "room": room},
    )


def _lines(drawers: list[DrawerRecord]) -> list[str]:
    text = format_thread_wake_up("identity", drawers, wing="main", thread_id="t1")
    return [line for line in text.splitlines() if line.strip()]


def test_thread_scoped_bullets_drop_shown_drawers() -> None:
    shown = _drawer("shown", "alpha secret")
    missing = _drawer("missing", "kept aside", room="notes")
    fresh = drawers_not_in_wake_up([shown, missing], _lines([shown]))
    assert [drawer.drawer_id for drawer in fresh] == ["missing"]


def test_cut_bullet_matches_the_full_drawer() -> None:
    long = _drawer("long", "L" + "o" * 260 + "ng drawer")
    assert drawers_not_in_wake_up([long], _lines([long])) == []


def test_short_bullet_does_not_prefix_match_a_longer_drawer() -> None:
    short = _drawer("short", "ok")
    longer = _drawer("longer", "okay then")
    fresh = drawers_not_in_wake_up([longer], _lines([short]))
    assert [drawer.drawer_id for drawer in fresh] == ["longer"]


def test_mempalace_sharing_on_lines_cover_the_wing() -> None:
    recording = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    case = next(item for item in recording["cases"] if item["name"] == "sharing_on")
    drawers = [
        DrawerRecord(
            drawer_id=item["drawer_id"],
            content=item["content"],
            wing=item["metadata"]["wing"],
            room=item["metadata"]["room"],
            filed_at=item["metadata"]["filed_at"],
            metadata=dict(item["metadata"]),
        )
        for item in recording["drawers"]
    ]
    long_line = next(line for line in case["lines"] if line.startswith("  - L"))
    snippet = long_line.removeprefix("  - ")
    assert len(snippet) == WAKE_UP_SNIPPET_CHARS
    assert snippet.endswith("...")

    fresh = drawers_not_in_wake_up(drawers, case["lines"])
    assert [drawer.drawer_id for drawer in fresh] == ["d5"]


@pytest.fixture
async def db_url(tmp_path: Path) -> str:
    path = tmp_path / "monkeybot.db"
    url = f"sqlite:///{path}"
    conn = await open_connection(url)
    await apply_schema(conn)
    from monkeybot.core.memory.outbox import ensure_outbox_schema

    await ensure_outbox_schema(conn)
    await conn.close()
    return url


def _subsystem(
    tmp_path: Path, db_url: str, palace: InMemoryPalace | None = None
) -> MemorySubsystem:
    palace = palace or InMemoryPalace(tmp_path / "palace", agent_name="agent-a")
    return MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=db_url,
        agent_id="agent-a",
        agent_name="agent-a",
        palace=palace,
    )


def _payload(sub: MemorySubsystem, lines: list[str]) -> HookPayload:
    ctx = TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="md",
        memory_index=lines,
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        memory=sub,
    )
    return HookPayload(
        event=HookEvent.PRE_TURN,
        thread_id="t1",
        request_id="r1",
        ctx=ctx,
        user_message="what was said?",
    )


@pytest.mark.asyncio
async def test_recall_adds_nothing_when_wake_up_shows_every_drawer(
    db_url: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    sub = _subsystem(tmp_path, db_url)
    await sub.ensure_ready()
    await sub.enqueue(
        thread_id="t1",
        turn_id="turn1",
        message_id="m1",
        role="user",
        content="already in the briefing",
    )
    await sub.drain_writer(timeout_s=2)
    lines = await sub.load_index(thread_id="t1")
    payload = _payload(sub, lines)
    with caplog.at_level(logging.INFO, logger="monkeybot.memory"):
        await sub._hook.on_pre_turn(payload)
    assert payload.inject_memory_lines == []
    assert payload.inject_text is None
    assert any("'memory_recall_skipped': 1" in record.message for record in caplog.records)
    await sub.close()


@pytest.mark.asyncio
async def test_recall_keeps_conversation_drawers_a_full_wake_up_left_out(
    db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ID", raising=False)
    palace = InMemoryPalace(tmp_path / "palace", agent_name="agent-a")
    for index in range(15):
        palace.upsert_drawer(
            f"note-{index}",
            f"note-body-{index}",
            {
                "wing": "main",
                "room": "notes",
                "filed_at": f"2026-02-{index + 1:02d}T00:00:00",
                "thread_id": "t1",
            },
        )
    palace.upsert_drawer(
        "conv-1",
        "conversation-only-fact",
        {
            "wing": "main",
            "room": "conversation",
            "filed_at": "2026-01-01T00:00:00",
            "thread_id": "t1",
        },
    )
    sub = _subsystem(tmp_path, db_url, palace)
    await sub.ensure_ready()
    lines = await sub.load_index(thread_id="t1")
    assert not any("conversation-only-fact" in line for line in lines)
    payload = _payload(sub, lines)
    await sub._hook.on_pre_turn(payload)
    injected = "\n".join(payload.inject_memory_lines)
    assert "conversation-only-fact" in injected
    assert payload.inject_text is not None
    assert _RECALL_HEADING in payload.inject_text
    await sub.close()
