"""Tests for :class:`monkeybot.core.tools.core_tool_executor.CoreToolExecutor`."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path

import pytest

from monkeybot.core.config.settings import SubagentConfig
from monkeybot.core.context import LoopsToolRegistry, SkillRef, TurnContext, _discover_skills
from monkeybot.core.llm.provider import Done, TextDelta, ToolCall, UsageEvent
from monkeybot.core.memory.subsystem import MemorySubsystem
from monkeybot.core.mcp.mcp_client import MCPDiagnosticError, MCPServerNotConnectedError
from monkeybot.core.tools.fs_isolation import isolation_support
from monkeybot.core.tools.core_tool_executor import CoreToolExecutor
from monkeybot.core.tools.types import unwrap_tool_execution_result
from monkeybot.core.types.types_tools import ToolDef
from tests.core.memory.helpers import make_memory_subsystem


def _mem_sub(root: Path) -> MemorySubsystem:
    # Palace sqlite files must not live inside the workspace under test (grep/glob).
    resolved = Path(root).resolve()
    return make_memory_subsystem(
        resolved.parent.parent / f"palace-{resolved.parent.name}-{resolved.name}"
    )


class _NoMCP:
    async def connect(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> list[ToolDef]:
        del name, command, args, env
        return []

    async def connect_streamable_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[ToolDef]:
        del name, url, headers
        return []

    async def disconnect(self, name: str) -> None:
        del name

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, object],
    ) -> str:
        del server_name, tool_name, args
        return ""

    def all_tools(self) -> list[ToolDef]:
        return []

    def catalog_names(self) -> list[str]:
        return []

    def known_server_names(self) -> list[str]:
        return []

    def is_connected(self, name: str) -> bool:
        del name
        return False

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        del prefixed_name
        return None

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        del name
        return []

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path, raise_on_error


class _MCPWithBlob:
    """MCP stub returning a large JSON payload with an embedded base64 field."""

    def __init__(self, *, blob_len: int = 1200) -> None:
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        blob = (alphabet * ((blob_len // len(alphabet)) + 1))[:blob_len]
        self._payload = json.dumps({"data": blob})
        self._blob_len = blob_len

    async def connect(
        self,
        name: str,
        command: str,
        args: list[str],
        env: dict[str, str],
    ) -> list[ToolDef]:
        del name, command, args, env
        return []

    async def connect_streamable_http(
        self,
        name: str,
        url: str,
        headers: dict[str, str] | None = None,
    ) -> list[ToolDef]:
        del name, url, headers
        return []

    async def disconnect(self, name: str) -> None:
        del name

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        args: dict[str, object],
    ) -> str:
        del server_name, tool_name, args
        return self._payload

    def all_tools(self) -> list[ToolDef]:
        return []

    def catalog_names(self) -> list[str]:
        return []

    def known_server_names(self) -> list[str]:
        return []

    def is_connected(self, name: str) -> bool:
        del name
        return False

    def split_prefixed_tool(self, prefixed_name: str) -> tuple[str, str] | None:
        if prefixed_name == "srv__capture":
            return ("srv", "capture")
        return None

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        del name
        return []

    async def load_from_config(self, path: Path, *, raise_on_error: bool = False) -> None:
        del path, raise_on_error


def _ctx(
    skills: list[SkillRef] | None = None,
    *,
    event_publisher: object | None = None,
) -> TurnContext:
    kwargs: dict[str, object] = {
        "thread_id": "t",
        "request_id": "r",
        "agent_md": "# Agent",
        "memory_index": [],
        "skills": skills or [],
        "tools": [],
        "user_id": None,
        "parent_run_id": None,
        "model": "gemini-2.5-flash",
    }
    if event_publisher is not None:
        kwargs["event_publisher"] = event_publisher
    return TurnContext(**kwargs)  # type: ignore[arg-type]


def _stub_agent_md_for_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = tmp_path / "AGENT.md"
    agent.write_text("# test agent\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_MD", str(agent))


@pytest.mark.asyncio
async def test_read_file_and_write_file(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    r1, e1 = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="read_file", args={"path": "hello.txt"}),
            ctx=ctx,
        )
    )
    assert e1 is not None
    err1 = json.loads(e1)
    assert err1["ok"] is False
    assert err1["error_kind"] == "validation"
    assert "Not a file" in err1["message"]

    w, ew = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="2",
                name="write_file",
                args={"path": "hello.txt", "content": "abc\n"},
            ),
            ctx=ctx,
        )
    )
    assert ew is None and w is not None and '"ok": true' in w

    r2, e2 = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="3", name="read_file", args={"path": "hello.txt", "limit": 10}),
            ctx=ctx,
        )
    )
    assert e2 is None and r2 is not None and "abc" in r2


@pytest.mark.asyncio
async def test_replace_in_file(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "hello.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="replace_in_file",
                args={
                    "path": "hello.txt",
                    "old_string": "beta",
                    "new_string": "BETA",
                },
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None and '"ok": true' in out
    assert (root / "hello.txt").read_text(encoding="utf-8") == "alpha BETA gamma\n"


@pytest.mark.asyncio
async def test_glob(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "index.html").write_text("<html></html>", encoding="utf-8")
    (root / "notes.md").write_text("# hi", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="glob", args={"pattern": "*.html"}),
            ctx=ctx,
        )
    )
    assert err is None and out is not None and '"ok": true' in out
    assert "index.html" in out
    assert "notes.md" not in out


def test_glob_paths_matches_directories(tmp_path: Path) -> None:
    """Checkout probes must see directories, not only files."""
    from monkeybot.core.tools.workspace_service import WorkspaceFileService

    root = tmp_path
    checkout = root / "repos" / "EPCAP" / "agentic-platform-monorepo"
    checkout.mkdir(parents=True)
    (checkout / "README.md").write_text("hi\n", encoding="utf-8")
    svc = WorkspaceFileService(root)

    exact = svc.glob_paths("EPCAP/agentic-platform-monorepo", root="repos")
    assert exact["ok"] is True
    assert "repos/EPCAP/agentic-platform-monorepo" in exact["paths"]

    wildcard = svc.glob_paths("EPCAP/*", root="repos")
    assert wildcard["ok"] is True
    assert "repos/EPCAP/agentic-platform-monorepo" in wildcard["paths"]


@pytest.mark.asyncio
async def test_glob_incomplete_scan_errors(tmp_path: Path) -> None:
    from monkeybot.core.tools.workspace_service import (
        WorkspaceError,
        WorkspaceFileService,
        WorkspaceSettings,
    )

    root = tmp_path
    for i in range(5):
        (root / f"f{i}.txt").write_text(f"x{i}\n", encoding="utf-8")
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GLOB_MAX_PATHS=2),
    )
    with pytest.raises(WorkspaceError) as ei:
        svc.glob_paths("*.txt")
    assert ei.value.code == "incomplete_scan"
    assert isinstance(ei.value.details, dict)
    assert ei.value.details["stop_reason"] == "max_paths"
    assert ei.value.details["count"] == 2
    assert len(ei.value.details["partial_paths"]) == 2

    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ex._workspace = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GLOB_MAX_PATHS=2),
    )
    ctx = _ctx()
    _out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="glob", args={"pattern": "*.txt"}),
            ctx=ctx,
        )
    )
    assert err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "incomplete_scan"
    assert "cannot be used to conclude absence" in payload["message"]
    assert "Narrow `root`" in payload["hint"]
    assert payload["details"]["count"] == 2
    assert len(payload["details"]["partial_paths"]) == 2


@pytest.mark.asyncio
async def test_grep(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (root / "b.md").write_text("no match here\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="grep",
                args={"pattern": "def foo", "file_glob": "*.py"},
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["scan_complete"] is True
    assert payload["match_count"] >= 1
    assert payload["total_match_count"] >= payload["match_count"]
    assert any(m["path"] == "a.py" for m in payload["matches"])


@pytest.mark.asyncio
async def test_grep_skips_noise_directories(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    for noisy_dir in ("node_modules", ".git", "__pycache__", ".venv", ".monkeybot"):
        d = root / noisy_dir
        d.mkdir()
        (d / "junk.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="grep",
                args={"pattern": "def foo"},
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    matched_paths = {m["path"] for m in payload["matches"]}
    assert matched_paths == {"a.py"}


@pytest.mark.asyncio
async def test_grep_root_file(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "only.py").write_text("unique_marker_xyz\n", encoding="utf-8")
    (root / "other.py").write_text("unique_marker_xyz\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="grep",
                args={"pattern": "unique_marker_xyz", "root": "only.py"},
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["scan_complete"] is True
    assert payload["match_count"] == 1
    assert payload["matches"][0]["path"] == "only.py"


@pytest.mark.asyncio
async def test_grep_brace_glob(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "a.py").write_text("BRACE_HIT\n", encoding="utf-8")
    (root / "b.md").write_text("BRACE_HIT\n", encoding="utf-8")
    (root / "c.txt").write_text("BRACE_HIT\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="grep",
                args={"pattern": "BRACE_HIT", "file_glob": "*.{py,md}"},
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    paths = {m["path"] for m in payload["matches"]}
    assert paths == {"a.py", "b.md"}


@pytest.mark.asyncio
async def test_grep_unparseable_glob(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "a.py").write_text("x\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    _out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="grep",
                args={"pattern": "x", "file_glob": "*.{py"},
            ),
            ctx=ctx,
        )
    )
    assert err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["details"]["code"] == "invalid_file_glob"


@pytest.mark.asyncio
async def test_grep_incomplete_scan_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.tools.workspace_service import (
        WorkspaceError,
        WorkspaceFileService,
        WorkspaceSettings,
    )

    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    root = tmp_path
    for i in range(5):
        (root / f"f{i}.py").write_text(f"MARKER_{i}\n", encoding="utf-8")
    (root / "hit.py").write_text("KNOWN_MATCH_TOKEN\n", encoding="utf-8")
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILES=2, WORKSPACE_GREP_MAX_MATCHES=50),
    )
    with pytest.raises(WorkspaceError) as ei:
        svc.grep("KNOWN_MATCH_TOKEN")
    assert ei.value.code == "incomplete_scan"

    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ex._workspace = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILES=2, WORKSPACE_GREP_MAX_MATCHES=50),
    )
    ctx = _ctx()
    _out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="grep",
                args={"pattern": "KNOWN_MATCH_TOKEN"},
            ),
            ctx=ctx,
        )
    )
    assert err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "incomplete_scan"


@pytest.mark.asyncio
async def test_grep_incomplete_scan_errors_with_rg(tmp_path: Path) -> None:
    """rg path must honor WORKSPACE_GREP_MAX_FILES (not only the Python walker)."""
    import shutil

    from monkeybot.core.tools.workspace_service import (
        WorkspaceError,
        WorkspaceFileService,
        WorkspaceSettings,
    )

    if shutil.which("rg") is None:
        pytest.skip("rg not on PATH")

    root = tmp_path
    for i in range(5):
        (root / f"f{i}.py").write_text(f"MARKER_{i}\n", encoding="utf-8")
    (root / "hit.py").write_text("KNOWN_MATCH_TOKEN\n", encoding="utf-8")
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILES=2, WORKSPACE_GREP_MAX_MATCHES=50),
    )
    with pytest.raises(WorkspaceError) as ei:
        svc.grep("KNOWN_MATCH_TOKEN")
    assert ei.value.code == "incomplete_scan"


@pytest.mark.asyncio
async def test_grep_skipped_oversized_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.tools.workspace_service import (
        WorkspaceError,
        WorkspaceFileService,
        WorkspaceSettings,
    )

    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    root = tmp_path
    (root / "small.py").write_text("nope\n", encoding="utf-8")
    (root / "big.py").write_bytes(b"SECRET_TOKEN\n" + b"x" * 200)
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILE_BYTES=50, WORKSPACE_GREP_MAX_FILES=100),
    )
    with pytest.raises(WorkspaceError) as ei:
        svc.grep("SECRET_TOKEN")
    assert ei.value.code == "incomplete_scan"
    assert ei.value.details is not None
    assert ei.value.details["files_skipped_oversized"] == 1
    assert ei.value.details["stop_reason"] == "skipped_files"

    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ex._workspace = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILE_BYTES=50, WORKSPACE_GREP_MAX_FILES=100),
    )
    _out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="grep", args={"pattern": "SECRET_TOKEN"}),
            ctx=_ctx(),
        )
    )
    assert err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "incomplete_scan"
    assert payload["details"]["files_skipped_oversized"] == 1


@pytest.mark.asyncio
async def test_grep_skipped_binary_is_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.tools.workspace_service import (
        WorkspaceError,
        WorkspaceFileService,
        WorkspaceSettings,
    )

    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    root = tmp_path
    (root / "a.py").write_text("hello\n", encoding="utf-8")
    (root / "blob.bin").write_bytes(b"abc\x00defSECRET")
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILES=100),
    )
    with pytest.raises(WorkspaceError) as ei:
        svc.grep("SECRET")
    assert ei.value.code == "incomplete_scan"
    assert ei.value.details is not None
    assert ei.value.details["files_skipped_binary"] == 1


@pytest.mark.asyncio
async def test_grep_skipped_oversized_incomplete_with_rg(tmp_path: Path) -> None:
    import shutil

    from monkeybot.core.tools.workspace_service import (
        WorkspaceError,
        WorkspaceFileService,
        WorkspaceSettings,
    )

    if shutil.which("rg") is None:
        pytest.skip("rg not on PATH")

    root = tmp_path
    (root / "small.py").write_text("nope\n", encoding="utf-8")
    (root / "big.py").write_bytes(b"SECRET_TOKEN\n" + b"x" * 200)
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_FILE_BYTES=50, WORKSPACE_GREP_MAX_FILES=100),
    )
    with pytest.raises(WorkspaceError) as ei:
        svc.grep("SECRET_TOKEN")
    assert ei.value.code == "incomplete_scan"
    assert ei.value.details is not None
    assert ei.value.details["files_skipped_oversized"] == 1


@pytest.mark.asyncio
async def test_grep_rg_timeout_is_incomplete_no_python_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rg TimeoutExpired must not fall through to unbounded Python walk."""
    import shutil
    import subprocess

    from monkeybot.core.tools.workspace_service import WorkspaceError, WorkspaceFileService

    if shutil.which("rg") is None:
        pytest.skip("rg not on PATH")

    root = tmp_path
    (root / "a.py").write_text("needle\n", encoding="utf-8")
    python_calls = {"n": 0}
    real_grep_python = WorkspaceFileService._grep_python

    def _track_python(self, *args, **kwargs):  # noqa: ANN001
        python_calls["n"] += 1
        return real_grep_python(self, *args, **kwargs)

    def _timeout_run(*_a, **_kw):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=["rg"], timeout=120)

    monkeypatch.setattr(WorkspaceFileService, "_grep_python", _track_python)
    monkeypatch.setattr(
        "monkeybot.core.tools.workspace_service.subprocess.run",
        _timeout_run,
    )
    svc = WorkspaceFileService(root)
    with pytest.raises(WorkspaceError) as ei:
        svc.grep("needle")
    assert ei.value.code == "incomplete_scan"
    assert ei.value.details is not None
    assert ei.value.details["stop_reason"] == "rg_timed_out"
    assert python_calls["n"] == 0

    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ex._workspace = WorkspaceFileService(root)
    _out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="grep", args={"pattern": "needle"}),
            ctx=_ctx(),
        )
    )
    assert err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "incomplete_scan"
    assert payload["details"]["stop_reason"] == "rg_timed_out"


