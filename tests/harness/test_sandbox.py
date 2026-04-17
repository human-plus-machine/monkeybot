"""Unit tests for Policy + LocalShellSandbox."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.harness.errors import SandboxDenied
from src.core.harness.sandbox.local_shell import LocalShellSandbox
from src.core.harness.sandbox.policy import Policy
from src.core.harness.specs import PolicySpec


def _allow_all_policy(tmp_path: Path) -> Policy:
    return Policy.from_spec(
        PolicySpec(fs_allow=["/**"], fs_deny=[], net_allow=[], net_deny=["*"]),
        timeout_seconds=30,
    )


def test_policy_fs_deny() -> None:
    p = Policy.from_spec(
        PolicySpec(fs_allow=["/tmp/**"], fs_deny=["/etc/**"]),
        timeout_seconds=30,
    )
    assert p.check_fs("/tmp/ok.txt")
    assert not p.check_fs("/etc/passwd")


@pytest.mark.asyncio
async def test_local_shell_echo(tmp_path: Path) -> None:
    sbx = LocalShellSandbox()
    result = await sbx.execute(["echo", "hi"], policy=_allow_all_policy(tmp_path))
    assert result.exit_code == 0
    assert "hi" in result.stdout


@pytest.mark.asyncio
async def test_local_shell_fs_deny(tmp_path: Path) -> None:
    p = Policy.from_spec(
        PolicySpec(fs_allow=["/tmp/**"], fs_deny=["/etc/**"]),
        timeout_seconds=10,
    )
    sbx = LocalShellSandbox()
    with pytest.raises(SandboxDenied):
        await sbx.read_file("/etc/passwd", policy=p)


def test_capabilities_report_local_shell() -> None:
    caps = LocalShellSandbox().capabilities()
    assert caps.network_egress_control is False


@pytest.mark.asyncio
async def test_execute_timeout(tmp_path: Path) -> None:
    p = Policy.from_spec(PolicySpec(fs_allow=["/**"]), timeout_seconds=1)
    sbx = LocalShellSandbox()
    result = await sbx.execute(["sleep", "3"], policy=p)
    assert result.exit_code == 124
