"""Tests for the layered ruleset in ``computer/permissions.py``:

precheck_policy (hard deny) < baseline ask < approvals.json overlay (allow) <
permissions.yaml (highest authority)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from monkeybot.computer.approvals import add_approval
from monkeybot.computer.permissions import (
    build_computer_permission_inspector,
    build_persist_hook,
)
from monkeybot.core.context import TurnContext
from monkeybot.core.tools.inspector import InspectorToolCall
from monkeybot.core.tools.permission import SessionApprovals


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """These tests exercise the real inspector, which now runs safety.precheck_policy
    first — so tool call paths must resolve under a real (fake) home directory or
    every call is denied before ask/allow evaluation even runs."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.delenv("MONKEYBOT_APP_HOME", raising=False)
    monkeypatch.delenv("MONKEYBOT_CONFIG", raising=False)
    return fake_home


def _ctx(*, bus: object | None = None) -> TurnContext:
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
        sse_bus=bus,  # type: ignore[arg-type]
    )


class _Bus:
    def __init__(self) -> None:
        self.session_approvals = SessionApprovals()


@pytest.mark.asyncio
async def test_baseline_asks_with_no_config_at_all(tmp_path: Path, home: Path) -> None:
    """The whole point of the baseline: no permissions.yaml, no approvals.json, still asks."""
    perm_path = tmp_path / "permissions.yaml"  # does not exist
    approvals_path = tmp_path / "approvals.json"  # does not exist
    insp = build_computer_permission_inspector(perm_path, approvals_path)
    d = await insp.check(
        InspectorToolCall("1", "computer_open", {"path": str(home / "Downloads")}), _ctx()
    )
    assert d.kind == "confirm"


@pytest.mark.asyncio
async def test_broken_yaml_still_asks_fail_open(tmp_path: Path, home: Path) -> None:
    """A user who breaks permissions.yaml must not silently lose the ask prompt."""
    perm_path = tmp_path / "permissions.yaml"
    perm_path.write_text("not: valid: yaml: [")
    approvals_path = tmp_path / "approvals.json"
    insp = build_computer_permission_inspector(perm_path, approvals_path)
    d = await insp.check(
        InspectorToolCall("1", "computer_open", {"path": str(home / "Downloads")}), _ctx()
    )
    assert d.kind == "confirm"


@pytest.mark.asyncio
async def test_other_tools_unaffected_by_baseline(tmp_path: Path, home: Path) -> None:
    perm_path = tmp_path / "permissions.yaml"  # missing -> default allow
    approvals_path = tmp_path / "approvals.json"
    insp = build_computer_permission_inspector(perm_path, approvals_path)
    d = await insp.check(InspectorToolCall("1", "read_file", {"path": "/x"}), _ctx())
    assert d.kind == "allow"


@pytest.mark.asyncio
async def test_approved_resource_stops_asking(tmp_path: Path, home: Path) -> None:
    downloads = home / "Downloads"
    desktop = home / "Desktop"
    perm_path = tmp_path / "permissions.yaml"
    approvals_path = tmp_path / "approvals.json"
    add_approval(
        approvals_path,
        tool="computer_open",
        resource=str(downloads),
        scope="resource",
        created_at="t1",
    )
    insp = build_computer_permission_inspector(perm_path, approvals_path)
    d = await insp.check(InspectorToolCall("1", "computer_open", {"path": str(downloads)}), _ctx())
    assert d.kind == "allow"
    # A different resource on the same tool still asks.
    d2 = await insp.check(InspectorToolCall("2", "computer_open", {"path": str(desktop)}), _ctx())
    assert d2.kind == "confirm"