@pytest.mark.asyncio
async def test_grep_capped_complete_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from monkeybot.core.tools.workspace_service import WorkspaceFileService, WorkspaceSettings

    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    root = tmp_path
    (root / "a.py").write_text("HIT\nHIT\nHIT\n", encoding="utf-8")
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_MATCHES=2, WORKSPACE_GREP_MAX_FILES=100),
    )
    page1 = svc.grep("HIT", max_matches=2, offset=0)
    assert page1["ok"] is True
    assert page1["scan_complete"] is True
    assert page1["match_count"] == 2
    assert page1["total_match_count"] == 3
    assert page1.get("next_offset") == 2
    page2 = svc.grep("HIT", max_matches=2, offset=page1["next_offset"])
    assert page2["match_count"] == 1
    assert page2["total_match_count"] == 3
    assert "next_offset" not in page2


@pytest.mark.asyncio
async def test_grep_rg_and_python_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from monkeybot.core.tools.workspace_service import WorkspaceFileService, WorkspaceSettings

    root = tmp_path
    (root / "a.py").write_text("parity_token_alpha\n", encoding="utf-8")
    (root / "b.md").write_text("parity_token_alpha\n", encoding="utf-8")
    (root / "c.txt").write_text("other\n", encoding="utf-8")
    mb = root / ".monkeybot"
    mb.mkdir()
    (mb / "noise.py").write_text("parity_token_alpha\n", encoding="utf-8")
    settings = WorkspaceSettings(WORKSPACE_GREP_MAX_MATCHES=50, WORKSPACE_GREP_MAX_FILES=100)
    svc = WorkspaceFileService(root, settings=settings)

    if shutil.which("rg") is None:
        pytest.skip("rg not on PATH")

    with_rg = svc.grep("parity_token_alpha", file_glob="*.{py,md}")
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    without_rg = svc.grep("parity_token_alpha", file_glob="*.{py,md}")

    assert with_rg["ok"] is True and without_rg["ok"] is True
    assert with_rg.keys() == without_rg.keys()
    assert {m["path"] for m in with_rg["matches"]} == {m["path"] for m in without_rg["matches"]}
    assert with_rg["total_match_count"] == without_rg["total_match_count"]
    assert with_rg["match_count"] == without_rg["match_count"]
    assert with_rg["scan_complete"] is True and without_rg["scan_complete"] is True
    assert all(not m["path"].startswith(".monkeybot") for m in with_rg["matches"])


async def test_grep_path_glob_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Path-style file_glob must match the same files with and without rg."""
    import shutil

    from monkeybot.core.tools.workspace_service import WorkspaceFileService, WorkspaceSettings

    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "nested").mkdir()
    (root / "src" / "nested" / "deep.py").write_text("PATH_GLOB_HIT\n", encoding="utf-8")
    (root / "src" / "top.py").write_text("PATH_GLOB_HIT\n", encoding="utf-8")
    (root / "other").mkdir()
    (root / "other" / "side.py").write_text("PATH_GLOB_HIT\n", encoding="utf-8")
    (root / "other" / "test_x.py").write_text("PATH_GLOB_HIT\n", encoding="utf-8")
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_MATCHES=50, WORKSPACE_GREP_MAX_FILES=100),
    )
    rg_bin = shutil.which("rg")

    def _paths(result: dict) -> set[str]:
        return {m["path"] for m in result["matches"]}

    # Python-only: path globs and basename globs.
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    assert _paths(svc.grep("PATH_GLOB_HIT", file_glob="src/**/*.py")) == {
        "src/nested/deep.py",
        "src/top.py",
    }
    assert _paths(svc.grep("PATH_GLOB_HIT", file_glob="src/*.py")) == {"src/top.py"}
    assert _paths(svc.grep("PATH_GLOB_HIT", file_glob="test_*.py")) == {"other/test_x.py"}

    if rg_bin is None:
        pytest.skip("rg not on PATH")

    monkeypatch.undo()
    with_rg = svc.grep("PATH_GLOB_HIT", file_glob="src/**/*.py")
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    without_rg = svc.grep("PATH_GLOB_HIT", file_glob="src/**/*.py")
    assert _paths(with_rg) == _paths(without_rg) == {"src/nested/deep.py", "src/top.py"}

    monkeypatch.undo()
    with_rg = svc.grep("PATH_GLOB_HIT", file_glob="src/*.py")
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    without_rg = svc.grep("PATH_GLOB_HIT", file_glob="src/*.py")
    assert _paths(with_rg) == _paths(without_rg) == {"src/top.py"}

    monkeypatch.undo()
    with_rg = svc.grep("PATH_GLOB_HIT", file_glob="test_*.py")
    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    without_rg = svc.grep("PATH_GLOB_HIT", file_glob="test_*.py")
    assert _paths(with_rg) == _paths(without_rg) == {"other/test_x.py"}


def test_clip_grep_match_line_centers_on_match() -> None:
    from monkeybot.core.tools.workspace_service import _clip_grep_match_line

    needle = "chore/dead-code"
    prefix = "x" * 2500
    suffix = "y" * 2500
    line = prefix + needle + suffix
    start = len(prefix)
    end = start + len(needle)

    clipped = _clip_grep_match_line(line, start, end, max_chars=2000)
    assert needle in clipped
    assert len(clipped) <= 2000
    assert clipped.startswith("…")
    assert clipped.endswith("…")


def test_clip_grep_match_line_short_unchanged() -> None:
    from monkeybot.core.tools.workspace_service import _clip_grep_match_line

    line = "short line with needle here"
    assert _clip_grep_match_line(line, 16, 22, max_chars=2000) == line


def test_clip_grep_match_line_missing_start_is_prefix() -> None:
    from monkeybot.core.tools.workspace_service import _clip_grep_match_line

    line = "a" * 3000
    assert _clip_grep_match_line(line, None, None, max_chars=2000) == line[:2000]


def test_utf8_byte_offsets_to_char_offsets_for_rg() -> None:
    """rg submatches are UTF-8 byte offsets; clipping must use character indices."""
    from monkeybot.core.tools.workspace_service import (
        _clip_grep_match_line,
        _utf8_byte_offsets_to_char_offsets,
    )

    needle = "NEEDLE"
    # ``ä`` is 2 UTF-8 bytes; enough multibyte prefix that byte≠char indices diverge.
    prefix = "ä" * 1500
    suffix = "x" * 1500
    line = prefix + needle + suffix
    byte_start = len(prefix.encode("utf-8"))
    byte_end = byte_start + len(needle.encode("utf-8"))
    assert byte_start != len(prefix)

    char_start, char_end = _utf8_byte_offsets_to_char_offsets(line, byte_start, byte_end)
    assert char_start == len(prefix)
    assert char_end == len(prefix) + len(needle)

    # Using raw byte offsets as char indices would miss the needle in the window.
    wrong = _clip_grep_match_line(line, byte_start, byte_end, max_chars=200)
    assert needle not in wrong

    clipped = _clip_grep_match_line(line, char_start, char_end, max_chars=200)
    assert needle in clipped


def test_grep_long_line_preview_contains_needle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mid-line needles past the 2000-char prefix must appear in match text."""
    import shutil

    from monkeybot.core.tools.workspace_service import WorkspaceFileService, WorkspaceSettings

    root = tmp_path
    needle = "chore/dead-code"
    (root / "spill.json").write_text(
        ("{" + '"pad":"' + ("z" * 4000) + '","ref":"' + needle + '"}'),
        encoding="utf-8",
    )
    svc = WorkspaceFileService(
        root,
        settings=WorkspaceSettings(WORKSPACE_GREP_MAX_MATCHES=50, WORKSPACE_GREP_MAX_FILES=100),
    )
    rg_bin = shutil.which("rg")

    monkeypatch.setattr("monkeybot.core.tools.workspace_service.shutil.which", lambda _name: None)
    py_result = svc.grep(needle)
    assert py_result["ok"] is True
    assert py_result["match_count"] >= 1
    assert any(needle in m["text"] for m in py_result["matches"])

    if rg_bin is None:
        return

    monkeypatch.undo()
    rg_result = svc.grep(needle)
    assert rg_result["ok"] is True
    assert rg_result["match_count"] >= 1
    assert any(needle in m["text"] for m in rg_result["matches"])


