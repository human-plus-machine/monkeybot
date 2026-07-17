"""Tests for Evidence path verification guard (F9 + F22)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.hooks import HookEvent, HookPayload
from monkeybot.core.knowledge.evidence_guard import (
    EvidencePathGuard,
    extract_evidence_paths,
    format_evidence_correction,
    format_verification_notice,
    missing_evidence_paths,
)


def _ctx(workspace_root: Path | None) -> TurnContext:
    return TurnContext(
        thread_id="t1",
        request_id="r1",
        agent_md="agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        workspace_root=workspace_root,
    )


def _payload(event: HookEvent, workspace_root: Path | None, **kw) -> HookPayload:
    return HookPayload(
        event=event,
        thread_id="t1",
        request_id="r1",
        ctx=_ctx(workspace_root),
        **kw,
    )


def test_extract_evidence_paths_variants() -> None:
    text = """
Q01: fifteen
Evidence: src/auth.ts

Q02: local
- **Evidence:** `src/lib/firebase.ts` L12–14

Q03: x
Evidence: public/og-image.png; missing/nope.ts
Evidence: unknown
"""
    paths = extract_evidence_paths(text)
    assert "src/auth.ts" in paths
    assert "src/lib/firebase.ts" in paths
    assert "public/og-image.png" in paths
    assert "missing/nope.ts" in paths
    assert "unknown" not in paths


def test_missing_evidence_paths_filters_existing(tmp_path: Path) -> None:
    real = tmp_path / "src" / "auth.ts"
    real.parent.mkdir(parents=True)
    real.write_text("x", encoding="utf-8")
    text = "Evidence: src/auth.ts\nEvidence: src/missing.ts\n"
    assert missing_evidence_paths(text, tmp_path) == ["src/missing.ts"]


def test_missing_rejects_path_escape(tmp_path: Path) -> None:
    text = "Evidence: ../outside.txt\n"
    assert missing_evidence_paths(text, tmp_path) == ["../outside.txt"]


async def _record_read(guard: EvidencePathGuard, root: Path, path: str) -> None:
    await guard.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            root,
            tool_name="read_file",
            tool_args={"path": path},
        )
    )


@pytest.mark.asyncio
async def test_guard_injects_correction_on_pre_turn(tmp_path: Path) -> None:
    (tmp_path / "ok.ts").write_text("1", encoding="utf-8")
    guard = EvidencePathGuard()
    await _record_read(guard, tmp_path, "ok.ts")
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Q01: x\nEvidence: ok.ts\nEvidence: fake/path.ts\n",
        )
    )
    pre = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre)
    assert pre.inject_text is not None
    assert "Evidence path correction" in pre.inject_text
    assert "`fake/path.ts`" in pre.inject_text
    assert "`ok.ts`" not in pre.inject_text

    # Cleared after one inject
    pre2 = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre2)
    assert pre2.inject_text is None


@pytest.mark.asyncio
async def test_guard_noop_when_all_paths_exist_and_read(tmp_path: Path) -> None:
    (tmp_path / "a.ts").write_text("1", encoding="utf-8")
    guard = EvidencePathGuard()
    await _record_read(guard, tmp_path, "a.ts")
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Evidence: a.ts\n",
        )
    )
    pre = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre)
    assert pre.inject_text is None


@pytest.mark.asyncio
async def test_guard_flags_existing_but_unread_citation(tmp_path: Path) -> None:
    """F22: citing a real file that was only seen in a search snippet."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "flags.ts").write_text("x", encoding="utf-8")
    guard = EvidencePathGuard()
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Q01: y\nEvidence: src/flags.ts\n",
        )
    )
    pre = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre)
    assert pre.inject_text is not None
    assert "Evidence verification required" in pre.inject_text
    assert "`src/flags.ts`" in pre.inject_text

    # One-shot per path: citing the same unread path again does not re-fire
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Q01: y\nEvidence: src/flags.ts\n",
        )
    )
    pre2 = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre2)
    assert pre2.inject_text is None


