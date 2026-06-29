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

import sys
from dataclasses import dataclass
from pathlib import Path


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

    ``argv`` is the prefix to prepend to ``-m monkeybot.gateway.main`` or
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


def gateway_argv(runtime: RuntimePython) -> list[str]:
    """Full argv to launch ``monkeybot.gateway.main`` under ``runtime``."""
    return [*runtime.argv, "-m", "monkeybot.gateway.main"]


def run_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> bool:
    """Run ``python -c code`` under ``runtime`` and return True on exit 0.

    Used by ``doctor`` to verify extras/imports in the *gateway* interpreter
    rather than the CLI's own process.
    """
    import subprocess

    kwargs: dict[str, object] = {}
    if runtime.source == "uv" and runtime.agent_root is not None:
        kwargs["cwd"] = str(runtime.agent_root)
    proc = subprocess.run(
        [*runtime.argv, "-c", code],
        capture_output=True,
        timeout=timeout,
        **kwargs,
    )
    return proc.returncode == 0