@pytest.mark.asyncio
async def test_apply_patch_tool(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "old.txt").write_text("old\n", encoding="utf-8")
    (root / "gone.txt").write_text("bye\n", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    patch = """*** Begin Patch
*** Add File: new.txt
+hello
*** Update File: old.txt
@@
-old
+new
*** Delete File: gone.txt
*** End Patch
"""
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="apply_patch", args={"patch_text": patch}),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert (root / "new.txt").read_text(encoding="utf-8") == "hello\n"
    assert (root / "old.txt").read_text(encoding="utf-8") == "new\n"
    assert not (root / "gone.txt").exists()

    # Fail-closed: bad hunk must not create partial files.
    bad = """*** Begin Patch
*** Add File: should_not_exist.txt
+x
*** Update File: missing.txt
@@
-a
+b
*** End Patch
"""
    out2, err2 = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="2", name="apply_patch", args={"patch_text": bad}),
            ctx=ctx,
        )
    )
    assert err2 is not None
    assert not (root / "should_not_exist.txt").exists()


@pytest.mark.asyncio
async def test_search_hits_knowledge_notes(tmp_path: Path) -> None:
    from monkeybot.core.knowledge import KnowledgeSubsystem
    from monkeybot.core.knowledge.types import KnowledgeSettings

    root = tmp_path / "ws"
    root.mkdir()
    (root / "policy.md").write_text(
        "Refund policy for annual plans requires approval.\n", encoding="utf-8"
    )
    knowledge_root = tmp_path / ".monkeybot" / "knowledge"
    notes = knowledge_root / "notes"
    notes.mkdir(parents=True)
    (notes / "refund.md").write_text(
        "Annual refunds.\n\n[[workspace:policy.md#L1-1]]\n",
        encoding="utf-8",
    )
    settings = KnowledgeSettings(
        enabled=True,
        knowledge_root=str(knowledge_root),
        index_path=str(knowledge_root / "index.sqlite"),
        debounce_ms=0,
        startup_scan=True,
        default_limit=8,
    )
    knowledge = await KnowledgeSubsystem.create(
        workspace_root=root,
        settings=settings,
        knowledge_root=knowledge_root,
        index_path=Path(settings.index_path),
    )
    await knowledge.ensure_ready()
    skills = tmp_path / "skills"
    skills.mkdir()
    try:
        ex = CoreToolExecutor(
            workspace_root=root,
            memory=_mem_sub(tmp_path / "memory"),
            knowledge=knowledge,
            skills_path=skills,
            mcp=_NoMCP(),
        )
        out, err = unwrap_tool_execution_result(
            await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="search",
                    args={"query": "annual refund approval"},
                ),
                ctx=_ctx(),
            )
        )
        assert err is None and out is not None
        payload = json.loads(out)
        assert payload["ok"] is True
        assert payload["hits"]
    finally:
        await knowledge.close()


@pytest.mark.asyncio
async def test_search_without_knowledge_returns_validation_error(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        knowledge=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="search", args={"query": "x"}),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    assert "knowledge" in err.lower() or "search" in err.lower()


@pytest.mark.asyncio
async def test_list_skills_echoes_context_skill_refs(tmp_path: Path) -> None:
    """``list_skills`` returns whatever is already on ``TurnContext.skills`` (no disk read)."""
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    sk = [SkillRef(name="n", description="d")]
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="list_skills", args={}),
            ctx=_ctx(skills=sk),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["skills"] == [{"name": "n", "description": "d"}]


@pytest.mark.asyncio
async def test_list_skills_returns_descriptions_from_discovered_skill_md(tmp_path: Path) -> None:
    """End-to-end: SKILL.md frontmatter description must surface in list_skills JSON."""
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    image_gen = skills / "image-generator"
    image_gen.mkdir(parents=True)
    (image_gen / "SKILL.md").write_text(
        "---\n"
        "name: image-generator\n"
        "description: Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat.\n"
        "---\n\n"
        "# image-generator\n\n"
        "Procedural body the list_skills tool must not use as the short description.\n",
        encoding="utf-8",
    )

    discovered = _discover_skills(skills)
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="list_skills", args={}),
            ctx=_ctx(skills=discovered),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["skills"] == [
        {
            "name": "image-generator",
            "description": (
                "Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat."
            ),
        }
    ]
    assert payload["skills"][0]["description"] != "# image-generator"


@pytest.mark.asyncio
async def test_run_command_cat_under_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent_dir = tmp_path / "agent"
    ws = agent_dir / "workspace"
    ws.mkdir(parents=True)
    mem = agent_dir / "memory"
    mem.mkdir(parents=True)
    (mem / "f.md").write_text("inside", encoding="utf-8")
    skills = ws / "skills"
    skills.mkdir()
    monkeypatch.chdir(ws)
    ex = CoreToolExecutor(
        workspace_root=ws,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={"argv": ["cat", "../memory/f.md"]},
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None and "inside" in out


@pytest.mark.asyncio
async def test_run_command_mempalace_is_blocked_when_memory_unavailable(tmp_path: Path) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )

    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="memory-disabled",
                name="run_command",
                args={"argv": ["mempalace", "search", "private"]},
            ),
            ctx=_ctx(),
        )
    )

    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["error_kind"] == "policy"
    assert "unavailable" in payload["message"].lower()


@pytest.mark.asyncio
async def test_run_command_drops_mempalace_capability_when_memory_unavailable(
    tmp_path: Path,
) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )

    assert "mempalace" not in ex._run_cmd_allowed_commands
    assert "mempalace" not in ex._terminal.allowed_commands


@pytest.mark.skipif(
    not isolation_support().available,
    reason=f"host cannot isolate filesystems: {isolation_support().detail}",
)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_id", "argv_template"),
    [
        ("shell-variable", ["bash", "-c", 'p={secret}; cat "$p"']),
        ("interpreter-io", ["python", "-c", "print(open({secret!r}).read())"]),
        ("nested-launcher", ["bash", "-c", "cat {secret}"]),
    ],
)
async def test_disabled_memory_is_unreachable_through_launchers(
    tmp_path: Path, call_id: str, argv_template: list[str]
) -> None:
    """Shells and interpreters can build any path, so the palace must be hidden."""
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    skills.mkdir(parents=True)
    secret = tmp_path / "memory" / "private.txt"
    secret.parent.mkdir()
    secret.write_text("PRIVATE-CONTENT", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=workspace,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
        run_command_allowed_path_prefixes=[str(tmp_path)],
    )

    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id=call_id,
                name="run_command",
                args={"argv": [part.format(secret=str(secret)) for part in argv_template]},
            ),
            ctx=dataclasses.replace(_ctx(), user_id="u"),
        )
    )

    assert "PRIVATE-CONTENT" not in (out or "") + (err or "")


@pytest.mark.skipif(
    not isolation_support().available,
    reason=f"host cannot isolate filesystems: {isolation_support().detail}",
)
@pytest.mark.asyncio
async def test_enabled_memory_remains_readable_through_launchers(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    skills = workspace / "skills"
    skills.mkdir(parents=True)
    secret = tmp_path / "memory" / "private.txt"
    secret.parent.mkdir()
    secret.write_text("PRIVATE-CONTENT", encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=workspace,
        memory=_mem_sub(tmp_path / "memory"),
        skills_path=skills,
        mcp=_NoMCP(),
        run_command_allowed_path_prefixes=[str(tmp_path)],
    )

    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="memory-enabled-read",
                name="run_command",
                args={"argv": ["bash", "-c", f"cat {secret}"]},
            ),
            ctx=dataclasses.replace(_ctx(), user_id="u"),
        )
    )

    assert err is None and out is not None
    assert "PRIVATE-CONTENT" in out


@pytest.mark.asyncio
async def test_run_command_memory_path_is_blocked_when_memory_unavailable(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    private = tmp_path / "memory" / "private.txt"
    private.parent.mkdir()
    private.write_text("PRIVATE-CONTENT", encoding="utf-8")
    skills = workspace / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=workspace,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )

    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="memory-path-disabled",
                name="run_command",
                args={"argv": ["cat", "../memory/private.txt"]},
            ),
            ctx=dataclasses.replace(_ctx(), user_id="u"),
        )
    )

    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["error_kind"] == "policy"
    assert "PRIVATE-CONTENT" not in err
    assert "../memory/private.txt" in payload["message"]


@pytest.mark.asyncio
async def test_direct_mempalace_route_is_owned_by_each_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.tools.terminal import ExecutionResult

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skills = workspace / "skills"
    skills.mkdir()
    palace_a = _mem_sub(tmp_path / "memory-a")
    palace_b = _mem_sub(tmp_path / "memory-b")
    seen: list[dict[str, str]] = []

    async def fake_execute(
        self,
        command,
        args,
        timeout=60,
        *,
        cwd=None,
        env_overrides=None,
    ):
        del self, command, args, timeout, cwd
        seen.append(dict(env_overrides or {}))
        return ExecutionResult(stdout="ok", stderr="", exit_code=0)

    monkeypatch.setattr(TerminalExecutor, "execute", fake_execute)
    monkeypatch.setenv("MEMPALACE_PALACE_PATH", str(palace_b.palace_path))
    executor_a = CoreToolExecutor(
        workspace_root=workspace,
        memory=palace_a,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    executor_b = CoreToolExecutor(
        workspace_root=workspace,
        memory=palace_b,
        skills_path=skills,
        mcp=_NoMCP(),
    )

    for executor, call_id in ((executor_a, "palace-a"), (executor_b, "palace-b")):
        out, err = unwrap_tool_execution_result(
            await executor.execute(
                call=ToolCall(
                    call_id=call_id,
                    name="run_command",
                    args={"argv": ["mempalace", "search", "query"]},
                ),
                ctx=dataclasses.replace(_ctx(), user_id="u"),
            )
        )
        assert err is None and out is not None

    assert [entry["MEMPALACE_PALACE_PATH"] for entry in seen] == [
        str(palace_a.palace_path),
        str(palace_b.palace_path),
    ]
    assert seen[0]["MEMPALACE_PALACE_PATH"] != str(palace_b.palace_path)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["bash", "-c", "echo shell-ok"], "shell-ok"),
        (["python", "-c", "print('python-ok')"], "python-ok"),
        (["git", "--version"], "git version"),
        (["uv", "--version"], "uv "),
        (["gh", "--version"], "gh version"),
    ],
)
async def test_run_command_launchers_remain_available_without_memory(
    tmp_path: Path,
    argv: list[str],
    expected: str,
) -> None:
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )

    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="memory-unavailable",
                name="run_command",
                args={"argv": argv},
            ),
            ctx=_ctx(),
        )
    )

    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert expected in payload["stdout"]


