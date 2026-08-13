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
4. ``<cache>/monkeybot/runtimes/<key>/bin/python`` — a CLI-managed venv holding
   ``monkeybot[memory]`` pinned to the running core version. Used only for config-only
   trees that enable memory when the CLI env itself has no MemPalace: the published
   CLI stays lean (``monkeybot[cli]``) because MemPalace pulls chromadb/onnxruntime,
   which have no wheels on manylinux2014/musl. Provisioned once, then reused offline.
   Because step 4 *replaces* ``sys.executable``, the managed runtime also mirrors the
   provider/storage extras present in the CLI env — otherwise a config-only agent on
   ``monkeybot[openai]`` would lose its provider the moment memory was enabled.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from importlib.metadata import version as _package_version
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

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
    source: str  # "venv" | "uv" | "cli" | "cli-managed"
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


MANAGED_RUNTIME_SOURCE = "cli-managed"


def _cache_root() -> Path:
    """Base cache directory: ``XDG_CACHE_HOME``, ``LOCALAPPDATA`` on Windows, else ``~/.cache``."""
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    if xdg:
        return Path(xdg)
    if sys.platform.startswith("win"):
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data)
    return Path.home() / ".cache"


def _sanitize_key_part(value: str) -> str:
    return "".join(char if (char.isalnum() or char in "._-") else "-" for char in value)


# Extras that belong to the CLI *client* rather than the gateway. `cli`/`cli-realtime`
# add typer and pyaudio (which needs portaudio headers to build from source) without
# giving the gateway anything, so mirroring them is pure risk. `memory` is always added.
_UNMIRRORED_EXTRAS = frozenset({"memory", "cli", "cli-realtime"})


def _package_installed(name: str) -> bool:
    try:
        _package_version(name)
    except PackageNotFoundError:
        return False
    return True


def _monkeybot_extra_requirements() -> dict[str, list[Requirement]]:
    """Map each ``monkeybot`` extra to the requirements it alone contributes.

    A requirement belongs to ``extra`` when its marker holds for that extra but not for
    the empty extra — which excludes base dependencies carrying unrelated markers
    (``python_version < "3.11"`` and friends).
    """
    dist = distribution("monkeybot")
    parsed: list[Requirement] = []
    for raw in dist.requires or []:
        try:
            parsed.append(Requirement(raw))
        except InvalidRequirement:
            continue
    mapping: dict[str, list[Requirement]] = {}
    for raw_extra in dist.metadata.get_all("Provides-Extra") or []:
        extra = str(raw_extra).strip()
        if not extra:
            continue
        mapping[canonicalize_name(extra)] = [
            req
            for req in parsed
            if req.marker is not None
            and req.marker.evaluate({"extra": extra})
            and not req.marker.evaluate({"extra": ""})
        ]
    return mapping


def _extra_satisfied(
    extra: str,
    mapping: dict[str, list[Requirement]],
    cache: dict[str, bool],
    stack: set[str],
) -> bool:
    """True when every package contributed by ``extra`` is installed in this env."""
    if extra in cache:
        return cache[extra]
    if extra in stack:  # self-referential extra; treated as satisfied by its own cycle
        return True
    requirements = mapping.get(extra)
    if not requirements:
        return False
    stack.add(extra)
    satisfied = True
    for req in requirements:
        if canonicalize_name(req.name) == "monkeybot":
            satisfied = all(
                _extra_satisfied(canonicalize_name(nested), mapping, cache, stack)
                for nested in req.extras
            )
        else:
            satisfied = _package_installed(req.name)
        if not satisfied:
            break
    stack.discard(extra)
    cache[extra] = satisfied
    return satisfied


def mirrored_monkeybot_extras() -> tuple[str, ...]:
    """Extras installed alongside ``monkeybot`` in the CLI env, to carry into the runtime.

    The managed runtime replaces ``sys.executable`` outright, so any provider or storage
    extra the CLI env satisfies (``openai``, ``bedrock``, ``postgres``, …) has to come
    with it. Detection is metadata-driven rather than a hardcoded list so new extras are
    picked up automatically. Returns ``()`` when the metadata cannot be read.
    """
    try:
        mapping = _monkeybot_extra_requirements()
    except PackageNotFoundError:
        return ()
    cache: dict[str, bool] = {}
    return tuple(
        sorted(
            extra
            for extra in mapping
            if extra not in _UNMIRRORED_EXTRAS and _extra_satisfied(extra, mapping, cache, set())
        )
    )


