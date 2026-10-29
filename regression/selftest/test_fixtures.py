"""§5.4 fixture properties.

``legacy_db`` is P2-07. The coding repos under ``workspaces/`` moved there from
the old evals tree in P4-01.
Generated pieces (oversized file, 301-entry directory, binary, symlinks) are built
once per session by ``regression.fixtures.build`` and are not committed.
"""

from __future__ import annotations

import base64
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from monkeybot.computer.safety import is_credential_path
from monkeybot.core.attachments.store import sniff_mime
from monkeybot.core.config.settings import get_verifier_config
from monkeybot.core.context import _discover_skills
from monkeybot.core.context.project_context import load_project_instructions
from monkeybot.core.mcp.mcp_client import MCPClient, MCPDiagnosticError
from monkeybot.core.tools.core_tool_executor import _LIST_DIR_MAX_ENTRIES
from monkeybot.core.tools.grant_store import Grants, load_grants
from monkeybot.core.tools.inspector import (
    DEFAULT_DENY_PATTERNS,
    CommandTierConfigError,
    InspectorToolCall,
    load_command_tier_policy,
)
from monkeybot.core.tools.path_grant_inspector import PathGrantInspector
from monkeybot.core.tools.permission import evaluate, load_permissions
from monkeybot.core.tools.workspace_service import (
    WorkspaceError,
    WorkspaceFileService,
    WorkspaceSettings,
)
from monkeybot.providers._openai_compat import _extract_pdf_text_sync
from regression.fixtures.build import (
    CROWD_ENTRY_COUNT,
    HOSTILE_BINARY_NAME,
    HOSTILE_CROWD_DIR,
    HOSTILE_HUGE_NAME,
    HOSTILE_LINK_NAME,
    HOSTILE_SOURCE,
    HOSTILE_WRITE_SCOPE,
    HUGE_MIN_BYTES,
    SKILL_ESCAPE_NAME,
    SKILLS_SOURCE,
    BuiltFixtures,
    build,
    refresh_seeded_attachment_clocks,
)

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "regression" / "fixtures"
CONFIGS = FIXTURES / "configs"
GRANTS = FIXTURES / "workspaces" / "grants_ws"
DRIFT = FIXTURES / "workspaces" / "drift_ws"
ATTACHMENTS = REPO / "testkit" / "data" / "attachments"
GREP_MAX_FILE_BYTES = WorkspaceSettings().WORKSPACE_GREP_MAX_FILE_BYTES


@dataclass
class _GrantCtx:
    turn_path_grants: frozenset[str] = frozenset()


@pytest.fixture(scope="session")
def generated(tmp_path_factory: pytest.TempPathFactory) -> BuiltFixtures:
    return build(tmp_path_factory.mktemp("generated-fixtures"))


def _lines(text: str) -> list[str]:
    return text.splitlines()


def test_hostile_text_fixtures_are_committed() -> None:
    root = HOSTILE_SOURCE
    for relative in (
        "src/app.py",
        "notes.txt",
        "node_modules/left.js",
        ".gitignore",
        "pkg/.gitignore",
        "pkg/hidden.txt",
        "pkg/visible.txt",
        "secret.txt",
        "wide.txt",
        "AGENTS.md",
    ):
        assert (root / relative).is_file(), relative
    assert "secret.txt" in (root / ".gitignore").read_text(encoding="utf-8").split()
    assert "hidden.txt" in (root / "pkg" / ".gitignore").read_text(encoding="utf-8").split()
    assert any(line == "## Runtime notes" for line in _lines((root / "AGENTS.md").read_text()))
    for name in (HOSTILE_HUGE_NAME, HOSTILE_BINARY_NAME, HOSTILE_LINK_NAME, HOSTILE_CROWD_DIR):
        assert not (root / name).exists()
    assert not (SKILLS_SOURCE / SKILL_ESCAPE_NAME).exists()