@pytest.mark.asyncio
async def test_run_command_blocked_command_returns_policy_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={"argv": ["curl", "http://example.com"]},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "policy"
    assert "curl" in payload["message"].lower() or "not allowed" in payload["message"].lower()
    assert "example_argv" in payload["details"]
    assert "allowed_commands" in payload["details"]


@pytest.mark.asyncio
async def test_run_command_uv_allowed_by_binary_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``uv`` is on the binary allowlist; install subcommands are blocked by deny_patterns in the loop inspector."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={"argv": ["uv", "--version"]},
            ),
            ctx=_ctx(),
        )
    )
    assert out is not None and err is None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["exit_code"] == 0


@pytest.mark.asyncio
async def test_run_command_blocked_path_returns_policy_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data" / "memory").mkdir(parents=True)
    mem = tmp_path / "data" / "memory"
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={"argv": ["cat", "./forbidden/x.txt"]},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "policy"
    assert "allowed_path_prefixes" in payload["details"]


@pytest.mark.asyncio
async def test_run_command_malformed_args_returns_validation_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="run_command", args={}),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "validation"
    assert "example" in payload["details"]


@pytest.mark.asyncio
async def test_run_command_timeout_hint_rejects_timeout_bump_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timeout guidance must not push 'raise timeout and retry the same argv'."""
    from unittest.mock import AsyncMock, MagicMock

    from monkeybot.core.tools.terminal import CommandTimeoutError

    monkeypatch.chdir(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    terminal = MagicMock()
    terminal.allowed_commands = ("python3",)
    terminal.allowed_path_prefixes = ("./",)
    terminal.execute = AsyncMock(
        side_effect=CommandTimeoutError(
            "Command exceeded 300s timeout",
            timeout=300,
            stdout="creating database test_epsilon_test\n",
            stderr="still migrating…\n",
        )
    )
    terminal.aclose = AsyncMock()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        terminal=terminal,
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="1",
                name="run_command",
                args={"argv": ["python3", "-m", "pytest"], "timeout": 300},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert payload["error_kind"] == "runtime"
    assert "300s" in payload["message"]
    hint = payload["hint"].lower()
    assert "partial state" in hint or "left partial" in hint
    assert "not a valid recovery" in hint
    assert "larger timeout" in hint
    assert "increase" not in hint
    assert "read_file" in hint
    details = payload["details"]
    assert "example" not in details
    assert "avoid" not in details
    assert "prefer" not in details
    spill_rel = details["partial_output_path"]
    assert spill_rel.endswith("-timeout.txt")
    assert details["stdout_chars"] > 0
    assert details["stderr_chars"] > 0
    assert "test_epsilon_test" in details["partial_output_tail"]
    # Full streams live on disk — not dumped as the error body.
    spill_body = (tmp_path / spill_rel).read_text(encoding="utf-8")
    assert "test_epsilon_test" in spill_body
    assert "still migrating" in spill_body
    assert spill_rel in payload["hint"]


@pytest.mark.asyncio
async def test_run_command_timeout_spills_partial_output_from_real_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: killed process output is spilled; envelope points at the path."""
    monkeypatch.chdir(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="to1",
                name="run_command",
                args={
                    "argv": [
                        "python3",
                        "-c",
                        "import sys, time; print('before-hang', flush=True); time.sleep(60)",
                    ],
                    "timeout": 1,
                },
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["error_kind"] == "runtime"
    spill_rel = payload["details"]["partial_output_path"]
    spill_text = (tmp_path / spill_rel).read_text(encoding="utf-8")
    assert "before-hang" in spill_text
    assert "before-hang" in payload["details"].get("partial_output_tail", "")
    # Envelope itself stays small — no multi-MB dump of streams at top level.
    assert "before-hang" not in payload["message"]
    assert len(json.dumps(payload)) < 8_000


@pytest.mark.asyncio
async def test_unknown_tool(tmp_path: Path) -> None:
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "m"),
        skills_path=tmp_path / "s",
        mcp=_NoMCP(),
    )
    (tmp_path / "s").mkdir(exist_ok=True)
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="1", name="not_a_real_tool", args={}),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    err_obj = json.loads(err)
    assert err_obj["ok"] is False
    assert err_obj["error_kind"] == "runtime"
    assert "unknown tool" in err_obj["message"]


@pytest.mark.asyncio
async def test_task_tool_aggregates_subagent_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import (
        AssistantDelta,
        ToolCallResult,
        ToolCallStarted,
        TurnComplete,
        UsageTotals,
    )

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        assert envelope.task == "do the thing"
        assert "ctx line" in envelope.context
        assert envelope.memory_storage_uri.startswith("local://")
        yield ToolCallStarted(request_id="r", tool="search", label="search", args={})
        yield ToolCallResult(request_id="r", tool="search", result="hit one", error=None)
        yield AssistantDelta(request_id="r", delta="partial")
        yield AssistantDelta(request_id="r", delta=" answer")
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=3,
                output_tokens=2,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=10,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c99",
                name="task",
                args={"task": "do the thing", "context": "ctx line"},
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["assistant_text"] == "partial answer"
    assert payload["final_message"] == "partial answer"
    assert payload["tool_call_count"] == 1
    assert payload["tool_results"] == [{"tool": "search", "snippet": "hit one"}]
    assert payload["usage"]["input_tokens"] == 3


@pytest.mark.asyncio
async def test_task_tool_resolves_subagent_type_agent_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    agents = tmp_path / "monkeybot_config" / "agents"
    agents.mkdir(parents=True)
    impl_md = agents / "researcher.md"
    impl_md.write_text("# researcher persona\n", encoding="utf-8")
    default_md = tmp_path / "monkeybot_config" / "AGENT.md"
    default_md.write_text("# parent\n", encoding="utf-8")

    seen_agent_md: list[str | None] = []

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        assert envelope.subagent_type == "researcher"
        seen_agent_md.append(envelope.agent_md)
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))

    registry = {
        "researcher": SubagentConfig(
            name="researcher",
            description="research",
            skills=[],
            agent_md="./monkeybot_config/agents/researcher.md",
        )
    }
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        subagent_registry=registry,
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-persona",
                name="task",
                args={"task": "research topic", "subagent_type": "researcher"},
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    assert seen_agent_md == [str(impl_md.resolve())]


@pytest.mark.asyncio
async def test_task_tool_unknown_subagent_type_returns_validation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        subagent_registry={},
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-bad",
                name="task",
                args={"task": "work", "subagent_type": "nope"},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    payload = json.loads(err)
    assert payload["error_kind"] == "validation"
    assert "Unknown subagent_type" in payload["message"]


@pytest.mark.asyncio
async def test_task_tool_spawns_without_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    seen_uri: list[str] = []

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        assert envelope.memory_storage_uri == ""
        seen_uri.append(envelope.memory_storage_uri)
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=None,
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="c1", name="task", args={"task": "summarize logs"}),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    assert seen_uri == [""]
    payload = json.loads(out)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_task_tool_parent_cancel_stops_hanging_subagent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hang = asyncio.Event()

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event
        await hang.wait()
        if False:
            yield  # pragma: no cover

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    parent_cancel = asyncio.Event()
    ctx = dataclasses.replace(_ctx(), cancelled=parent_cancel)

    exec_task = asyncio.create_task(
        ex.execute(
            call=ToolCall(
                call_id="c1",
                name="task",
                args={"task": "never finishes", "context": ""},
            ),
            ctx=ctx,
        )
    )
    await asyncio.sleep(0.05)
    parent_cancel.set()
    try:
        out, err = unwrap_tool_execution_result(await asyncio.wait_for(exec_task, timeout=5.0))
    finally:
        hang.set()

    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is False
    assert any("cancelled (parent)" in e for e in payload["errors"])


@pytest.mark.asyncio
async def test_write_spill_with_inventory_writes_full_payload(tmp_path: Path) -> None:
    from monkeybot.core.tools.spill_inventory import write_spill_with_inventory

    body = "x" * 25_000
    out = write_spill_with_inventory(body, tmp_path, "th1", "call-1", tool_name="run_command")
    spill = tmp_path / ".monkeybot" / "spill" / "th1" / "call-1.txt"
    assert spill.read_text(encoding="utf-8") == body
    assert body not in out
    assert "Spill inventory" in out
    assert "25000 total chars" in out
    assert "Preview:" in out
    assert ".monkeybot/spill/th1/call-1.txt" in out


