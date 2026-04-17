"""LangGraph :class:`BaseStore` adapter for a :class:`MemoryStore`.

See 1b-contracts.md §3.2. Every concrete :class:`MemoryStore` backend returns
``as_langgraph_store(self)`` from its ``as_langgraph_store()`` method so the
adapter is constructed lazily — ``langgraph`` remains a declared dependency
but only imported the first time a consumer asks for the adapter.

The adapter implements :meth:`BaseStore.abatch` by dispatching each op to the
underlying :class:`MemoryStore`. Every async convenience method
(``aput``/``aget``/``asearch``/``adelete``/``alist_namespaces``) therefore
works through the :class:`BaseStore` default implementations — we do not
re-implement them. The sync ``batch`` intentionally raises
:class:`NotImplementedError` because this surface is async-only (per spec).
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from typing import TYPE_CHECKING

from ..base import MemoryStore

if TYPE_CHECKING:  # pragma: no cover - only used for type checking
    from langgraph.store.base import BaseStore


def as_langgraph_store(ms: MemoryStore) -> BaseStore:
    """Return a :class:`langgraph.store.base.BaseStore` adapter for ``ms``.

    Args:
        ms: The underlying :class:`MemoryStore` to delegate to.

    Returns:
        A :class:`BaseStore`-compatible instance whose async API delegates
        every op to ``ms``. Sync ``batch`` raises
        :class:`NotImplementedError` — the adapter is async-only.

    Raises:
        ImportError: ``langgraph`` is not installed. ``langgraph`` is a
            declared runtime dependency, so this should never fire at
            runtime; the lazy import keeps module import cheap for tests
            that monkey-patch ``langgraph`` stubs.
    """
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        Op,
        PutOp,
        Result,
        SearchItem,
        SearchOp,
    )

    class _MemoryStoreAdapter(BaseStore):
        """:class:`BaseStore` view over an async :class:`MemoryStore`."""

        supports_ttl = True

        def batch(self, ops: Iterable[Op]) -> list[Result]:
            """Sync batch is unsupported — use the async API instead."""
            raise NotImplementedError(
                "MemoryStore LangGraph adapter is async-only; call abatch(...)."
            )

        async def abatch(self, ops: Iterable[Op]) -> list[Result]:
            """Dispatch each op to the underlying :class:`MemoryStore`.

            The order of ``ops`` is preserved in the returned results list so
            that :meth:`BaseStore.aput`/``aget``/etc. (which pass single-op
            lists) observe the correct result at index 0.
            """
            results: list[Result] = []
            for op in ops:
                if isinstance(op, PutOp):
                    results.append(await self._handle_put(op))
                elif isinstance(op, GetOp):
                    results.append(await self._handle_get(op))
                elif isinstance(op, SearchOp):
                    results.append(await self._handle_search(op))
                elif isinstance(op, ListNamespacesOp):
                    results.append(await self._handle_list_namespaces(op))
                else:  # pragma: no cover - defensive fallthrough
                    raise NotImplementedError(
                        f"MemoryStore adapter cannot handle op {type(op).__name__}"
                    )
            return results

        async def _handle_put(self, op: PutOp) -> None:
            namespace = tuple(op.namespace)
            if op.value is None:
                await ms.delete(namespace, str(op.key))
                return
            ttl_value = getattr(op, "ttl", None)
            ttl_td: timedelta | None = None
            if ttl_value is not None:
                try:
                    ttl_td = timedelta(minutes=float(ttl_value))
                except (TypeError, ValueError):
                    ttl_td = None
            await ms.put(namespace, str(op.key), dict(op.value), ttl=ttl_td)

        async def _handle_get(self, op: GetOp) -> Item | None:
            namespace = tuple(op.namespace)
            item = await ms.get(namespace, str(op.key))
            if item is None:
                return None
            return Item(
                value=dict(item.value),
                key=item.key,
                namespace=tuple(item.namespace),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )

        async def _handle_search(self, op: SearchOp) -> list[SearchItem]:
            namespace = tuple(op.namespace_prefix)
            items = await ms.search(
                namespace,
                query=op.query,
                filter=dict(op.filter) if op.filter else None,
                limit=op.limit,
            )
            offset = getattr(op, "offset", 0) or 0
            windowed = items[offset : offset + op.limit] if offset else items
            return [
                SearchItem(
                    value=dict(item.value),
                    key=item.key,
                    namespace=tuple(item.namespace),
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    score=None,
                )
                for item in windowed
            ]

        async def _handle_list_namespaces(
            self, op: ListNamespacesOp
        ) -> list[tuple[str, ...]]:
            prefix: tuple[str, ...] = ()
            for cond in op.match_conditions or ():
                if cond.match_type == "prefix" and cond.path is not None:
                    prefix = tuple(str(part) for part in cond.path if part != "*")
                    break
            namespaces = await ms.list_namespaces(prefix)
            if op.max_depth is not None:
                namespaces = [ns[: op.max_depth] for ns in namespaces]
            deduped: list[tuple[str, ...]] = []
            seen: set[tuple[str, ...]] = set()
            for ns in namespaces:
                if ns in seen:
                    continue
                seen.add(ns)
                deduped.append(ns)
            offset = op.offset or 0
            limit = op.limit or 100
            return deduped[offset : offset + limit]

    return _MemoryStoreAdapter()


__all__ = ["as_langgraph_store"]
