"""Gateways for live runs: one per effective scenario config.

A scenario's effective config is its ``config:`` (agent-config overrides, dotted or nested,
plus ``env`` for the gateway process), the base config file, and whether it reloads config
mid-run. The base is ``monkeybot.yaml``; a scenario with ``verifier_on`` / ``verifier_off``
blocks boots on ``monkeybot.verifier-tracker.yaml`` (or the file ``EVAL_VERIFIER_CONFIG``
names) when ``EVAL_VERIFIER_MODE`` is ``on``.

Scenarios with the default config run against the gateway at ``--agent-url``. Any other
config gets a gateway this module starts: ``uv run -m monkeybot.gateway.main`` from
``regression/t4/agent``, with the merged config written into ``monkeybot_config/`` (so the
agent root and the config's relative paths do not move), its own port and its own SQLite
database. Config ``includes:`` are not used for this because an include wins over the file
that includes it. Process env wins over YAML, which is how ``PORT``, ``DB_URL`` and the
scenario's ``env`` reach the gateway.

Scenarios are grouped by config so a suite boots each gateway once. A scenario with a
``reload_config`` step gets a gateway of its own, stopped after it, since the reload changes
that gateway's config file. Nothing here runs at import or collection time.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Protocol, TypeVar

import httpx
import yaml

from regression.t4.models import Scenario
from regression.t4.scenario_loader import EVAL_ROOT

AGENT_ROOT = EVAL_ROOT / "agent"
CONFIG_DIR = AGENT_ROOT / "monkeybot_config"
GATEWAYS_DIR = AGENT_ROOT / "data" / "gateways"
AGENT_ROOT_ENV = "REGRESSION_AGENT_ROOT"


def resolve_run_agent_root() -> Path:
    """Agent project for this process.

    Unset, this is ``regression/t4/agent``. ``REGRESSION_AGENT_ROOT`` points a
    parallel suite at its own copy so its memory, workspace, and database stay
    off the copy another suite is using.
    """
    raw = os.environ.get(AGENT_ROOT_ENV, "").strip()
    if not raw:
        return AGENT_ROOT
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise GatewayError(f"{AGENT_ROOT_ENV} is not a directory: {path}")
    return path


def resolve_config_dir() -> Path:
    return resolve_run_agent_root() / "monkeybot_config"


def resolve_gateways_dir() -> Path:
    return resolve_run_agent_root() / "data" / "gateways"


DEFAULT_BASE = "monkeybot.yaml"
DEFAULT_VERIFIER_BASE = "monkeybot.verifier-tracker.yaml"
VERIFIER_CONFIG_ENV = "EVAL_VERIFIER_CONFIG"
VERIFIER_MODE_ENV = "EVAL_VERIFIER_MODE"
GENERATED_PREFIX = ".t4-gateway-"
GATEWAY_COMMAND = ("uv", "run", "-m", "monkeybot.gateway.main")
HEALTH_TIMEOUT_SEC = 120.0
STOP_TIMEOUT_SEC = 15.0

T = TypeVar("T")


class GatewayError(RuntimeError):
    """A gateway could not be prepared, started, or reconfigured."""


@dataclass(frozen=True)
class GatewaySpec:
    """Everything that decides which gateway a scenario runs on."""

    base: str = DEFAULT_BASE
    overrides_json: str = "{}"
    env: tuple[tuple[str, str], ...] = ()
    exclusive: bool = False

    @property
    def overrides(self) -> dict[str, Any]:
        loaded = json.loads(self.overrides_json)
        return loaded if isinstance(loaded, dict) else {}

    @property
    def is_default(self) -> bool:
        return (
            self.base == DEFAULT_BASE
            and self.overrides_json == "{}"
            and not self.env
            and not self.exclusive
        )

    def token(self) -> str:
        raw = json.dumps([self.base, self.overrides_json, list(self.env), self.exclusive])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def describe(self) -> str:
        parts = [self.base]
        if self.overrides_json != "{}":
            parts.append(self.overrides_json)
        if self.env:
            parts.append(" ".join(f"{k}={v}" for k, v in self.env))
        if self.exclusive:
            parts.append("exclusive")
        return " ".join(parts)


def nest_overrides(raw: Mapping[str, Any]) -> dict[str, Any]:
    """``{"model.context_window": 6000}`` and ``{"model": {"context_window": 6000}}`` agree."""
    nested: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key.strip():
            raise GatewayError(f"config keys must be non-empty strings, got {key!r}")
        parts = key.split(".")
        if any(not part for part in parts):
            raise GatewayError(f"config key {key!r} has an empty segment")
        leaf: Any = nest_overrides(value) if isinstance(value, Mapping) else value
        for part in reversed(parts[1:]):
            leaf = {part: leaf}
        nested = deep_merge(nested, {parts[0]: leaf})
    return nested


def deep_merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def effective_verifier_mode(suite_mode: str | None = None) -> str | None:
    """``EVAL_VERIFIER_MODE`` from the environment, else the suite's ``verifier_mode``."""
    raw = os.environ.get(VERIFIER_MODE_ENV, "").strip().lower()
    if raw in ("on", "off"):
        return raw
    return suite_mode