@pytest.mark.asyncio
async def test_list_skills_spills_large_json(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    big_skills = [SkillRef(name=f"s{i}", description="d" * 400) for i in range(80)]
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    ctx = _ctx(skills=big_skills)
    out, err = unwrap_tool_execution_result(
        await ex.execute(call=ToolCall(call_id="c-spill", name="list_skills", args={}), ctx=ctx)
    )
    assert err is None and out is not None
    assert "Spill inventory" in out
    # Soft spill inlines a body prefix; preview is omitted when body is present.
    assert "Preview:" not in out
    spill = root / ".monkeybot" / "spill" / "t" / "c-spill.txt"
    assert spill.is_file()
    raw = spill.read_text(encoding="utf-8")
    assert len(raw) > 20_000
    assert raw[:200] in out


@pytest.mark.asyncio
async def test_list_skills_small_no_spill(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    ex = CoreToolExecutor(
        workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP()
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(call=ToolCall(call_id="c1", name="list_skills", args={}), ctx=ctx)
    )
    assert err is None and out is not None
    assert not (root / ".monkeybot" / "spill").exists()


@pytest.mark.asyncio
async def test_read_file_spill_path_caps_limit(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    spill = root / ".monkeybot" / "spill" / "t" / "big.txt"
    spill.parent.mkdir(parents=True)
    spill.write_text("\n".join(f"line{i}" for i in range(600)), encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP()
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="r1",
                name="read_file",
                args={"path": ".monkeybot/spill/t/big.txt", "offset": 1, "limit": 10_000},
            ),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["end_line"] - payload["start_line"] + 1 == 600
    assert "line0" in payload["content"]
    assert "omitted" not in payload["content"]


@pytest.mark.asyncio
async def test_read_file_preserves_large_content_through_ingress(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    (root / "big.txt").write_text("hello world\n" * 200, encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP()
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="rf-big", name="read_file", args={"path": "big.txt", "limit": 50}
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert "hello world" in payload["content"]
    assert "omitted" not in payload["content"]


@pytest.mark.asyncio
async def test_read_file_preserves_embedded_base64_blob(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    blob = (alphabet * 20)[:1200]
    (root / "fixture.json").write_text(json.dumps({"token": blob}), encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP()
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="rf-b64", name="read_file", args={"path": "fixture.json"}),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert blob[:80] in payload["content"]
    assert "base64 run" not in payload["content"]


@pytest.mark.asyncio
async def test_spill_writes_raw_payload_before_sanitize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del monkeypatch  # env sizing knobs are retired; threshold is window-derived
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    # Payload must exceed window-derived spill_threshold (~16k at 200k window).
    mcp = _MCPWithBlob(blob_len=20_000)
    ex = CoreToolExecutor(workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=mcp)
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="mcp-blob", name="srv__capture", args={}),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    assert "Spill inventory" in out
    spill = root / ".monkeybot" / "spill" / "t" / "mcp-blob.txt"
    assert spill.is_file()
    raw = spill.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert len(parsed["data"]) == 20_000
    assert "omitted" not in parsed["data"]
    # History is sanitized; raw base64 must not survive in the inline body.
    assert "omitted" in out
    assert parsed["data"][:80] not in out


@pytest.mark.asyncio
async def test_read_file_non_spill_uses_workspace_defaults(tmp_path: Path) -> None:
    root = tmp_path
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    p = root / "wide.txt"
    p.write_text("\n".join(f"L{i}" for i in range(400)), encoding="utf-8")
    ex = CoreToolExecutor(
        workspace_root=root, memory=_mem_sub(mem), skills_path=skills, mcp=_NoMCP()
    )
    ctx = _ctx()
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="r2", name="read_file", args={"path": "wide.txt"}),
            ctx=ctx,
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["end_line"] - payload["start_line"] + 1 <= 2000


# Removed in story-3-providers-and-snapshots: helper deleted

# ---------------------------------------------------------------------------
# Sandbox executor selection and aclose() lifecycle
# ---------------------------------------------------------------------------

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from monkeybot.core.tools.sandbox_executor import SandboxConfig, SandboxExecutor
from monkeybot.core.tools.terminal import TerminalExecutor


def _make_executor(tmp_path: Path) -> CoreToolExecutor:
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    return CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )


def _make_mock_sandbox_cls():
    sandbox = MagicMock()
    sandbox.id = "s1"
    sandbox.commands.run = AsyncMock(
        return_value=MagicMock(
            exit_code=0,
            logs=MagicMock(stdout=[], stderr=[]),
        )
    )
    sandbox.kill = AsyncMock()
    mock_cls = MagicMock()
    mock_cls.create = AsyncMock(return_value=sandbox)
    return mock_cls, sandbox


def _make_opensandbox_module(mock_cls):
    """Build a minimal opensandbox mock that satisfies all _ensure_sandbox imports."""
    mod = MagicMock()
    mod.Sandbox = mock_cls
    mod.config = MagicMock()
    mod.config.ConnectionConfig = MagicMock(side_effect=lambda **kw: MagicMock(**kw))

    class _Volume:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    class _Host:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    mod.models = MagicMock()
    mod.models.sandboxes = MagicMock()
    mod.models.sandboxes.Volume = _Volume
    mod.models.sandboxes.Host = _Host

    execd_mod = ModuleType("opensandbox.models.execd")

    class _RunCommandOpts:
        def __init__(
            self,
            *,
            timeout=None,
            background=False,
            working_directory=None,
            uid=None,
            gid=None,
            envs=None,
        ):
            self.timeout = timeout
            self.background = background
            self.working_directory = working_directory
            self.uid = uid
            self.gid = gid
            self.envs = envs

    execd_mod.RunCommandOpts = _RunCommandOpts
    mod.models.execd = execd_mod
    return mod


def _osb_patches(mock_cls):
    osb = _make_opensandbox_module(mock_cls)
    return osb, {
        "opensandbox": osb,
        "opensandbox.config": osb.config,
        "opensandbox.models.sandboxes": osb.models.sandboxes,
        "opensandbox.models.execd": osb.models.execd,
    }


class TestCoreToolExecutorSandboxSelection:
    """Verify that the correct executor type is chosen at init time."""

    def test_default_no_env_uses_terminal_executor(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
        ex = _make_executor(tmp_path)
        assert isinstance(ex._terminal, TerminalExecutor)

    def test_sandbox_enabled_false_uses_terminal_executor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "false")
        ex = _make_executor(tmp_path)
        assert isinstance(ex._terminal, TerminalExecutor)

    def test_sandbox_enabled_true_uses_sandbox_executor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        ex = _make_executor(tmp_path)
        assert isinstance(ex._terminal, SandboxExecutor)

    def test_explicit_terminal_injection_bypasses_sandbox_env(self, tmp_path, monkeypatch):
        # Tests that inject a terminal= override must still work regardless of env.
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        injected = TerminalExecutor()
        mem = tmp_path / "mem"
        mem.mkdir()
        skills = tmp_path / "skills"
        skills.mkdir()
        ex = CoreToolExecutor(
            workspace_root=tmp_path,
            memory=_mem_sub(mem),
            skills_path=skills,
            mcp=_NoMCP(),
            terminal=injected,
        )
        assert isinstance(ex._terminal, TerminalExecutor)
        assert ex._host_terminal is None
        # Memory is enabled here, so the injected policy is carried over intact.
        assert ex._terminal.allowed_commands == injected.allowed_commands
        assert ex._terminal.hidden_paths == ()

    def test_injected_terminal_is_bound_by_memory_off_policy(self, tmp_path):
        """A library caller's executor must not keep palace access we revoked."""
        injected = TerminalExecutor()
        skills = tmp_path / "skills"
        skills.mkdir()

        ex = CoreToolExecutor(
            workspace_root=tmp_path,
            memory=None,
            skills_path=skills,
            mcp=_NoMCP(),
            terminal=injected,
        )

        assert "../memory" in injected.allowed_path_prefixes  # caller's object untouched
        assert "mempalace" in injected.allowed_commands
        assert not any(
            prefix.startswith("../memory") for prefix in ex._terminal.allowed_path_prefixes
        )
        assert "mempalace" not in ex._terminal.allowed_commands
        assert ex._terminal.hidden_paths

    def test_injected_sandbox_gets_host_terminal_for_memory(self, tmp_path, monkeypatch):
        """Injected sandboxes need a host terminal; the palace is never mounted."""
        monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
        mem = tmp_path / "mem"
        mem.mkdir()
        skills = tmp_path / "skills"
        skills.mkdir()
        injected = SandboxExecutor(SandboxConfig.from_env(), tmp_path, skills_path=skills)

        ex = CoreToolExecutor(
            workspace_root=tmp_path,
            memory=_mem_sub(mem),
            skills_path=skills,
            mcp=_NoMCP(),
            terminal=injected,
        )

        assert ex._terminal is injected
        assert isinstance(ex._host_terminal, TerminalExecutor)


class TestCoreToolExecutorAclose:
    """Verify aclose() lifecycle — no-op for TerminalExecutor, cleanup for SandboxExecutor."""

    @pytest.mark.asyncio
    async def test_aclose_with_terminal_executor_is_noop(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
        ex = _make_executor(tmp_path)
        await ex.aclose()  # must not raise

    @pytest.mark.asyncio
    async def test_aclose_with_sandbox_executor_calls_sandbox_aclose(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        mock_cls, sandbox = _make_mock_sandbox_cls()
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            # Trigger sandbox creation by running a command
            await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"command": "echo hello", "argv": ["echo", "hello"]},
                ),
                ctx=_ctx(),
            )
            await ex.aclose()

        sandbox.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_aclose_twice_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        mock_cls, sandbox = _make_mock_sandbox_cls()
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            await ex.execute(
                call=ToolCall(
                    call_id="1",
                    name="run_command",
                    args={"argv": ["echo", "hello"]},
                ),
                ctx=_ctx(),
            )
            await ex.aclose()
            await ex.aclose()  # second call — must be a no-op

        sandbox.kill.assert_called_once()


class TestCoreToolExecutorRunCommandWithSandbox:
    """Verify run_command tool behaviour when sandbox executor is active."""

    @pytest.mark.asyncio
    async def test_sandbox_run_command_success_returns_ok_true(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        stdout_entry = MagicMock()
        stdout_entry.text = "hello"
        mock_execution = MagicMock(
            exit_code=0,
            logs=MagicMock(stdout=[stdout_entry], stderr=[]),
        )
        sandbox = MagicMock()
        sandbox.id = "s1"
        sandbox.commands.run = AsyncMock(return_value=mock_execution)
        sandbox.kill = AsyncMock()
        mock_cls = MagicMock()
        mock_cls.create = AsyncMock(return_value=sandbox)
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            out, err = unwrap_tool_execution_result(
                await ex.execute(
                    call=ToolCall(
                        call_id="1",
                        name="run_command",
                        args={"argv": ["echo", "hello"]},
                    ),
                    ctx=_ctx(),
                )
            )

        assert err is None
        payload = json.loads(out)
        assert payload["ok"] is True
        assert "hello" in payload["stdout"]

    @pytest.mark.asyncio
    async def test_sandbox_blocked_command_returns_error_envelope(self, tmp_path, monkeypatch):
        # A blocked command must return a tool error envelope, NOT raise an
        # uncaught exception into the loop. Regression guard for the security
        # error -> error envelope path.
        monkeypatch.setenv("SANDBOX_ENABLED", "true")
        mock_cls = MagicMock()
        mock_cls.create = AsyncMock()  # should never be called
        _, patches = _osb_patches(mock_cls)

        with patch.dict(sys.modules, patches):
            ex = _make_executor(tmp_path)
            out, err = unwrap_tool_execution_result(
                await ex.execute(
                    call=ToolCall(
                        call_id="1",
                        name="run_command",
                        args={"argv": ["rm", "-rf", "/"]},
                    ),
                    ctx=_ctx(),
                )
            )

        # Must be an error envelope, not a successful result
        assert out is None
        assert err is not None
        payload = json.loads(err)
        assert payload.get("ok") is False or "error" in payload or "denied" in str(payload).lower()
        mock_cls.create.assert_not_called()


@pytest.mark.asyncio
async def test_task_tool_queue_mode_requires_run_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        run_store=None,
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-queue",
                name="task",
                args={"task": "do work", "context": "ctx"},
            ),
            ctx=_ctx(),
        )
    )
    assert out is None and err is not None
    assert "requires a configured storage backend" in err


