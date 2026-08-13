"""Resolve the Python interpreter that should run the gateway.

The CLI is intentionally thin: it depends only on base ``monkeybot`` and does **not**
pull in provider/storage extras (``bedrock``, ``postgres``, …). Those extras are
declared on the *agent project* (e.g. ``pr-review-agent/pyproject.toml`` lists
``monkeybot[bedrock,postgres]``). To honor that, the gateway must be spawned from
the agent project's interpreter rather than the CLI's own ``sys.executable``.

Resolution order for an agent root:

1. ``<root>/.venv/bin/python`` (or the Windows variant) — direct, no subprocess overhead.
2. ``uv run python`` — when ``<root>/pyproject.toml`` exists but no ``.venv``.
3. ``sys.executable`` — legacy / config-only trees (just ``monkeybot_config/``, no
   ``pyproject.toml``). In this case extras must be installed in the CLI env.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from monkeybot_cli.scaffold import refresh_agent_pyproject

DEFAULT_PORT = 8080
SSE_GATEWAY_MODULE = "monkeybot.gateway.main"
COMBINED_GATEWAY_MODULE = "monkeybot.gateway.realtime_main"


def _venv_python(agent_root: Path) -> Path | None:
    """Return the project venv interpreter if it exists, else ``None``."""
    venv = agent_root / ".venv"
    for candidate in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return None


@dataclass(frozen=True)
class RuntimePython:
    """Resolved Python runtime for an agent project.

    ``argv`` is the prefix to prepend to ``-m monkeybot.gateway.*`` or
    ``-c "…"`` doctor probes. ``source`` is for diagnostics/remediation text.
    ``agent_root`` is set for ``uv run`` resolution (probes need the project cwd).
    """

    argv: list[str]
    source: str  # "venv" | "uv" | "cli"
    agent_root: Path | None = None


def resolve_runtime_python(agent_root: Path) -> RuntimePython:
    """Resolve the interpreter that should run the gateway for ``agent_root``."""
    venv_py = _venv_python(agent_root)
    if venv_py is not None:
        return RuntimePython([str(venv_py)], "venv", agent_root)
    if (agent_root / "pyproject.toml").is_file():
        return RuntimePython(["uv", "run", "python"], "uv", agent_root)
    return RuntimePython([sys.executable], "cli", agent_root)


_CORE_PROBE = (
    "import monkeybot; "
    "from importlib.metadata import version; "
    "ver = version('monkeybot'); "
    "parts = [int(p) for p in ver.split('.')[:3]]; "
    "assert [3, 0, 0] <= parts < [4, 0, 0], ver"
)
_MEMORY_PROBE = f"import mempalace; {_CORE_PROBE}"


class RuntimeUpgradeError(RuntimeError):
    """Raised when the agent interpreter cannot be upgraded to a compatible MonkeyBot."""


def prepare_runtime_python(
    agent_root: Path,
    config_path: Path | str | None = None,
) -> RuntimePython:
    """Resolve the gateway interpreter, upgrading its lock when dependencies are stale.

    MemPalace is required and installed only when memory is enabled for the
    effective gateway config. Every runtime must contain a compatible MonkeyBot
    3.x core.
    """
    from monkeybot.core.memory.config import memory_enabled_from_config

    runtime = resolve_runtime_python(agent_root)
    has_project = (agent_root / "pyproject.toml").is_file()
    effective_config = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else agent_root / "monkeybot_config" / "monkeybot.yaml"
    )
    memory_enabled = memory_enabled_from_config(
        str(effective_config) if effective_config.is_file() else None
    )
    probe = _MEMORY_PROBE if memory_enabled else _CORE_PROBE
    pyproject_updated = has_project and refresh_agent_pyproject(
        agent_root, include_memory=memory_enabled
    ).endswith(": updated")
    if run_probe(runtime, probe) and not pyproject_updated:
        return runtime
    if not has_project:
        requirement = "monkeybot[memory]>=3.0.0,<4" if memory_enabled else "monkeybot>=3.0.0,<4"
        raise RuntimeUpgradeError(
            "gateway interpreter is missing compatible harness packages; "
            f"install {requirement} in this environment before starting the gateway"
        )
    print(
        f"agent runtime dependencies are stale; upgrading monkeybot lock in {agent_root}",
        flush=True,
    )
    lock = subprocess.run(
        ["uv", "lock", "--upgrade-package", "monkeybot"],
        cwd=agent_root,
        check=False,
    )
    sync = subprocess.run(["uv", "sync"], cwd=agent_root, check=False)
    runtime = resolve_runtime_python(agent_root)
    if lock.returncode != 0 or sync.returncode != 0 or not run_probe(runtime, probe):
        detail = _probe_failure_detail(runtime, probe)
        capability = "MonkeyBot with MemPalace" if memory_enabled else "MonkeyBot"
        raise RuntimeUpgradeError(
            f"failed to upgrade the agent runtime to a compatible {capability}; "
            "refusing to start a stale gateway" + (f" ({detail})" if detail else "")
        )
    return runtime


def gateway_argv(
    runtime: RuntimePython,
    *,
    module: str = COMBINED_GATEWAY_MODULE,
) -> list[str]:
    """Full argv to launch a gateway module under ``runtime``.

    CLI auto-start defaults to the combined SSE+WebSocket entrypoint
    (``realtime_main``) so ``chat`` and ``talk`` share one process/port.
    """
    return [*runtime.argv, "-m", module]


def _runtime_cwd(runtime: RuntimePython) -> dict[str, object]:
    """Extra kwargs needed to run a probe under ``runtime`` (cwd for uv projects)."""
    if runtime.source == "uv" and runtime.agent_root is not None:
        return {"cwd": str(runtime.agent_root)}
    return {}


def run_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> bool:
    """Run ``python -c code`` under ``runtime`` and return True on exit 0.

    Used by ``doctor`` to verify extras/imports in the *gateway* interpreter
    rather than the CLI's own process.
    """
    proc = subprocess.run(
        [*runtime.argv, "-c", code],
        capture_output=True,
        timeout=timeout,
        **_runtime_cwd(runtime),
    )
    return proc.returncode == 0


def _probe_failure_detail(runtime: RuntimePython, probe: str, *, timeout: float = 15.0) -> str:
    try:
        proc = subprocess.run(
            [*runtime.argv, "-c", probe],
            capture_output=True,
            text=True,
            timeout=timeout,
            **_runtime_cwd(runtime),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return str(exc)
    text = (proc.stderr or proc.stdout or "").strip()
    if not text:
        return f"probe exit {proc.returncode}"
    return text[-500:]