def managed_memory_runtime_dir(
    monkeybot_version: str,
    extras: Sequence[str] = (),
) -> Path:
    """Deterministic cache directory for the managed MemPalace runtime.

    Keyed by the pinned MonkeyBot version, the CLI's Python version, and the mirrored
    extras, so that a CLI upgrade, a Python upgrade, or a change to the installed extras
    provisions a fresh runtime instead of reusing a mismatched one.
    """
    key = (
        f"memory-{_sanitize_key_part(monkeybot_version)}"
        f"-py{sys.version_info.major}.{sys.version_info.minor}"
    )
    normalized = sorted(canonicalize_name(extra) for extra in extras)
    if normalized:
        digest = hashlib.sha256(",".join(normalized).encode()).hexdigest()[:8]
        key = f"{key}-{digest}"
    return _cache_root() / "monkeybot" / "runtimes" / key


def _managed_interpreter(runtime_dir: Path) -> Path | None:
    for candidate in (runtime_dir / "bin" / "python", runtime_dir / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return None


def _managed_runtime_error(detail: str) -> RuntimeUpgradeError:
    """Actionable fail-closed error for the managed MemPalace runtime."""
    return RuntimeUpgradeError(
        "memory is enabled but MemPalace could not be provisioned for this config-only agent"
        + (f" ({detail})" if detail else "")
        + "; install it yourself with "
        "`uv tool install --with 'monkeybot-cli[memory]' monkeybot-cli` "
        "or set `memory.enabled: false` in monkeybot_config/monkeybot.yaml. Note that some "
        "platforms (manylinux2014, musl) ship no onnxruntime wheel for Python "
        f"{sys.version_info.major}.{sys.version_info.minor}, so chromadb — and therefore "
        "MemPalace — cannot be installed there."
    )


def _command_failure_detail(label: str, proc: subprocess.CompletedProcess[str]) -> str:
    text = ((proc.stderr or "") or (proc.stdout or "")).strip()
    return f"{label} exited {proc.returncode}" + (f": {text[-500:]}" if text else "")


def _run_provisioning(argv: list[str], label: str) -> None:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise _managed_runtime_error(f"{label} failed: {exc}") from exc
    if proc.returncode != 0:
        raise _managed_runtime_error(_command_failure_detail(label, proc))


def provision_managed_memory_runtime(agent_root: Path) -> RuntimePython:
    """Return a CLI-managed venv holding ``monkeybot[memory]`` at the running core version.

    The extras the CLI env already satisfies are mirrored into the runtime alongside
    ``memory``, since this interpreter replaces ``sys.executable`` for the gateway.
    A cached runtime that already passes the memory probe is reused as-is — no network
    access and no reinstall. Otherwise the runtime is created with ``uv venv`` and
    ``uv pip install``, pinned exactly to the MonkeyBot version running this CLI.
    """
    try:
        monkeybot_version = _package_version("monkeybot")
    except PackageNotFoundError as exc:
        raise _managed_runtime_error("the running monkeybot core version is unknown") from exc

    extras = mirrored_monkeybot_extras()
    runtime_dir = managed_memory_runtime_dir(monkeybot_version, extras)
    cached_interpreter = _managed_interpreter(runtime_dir)
    if cached_interpreter is not None:
        cached = RuntimePython([str(cached_interpreter)], MANAGED_RUNTIME_SOURCE, agent_root)
        if run_probe(cached, _MEMORY_PROBE):
            return cached

    if shutil.which("uv") is None:
        raise _managed_runtime_error("uv was not found on PATH")
    requested = ",".join([*extras, "memory"])
    requirement = f"monkeybot[{requested}]=={monkeybot_version}"
    print(
        f"agent memory runtime is missing MemPalace; provisioning {requirement} in {runtime_dir}",
        flush=True,
    )
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    _run_provisioning(["uv", "venv", str(runtime_dir)], "uv venv")
    interpreter = _managed_interpreter(runtime_dir)
    if interpreter is None:
        raise _managed_runtime_error(f"no interpreter was created in {runtime_dir}")
    _run_provisioning(
        ["uv", "pip", "install", "--python", str(interpreter), requirement],
        "uv pip install",
    )
    runtime = RuntimePython([str(interpreter)], MANAGED_RUNTIME_SOURCE, agent_root)
    if not run_probe(runtime, _MEMORY_PROBE):
        raise _managed_runtime_error(_probe_failure_detail(runtime, _MEMORY_PROBE))
    return runtime


def prepare_runtime_python(
    agent_root: Path,
    config_path: Path | str | None = None,
) -> RuntimePython:
    """Resolve the gateway interpreter, upgrading its lock when dependencies are stale.

    MemPalace is required and installed only when memory is enabled for the
    effective gateway config. Every runtime must contain a compatible MonkeyBot
    3.x core. Config-only trees (no agent ``pyproject.toml``) with memory enabled
    fall back to a CLI-managed runtime provisioned on demand.
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
        if memory_enabled:
            return provision_managed_memory_runtime(agent_root)
        raise RuntimeUpgradeError(
            "gateway interpreter is missing compatible harness packages; "
            "install monkeybot>=3.0.0,<4 in this environment before starting the gateway"
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