@pytest.mark.asyncio
async def test_task_tool_queue_mode_enqueues_pending_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from monkeybot.core.persistence.durable_runs import SubagentEnvelope as StoredEnvelope
    from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder for existence check\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    monkeypatch.setattr(
        "monkeybot.core.tools.core_tool_executor._inject_subagent_traceparent",
        lambda: "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
    )

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        ex = CoreToolExecutor(
            workspace_root=root,
            memory=_mem_sub(mem),
            skills_path=skills,
            mcp=_NoMCP(),
            run_store=backend.runs(),
        )
        out, err = unwrap_tool_execution_result(
            await ex.execute(
                call=ToolCall(
                    call_id="c-enq",
                    name="task",
                    args={"task": "queued task", "context": "ctx"},
                ),
                ctx=_ctx(),
            )
        )
        assert err is not None and out is None
        payload = json.loads(err)
        assert payload["ok"] is False
        assert payload["error_kind"] == "pending"
        assert payload["details"]["queued"] is True
        row = await backend.runs().get_run(payload["details"]["run_id"])
        assert row is not None
        assert row.status == "pending"
        stored = StoredEnvelope.from_json(row.envelope_json)
        assert stored.traceparent == "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_load_file_from_path_returns_image_block(tmp_path: Path) -> None:
    from monkeybot.core.attachments.store import FilesystemAttachmentStore
    from monkeybot.core.types.content_blocks import Image

    img_dir = tmp_path / "generated-media" / "images"
    img_dir.mkdir(parents=True)
    # minimal valid PNG for mime sniff
    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
        b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    rel = "./generated-media/images/test.png"
    (tmp_path / "generated-media" / "images" / "test.png").write_bytes(png)

    store = FilesystemAttachmentStore(tmp_path)
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        attachment_store=store,
    )

    result = await ex.execute(
        call=ToolCall(call_id="lf1", name="load_file", args={"path": rel}),
        ctx=_ctx(),
    )
    assert result.error is None
    assert any(isinstance(b, Image) for b in result.blocks)
    img = next(b for b in result.blocks if isinstance(b, Image))
    assert img.mime_type == "image/png"
    assert img.metadata is not None
    assert "attachment_id" in img.metadata


@pytest.mark.asyncio
async def test_load_file_rejects_plain_text_path(tmp_path: Path) -> None:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    ex = _make_executor(tmp_path)
    result = await ex.execute(
        call=ToolCall(call_id="lf2", name="load_file", args={"path": "./notes.txt"}),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "read_file" in result.error.lower() or "pdf" in result.error.lower()


@pytest.mark.asyncio
async def test_load_file_from_path_returns_pdf_file_block(tmp_path: Path) -> None:
    from monkeybot.core.types.content_blocks import File

    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    pdf_bytes = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    (tmp_path / "doc.pdf").write_bytes(pdf_bytes)
    ex = _make_executor(tmp_path)
    result = await ex.execute(
        call=ToolCall(call_id="lf3", name="load_file", args={"path": "./doc.pdf"}),
        ctx=_ctx(),
    )
    assert result.error is None
    assert any(isinstance(b, File) for b in result.blocks)
    block = next(b for b in result.blocks if isinstance(b, File))
    assert block.mime_type == "application/pdf"


@pytest.mark.asyncio
async def test_load_file_from_attachment_id_returns_image_block(tmp_path: Path) -> None:
    from monkeybot.core.attachments.store import FilesystemAttachmentStore
    from monkeybot.core.types.content_blocks import Image

    png = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc"
        b"\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    ctx = _ctx()
    store = FilesystemAttachmentStore(tmp_path)
    stored = store.save(ctx.thread_id, data=png, mime_type="image/png", filename="up.png")

    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        attachment_store=store,
    )

    result = await ex.execute(
        call=ToolCall(
            call_id="lf4",
            name="load_file",
            args={"attachment_id": stored.attachment_id},
        ),
        ctx=ctx,
    )
    assert result.error is None
    assert any(isinstance(b, Image) for b in result.blocks)
    img = next(b for b in result.blocks if isinstance(b, Image))
    assert img.mime_type == "image/png"
    assert img.metadata is not None
    assert img.metadata.get("attachment_id") == stored.attachment_id


@pytest.mark.asyncio
async def test_load_file_from_attachment_id_unknown_id_errors(tmp_path: Path) -> None:
    from monkeybot.core.attachments.store import FilesystemAttachmentStore

    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        attachment_store=FilesystemAttachmentStore(tmp_path),
    )

    result = await ex.execute(
        call=ToolCall(call_id="lf5", name="load_file", args={"attachment_id": "att_missing"}),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "att_missing" in result.error


@pytest.mark.asyncio
async def test_load_file_attachment_id_without_store_errors(tmp_path: Path) -> None:
    ex = _make_executor(tmp_path)
    result = await ex.execute(
        call=ToolCall(call_id="lf6", name="load_file", args={"attachment_id": "att_x"}),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "not enabled" in result.error.lower()


@pytest.mark.asyncio
async def test_load_file_rejects_both_attachment_id_and_path(tmp_path: Path) -> None:
    ex = _make_executor(tmp_path)
    result = await ex.execute(
        call=ToolCall(
            call_id="lf7",
            name="load_file",
            args={"attachment_id": "att_x", "path": "./notes.txt"},
        ),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "not both" in result.error.lower()


@pytest.mark.asyncio
async def test_load_file_requires_attachment_id_or_path(tmp_path: Path) -> None:
    ex = _make_executor(tmp_path)
    result = await ex.execute(
        call=ToolCall(call_id="lf8", name="load_file", args={}),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "requires" in result.error.lower()


@pytest.mark.asyncio
async def test_custom_tool_tool_execution_result_passthrough(tmp_path: Path) -> None:
    from monkeybot.core.tools.types import ToolExecutionResult
    from monkeybot.core.types.content_blocks import Image

    class _ImageTool:
        tool_def = ToolDef("emit_image", "emit test image", {"type": "object", "properties": {}})

        async def execute(self, args: dict[str, object]) -> ToolExecutionResult:
            del args
            return ToolExecutionResult.ok_blocks(
                [Image(mime_type="image/png", data="aW1n", metadata={"filename": "x.png"})]
            )

    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        extra_tools=[_ImageTool()],
    )
    result = await ex.execute(
        call=ToolCall(call_id="ct1", name="emit_image", args={}),
        ctx=_ctx(),
    )
    assert result.error is None
    assert any(isinstance(b, Image) for b in result.blocks)


@pytest.mark.asyncio
async def test_extra_tool_runtime_error_forbids_identical_retry(tmp_path: Path) -> None:
    from monkeybot.core.types.types_tools import ToolDef

    class _BoomTool:
        tool_def = ToolDef("boom", "raises", {"type": "object", "properties": {}})

        async def execute(self, args: dict[str, object]) -> str:
            del args
            raise RuntimeError("boom")

    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        extra_tools=[_BoomTool()],
    )
    _out, err = unwrap_tool_execution_result(
        await ex.execute(call=ToolCall(call_id="1", name="boom", args={}), ctx=_ctx())
    )
    assert err is not None
    payload = json.loads(err)
    assert payload["ok"] is False
    assert "Do not retry identical arguments" in payload["hint"]
    assert "if appropriate" not in payload["hint"]


class _CatalogMCP(_NoMCP):
    def __init__(self) -> None:
        self.connected: dict[str, list[ToolDef]] = {}
        self._catalog = {"browser": True}
        self.disconnected: list[str] = []

    def catalog_names(self) -> list[str]:
        return sorted(self._catalog)

    def known_server_names(self) -> list[str]:
        return sorted(set(self._catalog) | set(self.connected))

    def is_connected(self, name: str) -> bool:
        return name in self.connected

    async def connect_from_catalog(self, name: str) -> list[ToolDef]:
        if name not in self._catalog:
            from monkeybot.core.mcp.mcp_client import MCPDiagnosticError

            raise MCPDiagnosticError(name, f"Unknown MCP server {name!r}")
        defs = [ToolDef("browser__goto", "Go", {"type": "object"})]
        self.connected[name] = defs
        return defs

    async def disconnect(self, name: str) -> None:
        self.disconnected.append(name)
        self.connected.pop(name, None)

    def all_tools(self) -> list[ToolDef]:
        out: list[ToolDef] = []
        for tools in self.connected.values():
            out.extend(tools)
        return out

    def status(self, name: str | None = None):
        if name is None:
            return [
                {
                    "name": n,
                    "status": "connected" if n in self.connected else "catalogued",
                }
                for n in sorted(set(self._catalog) | set(self.connected))
            ]
        return {
            "name": name,
            "status": "connected" if name in self.connected else "catalogued",
        }


@pytest.mark.asyncio
async def test_enable_mcp_connects_from_catalog(tmp_path: Path) -> None:
    mcp = _CatalogMCP()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=mcp,
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    result = await ex.execute(
        call=ToolCall(name="enable_mcp", args={"name": "browser"}, call_id="1"),
        ctx=_ctx(),
    )
    assert result.error is None
    body = json.loads(result.blocks[0].text)  # type: ignore[index]
    assert body["ok"] is True
    assert body["server"] == "browser"
    assert body["status"]["status"] == "connected"
    assert body["tools"][0]["name"] == "browser__goto"
    assert "next model step" in body["note"]


@pytest.mark.asyncio
async def test_enable_mcp_unknown_server_errors(tmp_path: Path) -> None:
    mcp = _CatalogMCP()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=mcp,
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    result = await ex.execute(
        call=ToolCall(name="enable_mcp", args={"name": "missing"}, call_id="1"),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "Unknown MCP server" in result.error


@pytest.mark.asyncio
async def test_disable_mcp_disconnects_known_server(tmp_path: Path) -> None:
    mcp = _CatalogMCP()
    await mcp.connect_from_catalog("browser")
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=mcp,
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    result = await ex.execute(
        call=ToolCall(name="disable_mcp", args={"name": "browser"}, call_id="1"),
        ctx=_ctx(),
    )
    assert result.error is None
    body = json.loads(result.blocks[0].text)  # type: ignore[index]
    assert body["ok"] is True
    assert body["disconnected"] is True
    assert mcp.disconnected == ["browser"]
    assert not mcp.is_connected("browser")


@pytest.mark.asyncio
async def test_disable_mcp_unknown_server_errors(tmp_path: Path) -> None:
    mcp = _CatalogMCP()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=mcp,
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    result = await ex.execute(
        call=ToolCall(name="disable_mcp", args={"name": "typo-browser"}, call_id="1"),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "Unknown MCP server" in result.error
    assert "typo-browser" in result.error
    assert mcp.disconnected == []


@pytest.mark.asyncio
async def test_enable_loops_requires_store(tmp_path: Path) -> None:
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    result = await ex.execute(
        call=ToolCall(name="enable_loops", args={}, call_id="1"),
        ctx=_ctx(),
    )
    assert result.error is not None
    assert "durable storage" in result.error


@pytest.mark.asyncio
async def test_enable_and_disable_loops_toggle_advertisement(tmp_path: Path) -> None:
    registry = LoopsToolRegistry()
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        scheduled_loop_store=object(),  # type: ignore[arg-type]
        loops_registry=registry,
    )
    (tmp_path / "skills").mkdir(exist_ok=True)
    assert ex.loops_advertised is False
    enabled = await ex.execute(
        call=ToolCall(name="enable_loops", args={}, call_id="1"),
        ctx=_ctx(),
    )
    assert enabled.error is None
    body = json.loads(enabled.blocks[0].text)  # type: ignore[index]
    assert body["ok"] is True
    assert body["already_advertised"] is False
    assert {t["name"] for t in body["tools"]} >= {
        "start_loop",
        "loop_status",
        "pause_loop",
        "resume_loop",
        "stop_loop",
        "disable_loops",
    }
    assert ex.loops_advertised is True
    assert registry.advertised is True

    # Same registry shared across a fresh executor (next user turn).
    ex2 = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_NoMCP(),
        scheduled_loop_store=object(),  # type: ignore[arg-type]
        loops_registry=registry,
    )
    assert ex2.loops_advertised is True

    disabled = await ex2.execute(
        call=ToolCall(name="disable_loops", args={}, call_id="2"),
        ctx=_ctx(),
    )
    assert disabled.error is None
    dbody = json.loads(disabled.blocks[0].text)  # type: ignore[index]
    assert dbody["ok"] is True
    assert dbody["was_advertised"] is True
    assert dbody["tools_dropped"] == len(body["tools"])
    assert ex2.loops_advertised is False
    assert registry.advertised is False


class _ResourcesMCP(_NoMCP):
    def __init__(self) -> None:
        self._connected = True

    def is_connected(self, name: str) -> bool:
        return self._connected and name == "docs"

    def known_server_names(self) -> list[str]:
        return ["docs"]

    def status(self, name: str | None = None):
        entry = {
            "name": "docs",
            "status": "connected",
            "capabilities": {"tools": True, "resources": True, "prompts": True},
        }
        if name:
            return entry
        return [entry]

    async def list_resources(self, server_name: str | None = None):
        del server_name
        return [{"server": "docs", "name": "readme", "uri": "docs://readme"}]

    async def read_resource(self, server_name: str, uri: str):
        return {
            "server": server_name,
            "uri": uri,
            "text": "hello resource",
            "contents": [{"uri": uri, "text": "hello resource"}],
        }

    async def list_prompts(self, server_name: str | None = None):
        del server_name
        return [{"server": "docs", "name": "summarize", "description": "Sum"}]

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ):
        return {
            "server": server_name,
            "name": prompt_name,
            "description": "Sum",
            "messages": [{"role": "user", "content": {"type": "text", "text": "hi"}}],
            "arguments": arguments or {},
        }


@pytest.mark.asyncio
async def test_mcp_resource_and_prompt_tools(tmp_path: Path) -> None:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_ResourcesMCP(),
    )
    ctx = _ctx()

    listed = await ex.execute(
        call=ToolCall(name="list_mcp_resources", args={}, call_id="r1"),
        ctx=ctx,
    )
    assert listed.error is None
    body = json.loads(listed.blocks[0].text)  # type: ignore[index]
    assert body["count"] == 1
    assert body["resources"][0]["uri"] == "docs://readme"

    read = await ex.execute(
        call=ToolCall(
            name="read_mcp_resource",
            args={"server": "docs", "uri": "docs://readme"},
            call_id="r2",
        ),
        ctx=ctx,
    )
    assert read.error is None
    assert json.loads(read.blocks[0].text)["text"] == "hello resource"  # type: ignore[index]

    prompts = await ex.execute(
        call=ToolCall(name="list_mcp_prompts", args={"server": "docs"}, call_id="p1"),
        ctx=ctx,
    )
    assert prompts.error is None
    assert json.loads(prompts.blocks[0].text)["prompts"][0]["name"] == "summarize"  # type: ignore[index]

    got = await ex.execute(
        call=ToolCall(
            name="get_mcp_prompt",
            args={"server": "docs", "prompt": "summarize", "arguments": {"topic": "x"}},
            call_id="p2",
        ),
        ctx=ctx,
    )
    assert got.error is None
    assert json.loads(got.blocks[0].text)["name"] == "summarize"  # type: ignore[index]