def test_gitignore_is_honored(generated: BuiltFixtures) -> None:
    """Default grep skips ignored files.

    A ``file_glob`` is not used here. Ripgrep treats ``--glob`` as a whitelist
    and will search an ignored path the glob names. The root scan is partial
    because the generated oversized file is in the same tree; ignored paths
    stay out of ``matches``.
    """
    service = WorkspaceFileService(generated.hostile)
    hidden = service.grep("HOSTILE_SECRET_TOKEN")
    assert hidden["scan_complete"] is False
    assert "secret.txt" not in [match["path"] for match in hidden["matches"]]
    shown = service.grep("HOSTILE_SECRET_TOKEN", respect_gitignore=False)
    assert "secret.txt" in [match["path"] for match in shown["matches"]]

    nested_hidden = service.grep("NESTED_IGNORE_TOKEN", root="pkg")
    assert nested_hidden["matches"] == []
    nested_shown = service.grep("NESTED_IGNORE_TOKEN", root="pkg", respect_gitignore=False)
    assert any(match["path"] == "pkg/hidden.txt" for match in nested_shown["matches"])
    nested_visible = service.grep("NESTED_VISIBLE_TOKEN", root="pkg")
    assert any(match["path"] == "pkg/visible.txt" for match in nested_visible["matches"])


def test_node_modules_is_skipped(generated: BuiltFixtures) -> None:
    service = WorkspaceFileService(generated.hostile)
    assert service.grep("NODE_MODULES_TOKEN", file_glob="*.js")["matches"] == []
    walked = service.glob_paths("**/*.js")
    assert "node_modules/left.js" not in walked["paths"]
    named = service.glob_paths("node_modules/left.js")
    assert named["paths"] == ["node_modules/left.js"]


def test_size_limits_are_exceeded(generated: BuiltFixtures) -> None:
    service = WorkspaceFileService(generated.hostile)
    huge = generated.hostile / HOSTILE_HUGE_NAME
    assert huge.stat().st_size >= HUGE_MIN_BYTES
    assert huge.stat().st_size > 512 * 1024
    assert huge.stat().st_size > GREP_MAX_FILE_BYTES
    oversized = service.grep("H", file_glob=HOSTILE_HUGE_NAME)
    assert oversized["incomplete"]["files_skipped_oversized"] >= 1

    blob = (generated.hostile / HOSTILE_BINARY_NAME).read_bytes()
    assert b"\x00" in blob[:8192]
    assert len(blob) <= GREP_MAX_FILE_BYTES
    binary = service.grep("blob", file_glob=HOSTILE_BINARY_NAME)
    assert binary["scan_complete"] is True
    assert binary["matches"] == []

    entries = service.list_directory(HOSTILE_CROWD_DIR)
    assert len(entries) == CROWD_ENTRY_COUNT
    assert len(entries) > _LIST_DIR_MAX_ENTRIES


def test_symlink_leaves_the_hostile_root(generated: BuiltFixtures) -> None:
    link = generated.hostile / HOSTILE_LINK_NAME
    assert link.is_symlink()
    assert link.resolve() == generated.link_target
    assert not link.resolve().is_relative_to(generated.hostile.resolve())
    service = WorkspaceFileService(generated.hostile)
    with pytest.raises(WorkspaceError) as exc:
        service.read_file(HOSTILE_LINK_NAME)
    assert exc.value.code == "path_escape"


def test_wide_txt_is_outside_the_write_scope(generated: BuiltFixtures, tmp_path: Path) -> None:
    workspace = tmp_path / "hostile"
    shutil.copytree(generated.hostile, workspace, symlinks=True)
    service = WorkspaceFileService(
        workspace, WorkspaceSettings(WORKSPACE_WRITE_SCOPE_REL=HOSTILE_WRITE_SCOPE)
    )
    with pytest.raises(WorkspaceError) as exc:
        service.write_file("wide.txt", "nope")
    assert exc.value.code == "write_outside_scope"
    written = service.write_file("src/inside-scope.txt", "ok")
    assert written["ok"] is True


def test_agents_heading_is_neutralized(generated: BuiltFixtures) -> None:
    loaded = load_project_instructions(generated.hostile)
    assert not any(line == "## Runtime notes" for line in _lines(loaded))
    assert any(line == "### Runtime notes" for line in _lines(loaded))


