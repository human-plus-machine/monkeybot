"""T1-POL-01..03. Inspector decisions, empty allowlists, corrupt grants."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from monkeybot.computer.permissions import build_computer_permission_inspector
from monkeybot.core.context import TurnContext
from monkeybot.core.tools.grant_store import Grants, load_grants
from monkeybot.core.tools.inspector import (
    CommandTierConfigError,
    CommandTierInspector,
    InspectorToolCall,
    RulesInspector,
    load_command_tier_policy,
)
from monkeybot.core.tools.loop_inspector import LoopStartInspector
from monkeybot.core.tools.path_grant_inspector import PathGrantInspector
from monkeybot.core.tools.permission import (
    PermissionInspector,
    SessionApprovals,
    load_permissions,
)

_TABLE = Path(__file__).resolve().parent / "tables" / "policy_decisions.yaml"
_INSPECTORS = frozenset(
    {"command_tier", "rules", "path_grant", "permission", "loop_start", "computer"}
)


class _Bus:
    def __init__(self, approvals: SessionApprovals) -> None:
        self.session_approvals = approvals


def _tokens(tmp_path: Path) -> dict[str, str]:
    home = Path.home().resolve()
    workspace = (tmp_path / "ws").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "readme.txt").write_text("hello\n", encoding="utf-8")
    home_file = home / "contract-scratch" / "notes.txt"
    return {
        "{outside}": "/etc/hosts",
        "{home_env}": str(home / "contract-scratch" / ".env"),
        "{home_file}": str(home_file),
        "{home_file_dir}": str(home_file.parent),
        "{workspace_file}": str((workspace / "readme.txt").resolve()),
        "{workspace_root}": str(workspace),
        "{ssh}": str(home / ".ssh"),
    }


def _substitute(value: Any, tokens: dict[str, str]) -> Any:
    if isinstance(value, str):
        out = value
        for key, replacement in tokens.items():
            out = out.replace(key, replacement)
        return out
    if isinstance(value, list):
        return [_substitute(item, tokens) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, tokens) for key, item in value.items()}
    return value


def _context(row: dict[str, Any]) -> TurnContext:
    approvals = SessionApprovals()
    session = row.get("session_allow")
    if isinstance(session, list) and len(session) == 2:
        approvals.remember(str(session[0]), str(session[1]))
    return TurnContext(
        thread_id="t",
        request_id="r",
        agent_md="# agent",
        memory_index=[],
        skills=[],
        tools=[],
        user_id=None,
        parent_run_id=None,
        model="m",
        sse_bus=_Bus(approvals),
        turn_command_grants=set(row.get("turn_command_grants") or []),
        turn_path_grants=set(row.get("turn_path_grants") or []),
    )


def _write_allowlist(root: Path, row: dict[str, Any]) -> Path:
    document: dict[str, Any] = {"allowed_commands": list(row["allowed_commands"])}
    if "deny_patterns" in row:
        document["deny_patterns"] = list(row["deny_patterns"])
    path = root / "command_allowlist.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _write_grants(root: Path, row: dict[str, Any]) -> Path | None:
    raw = row.get("grants")
    if not isinstance(raw, dict):
        return None
    payload = {
        "commands": [
            {"command": name, "created_at": "2026-10-26T00:00:00Z"}
            for name in raw.get("commands") or []
        ],
        "paths": [
            {"path": folder, "mode": "read", "created_at": "2026-10-26T00:00:00Z"}
            for folder in raw.get("paths") or []
        ],
    }
    path = root / "grants.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_permissions(root: Path, row: dict[str, Any]) -> Path:
    document = {
        "default": row.get("permission_default", "allow"),
        "rules": list(row.get("rules") or []),
    }
    path = root / "permissions.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _write_approvals(root: Path, row: dict[str, Any]) -> Path:
    records = [
        {
            "tool": item["tool"],
            "resource": item["resource"],
            "scope": item.get("scope", "resource"),
            "created_at": "2026-10-26T00:00:00Z",
        }
        for item in row.get("approvals") or []
    ]
    path = root / "approvals.json"
    path.write_text(json.dumps({"version": 1, "approvals": records}), encoding="utf-8")
    return path


def _inspector(row: dict[str, Any], root: Path) -> Any:
    kind = row["inspector"]
    allow_ask = bool(row.get("allow_ask", True))
    if kind == "command_tier":
        allowlist = _write_allowlist(root, row)
        grants = _write_grants(root, row)
        return CommandTierInspector(allowlist, grants_path=grants)
    if kind == "rules":
        return RulesInspector(list(row.get("denied_patterns") or []))
    if kind == "path_grant":
        workspace = Path(str(row.get("workspace") or root))
        return PathGrantInspector(workspace_root=workspace, grants_path=_write_grants(root, row))
    if kind == "permission":
        return PermissionInspector(
            load_permissions(_write_permissions(root, row)), allow_ask=allow_ask
        )
    if kind == "loop_start":
        return LoopStartInspector()
    if kind == "computer":
        return build_computer_permission_inspector(
            _write_permissions(root, row),
            _write_approvals(root, row),
            allow_ask=allow_ask,
        )
    raise AssertionError(f"unknown inspector {kind}")


def _rows() -> list[dict[str, Any]]:
    document = yaml.safe_load(_TABLE.read_text(encoding="utf-8"))
    rows = list(document["rows"])
    assert len(rows) >= 40
    assert {row["inspector"] for row in rows} == _INSPECTORS
    return rows


@pytest.mark.case("T1-POL-01")
@pytest.mark.parametrize("row", _rows(), ids=[row["id"] for row in _rows()])
async def test_policy_decision_table(row: dict[str, Any], tmp_path: Path) -> None:
    """One inspector decision: allow, deny, or confirm, plus grant_kind."""
    prepared = _substitute(row, _tokens(tmp_path))
    call = InspectorToolCall(
        call_id="c",
        name=str(prepared["tool"]),
        args=dict(prepared.get("args") or {}),
    )
    decision = await _inspector(prepared, tmp_path).check(call, _context(prepared))
    expect = prepared["expect"]
    grant_kind = expect["grant_kind"]
    assert decision.kind == expect["kind"], prepared["id"]
    assert decision.grant_kind == grant_kind, prepared["id"]


@pytest.mark.case("T1-POL-02")
def test_empty_allowed_commands_raises(tmp_path: Path) -> None:
    """An allowlist with allowed_commands: [] raises."""
    path = tmp_path / "command_allowlist.yaml"
    path.write_text("allowed_commands: []\n", encoding="utf-8")
    with pytest.raises(CommandTierConfigError, match="allowed_commands"):
        load_command_tier_policy(path)


@pytest.mark.case("T1-POL-03")
def test_corrupt_grants_load_empty_and_log_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bad JSON in grants.json loads as empty and logs once across two loads."""
    path = tmp_path / "grants.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="monkeybot.core.tools.grant_store"):
        first = load_grants(path)
        second = load_grants(path)
    assert first == Grants()
    assert second == Grants()
    grant_logs = [record for record in caplog.records if record.name.endswith("grant_store")]
    assert len(grant_logs) == 1
