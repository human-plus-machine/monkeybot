"""Process-environment push for one scenario run."""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

from regression.drivers.support.paths import _WORKSPACE_TOKEN
from regression.drivers.support.tokens import _expand_tokens, _repo_tokens


def _expand_scenario_env(env: dict[str, str], workspace: Path | None) -> None:
    """Replace ``{workspace}`` and ``{repo_root}`` in scenario env values."""
    tokens = _repo_tokens()
    if workspace is not None:
        tokens[_WORKSPACE_TOKEN] = workspace.resolve().as_posix()
    for key, value in list(env.items()):
        if "{" in value:
            env[key] = _expand_tokens(value, tokens)


@contextmanager
def _pushed_env(updates: Mapping[str, str]) -> Iterator[None]:
    """Apply ``config.env`` for the run, then put the process environment back.

    Called only after :func:`build_runtime_config` has captured pins, so these
    keys are not frozen into the process-wide pin table.
    """
    previous = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