@contextlib.contextmanager
def verifier_mode_env(mode: str | None) -> Iterator[None]:
    """Set ``EVAL_VERIFIER_MODE`` to ``mode`` while the environment has none."""
    if mode is None or os.environ.get(VERIFIER_MODE_ENV, "").strip():
        yield
        return
    os.environ[VERIFIER_MODE_ENV] = mode
    try:
        yield
    finally:
        os.environ.pop(VERIFIER_MODE_ENV, None)


def spec_from_fields(
    config: Mapping[str, Any] | None,
    *,
    verifier_blocks: bool,
    reload_steps: bool,
    verifier_mode: str | None,
) -> GatewaySpec:
    """The spec for raw scenario fields; collection uses this on the YAML alone."""
    raw = dict(config or {})
    env_raw = raw.pop("env", None) or {}
    if not isinstance(env_raw, Mapping):
        raise GatewayError("config.env must be a mapping")
    env = tuple(sorted((str(k), str(v)) for k, v in env_raw.items()))
    overrides = nest_overrides(raw)
    base = DEFAULT_BASE
    if verifier_blocks and verifier_mode == "on":
        base = os.environ.get(VERIFIER_CONFIG_ENV, "").strip() or DEFAULT_VERIFIER_BASE
    return GatewaySpec(
        base=base,
        overrides_json=json.dumps(overrides, sort_keys=True),
        env=env,
        exclusive=reload_steps,
    )


def spec_for(scenario: Scenario, verifier_mode: str | None) -> GatewaySpec:
    return spec_from_fields(
        scenario.config,
        verifier_blocks=scenario.has_verifier_blocks(),
        reload_steps=any(step.action == "reload_config" for step in scenario.steps),
        verifier_mode=verifier_mode,
    )


def group_in_order(items: Sequence[T], spec_of: Callable[[T], GatewaySpec]) -> list[T]:
    """Items with the same spec run together, groups in order of first appearance.

    An exclusive spec is a group of one: its gateway is never shared.
    """
    groups: dict[object, list[T]] = {}
    for index, item in enumerate(items):
        spec = spec_of(item)
        key: object = ("exclusive", index) if spec.exclusive else spec
        groups.setdefault(key, []).append(item)
    return [item for group in groups.values() for item in group]


def merged_config(spec: GatewaySpec, config_dir: Path | None = None) -> dict[str, Any]:
    config_dir = CONFIG_DIR if config_dir is None else config_dir
    base_path = config_dir / spec.base
    if not base_path.is_file():
        raise GatewayError(f"base agent config is missing: {base_path}")
    loaded = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise GatewayError(f"{base_path} must be a mapping")
    if "includes" in loaded:
        raise GatewayError(f"{base_path}: a base config with includes cannot be overlaid")
    return deep_merge(loaded, spec.overrides)


def _write_yaml(path: Path, document: Mapping[str, Any], *, note: str) -> None:
    header = f"# Generated by regression/t4/gateway.py ({note}); deleted when its gateway stops.\n"
    path.write_text(header + yaml.safe_dump(dict(document), sort_keys=False), encoding="utf-8")


@dataclass
class Gateway:
    """A gateway a scenario talks to. ``managed`` ones were started by this module."""

    url: str
    spec: GatewaySpec
    managed: bool = False
    config_path: Path | None = None
    data_dir: Path | None = None
    process: Any = None
    log: IO[bytes] | None = field(default=None, repr=False)

    def rewrite_config(self, changes: Mapping[str, Any]) -> None:
        """Merge ``changes`` into this gateway's config file (before ``/admin/config/reload``)."""
        if not self.managed or self.config_path is None:
            raise GatewayError(
                "reload_config needs a gateway the runner started; "
                f"{self.url} was started outside the run"
            )
        current = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(current, dict):
            raise GatewayError(f"{self.config_path} must be a mapping")
        updated = deep_merge(current, nest_overrides(changes))
        _write_yaml(self.config_path, updated, note=f"{self.spec.describe()}, reloaded")


class Launcher(Protocol):
    def start(self, spec: GatewaySpec, config_path: Path, data_dir: Path) -> Gateway: ...

    def stop(self, gateway: Gateway) -> None: ...


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def launch_env(
    spec: GatewaySpec,
    *,
    port: int,
    config_path: Path,
    data_dir: Path,
    base_env: Mapping[str, str],
) -> dict[str, str]:
    """Process env for a managed gateway. The runner's own values win over ``spec.env``."""
    env = dict(base_env)
    env.pop("VIRTUAL_ENV", None)
    env.update(dict(spec.env))
    env["PORT"] = str(port)
    env["MONKEYBOT_CONFIG"] = str(config_path)
    env["DB_URL"] = f"sqlite:///{(data_dir / 'monkeybot.db').resolve()}"
    return env


