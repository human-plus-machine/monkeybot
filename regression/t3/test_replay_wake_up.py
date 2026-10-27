"""T3-MEM-01: recorded ``load_index()`` lines for a palace snapshot match byte for byte.

``regression/t3/memory/wake_up_snapshot.json`` holds a palace snapshot (identity text and
drawers) and, for two settings of "share memory across chats", the lines that
``MemorySubsystem.load_index()`` returned from a local run of a real MemPalace (Chroma)
palace. No model and no network: the drawers are written with fixed vectors, so the
embedder never loads.

* Sharing on: the lines come from MemPalace's own ``MemoryStack`` over the Chroma
  collection. Replay rebuilds that collection, so it needs the optional ``memory`` extra.
* Sharing off: the lines come from ``format_thread_wake_up`` over the palace's drawers.
  Replay feeds the snapshot's drawers to the real adapter, so it needs nothing optional.

``--update-golden`` re-records the snapshot (refused when ``CI`` is set); without it, a fresh
local run must equal the committed file.
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import pytest

from monkeybot.core.memory.palace import DrawerRecord, MemPalaceAdapter
from monkeybot.core.memory.subsystem import MemorySubsystem

_REPO = Path(__file__).resolve().parents[2]
_SNAPSHOT = _REPO / "regression" / "t3" / "memory" / "wake_up_snapshot.json"
_AGENT = "agent-a"
_WING = "main"
_THREAD = "t2"
_SHARE_ENV = "MONKEYBOT_MEMORY_SHARE_THREADS"
_VECTOR = [0.0] * 384

_LONG = "L" + "o" * 260 + "ng drawer, cut at two hundred characters."
# (id, content, wing, thread, filed_at). Two chats, one chat-less line, a second wing,
# a multi-line line and a long one, so scoping, wing and snippet rules all show.
_DRAWERS: list[tuple[str, str, str, str, str]] = [
    (
        "d1",
        "Deploys go out on Tuesdays after the freeze check.",
        _WING,
        "t1",
        "2026-01-01T09:00:00",
    ),
    ("d2", "User prefers short answers\nwith no emoji.", _WING, "t2", "2026-01-02T09:00:00"),
    ("d3", "Project codename is Falcon.", _WING, "", "2026-01-03T09:00:00"),
    ("d4", _LONG, _WING, "t2", "2026-01-04T09:00:00"),
    ("d5", "A note filed in a different wing.", "side", "t2", "2026-01-05T09:00:00"),
]


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """MemPalace keeps its write locks under the home directory. Keep that in tmp."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("MONKEYBOT_WORKSPACE_ID", raising=False)
    monkeypatch.delenv(_SHARE_ENV, raising=False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")


def _metadata(drawer: tuple[str, str, str, str, str]) -> dict[str, str]:
    _, _, wing, thread, filed_at = drawer
    meta = {
        "wing": wing,
        "room": "conversation",
        "filed_at": filed_at,
        "chunk_index": "0",
        "source_file": "",
        "added_by": "monkeybot",
    }
    if thread:
        meta["thread_id"] = thread
    return meta


def _subsystem(palace: MemPalaceAdapter, tmp_path: Path) -> MemorySubsystem:
    return MemorySubsystem(
        memory_uri=f"local://{palace.palace_path}",
        db_url=f"sqlite:///{tmp_path / 'unused.db'}",
        agent_id=_AGENT,
        agent_name=_AGENT,
        palace=palace,
    )


def _chroma_palace(
    root: Path, drawers: list[dict[str, Any]], identity: str | None
) -> MemPalaceAdapter:
    """A real MemPalace palace holding ``drawers``, written with fixed vectors."""
    pytest.importorskip("mempalace", reason="T3-MEM-01 sharing-on lines need the memory extra")
    root.mkdir(parents=True, exist_ok=True)
    if identity is not None:
        (root / "identity.txt").write_text(identity, encoding="utf-8")
    palace = MemPalaceAdapter(root, agent_name=_AGENT)
    collection = palace._collection(create=True)
    collection.upsert(
        ids=[item["drawer_id"] for item in drawers],
        documents=[item["content"] for item in drawers],
        metadatas=[item["metadata"] for item in drawers],
        embeddings=[list(_VECTOR) for _ in drawers],
    )
    return palace


class _SnapshotPalace(MemPalaceAdapter):
    """The real adapter, listing the snapshot's drawers instead of opening Chroma."""

    def __init__(self, root: Path, drawers: list[dict[str, Any]], identity: str) -> None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "identity.txt").write_text(identity, encoding="utf-8")
        super().__init__(root, agent_name=_AGENT)
        self._snapshot = drawers

    def list_drawers(self, *, limit: int = 2000) -> list[DrawerRecord]:
        records = [
            DrawerRecord(
                drawer_id=item["drawer_id"],
                content=item["content"],
                wing=item["metadata"]["wing"],
                room=item["metadata"]["room"],
                filed_at=item["metadata"]["filed_at"],
                metadata=dict(item["metadata"]),
            )
            for item in self._snapshot
        ]
        records.sort(key=lambda drawer: drawer.filed_at, reverse=True)
        return records[:limit]

    def status(self) -> dict[str, Any]:
        return {"total_drawers": len(self._snapshot), "backend": self.backend}