@pytest.mark.asyncio
async def test_yaml_deny_beats_approved_resource(tmp_path: Path, home: Path) -> None:
    """permissions.yaml is the highest authority: an explicit deny wins even over a
    remembered approval."""
    target = home / "Downloads" / "old-file.txt"
    target.parent.mkdir()
    target.write_text("x")
    perm_path = tmp_path / "permissions.yaml"
    perm_path.write_text(
        yaml.dump(
            {
                "default": "allow",
                "rules": [{"tool": "computer_trash", "pattern": "*", "effect": "deny"}],
            }
        )
    )
    approvals_path = tmp_path / "approvals.json"
    add_approval(
        approvals_path,
        tool="computer_trash",
        resource=str(target),
        scope="resource",
        created_at="t1",
    )
    insp = build_computer_permission_inspector(perm_path, approvals_path)
    d = await insp.check(InspectorToolCall("1", "computer_trash", {"path": str(target)}), _ctx())
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_allow_ask_false_denies_instead_of_confirm(tmp_path: Path, home: Path) -> None:
    perm_path = tmp_path / "permissions.yaml"
    approvals_path = tmp_path / "approvals.json"
    insp = build_computer_permission_inspector(perm_path, approvals_path, allow_ask=False)
    d = await insp.check(
        InspectorToolCall("1", "computer_open", {"path": str(home / "Downloads")}), _ctx()
    )
    assert d.kind == "deny"


@pytest.mark.asyncio
async def test_hot_reload_picks_up_new_approval(tmp_path: Path, home: Path) -> None:
    target = home / "Downloads"
    perm_path = tmp_path / "permissions.yaml"
    approvals_path = tmp_path / "approvals.json"
    insp = build_computer_permission_inspector(perm_path, approvals_path)

    d1 = await insp.check(InspectorToolCall("1", "computer_open", {"path": str(target)}), _ctx())
    assert d1.kind == "confirm"

    add_approval(
        approvals_path,
        tool="computer_open",
        resource=str(target),
        scope="resource",
        created_at="t1",
    )
    d2 = await insp.check(InspectorToolCall("2", "computer_open", {"path": str(target)}), _ctx())
    assert d2.kind == "allow"


@pytest.mark.asyncio
async def test_revoke_clears_session_cache(tmp_path: Path, home: Path) -> None:
    """A revoke must take effect immediately in the same session, not just on restart —
    the session-approvals cache is checked before the ruleset and must be invalidated
    when the overlay file changes."""
    target = home / "Downloads"
    perm_path = tmp_path / "permissions.yaml"
    approvals_path = tmp_path / "approvals.json"
    add_approval(
        approvals_path,
        tool="computer_open",
        resource=str(target),
        scope="resource",
        created_at="t1",
    )
    insp = build_computer_permission_inspector(perm_path, approvals_path)
    bus = _Bus()

    d1 = await insp.check(
        InspectorToolCall("1", "computer_open", {"path": str(target)}), _ctx(bus=bus)
    )
    assert d1.kind == "allow"
    # Simulate the app UI "always allow" round trip having populated the session cache too.
    bus.session_approvals.remember("computer_open", str(target))

    from monkeybot.computer.approvals import remove_approval

    remove_approval(approvals_path, tool="computer_open", resource=str(target))
    d2 = await insp.check(
        InspectorToolCall("2", "computer_open", {"path": str(target)}), _ctx(bus=bus)
    )
    assert d2.kind == "confirm"


class TestPrecheckPolicy:
    @pytest.mark.asyncio
    async def test_denied_path_never_shows_a_confirmation_card(
        self, tmp_path: Path, home: Path
    ) -> None:
        """The scenario this whole layer exists for: opening a credential path must
        be refused outright, not asked about and then failed after approval."""
        ssh_key = home / ".ssh" / "id_rsa"
        ssh_key.parent.mkdir()
        ssh_key.write_text("fake key")
        perm_path = tmp_path / "permissions.yaml"
        approvals_path = tmp_path / "approvals.json"
        insp = build_computer_permission_inspector(perm_path, approvals_path)
        d = await insp.check(
            InspectorToolCall("1", "computer_open", {"path": str(ssh_key)}), _ctx()
        )
        assert d.kind == "deny"

    @pytest.mark.asyncio
    async def test_denied_path_wins_over_a_durable_always_allow(
        self, tmp_path: Path, home: Path
    ) -> None:
        """Hard denial is unconditional — even a resource that was somehow approved
        (e.g. approved before it moved into a denylisted spot, or a hand-edited
        approvals.json) must still be refused."""
        secret = home / ".ssh" / "id_rsa"
        secret.parent.mkdir()
        secret.write_text("fake key")
        perm_path = tmp_path / "permissions.yaml"
        approvals_path = tmp_path / "approvals.json"
        add_approval(
            approvals_path,
            tool="computer_open",
            resource=str(secret),
            scope="resource",
            created_at="t1",
        )
        insp = build_computer_permission_inspector(perm_path, approvals_path)
        d = await insp.check(InspectorToolCall("1", "computer_open", {"path": str(secret)}), _ctx())
        assert d.kind == "deny"

    @pytest.mark.asyncio
    async def test_exec_suffix_never_shows_a_confirmation_card(
        self, tmp_path: Path, home: Path
    ) -> None:
        installer = home / "Downloads" / "installer.command"
        installer.parent.mkdir()
        installer.write_text("x")
        perm_path = tmp_path / "permissions.yaml"
        approvals_path = tmp_path / "approvals.json"
        insp = build_computer_permission_inspector(perm_path, approvals_path)
        d = await insp.check(
            InspectorToolCall("1", "computer_open", {"path": str(installer)}), _ctx()
        )
        assert d.kind == "deny"

    @pytest.mark.asyncio
    async def test_missing_path_arg_is_not_denied_by_precheck(
        self, tmp_path: Path, home: Path
    ) -> None:
        """A validation problem (no path given) is not a policy violation — precheck
        must not deny it; the tool itself reports the validation error after asking,
        same as any other malformed call."""
        perm_path = tmp_path / "permissions.yaml"
        approvals_path = tmp_path / "approvals.json"
        insp = build_computer_permission_inspector(perm_path, approvals_path)
        d = await insp.check(InspectorToolCall("1", "computer_open", {}), _ctx())
        assert d.kind == "confirm"


