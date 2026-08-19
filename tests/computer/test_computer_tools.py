"""Tests for the ``computer_*`` CustomTool bodies.

``safety.run_argv`` is monkeypatched everywhere so no test ever invokes the
real ``open``/``pbcopy``/``pbpaste`` binaries (which would open real Finder
windows / touch the real clipboard). Filesystem tools (list/find/move/trash)
run against a real temp "home" directory instead — no OS-level side effects.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from monkeybot.computer import safety
from monkeybot.computer.tools import (
    ComputerClipboardReadTool,
    ComputerClipboardWriteTool,
    ComputerFindTool,
    ComputerListDirTool,
    ComputerMoveTool,
    ComputerOpenAppTool,
    ComputerOpenTool,
    ComputerOpenURLTool,
    ComputerTrashTool,
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("MONKEYBOT_APP_HOME", raising=False)
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    return fake_home


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test forgets to stub ``run_argv`` and would hit real OS state."""

    def _boom(argv, **kwargs):
        raise AssertionError(f"real subprocess invoked in test: {argv!r}")

    monkeypatch.setattr(safety, "run_argv", _boom)


def _stub_run_argv(monkeypatch: pytest.MonkeyPatch, *, stdout: str = "", returncode: int = 0):
    calls: list[list[str]] = []

    def fake(argv, *, input_text=None, timeout=safety.DEFAULT_TIMEOUT_SEC):
        calls.append(argv)
        return safety.RunResult(stdout=stdout, stderr="", returncode=returncode)

    monkeypatch.setattr(safety, "run_argv", fake)
    return calls


