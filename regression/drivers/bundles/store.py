"""History row ids and turn_usage rows for scripted loop scenarios.

``Message.from_dict`` drops ``row_id``. Scenarios that name preserved ids
carry them in the YAML; this bundle stamps those ids onto the loaded
messages before the driver builds history.

The loop, not the gateway, is what this driver runs. When a scenario
asserts ``store.usage``, each ``TurnComplete`` is recorded the way the
gateway records one: ``usage_from_totals`` of that event's ``usage``.
The snapshot count is the number of ``turn_usage`` rows whose columns
match those events, in order.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from monkeybot.core.llm.provider import Message
from monkeybot.core.llm.usage import usage_from_totals
from monkeybot.core.persistence.sqlite_backend import SQLiteStorageBackend
from monkeybot.core.runtime.events import TurnComplete
from regression.drivers.bundles.protocol import Bundle, RunCtx
from regression.schema.assertions import RunView, StoreView

_REPO = Path(__file__).resolve().parents[3]
_USAGE_COLUMNS = (
    "input_tokens",
    "output_tokens",
    "cached_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
    "cost_usd",
    "duration_ms",
    "estimated_prompt_tokens",
)


def _scenario_yaml(scenario_id: str) -> Path | None:
    relative = Path(f"{scenario_id}.yaml")
    for candidate in (relative, _REPO / relative):
        if candidate.is_file():
            return candidate
    return None


def _stamp_history_row_ids(ctx: RunCtx) -> None:
    if not ctx.scenario.history:
        return
    path = _scenario_yaml(ctx.scenario.id)
    if path is None:
        return
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    items = loaded.get("history") if isinstance(loaded, dict) else None
    history = ctx.scenario.history
    if not isinstance(items, list) or len(items) != len(history):
        return
    stamped: list[Message] = []
    changed = False
    for item, message in zip(items, history, strict=True):
        row_id = item.get("row_id") if isinstance(item, dict) else None
        if isinstance(row_id, str) and row_id:
            stamped.append(replace(message, row_id=row_id))
            changed = True
        else:
            stamped.append(message)
    if changed:
        ctx.scenario.history = stamped


def _wants_usage(ctx: RunCtx) -> bool:
    raw = ctx.scenario.assertions
    if not isinstance(raw, dict):
        return False
    store = raw.get("store")
    return isinstance(store, dict) and "usage" in store


def _usage_value(usage: Any, name: str) -> Any:
    return getattr(usage, name)


async def _matching_usage_rows(store: Any, completes: list[TurnComplete]) -> int:
    cursor = await store._conn.execute(
        """
        SELECT input_tokens, output_tokens, cached_tokens,
               cache_read_tokens, cache_creation_tokens,
               cost_usd, duration_ms, estimated_prompt_tokens
        FROM turn_usage
        ORDER BY id
        """
    )
    rows = await cursor.fetchall()
    await cursor.close()
    if len(rows) != len(completes):
        return 0
    for row, event in zip(rows, completes, strict=True):
        for index, name in enumerate(_USAGE_COLUMNS):
            actual = row[index]
            expected = _usage_value(event.usage, name)
            if name == "cost_usd":
                if float(actual) != float(expected):
                    return 0
            elif int(actual) != int(expected):
                return 0
    return len(rows)


async def apply_turn_usage(ctx: RunCtx, events: list[Any], view: RunView) -> None:
    """Record one ``turn_usage`` row per ``TurnComplete`` and set the snapshot count."""
    backend = ctx.usage_backend
    if backend is None:
        return
    completes = [event for event in events if isinstance(event, TurnComplete)]
    store = backend.usage()
    model = ctx.cfg.model.name or "fake"
    for event in completes:
        await store.record(
            ctx.session_id,
            model,
            usage_from_totals(event.usage),
            run_id=event.request_id,
        )
    matched = await _matching_usage_rows(store, completes)
    if view.store is None:
        view.store = StoreView(usage=matched)
    else:
        view.store.usage = matched


class _StoreBundle(Bundle):
    order = 15
    hooks = ()

    async def prepare(self, ctx: RunCtx) -> None:
        _stamp_history_row_ids(ctx)
        if not _wants_usage(ctx):
            return
        backend = SQLiteStorageBackend(f"sqlite:///{ctx.workdir / 'usage.db'}")
        await backend.open()
        ctx.usage_backend = backend

    async def close(self, ctx: RunCtx) -> None:
        backend = ctx.usage_backend
        ctx.usage_backend = None
        if backend is not None:
            await backend.close()


BUNDLE = _StoreBundle()