async def test_grants_folder_is_outside_the_workspace(tmp_path: Path) -> None:
    loaded = load_grants(CONFIGS / "grants.json")
    assert [item.command for item in loaded.commands] == ["ffmpeg"]
    assert len(loaded.paths) == 1
    granted = (REPO / loaded.paths[0].path).resolve()
    workspace = (GRANTS / "workspace").resolve()
    assert granted.is_dir()
    assert (granted / ".env").is_file()
    assert (granted / "notes.md").is_file()
    assert not granted.is_relative_to(workspace)
    assert is_credential_path(granted / ".env")
    assert not is_credential_path(granted / "notes.md")

    grants_path = tmp_path / "grants.json"
    grants_path.write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [{"command": "ffmpeg", "created_at": "2026-01-01T00:00:00Z"}],
                "paths": [
                    {"path": str(granted), "mode": "read", "created_at": "2026-01-01T00:00:00Z"}
                ],
            }
        ),
        encoding="utf-8",
    )
    inspector = PathGrantInspector(workspace_root=workspace, grants_path=grants_path)
    ctx = _GrantCtx()
    notes = await inspector.check(
        InspectorToolCall("notes", "read_file", {"path": str(granted / "notes.md")}),
        ctx,  # type: ignore[arg-type]
    )
    secret = await inspector.check(
        InspectorToolCall("env", "read_file", {"path": str(granted / ".env")}),
        ctx,  # type: ignore[arg-type]
    )
    assert notes.kind == "allow"
    assert secret.kind == "deny"


def test_corrupt_grants_load_empty() -> None:
    assert load_grants(CONFIGS / "grants.corrupt.json") == Grants()


def test_command_allowlists() -> None:
    default = load_command_tier_policy(CONFIGS / "command_allowlist.default.yaml")
    strict = load_command_tier_policy(CONFIGS / "command_allowlist.strict.yaml")
    omitted = load_command_tier_policy(CONFIGS / "command_allowlist.no_deny_patterns.yaml")
    assert set(strict.allowed_commands) < set(default.allowed_commands)
    assert "deny_patterns:" not in (CONFIGS / "command_allowlist.no_deny_patterns.yaml").read_text(
        encoding="utf-8"
    )
    assert omitted.deny_patterns == DEFAULT_DENY_PATTERNS
    with pytest.raises(CommandTierConfigError, match="allowed_commands"):
        load_command_tier_policy(CONFIGS / "command_allowlist.empty.yaml")


def test_permissions_last_match_wins() -> None:
    default = load_permissions(CONFIGS / "permissions.default.yaml")
    assert default.default == "allow"
    assert default.rules == ()
    overlap = load_permissions(CONFIGS / "permissions.overlap.yaml")
    push = evaluate("run_command", "git push origin", overlap.rules)
    status = evaluate("run_command", "git status", overlap.rules)
    assert push is not None and push.effect == "deny"
    assert status is not None and status.effect == "allow"


def _mcp(name: str) -> dict[str, object]:
    loaded = json.loads((CONFIGS / name).read_text(encoding="utf-8"))
    servers = loaded["mcpServers"]
    assert isinstance(servers, dict)
    return servers


def test_mcp_autoconnect_progressive_and_two_prefix() -> None:
    autoconnect = _mcp("mcp.autoconnect.json")["fixture"]
    progressive = _mcp("mcp.progressive.json")["fixture"]
    assert isinstance(autoconnect, dict) and autoconnect["autoConnect"] is True
    assert isinstance(progressive, dict) and progressive["autoConnect"] is False
    broken = _mcp("mcp.broken.json")
    fixture = broken["fixture"]
    sibling = broken["broken"]
    assert isinstance(fixture, dict) and fixture["command"]
    assert isinstance(sibling, dict) and sibling["command"] == ""
    names = set(_mcp("mcp.two_prefix.json"))
    assert names == {"fix", "fix__extra"}
    sample = "fix__extra__ping"
    assert sample.startswith("fix__")
    assert sample.startswith("fix__extra__")


async def test_mcp_broken_command_raises() -> None:
    sibling = _mcp("mcp.broken.json")["broken"]
    assert isinstance(sibling, dict)
    client = MCPClient()
    with pytest.raises(MCPDiagnosticError, match="missing command"):
        await client._connect_from_spec(
            "broken",
            sibling,
            mcp_json_path=CONFIGS / "mcp.broken.json",
            raise_on_error=True,
        )


def test_verifier_configs() -> None:
    tracker = get_verifier_config(str(CONFIGS / "verifier.tracker.yaml"))
    assert tracker.enabled is True
    assert tracker.tracker.enabled is True
    assert tracker.judge.enabled is False
    assert tracker.escalation.max_severity == "nudge"
    judge = get_verifier_config(str(CONFIGS / "verifier.judge.yaml"))
    assert judge.judge.enabled is True
    assert judge.judge.model == "fake"
    replan = get_verifier_config(str(CONFIGS / "verifier.replan.yaml"))
    assert replan.escalation.max_severity == "replan"
    block = get_verifier_config(str(CONFIGS / "verifier.block.yaml"))
    assert block.escalation.max_severity == "block"


