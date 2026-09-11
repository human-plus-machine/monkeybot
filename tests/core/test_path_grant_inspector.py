"""Tests for PathGrantInspector — the ask-to-read front door for
read_file/load_file/glob/grep paths outside the workspace."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.context import TurnContext
from monkeybot.core.tools.grant_store import add_path_grant
from monkeybot.core.tools.inspector import InspectorToolCall
from monkeybot.core.tools.path_grant_inspector import PathGrantInspector


def _ctx(*, turn_path_grants: set[str] | None = None) -> TurnContext:
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="gemini-2.5-flash",
        turn_path_grants=turn_path_grants if turn_path_grants is not None else set(),
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    # Path.expanduser() reads $HOME directly (not Path.home()), so a tilde
    # path only resolves against the fake home when this is set too.
    monkeypatch.setenv("HOME", str(fake_home))
    return fake_home


@pytest.fixture
def workspace(home: Path) -> Path:
    ws = home / "agent" / "workspace"
    ws.mkdir(parents=True)
    return ws


class TestNonApplicableCalls:
    @pytest.mark.asyncio
    async def test_ignores_other_tools(self, workspace: Path) -> None:
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(InspectorToolCall("1", "write_file", {"path": "a.txt"}), _ctx())
        assert d.kind == "allow"

    @pytest.mark.asyncio
    async def test_ignores_relative_path(self, workspace: Path) -> None:
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": "README.md"}), _ctx()
        )
        assert d.kind == "allow"

    @pytest.mark.asyncio
    async def test_ignores_missing_path_arg(self, workspace: Path) -> None:
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(InspectorToolCall("1", "glob", {"pattern": "*.py"}), _ctx())
        assert d.kind == "allow"


class TestWorkspacePathsAllowed:
    @pytest.mark.asyncio
    async def test_absolute_path_inside_workspace_is_allowed(self, workspace: Path) -> None:
        insp = PathGrantInspector(workspace_root=workspace)
        target = workspace / "notes.txt"
        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(target)}), _ctx()
        )
        assert d.kind == "allow"


class TestDenylist:
    @pytest.mark.asyncio
    async def test_credential_dir_is_hard_denied(self, home: Path, workspace: Path) -> None:
        ssh = home / ".ssh"
        ssh.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(ssh / "id_rsa")}), _ctx()
        )
        assert d.kind == "deny"
        assert d.grant_key is None

    @pytest.mark.asyncio
    async def test_credential_filename_is_hard_denied(self, home: Path, workspace: Path) -> None:
        desktop = home / "Desktop"
        desktop.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(desktop / ".env")}), _ctx()
        )
        assert d.kind == "deny"

    @pytest.mark.asyncio
    async def test_outside_home_is_denied(self, tmp_path: Path, workspace: Path) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(outside / "x.txt")}), _ctx()
        )
        assert d.kind == "deny"


class TestAskAndGrant:
    @pytest.mark.asyncio
    async def test_ungranted_desktop_file_asks_with_folder_grant_key(
        self, home: Path, workspace: Path
    ) -> None:
        desktop = home / "Desktop"
        desktop.mkdir()
        target = desktop / "report.pdf"
        insp = PathGrantInspector(workspace_root=workspace)

        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(target)}), _ctx()
        )

        assert d.kind == "confirm"
        assert d.grant_kind == "path"
        assert d.grant_key == str(desktop.resolve())
        assert "Desktop" in (d.message or "")

    @pytest.mark.asyncio
    async def test_load_file_and_glob_and_grep_all_covered(
        self, home: Path, workspace: Path
    ) -> None:
        desktop = home / "Desktop"
        desktop.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)

        for name, args in (
            ("load_file", {"path": str(desktop / "x.pdf")}),
            ("glob", {"pattern": "*.pdf", "root": str(desktop)}),
            ("grep", {"pattern": "TODO", "root": str(desktop)}),
        ):
            d = await insp.check(InspectorToolCall("1", name, args), _ctx())
            assert d.kind == "confirm", name
            assert d.grant_kind == "path", name

    @pytest.mark.asyncio
    async def test_turn_grant_allows_second_file_in_same_folder(
        self, home: Path, workspace: Path
    ) -> None:
        desktop = home / "Desktop"
        desktop.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        ctx = _ctx(turn_path_grants={str(desktop.resolve())})

        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(desktop / "other.txt")}), ctx
        )
        assert d.kind == "allow"

    @pytest.mark.asyncio
    async def test_turn_grant_does_not_cover_a_different_folder(
        self, home: Path, workspace: Path
    ) -> None:
        desktop = home / "Desktop"
        documents = home / "Documents"
        desktop.mkdir()
        documents.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        ctx = _ctx(turn_path_grants={str(desktop.resolve())})

        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(documents / "x.txt")}), ctx
        )
        assert d.kind == "confirm"

    @pytest.mark.asyncio
    async def test_durable_grant_allows_without_asking(
        self, home: Path, workspace: Path, tmp_path: Path
    ) -> None:
        desktop = home / "Desktop"
        desktop.mkdir()
        grants_path = tmp_path / "grants.json"
        add_path_grant(
            grants_path,
            folder=str(desktop.resolve()),
            mode="read",
            created_at="2026-01-01T00:00:00+00:00",
        )
        insp = PathGrantInspector(workspace_root=workspace, grants_path=grants_path)

        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(desktop / "x.txt")}), _ctx()
        )
        assert d.kind == "allow"

    @pytest.mark.asyncio
    async def test_grant_key_is_the_folder_not_the_file(self, home: Path, workspace: Path) -> None:
        """A durable grant for the *folder* must cover any file within it,
        not just the one that triggered the original ask."""
        desktop = home / "Desktop"
        desktop.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        d1 = await insp.check(
            InspectorToolCall("1", "read_file", {"path": str(desktop / "a.pdf")}), _ctx()
        )
        assert d1.grant_key == str(desktop.resolve())

        ctx = _ctx(turn_path_grants={d1.grant_key})
        d2 = await insp.check(
            InspectorToolCall("2", "read_file", {"path": str(desktop / "b.pdf")}), ctx
        )
        assert d2.kind == "allow"

    @pytest.mark.asyncio
    async def test_tilde_path_is_treated_the_same_as_absolute(
        self, home: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        desktop = home / "Desktop"
        desktop.mkdir()
        insp = PathGrantInspector(workspace_root=workspace)
        d = await insp.check(
            InspectorToolCall("1", "read_file", {"path": "~/Desktop/x.pdf"}), _ctx()
        )
        assert d.kind == "confirm"
        assert d.grant_kind == "path"
