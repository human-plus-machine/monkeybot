"""Bundle discovery stays unique, and EvalHook stays out of it."""

from __future__ import annotations

from regression.drivers.bundles import discover, registered_hooks
from regression.drivers.record import EvalHook


def test_bundle_orders_are_unique() -> None:
    orders = [bundle.order for bundle in discover()]
    assert orders
    assert len(orders) == len(set(orders))
    assert orders == sorted(orders)


def test_discovered_hooks_are_the_hooks_the_driver_allows() -> None:
    assert registered_hooks() == frozenset(name for bundle in discover() for name in bundle.hooks)
    assert registered_hooks() == frozenset(
        {
            "verification_gate",
            "memory",
            "verifier",
            "git_context",
            "read_ledger_compaction",
            "evidence_guard",
            "secret_scanner",
        }
    )


def test_eval_hook_is_not_a_discovered_bundle() -> None:
    for bundle in discover():
        assert not isinstance(bundle, EvalHook)
        assert type(bundle).__name__ != "EvalHook"