def test_drift_workspace_disagrees_with_its_target() -> None:
    migrations = DRIFT / "db" / "migrations"
    assert migrations.is_dir()
    assert any(migrations.iterdir())
    migration = (migrations / "001_init.sql").read_text(encoding="utf-8")
    model = (DRIFT / "src" / "models.py").read_text(encoding="utf-8")
    done = (DRIFT / "DONE.txt").read_text(encoding="utf-8")
    assert "name" in migration
    assert "email" in model
    assert "email" not in migration
    assert "target" in done


def test_skills_discovery_skips_nested_and_escape_leaves_the_root(
    generated: BuiltFixtures, tmp_path: Path
) -> None:
    assert (SKILLS_SOURCE / "diagnostics" / "SKILL.md").is_file()
    assert (SKILLS_SOURCE / "plain" / "SKILL.md").is_file()
    assert (SKILLS_SOURCE / "a" / "nested" / "SKILL.md").is_file()
    found = {skill.name: skill.description for skill in _discover_skills(generated.skills)}
    assert found["diagnostics"] == "Inspect a failing run and report the first error."
    assert found["plain"] == "Print the workspace layout before changing files."
    assert "nested" not in found
    assert "a" not in found
    assert SKILL_ESCAPE_NAME not in found
    escape = generated.skills / SKILL_ESCAPE_NAME
    assert escape.is_symlink()
    assert escape.resolve() == generated.skill_target
    assert not escape.resolve().is_relative_to(generated.skills.resolve())
    service = WorkspaceFileService(tmp_path, skills_root=generated.skills)
    with pytest.raises(WorkspaceError) as exc:
        service.read_file(f"skills/{SKILL_ESCAPE_NAME}")
    assert exc.value.code == "path_escape"


def test_attachments_match_their_kinds() -> None:
    png = (ATTACHMENTS / "tiny.png").read_bytes()
    jpeg = (ATTACHMENTS / "tiny.jpg").read_bytes()
    with_text = (ATTACHMENTS / "with-text.pdf").read_bytes()
    without_text = (ATTACHMENTS / "without-text.pdf").read_bytes()
    assert sniff_mime(png) == "image/png"
    assert sniff_mime(jpeg) == "image/jpeg"
    assert sniff_mime(with_text) == "application/pdf"
    assert sniff_mime(without_text) == "application/pdf"
    extracted = _extract_pdf_text_sync(base64.b64encode(with_text).decode(), 1000)
    assert extracted is not None
    assert "Hello fixture" in extracted[0]
    assert _extract_pdf_text_sync(base64.b64encode(without_text).decode(), 1000) is None


def test_seeded_attachment_clocks_are_restamped_on_the_copy(tmp_path: Path) -> None:
    session = tmp_path / ".monkeybot" / "attachments" / "loop-scenario"
    session.mkdir(parents=True)
    meta = session / "att-pdf.json"
    meta.write_text(
        json.dumps({"attachment_id": "att-pdf", "created_at_ms": 1}),
        encoding="utf-8",
    )
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"created_at_ms": 1}), encoding="utf-8")
    (session / "att-link.json").symlink_to(outside)
    (session / "note.json").write_text(json.dumps({"filename": "x"}), encoding="utf-8")

    refresh_seeded_attachment_clocks(tmp_path, now_ms=1234)

    assert json.loads(meta.read_text(encoding="utf-8"))["created_at_ms"] == 1234
    assert json.loads(outside.read_text(encoding="utf-8"))["created_at_ms"] == 1
    assert "created_at_ms" not in json.loads((session / "note.json").read_text(encoding="utf-8"))


def test_subagent_tapes_are_present() -> None:
    root = FIXTURES / "subagent_tapes"
    names = sorted(path.name for path in root.glob("*.ndjson"))
    assert names == [
        "absent_file.ndjson",
        "child_error.ndjson",
        "echo.ndjson",
        "hold.ndjson",
        "large_line.ndjson",
        "nested_task.ndjson",
        "present_file.ndjson",
        "roundtrip.ndjson",
        "system_prompt.ndjson",
    ]
