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


_HARNESS_PROBE = "import mempalace"


def prepare_runtime_python(agent_root: Path) -> RuntimePython:
    """Resolve the gateway interpreter, syncing the agent venv when harness deps are missing.

    Agent ``.venv`` trees are created once and reused. New transitive deps on
    ``monkeybot`` (e.g. ``mempalace``) do not install themselves; ``uv sync``
    against the agent's ``pyproject.toml`` pulls them in.
    """
    runtime = resolve_runtime_python(agent_root)
    if runtime.source != "venv" or not (agent_root / "pyproject.toml").is_file():
        return runtime
    if run_probe(runtime, _HARNESS_PROBE):
        return runtime
    print(
        f"agent venv is missing harness packages; running uv sync in {agent_root}",
        flush=True,
    )
    sync = subprocess.run(["uv", "sync"], cwd=agent_root, check=False)
    if sync.returncode != 0 or not run_probe(runtime, _HARNESS_PROBE):
        print(
            "warning: uv sync did not make mempalace importable in the agent venv",
            flush=True,
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


def run_probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> bool:
    """Run ``python -c code`` under ``runtime`` and return True on exit 0.

    Used by ``doctor`` to verify extras/imports in the *gateway* interpreter
    rather than the CLI's own process.
    """
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