class TestComputerOpen:
    @pytest.mark.asyncio
    async def test_opens_existing_folder(self, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = home / "Downloads"
        target.mkdir()
        calls = _stub_run_argv(monkeypatch)
        result = json.loads(await ComputerOpenTool().execute({"path": str(target)}))
        assert result["ok"] is True
        assert calls == [["/usr/bin/open", str(target.resolve())]]

    @pytest.mark.asyncio
    async def test_missing_path_is_validation_error(self, home: Path) -> None:
        result = json.loads(await ComputerOpenTool().execute({"path": str(home / "nope")}))
        assert result["ok"] is False
        assert result["error_kind"] == "validation"

    @pytest.mark.asyncio
    async def test_denied_path_is_policy_error_no_subprocess(self, home: Path) -> None:
        target = home / ".ssh"
        target.mkdir()
        result = json.loads(await ComputerOpenTool().execute({"path": str(target)}))
        assert result["ok"] is False
        assert result["error_kind"] == "policy"

    @pytest.mark.asyncio
    async def test_exec_suffix_is_policy_error_no_subprocess(self, home: Path) -> None:
        target = home / "installer.command"
        target.write_text("x")
        result = json.loads(await ComputerOpenTool().execute({"path": str(target)}))
        assert result["ok"] is False
        assert result["error_kind"] == "policy"


class TestComputerOpenURL:
    @pytest.mark.asyncio
    async def test_opens_https(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _stub_run_argv(monkeypatch)
        result = json.loads(await ComputerOpenURLTool().execute({"url": "https://example.com"}))
        assert result["ok"] is True
        assert calls == [["/usr/bin/open", "https://example.com"]]

    @pytest.mark.asyncio
    async def test_rejects_file_scheme(self) -> None:
        result = json.loads(await ComputerOpenURLTool().execute({"url": "file:///etc/passwd"}))
        assert result["ok"] is False
        assert result["error_kind"] == "policy"


class TestComputerOpenApp:
    @pytest.mark.asyncio
    async def test_launches_installed_app(
        self, home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        apps = home / "Applications"
        apps.mkdir()
        (apps / "Notes.app").mkdir()
        calls = _stub_run_argv(monkeypatch)
        result = json.loads(await ComputerOpenAppTool().execute({"app": "Notes"}))
        assert result["ok"] is True
        assert calls == [["/usr/bin/open", "-a", str((apps / "Notes.app").resolve())]]

    @pytest.mark.asyncio
    async def test_refuses_terminal(self, home: Path) -> None:
        apps = home / "Applications"
        apps.mkdir()
        (apps / "Terminal.app").mkdir()
        result = json.loads(await ComputerOpenAppTool().execute({"app": "Terminal"}))
        assert result["ok"] is False
        assert result["error_kind"] == "policy"


class TestClipboard:
    @pytest.mark.asyncio
    async def test_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _stub_run_argv(monkeypatch, stdout="hello")
        result = json.loads(await ComputerClipboardReadTool().execute({}))
        assert result == {"ok": True, "text": "hello", "truncated": False}

    @pytest.mark.asyncio
    async def test_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[list[str], str | None]] = []

        def fake(argv, *, input_text=None, timeout=safety.DEFAULT_TIMEOUT_SEC):
            calls.append((argv, input_text))
            return safety.RunResult("", "", 0)

        monkeypatch.setattr(safety, "run_argv", fake)
        result = json.loads(await ComputerClipboardWriteTool().execute({"text": "abc"}))
        assert result["ok"] is True
        assert calls == [(["/usr/bin/pbcopy"], "abc")]

    @pytest.mark.asyncio
    async def test_write_requires_text(self) -> None:
        result = json.loads(await ComputerClipboardWriteTool().execute({}))
        assert result["ok"] is False
        assert result["error_kind"] == "validation"


class TestListDir:
    @pytest.mark.asyncio
    async def test_lists_entries_sorted_dirs_first(self, home: Path) -> None:
        target = home / "Downloads"
        target.mkdir()
        (target / "b.txt").write_text("x")
        (target / "a.txt").write_text("x")
        (target / "subdir").mkdir()
        result = json.loads(await ComputerListDirTool().execute({"path": str(target)}))
        assert result["ok"] is True
        names = [e["name"] for e in result["entries"]]
        assert names == ["subdir", "a.txt", "b.txt"]

    @pytest.mark.asyncio
    async def test_hides_dotfiles_by_default(self, home: Path) -> None:
        target = home / "Downloads"
        target.mkdir()
        (target / ".hidden").write_text("x")
        (target / "visible.txt").write_text("x")
        result = json.loads(await ComputerListDirTool().execute({"path": str(target)}))
        names = [e["name"] for e in result["entries"]]
        assert names == ["visible.txt"]

    @pytest.mark.asyncio
    async def test_filters_denied_entries(self, home: Path) -> None:
        target = home / "Downloads"
        target.mkdir()
        (target / "id_rsa").write_text("x")
        (target / "notes.txt").write_text("x")
        result = json.loads(await ComputerListDirTool().execute({"path": str(target)}))
        names = [e["name"] for e in result["entries"]]
        assert names == ["notes.txt"]

    @pytest.mark.asyncio
    async def test_rejects_denied_root_before_listing(self, home: Path) -> None:
        target = home / ".ssh"
        target.mkdir()
        (target / "id_rsa").write_text("x")
        result = json.loads(await ComputerListDirTool().execute({"path": str(target)}))
        assert result["ok"] is False
        assert result["error_kind"] == "policy"


class TestFind:
    @pytest.mark.asyncio
    async def test_finds_by_substring(self, home: Path) -> None:
        root = home / "Downloads"
        (root / "sub").mkdir(parents=True)
        (root / "report-final.pdf").write_text("x")
        (root / "sub" / "report-draft.pdf").write_text("x")
        (root / "other.txt").write_text("x")
        result = json.loads(
            await ComputerFindTool().execute({"path": str(root), "query": "report"})
        )
        assert result["ok"] is True
        found_names = {Path(r["path"]).name for r in result["results"]}
        assert found_names == {"report-final.pdf", "report-draft.pdf"}

    @pytest.mark.asyncio
    async def test_prunes_denied_subdirs(self, home: Path) -> None:
        root = home / "Downloads"
        root.mkdir()
        (root / ".ssh").mkdir()
        (root / ".ssh" / "report.txt").write_text("x")
        result = json.loads(
            await ComputerFindTool().execute({"path": str(root), "query": "report"})
        )
        assert result["results"] == []


class TestMove:
    @pytest.mark.asyncio
    async def test_renames_within_same_dir(self, home: Path) -> None:
        target = home / "Downloads" / "old.txt"
        target.parent.mkdir()
        target.write_text("hi")
        result = json.loads(
            await ComputerMoveTool().execute(
                {"path": str(target), "destination": str(home / "Downloads" / "new.txt")}
            )
        )
        assert result["ok"] is True
        assert not target.exists()
        assert (home / "Downloads" / "new.txt").read_text() == "hi"

    @pytest.mark.asyncio
    async def test_moves_into_directory(self, home: Path) -> None:
        src = home / "Downloads" / "file.txt"
        src.parent.mkdir()
        src.write_text("hi")
        dest_dir = home / "Desktop"
        dest_dir.mkdir()
        result = json.loads(
            await ComputerMoveTool().execute({"path": str(src), "destination": str(dest_dir)})
        )
        assert result["ok"] is True
        assert (dest_dir / "file.txt").read_text() == "hi"

    @pytest.mark.asyncio
    async def test_refuses_overwrite_without_flag(self, home: Path) -> None:
        src = home / "Downloads" / "file.txt"
        src.parent.mkdir()
        src.write_text("new")
        dest = home / "Desktop" / "file.txt"
        dest.parent.mkdir()
        dest.write_text("existing")
        result = json.loads(
            await ComputerMoveTool().execute({"path": str(src), "destination": str(dest)})
        )
        assert result["ok"] is False
        assert result["error_kind"] == "validation"
        assert dest.read_text() == "existing"

    @pytest.mark.asyncio
    async def test_refuses_moving_dir_into_itself(self, home: Path) -> None:
        src = home / "Downloads"
        src.mkdir()
        result = json.loads(
            await ComputerMoveTool().execute({"path": str(src), "destination": str(src / "sub")})
        )
        assert result["ok"] is False
        assert result["error_kind"] == "validation"


class TestTrash:
    @pytest.mark.asyncio
    async def test_moves_to_trash(self, home: Path) -> None:
        target = home / "Downloads" / "file.txt"
        target.parent.mkdir()
        target.write_text("hi")
        result = json.loads(await ComputerTrashTool().execute({"path": str(target)}))
        assert result["ok"] is True
        assert not target.exists()
        assert Path(result["trashed_to"]).read_text() == "hi"

    @pytest.mark.asyncio
    async def test_refuses_top_level_folder(self, home: Path) -> None:
        target = home / "Documents"
        target.mkdir()
        result = json.loads(await ComputerTrashTool().execute({"path": str(target)}))
        assert result["ok"] is False
        assert result["error_kind"] == "policy"
