"""Run several T4 suites at once, each on its own gateway.

P5-02 writes baselines with ``--update-baseline``. P5-03's release-gate compare
is the same command without that flag, so it checks the suites side by side
instead of one after another. Trials inside one suite stay serial: they share
that suite's gateway.

Each suite gets a private agent home (config, memory, workspace, database) so
it does not touch the home another suite is using. ``REGRESSION_AGENT_ROOT``
points the trial runner's managed gateways at that same home.

Smoke is omitted here when it is already running on the shared agent. Pass
``--include-smoke`` to run it too.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
AGENT = REPO / "regression" / "t4" / "agent"
FIXTURE_MCP = REPO / "regression" / "fixtures" / "mcp" / "fixture_mcp_server.py"
HOMES = Path("/tmp/p502/iso")
LOGS = Path("/tmp/p502/parallel")

# (home name, suite, --config, EVAL_VERIFIER_CONFIG filename or "")
JOBS: tuple[tuple[str, str, str, str], ...] = (
    ("coding", "coding", "", ""),
    ("coding_hard", "coding_hard", "", ""),
    ("subagents", "subagents", "", ""),
    ("memory", "memory", "", ""),
    ("mcp", "mcp", "", ""),
    ("drift-tracker", "drift", "verifier-tracker", "monkeybot.verifier-tracker.yaml"),
    ("drift-judge", "drift", "verifier-judge", "monkeybot.verifier-judge.yaml"),
)
SMOKE_JOB = ("smoke", "smoke", "", "")


def _link(src: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        return
    dest.symlink_to(src)


def prepare_home(name: str) -> Path:
    """A private agent root at the same depth as ``regression/t4/agent``.

    ``mcp.json`` reaches the fixture server through ``../../fixtures/...`` from
    the real agent root. Homes live under ``/tmp`` and the copied ``mcp.json``
    uses the absolute fixture path instead.
    """
    home = HOMES / name
    config = home / "monkeybot_config"
    if config.exists():
        shutil.rmtree(config)
    shutil.copytree(AGENT / "monkeybot_config", config)
    mcp = config / "mcp.json"
    text = mcp.read_text(encoding="utf-8")
    text = text.replace(
        '"${MONKEYBOT_AGENT_ROOT}/../../fixtures/mcp/fixture_mcp_server.py"',
        f'"{FIXTURE_MCP}"',
    )
    mcp.write_text(text, encoding="utf-8")
    _link(AGENT / "skills", home / "skills")
    _link(AGENT / "pyproject.toml", home / "pyproject.toml")
    _link(AGENT / "uv.lock", home / "uv.lock")
    (home / "workspace").mkdir(exist_ok=True)
    (home / "memory").mkdir(exist_ok=True)
    (home / "data").mkdir(exist_ok=True)
    return home


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def start_gateway(home: Path, port: int, log_path: Path) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["SANDBOX_ENABLED"] = "false"
    env["MONKEYBOT_CONFIG"] = str(home / "monkeybot_config" / "monkeybot.yaml")
    env["DB_URL"] = f"sqlite:///{(home / 'data' / 'monkeybot.db').resolve()}"
    env.pop("EVAL_VERIFIER_MODE", None)
    env.pop("EVAL_VERIFIER_CONFIG", None)
    env.pop("REGRESSION_AGENT_ROOT", None)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("ab")
    process = subprocess.Popen(
        ["uv", "run", "--project", str(AGENT), "-m", "monkeybot.gateway.main"],
        cwd=str(home),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"gateway for {home.name} exited {process.returncode}")
        if _healthy(f"http://127.0.0.1:{port}"):
            return process
        time.sleep(0.5)
    process.terminate()
    raise RuntimeError(f"gateway for {home.name} on {port} did not become healthy")


def start_suite(
    home: Path,
    suite: str,
    port: int,
    *,
    config: str,
    verifier_config: str,
    update_baseline: bool,
    log_path: Path,
) -> subprocess.Popen[bytes]:
    command = [
        "uv",
        "run",
        "python",
        "-m",
        "regression.t5",
        "--suite",
        suite,
        "--agent-url",
        f"http://127.0.0.1:{port}",
        "--scenario-delay-sec",
        "0",
        "--out",
        f"regression/t5/runs/{home.name}.json",
    ]
    if config:
        command.extend(["--config", config])
    if update_baseline:
        command.append("--update-baseline")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["REGRESSION_AGENT_ROOT"] = str(home)
    env.pop("EVAL_VERIFIER_MODE", None)
    env.pop("EVAL_VERIFIER_CONFIG", None)
    if verifier_config:
        env["EVAL_VERIFIER_MODE"] = "on"
        env["EVAL_VERIFIER_CONFIG"] = verifier_config
    handle = log_path.open("ab")
    return subprocess.Popen(
        command,
        cwd=str(REPO),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )


def jobs(include_smoke: bool) -> tuple[tuple[str, str, str, str], ...]:
    if include_smoke:
        return (SMOKE_JOB, *JOBS)
    return JOBS


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    update = "--update-baseline" in args
    include_smoke = "--include-smoke" in args
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    from regression.t4.preflight import ensure_key

    if not ensure_key():
        print("OLLAMA_API_KEY is not set", file=sys.stderr)
        return 2
    (REPO / "regression" / "t5" / "runs").mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    port = 8810
    for name, suite, config, verifier in jobs(include_smoke):
        home = prepare_home(name)
        gateway_log = LOGS / f"{name}-gateway.log"
        suite_log = LOGS / f"{name}.log"
        gateway = start_gateway(home, port, gateway_log)
        suite_proc = start_suite(
            home,
            suite,
            port,
            config=config,
            verifier_config=verifier,
            update_baseline=update,
            log_path=suite_log,
        )
        print(
            f"{name} suite={suite} port={port} gateway={gateway.pid} trials={suite_proc.pid}",
            flush=True,
        )
        port += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
