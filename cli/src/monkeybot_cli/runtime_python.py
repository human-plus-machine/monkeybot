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
   ``monkeybot[memory]`` pinned to the running core version. Used only for
   config-only trees that enable memory when the CLI env itself cannot import
   MemPalace. Provisioned once, then reused offline. The managed runtime also
   mirrors provider/storage extras present in the CLI env.

``prepare_runtime_python`` probes that interpreter for a MonkeyBot 3.x core
(and MemPalace when memory is enabled). A stale venv may be ``uv sync``'d against
the existing lock; the CLI never rewrites ``pyproject.toml`` pins. A failed
probe refuses to start the gateway.
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

from monkeybot_cli.compat import (
    COMPATIBLE_CORE_LOWER_VERSION,
    COMPATIBLE_CORE_RANGE,
    COMPATIBLE_CORE_UPPER_VERSION,
)

DEFAULT_PORT = 8080
SSE_GATEWAY_MODULE = "monkeybot.gateway.main"
COMBINED_GATEWAY_MODULE = "monkeybot.gateway.realtime_main"

# Stdlib-only snippet executed in the *agent* interpreter. One embedded parser
# compares the installed version against bounds derived from COMPATIBLE_CORE_RANGE
# so SpecifierSet defaults stay aligned (local ignored; epochs/pre/post/dev ordered).
# Uses ``raise`` rather than ``assert`` so ``python -O`` cannot strip the gate.
_VERSION_RE = (
    r"^\s*v?"
    r"(?:(?P<epoch>[0-9]+)!)?"
    r"(?P<release>[0-9]+(?:\.[0-9]+)*)"
    r"(?:[-_\.]?(?P<pre_l>alpha|beta|preview|pre|rc|a|b|c)[-_\.]?(?P<pre_n>[0-9]+)?)?"
    r"(?:(?:[-_\.]?(?:post|rev|r)[-_\.]?(?P<post>[0-9]*))|-(?P<post2>[0-9]+))?"
    r"(?:[-_\.]?dev[-_\.]?(?P<dev>[0-9]*))?"
    r"\s*$"
)
CORE_PROBE = f"""
from importlib.metadata import version
import re
_PRE = {{"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}}
_RX = re.compile({_VERSION_RE!r}, re.I)

def _key(v):
    p = v.split("+", 1)[0]
    m = _RX.fullmatch(p)
    if not m:
        raise ValueError(v)
    rel = [int(x) for x in m["release"].split(".")]
    while len(rel) > 1 and rel[-1] == 0:
        rel.pop()
    pl = m["pre_l"]
    if pl:
        pre = (1, _PRE[pl.lower()], int(m["pre_n"] or 0))
    elif m["dev"] is not None:
        pre = (0, 0, 0)
    else:
        pre = (2, 0, 0)
    pn = m["post"] if m["post"] is not None else m["post2"]
    post = (1, int(pn or 0)) if pn is not None else (0, 0)
    dev = (0, int(m["dev"] or 0)) if m["dev"] is not None else (1, 0)
    return (int(m["epoch"] or 0), tuple(rel), pre, post, dev)

ver = version("monkeybot")
if not (_key({COMPATIBLE_CORE_LOWER_VERSION!r}) <= _key(ver) < _key({COMPATIBLE_CORE_UPPER_VERSION!r})):
    raise SystemExit(ver)
""".strip()
MEMORY_PROBE = f"import mempalace\n{CORE_PROBE}"


class RuntimeUpgradeError(RuntimeError):
    """Raised when the agent interpreter cannot run a compatible MonkeyBot gateway."""


def report_runtime_upgrade_error(exc: RuntimeUpgradeError) -> int:
    """Print a spawn refusal to stderr and return the CLI exit code."""
    print(f"error: {exc}", file=sys.stderr)
    return 2


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


def _runtime_cwd(runtime: RuntimePython) -> dict[str, object]:
    """Extra kwargs needed to run a probe under ``runtime`` (cwd for uv projects)."""
    if runtime.source == "uv" and runtime.agent_root is not None:
        return {"cwd": str(runtime.agent_root)}
    return {}