@pytest.mark.asyncio
async def test_read_mcp_resource_requires_server_and_uri(tmp_path: Path) -> None:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_ResourcesMCP(),
    )
    ctx = _ctx()

    missing_server = await ex.execute(
        call=ToolCall(name="read_mcp_resource", args={"uri": "docs://readme"}, call_id="r1"),
        ctx=ctx,
    )
    assert missing_server.error == "read_mcp_resource requires server"

    missing_uri = await ex.execute(
        call=ToolCall(name="read_mcp_resource", args={"server": "docs"}, call_id="r2"),
        ctx=ctx,
    )
    assert missing_uri.error == "read_mcp_resource requires uri"


@pytest.mark.asyncio
async def test_get_mcp_prompt_requires_server_and_prompt(tmp_path: Path) -> None:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_ResourcesMCP(),
    )
    ctx = _ctx()

    missing_server = await ex.execute(
        call=ToolCall(name="get_mcp_prompt", args={"prompt": "summarize"}, call_id="p1"),
        ctx=ctx,
    )
    assert missing_server.error == "get_mcp_prompt requires server"

    missing_prompt = await ex.execute(
        call=ToolCall(name="get_mcp_prompt", args={"server": "docs"}, call_id="p2"),
        ctx=ctx,
    )
    assert missing_prompt.error == "get_mcp_prompt requires prompt"


class _ResourcesMCPNotConnected(_NoMCP):
    """MCP stub whose resource/prompt calls fail: server unknown or missing capability."""

    async def list_resources(self, server_name: str | None = None):
        raise MCPServerNotConnectedError(server_name or "docs")

    async def read_resource(self, server_name: str, uri: str):
        del uri
        raise MCPServerNotConnectedError(server_name)

    async def list_prompts(self, server_name: str | None = None):
        raise MCPDiagnosticError(
            server_name or "docs",
            f"MCP server {server_name!r} does not advertise prompts capability",
            remedy="Use a server that supports prompts, or call enable_mcp first.",
        )

    async def get_prompt(
        self,
        server_name: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
    ):
        del prompt_name, arguments
        raise MCPDiagnosticError(
            server_name,
            f"MCP server {server_name!r} does not advertise prompts capability",
            remedy="Use a server that supports prompts, or call enable_mcp first.",
        )


@pytest.mark.asyncio
async def test_mcp_resource_and_prompt_tools_not_found_paths(tmp_path: Path) -> None:
    (tmp_path / "mem").mkdir(exist_ok=True)
    (tmp_path / "skills").mkdir(exist_ok=True)
    ex = CoreToolExecutor(
        workspace_root=tmp_path,
        memory=_mem_sub(tmp_path / "mem"),
        skills_path=tmp_path / "skills",
        mcp=_ResourcesMCPNotConnected(),
    )
    ctx = _ctx()

    listed = await ex.execute(
        call=ToolCall(name="list_mcp_resources", args={"server": "missing"}, call_id="r1"),
        ctx=ctx,
    )
    assert listed.error is not None
    assert "not connected" in listed.error

    read = await ex.execute(
        call=ToolCall(
            name="read_mcp_resource",
            args={"server": "missing", "uri": "docs://readme"},
            call_id="r2",
        ),
        ctx=ctx,
    )
    assert read.error is not None
    assert "not connected" in read.error

    prompts = await ex.execute(
        call=ToolCall(name="list_mcp_prompts", args={"server": "docs"}, call_id="p1"),
        ctx=ctx,
    )
    assert prompts.error is not None
    assert "does not advertise prompts capability" in prompts.error

    got = await ex.execute(
        call=ToolCall(
            name="get_mcp_prompt",
            args={"server": "docs", "prompt": "summarize"},
            call_id="p2",
        ),
        ctx=ctx,
    )
    assert got.error is not None
    assert "does not advertise prompts capability" in got.error


# --- Story 2: subagent progress publishing ---------------------------------


class _FakeEventPublisher:
    """Captures publish_event calls for task-tool SSE tests."""

    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[object] = []
        self.fail = fail

    async def publish_event(self, event: object) -> None:
        if self.fail:
            raise RuntimeError("publish boom")
        self.events.append(event)


def test_turn_context_event_publisher_defaults_none() -> None:
    ctx = TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# Agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
    )
    assert ctx.event_publisher is None


@pytest.mark.asyncio
async def test_build_context_accepts_event_publisher(tmp_path: Path) -> None:
    from monkeybot.core.context import build_context

    agent_path = tmp_path / "AGENT.md"
    agent_path.write_text("You are helpful.\n", encoding="utf-8")
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "INDEX.md").write_text("", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    pub = _FakeEventPublisher()
    ctx = await build_context(
        "thread-1",
        "req-1",
        agent_md_path=agent_path,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp_client=_NoMCP(),
        event_publisher=pub,
    )
    assert ctx.event_publisher is pub


@pytest.mark.asyncio
async def test_task_tool_result_includes_child_thread_id_and_subagent_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    agents = tmp_path / "monkeybot_config" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text("# researcher\n", encoding="utf-8")
    (tmp_path / "monkeybot_config" / "AGENT.md").write_text("# parent\n", encoding="utf-8")

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env, envelope
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))

    registry = {
        "researcher": SubagentConfig(
            name="researcher",
            description="research",
            skills=[],
            agent_md="./monkeybot_config/agents/researcher.md",
        )
    }
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
        subagent_registry=registry,
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-link",
                name="task",
                args={"task": "research topic", "subagent_type": "researcher"},
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert isinstance(payload["run_id"], str) and payload["run_id"]
    assert isinstance(payload["child_thread_id"], str)
    assert payload["child_thread_id"].startswith("subagent:t:")
    assert payload["subagent_type"] == "researcher"


@pytest.mark.asyncio
async def test_task_tool_publishes_started_events_completed_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import (
        AssistantDelta,
        SubagentCompleted,
        SubagentEvent,
        SubagentStarted,
        ToolCallStarted,
        TurnComplete,
        UsageTotals,
    )

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, extra_env, envelope
        events = [
            AssistantDelta(request_id="r", delta="hi"),
            ToolCallStarted(request_id="r", tool="search", label="search", args={}, call_id="c1"),
            TurnComplete(
                request_id="r",
                usage=UsageTotals(
                    input_tokens=1,
                    output_tokens=1,
                    cached_tokens=0,
                    cost_usd=0.0,
                    duration_ms=1,
                    estimated_prompt_tokens=0,
                ),
            ),
        ]
        for evt in events:
            if on_event is not None:
                await on_event(evt)  # type: ignore[misc]
            yield evt

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    pub = _FakeEventPublisher()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-pub",
                name="task",
                args={"task": "do the thing", "context": "ctx"},
            ),
            ctx=_ctx(event_publisher=pub),
        )
    )
    assert err is None and out is not None
    kinds = [getattr(e, "kind", None) for e in pub.events]
    assert kinds[0] == "SubagentStarted"
    assert isinstance(pub.events[0], SubagentStarted)
    assert "SubagentEvent" in kinds
    assert any(isinstance(e, SubagentEvent) for e in pub.events)
    assert kinds[-1] == "SubagentCompleted"
    assert isinstance(pub.events[-1], SubagentCompleted)


