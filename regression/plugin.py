"""Pytest plugin for the regression tree.

Loaded once for every test root by ``-p regression.plugin`` in ``pytest.ini``.
Options live here only: a second ``addoption`` in any conftest makes pytest
fail with "option names already added".
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

_LATER = "Unused until a later task."


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("regression", "Regression tiers")
    group.addoption(
        "--live",
        action="store_true",
        default=False,
        help="Collect T4 and T5 items. Without it those items are deselected.",
    )
    group.addoption(
        "--record",
        action="store_true",
        default=False,
        help="Re-record T3 cassettes: sets MONKEYBOT_RECORD_CASSETTES=1 for the run.",
    )
    group.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Rewrite goldens. Refused when CI is set. Otherwise unused.",
    )
    group.addoption(
        "--trials",
        action="store",
        default=None,
        help=f"Trial count for a T5 run. {_LATER}",
    )
    group.addoption(
        "--suite",
        action="store",
        default=None,
        help=(
            "Collect the T4 scenarios in regression/t4/suites/<name>.yaml, and only the "
            "regression/t4/python files that suite names. With no --suite, every file in "
            "regression/t4/python runs."
        ),
    )
    group.addoption(
        "--agent-url",
        action="store",
        default=None,
        help="Base URL of the live agent T4 items run against (default: AGENT_URL, then "
        "http://127.0.0.1:8080).",
    )


_RECORD_ENV = "MONKEYBOT_RECORD_CASSETTES"
_RECORD_PREVIOUS = pytest.StashKey[str | None]()


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("update_golden") and os.environ.get("CI", "").strip():
        raise pytest.UsageError("--update-golden is refused when CI is set")
    if config.getoption("record"):
        # ``--record`` and the variable mean the same thing. Put it back at the end.
        config.stash[_RECORD_PREVIOUS] = os.environ.get(_RECORD_ENV)
        os.environ[_RECORD_ENV] = "1"


def pytest_unconfigure(config: pytest.Config) -> None:
    if _RECORD_PREVIOUS not in config.stash:
        return
    previous = config.stash[_RECORD_PREVIOUS]
    if previous is None:
        os.environ.pop(_RECORD_ENV, None)
    else:
        os.environ[_RECORD_ENV] = previous


def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    """Turn each scenario YAML into one item.

    T2: ``regression/t2/**/scenarios/**/*.yaml``. T3: ``regression/t3/trajectories/**/*.yaml``,
    one replay scenario next to each cassette. T4: ``regression/t4/scenarios/**/*.yaml``,
    only those in ``--suite`` when it is given. ``.yml`` is not collected.
    """
    if file_path.suffix != ".yaml":
        return None
    root = Path(str(parent.config.rootpath)).resolve()
    try:
        relative = file_path.resolve().relative_to(root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 4 or parts[0] != "regression":
        return None
    if parts[1] == "t2" and "scenarios" in parts[:-1]:
        return ScenarioFile.from_parent(parent, path=file_path)
    if parts[1] == "t3" and parts[2] == "trajectories":
        return ScenarioFile.from_parent(parent, path=file_path)
    if parts[1] == "t4" and parts[2] == "scenarios":
        scenario_id = Path(*parts[3:]).with_suffix("").as_posix()
        if not _in_suite(parent.config, scenario_id):
            return None
        return LiveScenarioFile.from_parent(parent, path=file_path)
    return None


# (scenario ids or None, python filenames or None, suite verifier_mode).
# None for the first two means no --suite: collect every scenario and every python file.
_SUITE = pytest.StashKey[tuple[frozenset[str] | None, frozenset[str] | None, str | None]]()
_POOL = pytest.StashKey[object]()


def suite_keeps_python(filename: str, named: frozenset[str] | None) -> bool:
    """A suite runs only the python files it names. No --suite runs every file."""
    return named is None or filename in named


def _suite_state(
    config: pytest.Config,
) -> tuple[frozenset[str] | None, frozenset[str] | None, str | None]:
    if _SUITE not in config.stash:
        name = config.getoption("suite")
        if name is None:
            config.stash[_SUITE] = (None, None, None)
        else:
            from regression.t4.scenario_loader import EVAL_ROOT, load_suite_manifest

            path = EVAL_ROOT / "suites" / f"{name}.yaml"
            if not path.is_file():
                raise pytest.UsageError(f"--suite {name}: {path} does not exist")
            manifest = load_suite_manifest(path)
            config.stash[_SUITE] = (
                frozenset(manifest.scenarios),
                frozenset(manifest.python),
                manifest.verifier_mode,
            )
    return config.stash[_SUITE]


def _in_suite(config: pytest.Config, scenario_id: str) -> bool:
    wanted, _, _ = _suite_state(config)
    return wanted is None or scenario_id in wanted


def _is_t4_python(item: pytest.Item) -> bool:
    return "python" in Path(str(item.path)).parts and "t4" in Path(str(item.path)).parts


def _is_live_scenario(item: pytest.Item) -> bool:
    return isinstance(item, LiveScenarioItem)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Filter python files by the suite, then group live scenarios by config.

    Grouping reads YAML only. It does not start a gateway, run preflight, or open a socket.
    """
    _, python_names, _ = _suite_state(config)
    if python_names is not None:
        items[:] = [
            item
            for item in items
            if not _is_t4_python(item)
            or suite_keeps_python(Path(str(item.path)).name, python_names)
        ]
    _group_live_items(items)


