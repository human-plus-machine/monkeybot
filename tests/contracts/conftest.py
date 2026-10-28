"""Mark contract tests as tier 1 and print catalog coverage after the pairing run."""

from __future__ import annotations

from pathlib import Path

import pytest

UNCOVERED_LINES: list[str] = []


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).resolve().parent
    for item in items:
        path = Path(str(item.path))
        if path == here or here in path.parents:
            item.add_marker(pytest.mark.t1)


def pytest_terminal_summary(
    terminalreporter: pytest.TerminalReporter,
    exitstatus: int,
    config: pytest.Config,
) -> None:
    del exitstatus, config
    if not UNCOVERED_LINES:
        return
    terminalreporter.write_sep("-", "uncovered catalog rows")
    for line in UNCOVERED_LINES:
        terminalreporter.write_line(line)


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.needs_postgres),
        pytest.param("firestore", marks=pytest.mark.needs_firestore),
    ]
)
async def open_backend(request: pytest.FixtureRequest, tmp_path: Path):
    """Parametrized opener: sqlite always, Postgres and Firestore when configured.

    Yields an async callable ``open_backend(agent_scope=...)`` that shares one
    database across scopes. Backends it opened are closed after the test.
    """
    import os
    import uuid
    from typing import Any

    from testkit.backends import FIRESTORE_SKIP, POSTGRES_SKIP
    from testkit.backends import open_backend as open_storage_backend

    kind = str(request.param)
    if kind == "postgres" and not os.environ.get("MONKEYBOT_TEST_POSTGRES_URL"):
        pytest.skip(POSTGRES_SKIP)
    if kind == "firestore" and not os.environ.get("FIRESTORE_EMULATOR_HOST"):
        pytest.skip(FIRESTORE_SKIP)
    db_url = f"sqlite:///{tmp_path / 'parity.sqlite'}" if kind == "sqlite" else None
    prefix = f"p{uuid.uuid4().hex[:12]}"
    opened: list[Any] = []

    async def _open(*, agent_scope: str = "", agent_root: Path | None = None) -> Any:
        backend = await open_storage_backend(
            kind,
            agent_scope=agent_scope,
            agent_root=agent_root,
            db_url=db_url,
            prefix=prefix,
        )
        opened.append(backend)
        return backend

    _open.kind = kind  # type: ignore[attr-defined]
    yield _open
    for backend in reversed(opened):
        await backend.close()
