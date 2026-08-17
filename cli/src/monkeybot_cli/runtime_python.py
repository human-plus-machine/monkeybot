"""Resolve the Python interpreter that should run the gateway.

The CLI is intentionally thin: it depends only on base ``monkeybot`` and does **not**
pull in provider/storage extras (``bedrock``, ``postgres``, …). Those extras are
declared on the *agent project* (e.g. ``pr-review-agent/pyproject.toml`` lists
``monkeybot[bedrock,postgres]``). To honor that, the gateway must be spawned from
the agent project's interpreter rather than the CLI's own ``sys.executable``.

Resolution order for an agent root:

1. ``<root>/.venv/bin/python`` (or the Windows variant) — direct, no subprocess overhead.
2. ``uv run python`` — when ``<root>/pyproject.toml`` exists but no ``.venv``.
3. ``<cache>/monkeybot/runtimes/<key>/bin/python`` — a CLI-managed venv holding
   ``monkeybot[memory]``. When this process is running from a source checkout
   (or ``MONKEYBOT_CHECKOUT`` points at one), the install is that tree plus
   extras from PyPI; otherwise it is ``monkeybot[memory]==<running version>``.
   Used only for config-only trees with memory enabled when a previously
   provisioned cache interpreter exists. The managed runtime also mirrors
   provider/storage extras present in the CLI env (excluding extras whose
   probe module is a base dep) and the extra required by the agent's YAML
   ``model.provider`` (so a thin CLI still gets ``tiktoken`` for NVIDIA/OpenAI).
4. ``sys.executable`` — legacy / config-only trees (just ``monkeybot_config/``, no
   ``pyproject.toml``). In this case extras must be installed in the CLI env,
   unless memory is enabled and ``prepare_runtime_python`` provisions (3).

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
import tempfile
import tomllib
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _package_version
from pathlib import Path

from monkeybot_cli.compat import (
    COMPATIBLE_CORE_LOWER_VERSION,
    COMPATIBLE_CORE_RANGE,
    COMPATIBLE_CORE_UPPER_VERSION,
)
from monkeybot_cli.extras_catalog import FEATURE_CHOICES, provider_extra_name
from monkeybot_cli.providers import PROVIDER_SPECS, extra_installed, extra_module

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


def resolve_runtime_python(
    agent_root: Path,
    *,
    memory_enabled: bool = False,
    config_path: Path | str | None = None,
) -> RuntimePython:
    """Resolve the interpreter that should run the gateway for ``agent_root``.

    Config-only trees with memory enabled reuse a previously provisioned
    CLI-managed cache venv when the interpreter path exists. Doctor and
    ``prepare_runtime_python`` share this lookup and each run a single harness
    probe — resolve itself does not spawn a probe subprocess. Lookup never
    raises on unreadable agent YAML; provisioning owns fail-closed errors.
    """
    venv_py = _venv_python(agent_root)
    if venv_py is not None:
        return RuntimePython([str(venv_py)], "venv", agent_root)
    if (agent_root / "pyproject.toml").is_file():
        return RuntimePython(["uv", "run", "python"], "uv", agent_root)
    if memory_enabled:
        cached = _existing_managed_runtime(agent_root, config_path=config_path)
        if cached is not None:
            return cached
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
# `memory` is always pinned into the managed runtime, so it is not mirrored.
_UNMIRRORED_EXTRAS = frozenset({"memory", "cli", "cli-realtime"})

# Probe modules that ship with base ``monkeybot`` — importing them does not mean
# the corresponding optional extra is installed (e.g. ``gemini`` /
# ``realtime-gemini`` both probe ``google.genai``, a core dependency).
_BASE_PROBE_MODULES = frozenset({"google.genai"})


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


def _mirrorable_extras() -> frozenset[str]:
    extras = {spec.extra for spec in PROVIDER_SPECS.values() if spec.extra}
    extras.update(choice.key for choice in FEATURE_CHOICES)
    return frozenset(extras - _UNMIRRORED_EXTRAS)


def mirrored_monkeybot_extras() -> tuple[str, ...]:
    """Extras installed in the CLI env that the gateway needs after an interpreter swap.

    Uses the same extra → import probe as ``doctor`` (``extra_installed`` /
    ``extra_module``) over the extras catalog, instead of re-parsing package
    metadata. Returns extras that are present in this process and belong on the
    gateway, not CLI-only extras. Extras whose probe module is a base MonkeyBot
    dependency are skipped — those probes are always true and would pollute the
    pin and cache-key digest.
    """
    return tuple(
        sorted(
            extra
            for extra in _mirrorable_extras()
            if extra_module(extra) not in _BASE_PROBE_MODULES and extra_installed(extra)
        )
    )


def managed_memory_runtime_dir(
    monkeybot_version: str,
    extras: Sequence[str] = (),
    *,
    checkout: Path | None = None,
) -> Path:
    """Deterministic cache directory for the managed MemPalace runtime.

    Keyed by the pinned MonkeyBot version, the CLI's Python version, mirrored
    extras, and (when installing from a source tree) the resolved checkout
    path plus ``pyproject.toml`` mtime. A CLI upgrade, Python upgrade, extras
    change, or checkout move/edit provisions a fresh runtime instead of reusing
    a mismatched one — important because ``monkeybot @ file://`` is a
    snapshot copy, not an editable install.
    """
    key = (
        f"memory-{_sanitize_key_part(monkeybot_version)}"
        f"-py{sys.version_info.major}.{sys.version_info.minor}"
    )
    digest_parts = sorted(extra.lower() for extra in extras)
    if checkout is not None:
        resolved = checkout.resolve()
        try:
            mtime_ns = (resolved / "pyproject.toml").stat().st_mtime_ns
        except OSError:
            mtime_ns = 0
        digest_parts.append(f"checkout:{resolved.as_posix()}:{mtime_ns}")
    if digest_parts:
        digest = hashlib.sha256(",".join(digest_parts).encode()).hexdigest()[:8]
        key = f"{key}-{digest}"
    return _cache_root() / "monkeybot" / "runtimes" / key


def _managed_interpreter(runtime_dir: Path) -> Path | None:
    for candidate in (runtime_dir / "bin" / "python", runtime_dir / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return None


def _managed_runtime(runtime_dir: Path, agent_root: Path) -> RuntimePython | None:
    """Return the managed interpreter if the venv exists (no probe)."""
    interpreter = _managed_interpreter(runtime_dir)
    if interpreter is None:
        return None
    return RuntimePython([str(interpreter)], MANAGED_RUNTIME_SOURCE, agent_root)


def _managed_if_ready(runtime_dir: Path, agent_root: Path) -> RuntimePython | None:
    """Return the managed interpreter only when it already passes the memory probe."""
    runtime = _managed_runtime(runtime_dir, agent_root)
    if runtime is None:
        return None
    if run_probe(runtime, MEMORY_PROBE):
        return runtime
    return None


def _agent_provider_extras(
    agent_root: Path,
    config_path: Path | str | None = None,
    *,
    fail_closed: bool = False,
) -> tuple[str, ...]:
    """Package extra required by ``model.provider`` in the agent's YAML, if any.

    Honors an explicit ``config_path`` (e.g. ``--config``). When ``fail_closed``
    is false, load/parse failures return ``()`` so quiet lookups stay total.
    """
    from monkeybot.core.config.yaml_loader import load_monkeybot_yaml_dict

    effective = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else agent_root / "monkeybot_config" / "monkeybot.yaml"
    )
    if not effective.is_file():
        return ()
    try:
        _path, data = load_monkeybot_yaml_dict(effective)
    except Exception as exc:
        if fail_closed:
            raise _managed_runtime_error(f"could not read {effective}: {exc}") from exc
        return ()
    model = data.get("model")
    if model is None:
        return ()
    if not isinstance(model, dict):
        if fail_closed:
            raise _managed_runtime_error(f"{effective} model is not a mapping")
        return ()
    raw = model.get("provider")
    extra = provider_extra_name(raw if isinstance(raw, str) else None)
    return (extra,) if extra else ()


def _managed_runtime_extras(
    agent_root: Path,
    config_path: Path | str | None = None,
    *,
    fail_closed: bool = False,
) -> tuple[str, ...]:
    """CLI-mirrored extras plus the extra required by this agent's YAML provider."""
    extras = set(mirrored_monkeybot_extras())
    extras.update(
        _agent_provider_extras(agent_root, config_path, fail_closed=fail_closed)
    )
    return tuple(sorted(extras))