def _group_live_items(items: list[pytest.Item]) -> None:
    """Python files first, then live scenarios that share a gateway spec."""
    from regression.t4.gateway import group_in_order

    indexes = [
        index for index, item in enumerate(items) if _is_t4_python(item) or _is_live_scenario(item)
    ]
    if not indexes:
        return
    chosen = [items[index] for index in indexes]
    python_items = [item for item in chosen if _is_t4_python(item)]
    scenarios = [item for item in chosen if _is_live_scenario(item)]
    grouped = group_in_order(scenarios, _spec_of_item)
    ordered = python_items + grouped
    for index, item in zip(indexes, ordered, strict=True):
        items[index] = item


def _spec_of_item(item: pytest.Item):
    import yaml

    from regression.t4.gateway import effective_verifier_mode, spec_from_fields

    loaded = yaml.safe_load(Path(str(item.path)).read_text(encoding="utf-8"))
    data = loaded if isinstance(loaded, dict) else {}
    config = data.get("config") if isinstance(data.get("config"), dict) else {}
    steps = data.get("steps") if isinstance(data.get("steps"), list) else []
    assertions = data.get("assertions") if isinstance(data.get("assertions"), dict) else {}
    reload = any(isinstance(step, dict) and step.get("action") == "reload_config" for step in steps)
    verifier = bool({"verifier_on", "verifier_off"} & set(assertions))
    _, _, suite_mode = _suite_state(item.config)
    return spec_from_fields(
        config,
        verifier_blocks=verifier,
        reload_steps=reload,
        verifier_mode=effective_verifier_mode(suite_mode),
    )


def _gateway_pool(config: pytest.Config, agent_url: str):
    """The session's pool. Constructing it does not start a process."""
    if _POOL not in config.stash:
        from regression.t4.gateway import GatewayPool

        config.stash[_POOL] = GatewayPool(external_url=agent_url)
    return config.stash[_POOL]


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _POOL not in session.config.stash:
        return
    pool = session.config.stash[_POOL]
    close = getattr(pool, "close", None)
    if close is not None:
        close()


class ScenarioFile(pytest.File):
    """One scenario YAML file. Collection sets the ledger case id."""

    def collect(self) -> object:
        from regression.schema.scenario import load_scenario

        scenario = load_scenario(self.path)
        item = ScenarioItem.from_parent(self, name=self.path.stem)
        item.user_properties.append(("case", scenario.case))
        if "darwin-only" in scenario.tags:
            item.add_marker(pytest.mark.darwin_only)
        yield item