def _probe(runtime: RuntimePython, code: str, *, timeout: float = 15.0) -> tuple[bool, str]:
    """Run ``python -c code`` under ``runtime``. Never raises on spawn failure."""
    try:
        proc = subprocess.run(
            [*runtime.argv, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
            **_runtime_cwd(runtime),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if proc.returncode == 0:
        return True, (proc.stdout or "").strip()
    text = (proc.stderr or proc.stdout or "").strip()
    if not text:
        return False, f"probe exit {proc.returncode}"
    return False, text[-500:]


def _probe_failure_detail(runtime: RuntimePython, probe: str, *, timeout: float = 15.0) -> str:
    """Return the probe's stderr/stdout snippet, or a spawn/timeout message."""
    _ok, detail = _probe(runtime, probe, timeout=timeout)
    return detail


def _upgrade_error(
    *,
    agent_root: Path,
    memory_enabled: bool,
    has_project: bool,
    detail: str,
) -> RuntimeUpgradeError:
    range_ = COMPATIBLE_CORE_RANGE
    extra = "[memory]" if memory_enabled else ""
    if has_project:
        how = (
            f"pin monkeybot{extra}{range_} in {agent_root}/pyproject.toml, "
            f"then `cd {agent_root} && uv sync`"
        )
    elif memory_enabled:
        how = (
            f"install monkeybot[memory]{range_} in this environment, or set `memory.enabled: false`"
        )
    else:
        how = f"install monkeybot{range_} in this environment"
    capability = "MemPalace and MonkeyBot" if memory_enabled else "MonkeyBot"
    suffix = f" ({detail})" if detail else ""
    return RuntimeUpgradeError(
        f"gateway interpreter is missing a compatible {capability} {range_}; "
        f"refusing to start{suffix}. {how}"
    )


MANAGED_RUNTIME_SOURCE = "cli-managed"

# Extras that belong to the CLI *client* rather than the gateway. `cli` /
# `cli-realtime` add typer and pyaudio without giving the gateway anything.
# `memory` is always added to the managed runtime.
_UNMIRRORED_EXTRAS = frozenset({"memory", "cli", "cli-realtime"})


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


def _package_installed(name: str) -> bool:
    try:
        _package_version(name)
    except PackageNotFoundError:
        return False
    return True


def _monkeybot_extra_requirements() -> dict[str, list[Requirement]]:
    """Map each ``monkeybot`` extra to the requirements it alone contributes.

    A requirement belongs to ``extra`` when its marker holds for that extra but
    not for the empty extra — which excludes base dependencies carrying
    unrelated markers (``python_version < "3.11"`` and friends).
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
    if extra in stack:
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

    The managed runtime replaces ``sys.executable`` outright, so any provider or
    storage extra the CLI env satisfies has to come with it. Returns ``()`` when
    the metadata cannot be read.
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

    Keyed by the pinned MonkeyBot version, the CLI's Python version, and the
    mirrored extras, so a CLI upgrade, Python upgrade, or extras change
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
        "memory is enabled but MemPalace could not be provisioned for this "
        "config-only agent"
        + (f" ({detail})" if detail else "")
        + "; install a compatible MonkeyBot 3.x (includes MemPalace) in this "
        "environment, or set `memory.enabled: false` in "
        "monkeybot_config/monkeybot.yaml. Note that some platforms "
        "(manylinux2014, musl) ship no onnxruntime wheel for Python "
        f"{sys.version_info.major}.{sys.version_info.minor}, so chromadb — "
        "and therefore MemPalace — cannot be installed there."
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

    Extras the CLI env already satisfies are mirrored into the runtime alongside
    ``memory``. A cached runtime that already passes the memory probe is reused
    as-is. Otherwise the runtime is created with ``uv venv`` and ``uv pip install``,
    pinned exactly to the MonkeyBot version running this CLI. Pins in an agent
    ``pyproject.toml`` are never rewritten.
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
        if run_probe(cached, MEMORY_PROBE):
            return cached

    if shutil.which("uv") is None:
        raise _managed_runtime_error("uv was not found on PATH")
    extra_spec = ",".join(("memory", *extras))
    requirement = f"monkeybot[{extra_spec}]=={monkeybot_version}"
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
    if not run_probe(runtime, MEMORY_PROBE):
        raise _managed_runtime_error(_probe_failure_detail(runtime, MEMORY_PROBE))
    return runtime


def prepare_runtime_python(
    agent_root: Path,
    config_path: Path | str | None = None,
) -> RuntimePython:
    """Resolve the gateway interpreter and refuse to start if the harness is stale.

    When memory is enabled the interpreter must import MemPalace and a MonkeyBot
    3.x core. When memory is disabled only the core range is required. If the
    agent has a ``pyproject.toml`` and the probe fails, ``uv sync`` is tried
    once against the existing lock — pins are not rewritten. Config-only trees
    (no ``pyproject.toml`` and no project ``.venv``) with memory enabled fall
    back to a CLI-managed runtime provisioned on demand.
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
    probe = MEMORY_PROBE if memory_enabled else CORE_PROBE
    ok, detail = _probe(runtime, probe)
    if ok:
        return runtime
    if not has_project:
        if memory_enabled and runtime.source == "cli":
            return provision_managed_memory_runtime(agent_root)
        raise _upgrade_error(
            agent_root=agent_root,
            memory_enabled=memory_enabled,
            has_project=False,
            detail=detail,
        )
    print(
        f"agent runtime is missing harness packages; running uv sync in {agent_root}",
        flush=True,
    )
    try:
        sync = subprocess.run(["uv", "sync"], cwd=agent_root, check=False)
    except FileNotFoundError as exc:
        raise _upgrade_error(
            agent_root=agent_root,
            memory_enabled=memory_enabled,
            has_project=True,
            detail=str(exc),
        ) from exc
    runtime = resolve_runtime_python(agent_root)
    ok, detail = _probe(runtime, probe)
    if sync.returncode != 0 or not ok:
        raise _upgrade_error(
            agent_root=agent_root,
            memory_enabled=memory_enabled,
            has_project=True,
            detail=detail,
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
    rather than the CLI's own process. Missing interpreters and timeouts are
    treated as a failed probe, not an uncaught spawn error.
    """
    ok, _detail = _probe(runtime, code, timeout=timeout)
    return ok