def _load() -> dict[str, Any]:
    loaded = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _case(recording: dict[str, Any], name: str) -> dict[str, Any]:
    return next(case for case in recording["cases"] if case["name"] == name)


def _apply(case: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_SHARE_ENV, "1" if case["share_threads"] else "0")


@pytest.mark.case("T3-MEM-01")
async def test_replayed_sharing_off_lines_match_the_recording_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = _load()
    case = _case(recording, "sharing_off")
    _apply(case, monkeypatch)
    palace = _SnapshotPalace(tmp_path / "palace", recording["drawers"], recording["identity"])

    lines = await _subsystem(palace, tmp_path).load_index(thread_id=case["thread_id"])

    assert lines == case["lines"]
    assert "\n".join(lines) == "\n".join(case["lines"])
    joined = "\n".join(lines)
    assert "Project codename is Falcon." in joined
    assert "User prefers short answers with no emoji." in joined
    assert "Deploys go out on Tuesdays" not in joined
    assert "different wing" not in joined
    long_line = next(line for line in lines if line.startswith("- [conversation] Loo"))
    assert long_line.endswith("...") and len(long_line) == len("- [conversation] ") + 200


@pytest.mark.case("T3-MEM-01")
async def test_replayed_sharing_on_lines_match_the_recording_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recording = _load()
    case = _case(recording, "sharing_on")
    _apply(case, monkeypatch)
    palace = _chroma_palace(tmp_path / "palace", recording["drawers"], recording["identity"])

    lines = await _subsystem(palace, tmp_path).load_index(thread_id=case["thread_id"])

    assert lines == case["lines"]
    joined = "\n".join(lines)
    assert "Deploys go out on Tuesdays" in joined
    assert "different wing" not in joined


@pytest.mark.case("T3-MEM-01")
async def test_the_recorded_scoping_differs_between_the_two_settings() -> None:
    recording = _load()
    shared = "\n".join(_case(recording, "sharing_on")["lines"])
    scoped = "\n".join(_case(recording, "sharing_off")["lines"])
    assert shared != scoped
    assert "Deploys go out on Tuesdays" in shared and "Deploys go out on Tuesdays" not in scoped


async def _record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """One local run of a real MemPalace palace, read through ``load_index``."""
    drawers = [
        {"drawer_id": item[0], "content": item[1], "metadata": _metadata(item)} for item in _DRAWERS
    ]
    palace = _chroma_palace(tmp_path / "palace", drawers, None)
    identity = (palace.palace_path / "identity.txt").read_text(encoding="utf-8")
    cases: list[dict[str, Any]] = []
    for name, share in (("sharing_on", True), ("sharing_off", False)):
        case: dict[str, Any] = {"name": name, "share_threads": share, "thread_id": _THREAD}
        _apply(case, monkeypatch)
        case["lines"] = await _subsystem(palace, tmp_path).load_index(thread_id=_THREAD)
        cases.append(case)
    try:
        sdk = version("mempalace")
    except PackageNotFoundError:
        sdk = "unknown"
    return {
        "schema_version": 1,
        "kind": "wake_up",
        "sdk": {"mempalace": sdk},
        "wing": _WING,
        "identity": identity,
        "drawers": drawers,
        "cases": cases,
    }


@pytest.mark.case("T3-MEM-01")
async def test_the_recording_matches_a_fresh_local_run(
    request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = json.dumps(await _record(tmp_path, monkeypatch), indent=2, ensure_ascii=False) + "\n"
    if request.config.getoption("update_golden"):
        _SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
        _SNAPSHOT.write_text(fresh, encoding="utf-8")
        return
    assert _SNAPSHOT.is_file(), "missing wake_up_snapshot.json; re-run with --update-golden"
    assert _SNAPSHOT.read_text(encoding="utf-8") == fresh