@pytest.mark.asyncio
async def test_guard_accepts_glob_confirmation_for_assets(tmp_path: Path) -> None:
    """F22: binary assets confirmed via glob need no read_file."""
    pub = tmp_path / "public"
    pub.mkdir()
    (pub / "og-image.png").write_bytes(b"\x89PNG")
    guard = EvidencePathGuard()
    await guard.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tmp_path,
            tool_name="glob",
            tool_args={"pattern": "public/**"},
            tool_result=json.dumps({"ok": True, "paths": ["public/og-image.png"]}),
        )
    )
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Q39: og image\nEvidence: public/og-image.png\n",
        )
    )
    pre = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre)
    assert pre.inject_text is None


@pytest.mark.asyncio
async def test_guard_read_confirmation_tolerates_prefix(tmp_path: Path) -> None:
    """Cited `repo/src/x.ts` counts as read when read_file used `src/x.ts`."""
    repo = tmp_path / "auriga-web" / "src"
    repo.mkdir(parents=True)
    (repo / "routing.ts").write_text("x", encoding="utf-8")
    guard = EvidencePathGuard()
    await _record_read(guard, tmp_path, "./src/routing.ts")
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Q47: student\nEvidence: auriga-web/src/routing.ts\n",
        )
    )
    pre = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre)
    assert pre.inject_text is None


@pytest.mark.asyncio
async def test_guard_scans_written_file_content(tmp_path: Path) -> None:
    """F22 blind spot: Evidence lines inside a write_file deliverable are checked."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "read.ts").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "unread.ts").write_text("y", encoding="utf-8")
    guard = EvidencePathGuard()
    await _record_read(guard, tmp_path, "src/read.ts")
    await guard.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tmp_path,
            tool_name="write_file",
            tool_args={
                "path": "answers.md",
                "content": (
                    "## Q01\nAnswer: a\nEvidence: src/read.ts\n\n"
                    "## Q02\nAnswer: b\nEvidence: src/unread.ts\n\n"
                    "## Q03\nAnswer: c\nEvidence: fake/missing.ts\n"
                ),
            },
        )
    )
    pre = _payload(HookEvent.PRE_TOOL, tmp_path)
    await guard.on_pre_tool(pre)
    assert pre.inject_text is not None
    assert "Evidence path correction" in pre.inject_text
    assert "`fake/missing.ts`" in pre.inject_text
    assert "Evidence verification required" in pre.inject_text
    assert "`src/unread.ts`" in pre.inject_text
    assert "`src/read.ts`" not in pre.inject_text


@pytest.mark.asyncio
async def test_guard_written_file_counts_as_confirmed(tmp_path: Path) -> None:
    """A file the model wrote itself is a valid citation target."""
    guard = EvidencePathGuard()
    await guard.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tmp_path,
            tool_name="write_file",
            tool_args={"path": "notes/summary.md", "content": "no citations here"},
        )
    )
    (tmp_path / "notes").mkdir()
    (tmp_path / "notes" / "summary.md").write_text("x", encoding="utf-8")
    await guard.on_after_provider(
        _payload(
            HookEvent.AFTER_PROVIDER_RESPONSE,
            tmp_path,
            assistant_text="Done.\nEvidence: notes/summary.md\n",
        )
    )
    pre = _payload(HookEvent.PRE_TURN, tmp_path)
    await guard.on_pre_turn(pre)
    assert pre.inject_text is None


@pytest.mark.asyncio
async def test_guard_scans_replace_in_file_new_string(tmp_path: Path) -> None:
    (tmp_path / "real.ts").write_text("x", encoding="utf-8")
    guard = EvidencePathGuard()
    await guard.on_post_tool(
        _payload(
            HookEvent.POST_TOOL,
            tmp_path,
            tool_name="replace_in_file",
            tool_args={
                "path": "answers.md",
                "old_string": "TODO",
                "new_string": "Q05: z\nEvidence: real.ts\n",
            },
        )
    )
    pre = _payload(HookEvent.PRE_TOOL, tmp_path)
    await guard.on_pre_tool(pre)
    assert pre.inject_text is not None
    assert "Evidence verification required" in pre.inject_text
    assert "`real.ts`" in pre.inject_text


def test_format_evidence_correction_lists_paths() -> None:
    notice = format_evidence_correction(["a/b.ts", "c.md"])
    assert "`a/b.ts`" in notice
    assert "`c.md`" in notice
    assert "Evidence: unknown" in notice


def test_format_verification_notice_lists_paths() -> None:
    notice = format_verification_notice(["src/flags.ts"])
    assert "Evidence verification required" in notice
    assert "`src/flags.ts`" in notice
    assert "read_file" in notice