class ScenarioItem(pytest.Item):
    """Runs one YAML scenario through its driver. Loop is the default."""

    def runtest(self) -> None:
        from regression.drivers.loop import run_scenario
        from regression.schema.scenario import load_scenario

        scenario = load_scenario(self.path)
        if scenario.mode == "replay":
            from regression.drivers.replay import run_scenario as run_replay_scenario

            asyncio.run(run_replay_scenario(scenario))
            return
        if scenario.driver == "realtime":
            from regression.drivers.realtime import run_scenario as run_realtime_scenario

            asyncio.run(run_realtime_scenario(scenario))
            return
        if scenario.driver == "gateway":
            from regression.drivers.gateway import run_scenario as run_gateway_scenario
            from regression.drivers.replay import (
                run_transcript_roundtrip,
                transcript_roundtrip_requested,
            )
            from regression.schema.sse_norm import update_golden

            if transcript_roundtrip_requested(scenario):
                asyncio.run(run_transcript_roundtrip(scenario))
                return

            token = update_golden.set(bool(self.config.getoption("update_golden")))
            try:
                asyncio.run(run_gateway_scenario(scenario))
            finally:
                update_golden.reset(token)
            return
        asyncio.run(run_scenario(scenario))

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: str | None = None,
    ) -> str | pytest.TerminalRepr:
        if isinstance(excinfo.value, AssertionError):
            return str(excinfo.value)
        return excinfo.getrepr(style=style)

    def reportinfo(self) -> tuple[Path, None, str]:
        return self.path, None, self.path.name


class LiveScenarioFile(pytest.File):
    """One T4 scenario YAML file. Collection reads only ``case`` and ``tags``.

    The full schema load happens when the item runs: ``verifier_on`` / ``verifier_off`` are a
    load error while ``EVAL_VERIFIER_MODE`` is unset, and collection must not depend on it.
    Collection never starts the agent, runs the preflight check, or opens a connection.
    """

    def collect(self) -> object:
        import yaml

        loaded = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        data = loaded if isinstance(loaded, dict) else {}
        item = LiveScenarioItem.from_parent(self, name=self.path.stem)
        if data.get("case"):
            item.user_properties.append(("case", str(data["case"])))
        tags = data.get("tags")
        if isinstance(tags, list) and "darwin-only" in tags:
            item.add_marker(pytest.mark.darwin_only)
        yield item


class LiveScenarioItem(pytest.Item):
    """Runs one T4 scenario against ``--agent-url``, after the preflight check has passed once."""

    def runtest(self) -> None:
        from regression.t4 import preflight, report
        from regression.t4.gateway import effective_verifier_mode, spec_for, verifier_mode_env
        from regression.t4.scenario_loader import load_scenario_file

        _, _, suite_mode = _suite_state(self.config)
        mode = effective_verifier_mode(suite_mode)
        agent_url = (
            self.config.getoption("agent_url")
            or os.environ.get("AGENT_URL")
            or report.DEFAULT_AGENT_URL
        )
        os.environ.setdefault("MONKEYBOT_AGENT_CONFIG", str(preflight.AGENT_CONFIG))
        no_ledger = bool(getattr(self.config.option, "no_ledger", False))
        try:
            preflight.run_preflight_once(
                suite=self.config.getoption("suite") or "t4",
                agent_url=agent_url,
                ledger_dir=getattr(self.config.option, "ledger_dir", None),
                ledger=not no_ledger and os.environ.get("MONKEYBOT_TEST_LEDGER") != "0",
            )
        except preflight.PreflightError as exc:
            pytest.exit(str(exc), returncode=2)
        with verifier_mode_env(mode):
            scenario = load_scenario_file(self.path)
            pool = _gateway_pool(self.config, agent_url)
            gateway = pool.acquire(spec_for(scenario, mode))
            try:
                aggregate = asyncio.run(report.run_scenario(scenario, gateway.url, gateway=gateway))
            finally:
                pool.release(gateway)
        if aggregate.status == "errored":
            raise RuntimeError(aggregate.failure_reason or "scenario errored")
        if aggregate.requirement_failures:
            raise AssertionError("\n".join(aggregate.requirement_failures))

    def repr_failure(
        self,
        excinfo: pytest.ExceptionInfo[BaseException],
        style: str | None = None,
    ) -> str | pytest.TerminalRepr:
        if isinstance(excinfo.value, AssertionError):
            return str(excinfo.value)
        return excinfo.getrepr(style=style)

    def reportinfo(self) -> tuple[Path, None, str]:
        return self.path, None, self.path.name