@pytest.mark.asyncio
async def test_task_tool_noop_publish_when_publisher_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import AssistantDelta, TurnComplete, UsageTotals

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, extra_env, envelope
        events = [
            AssistantDelta(request_id="r", delta="ok"),
            TurnComplete(
                request_id="r",
                usage=UsageTotals(
                    input_tokens=1,
                    output_tokens=1,
                    cached_tokens=0,
                    cost_usd=0.0,
                    duration_ms=1,
                    estimated_prompt_tokens=0,
                ),
            ),
        ]
        for evt in events:
            if on_event is not None:
                await on_event(evt)  # type: ignore[misc]
            yield evt

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-noop",
                name="task",
                args={"task": "do the thing"},
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["assistant_text"] == "ok"
    assert isinstance(payload.get("child_thread_id"), str)
    assert payload["child_thread_id"].startswith("subagent:t:")
    assert "run_id" in payload


@pytest.mark.asyncio
async def test_task_tool_publish_error_does_not_fail_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import AssistantDelta, TurnComplete, UsageTotals

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, extra_env, envelope
        events = [
            AssistantDelta(request_id="r", delta="hi"),
            TurnComplete(
                request_id="r",
                usage=UsageTotals(
                    input_tokens=1,
                    output_tokens=1,
                    cached_tokens=0,
                    cost_usd=0.0,
                    duration_ms=1,
                    estimated_prompt_tokens=0,
                ),
            ),
        ]
        for evt in events:
            if on_event is not None:
                await on_event(evt)  # type: ignore[misc]
            yield evt

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    pub = _FakeEventPublisher(fail=True)
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-fail-pub",
                name="task",
                args={"task": "do the thing"},
            ),
            ctx=_ctx(event_publisher=pub),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_task_tool_sets_child_thread_id_on_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import TurnComplete, UsageTotals

    seen: list[object] = []

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, on_event, extra_env
        seen.append(envelope)
        yield TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        )

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-env",
                name="task",
                args={"task": "do the thing"},
            ),
            ctx=_ctx(),
        )
    )
    assert err is None and out is not None
    payload = json.loads(out)
    assert len(seen) == 1
    env = seen[0]
    assert getattr(env, "child_thread_id", None) == payload["child_thread_id"]
    assert isinstance(payload["child_thread_id"], str)
    assert payload["child_thread_id"].startswith("subagent:t:")


@pytest.mark.asyncio
async def test_task_tool_queue_mode_includes_linkage_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.persistence.durable_runs import SubagentEnvelope as StoredEnvelope
    from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))
    monkeypatch.setenv("MONKEYBOT_AGENT_ROOT", str(tmp_path))

    agents = tmp_path / "monkeybot_config" / "agents"
    agents.mkdir(parents=True)
    (agents / "researcher.md").write_text("# researcher\n", encoding="utf-8")
    (tmp_path / "monkeybot_config" / "AGENT.md").write_text("# parent\n", encoding="utf-8")

    registry = {
        "researcher": SubagentConfig(
            name="researcher",
            description="research",
            skills=[],
            agent_md="./monkeybot_config/agents/researcher.md",
        )
    }

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        ex = CoreToolExecutor(
            workspace_root=root,
            memory=_mem_sub(mem),
            skills_path=skills,
            mcp=_NoMCP(),
            run_store=backend.runs(),
            subagent_registry=registry,
        )
        out, err = unwrap_tool_execution_result(
            await ex.execute(
                call=ToolCall(
                    call_id="c-q-link",
                    name="task",
                    args={
                        "task": "queued task",
                        "context": "ctx",
                        "subagent_type": "researcher",
                    },
                ),
                ctx=_ctx(),
            )
        )
        assert err is not None and out is None
        payload = json.loads(err)
        assert payload["ok"] is False
        assert payload["error_kind"] == "pending"
        details = payload["details"]
        assert details["queued"] is True
        assert isinstance(details["child_thread_id"], str)
        assert details["child_thread_id"].startswith("subagent:t:")
        assert details["subagent_type"] == "researcher"
        assert "Do not treat this as task completion" in payload["hint"]
        row = await backend.runs().get_run(details["run_id"])
        assert row is not None
        stored = StoredEnvelope.from_json(row.envelope_json)
        assert stored.child_thread_id == details["child_thread_id"]
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_task_tool_queue_mode_skips_nested_sse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue mode returns before SubagentStarted/Completed (no parent publisher reachability)."""
    from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend

    monkeypatch.setenv("MONKEYBOT_TASK_QUEUE", "1")
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    backend = SQLiteStorageBackend("sqlite:///:memory:")
    await backend.open()
    try:
        pub = _FakeEventPublisher()
        ex = CoreToolExecutor(
            workspace_root=root,
            memory=_mem_sub(mem),
            skills_path=skills,
            mcp=_NoMCP(),
            run_store=backend.runs(),
        )
        out, err = unwrap_tool_execution_result(
            await ex.execute(
                call=ToolCall(
                    call_id="c-q-sse",
                    name="task",
                    args={"task": "queued task", "context": "ctx"},
                ),
                ctx=_ctx(event_publisher=pub),
            )
        )
        assert err is not None and out is None
        assert json.loads(err)["error_kind"] == "pending"
        assert pub.events == []
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_task_tool_publishes_completed_when_payload_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SubagentCompleted must still fire if finalize/payload assembly raises."""
    from monkeybot.core.runtime.events import (
        AssistantDelta,
        SubagentCompleted,
        SubagentStarted,
        TurnComplete,
        UsageTotals,
    )

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, extra_env, envelope
        events = [
            AssistantDelta(request_id="r", delta="hi"),
            TurnComplete(
                request_id="r",
                usage=UsageTotals(
                    input_tokens=1,
                    output_tokens=1,
                    cached_tokens=0,
                    cost_usd=0.0,
                    duration_ms=1,
                    estimated_prompt_tokens=0,
                ),
            ),
        ]
        for evt in events:
            if on_event is not None:
                await on_event(evt)  # type: ignore[misc]
            yield evt

    monkeypatch.setattr("monkeybot.core.tools.core_tool_executor.spawn_subagent", fake_spawn)

    def boom(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("payload boom")

    monkeypatch.setattr(
        "monkeybot.core.tools.core_tool_executor._task_result_payload",
        boom,
    )

    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir()
    skills = tmp_path / "skills"
    skills.mkdir()
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    pub = _FakeEventPublisher()
    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(
                call_id="c-finalize",
                name="task",
                args={"task": "do the thing"},
            ),
            ctx=_ctx(event_publisher=pub),
        )
    )
    assert err is None and out is not None
    kinds = [getattr(e, "kind", None) for e in pub.events]
    assert kinds[0] == "SubagentStarted"
    assert isinstance(pub.events[0], SubagentStarted)
    assert kinds[-1] == "SubagentCompleted"
    completed = pub.events[-1]
    assert isinstance(completed, SubagentCompleted)
    assert completed.ok is False
    assert any("payload boom" in e for e in completed.errors)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert any("payload boom" in e for e in payload["errors"])


def _fake_spawn_emitting(events: list[object]):
    """Build a ``spawn_subagent`` stand-in that replays ``events`` from a child."""

    async def fake_spawn(
        script: str,
        envelope: object,
        *,
        scratch_dir: object,
        subprocess_exec: object | None = None,
        on_event: object | None = None,
        extra_env: dict[str, str] | None = None,
    ):
        del script, scratch_dir, subprocess_exec, extra_env, envelope
        for evt in events:
            if on_event is not None:
                await on_event(evt)  # type: ignore[misc]
            yield evt

    return fake_spawn


def _clean_child_events() -> list[object]:
    from monkeybot.core.runtime.events import AssistantDelta, TurnComplete, UsageTotals

    return [
        AssistantDelta(request_id="r", delta="done"),
        TurnComplete(
            request_id="r",
            usage=UsageTotals(
                input_tokens=1,
                output_tokens=1,
                cached_tokens=0,
                cost_usd=0.0,
                duration_ms=1,
                estimated_prompt_tokens=0,
            ),
        ),
    ]


async def _run_task_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    child_events: list[object],
    args: dict[str, object],
) -> dict[str, object]:
    monkeypatch.setattr(
        "monkeybot.core.tools.core_tool_executor.spawn_subagent",
        _fake_spawn_emitting(child_events),
    )
    root = tmp_path
    _stub_agent_md_for_tasks(root, monkeypatch)
    mem = tmp_path / "mem"
    mem.mkdir(exist_ok=True)
    skills = tmp_path / "skills"
    skills.mkdir(exist_ok=True)
    worker = root / "subagent_worker.py"
    worker.write_text("# placeholder\n", encoding="utf-8")
    monkeypatch.setenv("MONKEYBOT_SUBAGENT_SCRIPT", str(worker))

    ex = CoreToolExecutor(
        workspace_root=root,
        memory=_mem_sub(mem),
        skills_path=skills,
        mcp=_NoMCP(),
    )
    out, err = unwrap_tool_execution_result(
        await ex.execute(
            call=ToolCall(call_id="c-exit", name="task", args=dict(args)),
            ctx=_ctx(event_publisher=_FakeEventPublisher()),
        )
    )
    # Rejected input comes back on the error channel; the caller asserts on it.
    body = out if out is not None else err
    assert body is not None
    return json.loads(body)


@pytest.mark.asyncio
async def test_task_exit_reason_is_completed_on_a_clean_child_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=_clean_child_events(),
        args={"task": "do the thing"},
    )
    assert payload["ok"] is True
    assert payload["exit_reason"] == "completed"


@pytest.mark.asyncio
async def test_task_exit_reason_distinguishes_max_turns_from_a_generic_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from monkeybot.core.runtime.events import Error
    from monkeybot.core.runtime.turn_loop import MAX_TURNS_ERROR

    max_turns = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=[Error(request_id="r", error=MAX_TURNS_ERROR)],
        args={"task": "do the thing"},
    )
    assert max_turns["ok"] is False
    assert max_turns["exit_reason"] == "max_turns"

    other = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=[Error(request_id="r", error="provider blew up")],
        args={"task": "do the thing"},
    )
    assert other["exit_reason"] == "error"


@pytest.mark.asyncio
async def test_task_reports_artifact_existence_only_when_the_caller_names_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "landed.md").write_text("hi\n", encoding="utf-8")

    unasked = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=_clean_child_events(),
        args={"task": "write the doc"},
    )
    # No expectation declared -> the harness must not claim to know.
    assert unasked["artifact_exists"] is None
    assert unasked["artifacts"] == []

    checked = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=_clean_child_events(),
        args={"task": "write the doc", "expect_files": ["landed.md", "missing.md"]},
    )
    assert checked["artifact_exists"] is False
    assert checked["artifacts"] == [
        {"path": "landed.md", "exists": True},
        {"path": "missing.md", "exists": False},
    ]

    all_there = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=_clean_child_events(),
        args={"task": "write the doc", "expect_files": ["landed.md"]},
    )
    assert all_there["artifact_exists"] is True


@pytest.mark.asyncio
async def test_task_rejects_expect_files_that_escape_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = await _run_task_tool(
        tmp_path,
        monkeypatch,
        child_events=_clean_child_events(),
        args={"task": "x", "expect_files": ["../outside.md"]},
    )
    assert payload["ok"] is False
    assert payload["error_kind"] == "validation"