class SubprocessLauncher:
    """Start ``uv run -m monkeybot.gateway.main`` in the agent project and wait for ``/health``."""

    def __init__(
        self,
        *,
        agent_root: Path | None = None,
        command: Sequence[str] = GATEWAY_COMMAND,
        health_timeout_sec: float = HEALTH_TIMEOUT_SEC,
    ) -> None:
        self._agent_root = AGENT_ROOT if agent_root is None else agent_root
        self._command = list(command)
        self._health_timeout_sec = health_timeout_sec

    def start(self, spec: GatewaySpec, config_path: Path, data_dir: Path) -> Gateway:
        port = free_port()
        env = launch_env(
            spec, port=port, config_path=config_path, data_dir=data_dir, base_env=os.environ
        )
        log_path = data_dir / "gateway.log"
        log = log_path.open("wb")
        # ``--project`` is the real agent checkout so ``uv`` can see monkeybot.
        # ``cwd`` stays the suite's private home, which is what ``REGRESSION_AGENT_ROOT``
        # selected, so memory and workspace stay isolated.
        command = list(self._command)
        if "--project" not in command:
            command = ["uv", "run", "--project", str(AGENT_ROOT), "-m", "monkeybot.gateway.main"]
        process = subprocess.Popen(
            command,
            cwd=self._agent_root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        gateway = Gateway(
            url=f"http://127.0.0.1:{port}",
            spec=spec,
            managed=True,
            config_path=config_path,
            data_dir=data_dir,
            process=process,
            log=log,
        )
        try:
            _wait_healthy(gateway.url, process, self._health_timeout_sec, log_path)
        except BaseException:
            self.stop(gateway)
            raise
        return gateway

    def stop(self, gateway: Gateway) -> None:
        process = gateway.process
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=STOP_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=STOP_TIMEOUT_SEC)
        if gateway.log is not None:
            gateway.log.close()


def _log_tail(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _wait_healthy(url: str, process: Any, timeout_sec: float, log_path: Path) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise GatewayError(
                f"gateway exited with {code} before /health; log tail:\n{_log_tail(log_path)}"
            )
        try:
            response = httpx.get(f"{url}/health", timeout=2.0)
            if response.status_code == 200 and response.json().get("status") == "ok":
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(0.5)
    raise GatewayError(
        f"gateway at {url} not healthy after {timeout_sec:.0f}s; log tail:\n{_log_tail(log_path)}"
    )


class GatewayPool:
    """Hand each scenario its gateway: the external one, the running managed one, or a new one.

    At most one managed gateway runs at a time; asking for a different spec stops it first.
    """

    def __init__(
        self,
        *,
        external_url: str | None,
        launcher: Launcher | None = None,
        config_dir: Path | None = None,
        data_root: Path | None = None,
    ) -> None:
        root = resolve_run_agent_root()
        self._external_url = external_url
        self._launcher: Launcher = (
            launcher if launcher is not None else SubprocessLauncher(agent_root=root)
        )
        self._config_dir = root / "monkeybot_config" if config_dir is None else config_dir
        self._data_root = root / "data" / "gateways" if data_root is None else data_root
        self._current: Gateway | None = None
        self._starts = 0

    @property
    def current(self) -> Gateway | None:
        return self._current

    def acquire(self, spec: GatewaySpec) -> Gateway:
        if spec.is_default and self._external_url:
            return Gateway(url=self._external_url, spec=spec)
        current = self._current
        if current is not None and current.spec == spec and not spec.exclusive:
            return current
        self._stop_current()
        self._starts += 1
        token = spec.token() if not spec.exclusive else f"{spec.token()}-{self._starts}"
        config_path = self._config_dir / f"{GENERATED_PREFIX}{token}.yaml"
        data_dir = self._data_root / token
        data_dir.mkdir(parents=True, exist_ok=True)
        _write_yaml(config_path, merged_config(spec, self._config_dir), note=spec.describe())
        try:
            gateway = self._launcher.start(spec, config_path, data_dir)
        except BaseException:
            config_path.unlink(missing_ok=True)
            raise
        self._current = gateway
        return gateway

    def release(self, gateway: Gateway) -> None:
        """Done with ``gateway`` for one scenario. An exclusive one stops now."""
        if gateway.managed and gateway.spec.exclusive and gateway is self._current:
            self._stop_current()

    def close(self) -> None:
        self._stop_current()

    def _stop_current(self) -> None:
        current = self._current
        self._current = None
        if current is None:
            return
        try:
            self._launcher.stop(current)
        finally:
            if current.config_path is not None:
                current.config_path.unlink(missing_ok=True)