def _existing_managed_runtime(
    agent_root: Path,
    config_path: Path | str | None = None,
) -> RuntimePython | None:
    """Locate a cached managed interpreter without probing it.

    ``prepare_runtime_python`` / ``doctor`` own the single harness probe so a
    warm cache does not pay for two interpreter spawns per start. Never raises
    on agent YAML errors — provisioning is the fail-closed path.
    """
    try:
        monkeybot_version = _package_version("monkeybot")
    except PackageNotFoundError:
        return None
    extras = _managed_runtime_extras(agent_root, config_path, fail_closed=False)
    return _managed_runtime(
        managed_memory_runtime_dir(
            monkeybot_version,
            extras,
            checkout=_monkeybot_checkout_root(),
        ),
        agent_root,
    )


@contextmanager
def _exclusive_file_lock(lock_path: Path) -> Iterator[None]:
    """Block until this process holds ``lock_path`` (fcntl on POSIX, msvcrt on Windows)."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(fd)


def _managed_runtime_error(detail: str) -> RuntimeUpgradeError:
    """Actionable fail-closed error for the managed MemPalace runtime."""
    return RuntimeUpgradeError(
        "memory is enabled but MemPalace could not be provisioned for this "
        "config-only agent"
        + (f" ({detail})" if detail else "")
        + f"; install monkeybot[memory]{COMPATIBLE_CORE_RANGE} in this "
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


def _is_monkeybot_project(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    project = data.get("project")
    return isinstance(project, dict) and project.get("name") == "monkeybot"


def _checkout_from_module_file(module_file: str | None) -> Path | None:
    if not module_file:
        return None
    path = Path(module_file).resolve()
    for parent in path.parents:
        if _is_monkeybot_project(parent):
            return parent
    return None


def _monkeybot_checkout_root() -> Path | None:
    """Return the MonkeyBot source tree when this process can see a checkout.

    ``MONKEYBOT_CHECKOUT`` wins when it points at a tree whose ``pyproject.toml``
    names the ``monkeybot`` project. Otherwise walk parents of the running
    ``monkeybot_cli`` / ``monkeybot`` modules (covers editable installs, since
    ``monkeybot.__file__`` points into the source tree).
    """
    override = os.environ.get("MONKEYBOT_CHECKOUT", "").strip()
    if override:
        candidate = Path(override).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError:
            resolved = None
        if resolved is not None and _is_monkeybot_project(resolved):
            return resolved
    for mod_name in ("monkeybot_cli", "monkeybot"):
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                mod = __import__(mod_name)
            except ImportError:
                continue
        found = _checkout_from_module_file(getattr(mod, "__file__", None))
        if found is not None:
            return found
    return None


def _managed_memory_requirement(
    monkeybot_version: str,
    extra_spec: str,
    *,
    checkout: Path | None = None,
) -> str:
    """Install spec for the managed runtime: local checkout when present, else PyPI pin."""
    extras = f"[{extra_spec}]" if extra_spec else ""
    root = checkout if checkout is not None else _monkeybot_checkout_root()
    if root is not None:
        return f"monkeybot{extras} @ {root.resolve().as_uri()}"
    return f"monkeybot{extras}=={monkeybot_version}"


def provision_managed_memory_runtime(
    agent_root: Path,
    config_path: Path | str | None = None,
) -> RuntimePython:
    """Return a CLI-managed venv holding ``monkeybot[memory]`` at the running core version.

    Extras the CLI env already satisfies are mirrored into the runtime alongside
    ``memory``. A cached runtime that already passes the memory probe is reused
    as-is. Otherwise the runtime is created in a staging directory with
    ``uv venv --python`` pointing at this CLI interpreter, then swapped into
    the cache with ``os.replace``. Pins in an agent ``pyproject.toml`` are never
    rewritten. An unpublished checkout is installed from disk so MemPalace can
    still come from PyPI. Unreadable agent YAML fails closed here (not during
    quiet resolve lookups).
    """
    try:
        monkeybot_version = _package_version("monkeybot")
    except PackageNotFoundError as exc:
        raise _managed_runtime_error("the running monkeybot core version is unknown") from exc

    extras = _managed_runtime_extras(agent_root, config_path, fail_closed=True)
    checkout = _monkeybot_checkout_root()
    runtime_dir = managed_memory_runtime_dir(monkeybot_version, extras, checkout=checkout)
    cached = _managed_if_ready(runtime_dir, agent_root)
    if cached is not None:
        return cached

    if shutil.which("uv") is None:
        raise _managed_runtime_error("uv was not found on PATH")
    extra_spec = ",".join(("memory", *extras))
    requirement = _managed_memory_requirement(
        monkeybot_version, extra_spec, checkout=checkout
    )
    print(
        f"agent memory runtime is missing MemPalace; provisioning {requirement} in {runtime_dir}",
        flush=True,
    )
    runtime_dir.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_file_lock(runtime_dir.parent / f".{runtime_dir.name}.lock"):
        cached = _managed_if_ready(runtime_dir, agent_root)
        if cached is not None:
            return cached
        return _install_managed_runtime(runtime_dir, agent_root, requirement)


def _retire_runtime_dir(runtime_dir: Path) -> None:
    """Move ``runtime_dir`` aside so a live gateway is not deleted underneath.

    Readers may still hold absolute paths into the old tree; renaming keeps the
    inode tree intact instead of ``rmtree``-ing files out from under them.
    Retired dirs are left in place (best-effort cleanup would reintroduce the
    same hazard for any process still using that tree).
    """
    retired = runtime_dir.with_name(f".{runtime_dir.name}.retired-{uuid.uuid4().hex[:8]}")
    os.replace(runtime_dir, retired)


def _install_managed_runtime(
    runtime_dir: Path,
    agent_root: Path,
    requirement: str,
) -> RuntimePython:
    """Create ``requirement`` in a staging dir, then atomically replace ``runtime_dir``."""
    staging: Path | None = Path(
        tempfile.mkdtemp(prefix=f".{runtime_dir.name}.tmp-", dir=runtime_dir.parent)
    )
    try:
        # --relocatable keeps console-script shebangs / activate usable after
        # the staging directory is moved into the cache key path.
        _run_provisioning(
            ["uv", "venv", "--python", sys.executable, "--relocatable", str(staging)],
            "uv venv",
        )
        interpreter = _managed_interpreter(staging)
        if interpreter is None:
            raise _managed_runtime_error(f"no interpreter was created in {staging}")
        _run_provisioning(
            ["uv", "pip", "install", "--python", str(interpreter), requirement],
            "uv pip install",
        )
        staged = RuntimePython([str(interpreter)], MANAGED_RUNTIME_SOURCE, agent_root)
        if not run_probe(staged, MEMORY_PROBE):
            raise _managed_runtime_error(_probe_failure_detail(staged, MEMORY_PROBE))
        if runtime_dir.exists():
            _retire_runtime_dir(runtime_dir)
        os.replace(staging, runtime_dir)
        staging = None
    finally:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
    ready = _managed_runtime(runtime_dir, agent_root)
    if ready is None:
        raise _managed_runtime_error(f"no interpreter was created in {runtime_dir}")
    return ready


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

    has_project = (agent_root / "pyproject.toml").is_file()
    effective_config = (
        Path(config_path).expanduser().resolve()
        if config_path is not None
        else agent_root / "monkeybot_config" / "monkeybot.yaml"
    )
    memory_enabled = memory_enabled_from_config(
        str(effective_config) if effective_config.is_file() else None
    )
    runtime = resolve_runtime_python(
        agent_root, memory_enabled=memory_enabled, config_path=config_path
    )
    probe = MEMORY_PROBE if memory_enabled else CORE_PROBE
    ok, detail = _probe(runtime, probe)
    if ok:
        return runtime
    if not has_project:
        if memory_enabled and runtime.source in {"cli", MANAGED_RUNTIME_SOURCE}:
            return provision_managed_memory_runtime(agent_root, config_path=config_path)
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
    runtime = resolve_runtime_python(
        agent_root, memory_enabled=memory_enabled, config_path=config_path
    )
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