class TestBuildPersistHook:
    def test_persists_always_allowable_tool(self, tmp_path: Path) -> None:
        approvals_path = tmp_path / "approvals.json"
        hook = build_persist_hook(approvals_path)
        assert hook("computer_open", "/x") is True
        from monkeybot.computer.approvals import load_approvals

        records = load_approvals(approvals_path)
        assert len(records) == 1
        assert records[0].tool == "computer_open"

    def test_silently_skips_mutating_tools(self, tmp_path: Path) -> None:
        """computer_move / computer_trash are excluded from ALWAYS_SCOPE — a stray
        `always: true` for one of them must not persist an overly-broad rule."""
        approvals_path = tmp_path / "approvals.json"
        hook = build_persist_hook(approvals_path)
        assert hook("computer_move", "/x") is False
        assert not approvals_path.exists()

    @pytest.mark.asyncio
    async def test_mutating_tool_always_is_not_remembered_end_to_end(
        self, tmp_path: Path, home: Path
    ) -> None:
        """The scenario the veto exists for: even without any durable write, a
        mutating tool's "always" must not silently allow a second call in the
        same session — resource_for_call only captures the source path, so an
        in-session-only remember would let a second computer_move act on any
        destination without asking."""
        from monkeybot.core.tools.permission import remember_always_approval, resource_for_call

        src = home / "Downloads" / "file.txt"
        src.parent.mkdir()
        src.write_text("x")
        dest1 = home / "Desktop"
        dest2 = home / "Documents"

        approvals_path = tmp_path / "approvals.json"
        persist = build_persist_hook(approvals_path)
        perm_path = tmp_path / "permissions.yaml"
        insp = build_computer_permission_inspector(perm_path, approvals_path)
        bus = _Bus()

        call = InspectorToolCall(
            "1", "computer_move", {"path": str(src), "destination": str(dest1)}
        )
        d1 = await insp.check(call, _ctx(bus=bus))
        assert d1.kind == "confirm"
        # Simulate the user approving with always=true, as tool_dispatch.py would.
        remember_always_approval(bus, "computer_move", resource_for_call(call), persist=persist)

        d2 = await insp.check(
            InspectorToolCall("2", "computer_move", {"path": str(src), "destination": str(dest2)}),
            _ctx(bus=bus),
        )
        assert d2.kind == "confirm"  # still asks — not silently allowed

    def test_skips_non_computer_tools(self, tmp_path: Path) -> None:
        """Must return True (no-op, changing nothing) for a tool this package
        doesn't own — returning False here previously broke "Always allow"
        app-wide for every non-computer tool whenever computer tools were
        enabled, since `remember_always_approval`'s veto semantics treat a
        False return as "don't remember this, not even in-session"."""
        approvals_path = tmp_path / "approvals.json"
        hook = build_persist_hook(approvals_path)
        assert hook("write_file", "/x") is True
        assert not approvals_path.exists()
