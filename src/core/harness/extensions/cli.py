"""Plugin-listing CLI helpers for ``emonk-harness plugin ls``.

Invoked via the top-level ``emonk-harness`` script (see ``linter.py`` which
wires the ``plugin`` sub-parser into the existing argparse tree).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .base import (
    Checkpointer,
    IdentitySource,
    JobStorage,
    MemoryStore,
    ModelProvider,
    SecretResolver,
)
from .registry import Registry, RegistryEntry

_ABCS: tuple[tuple[str, type], ...] = (
    ("checkpointer", Checkpointer),
    ("memory_store", MemoryStore),
    ("job_storage", JobStorage),
    ("identity_source", IdentitySource),
    ("secret_resolver", SecretResolver),
    ("model_provider", ModelProvider),
)


def _collect_entries(
    kind_filter: str | None,
    source_filter: str | None,
) -> tuple[list[RegistryEntry], list[dict[str, Any]]]:
    entries: list[RegistryEntry] = []
    collisions: list[dict[str, Any]] = []
    for kind, abc_cls in _ABCS:
        if kind_filter and kind != kind_filter:
            continue
        registry: Registry[Any] = abc_cls.registry
        for entry in registry.entries():
            if source_filter and entry.source != source_filter:
                continue
            entries.append(entry)
        for shadowed in registry._shadowed_entries():
            winner = registry.entry(shadowed.name)
            collisions.append(
                {
                    "kind": kind,
                    "name": shadowed.name,
                    "tiers": sorted(
                        {shadowed.source, winner.source if winner else "unknown"}
                    ),
                    "winner": winner.source if winner else None,
                }
            )
    return entries, collisions


def plugin_ls(args: argparse.Namespace) -> int:
    """Print a JSON report of every registered backend across every surface.

    Args:
        args: An ``argparse.Namespace`` with optional ``kind``, ``source`` and
            ``strict`` attributes.

    Returns:
        ``0`` on success. ``1`` if ``--strict`` is set and any collision was
        detected.
    """
    kind = getattr(args, "kind", None)
    source = getattr(args, "source", None)
    strict = bool(getattr(args, "strict", False))

    entries, collisions = _collect_entries(kind, source)
    report = {
        "plugins": [e.model_dump() for e in entries],
        "collisions": collisions,
    }
    print(json.dumps(report, indent=2))
    if strict and collisions:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point for the ``plugin`` subcommand.

    This is only used by tests; production invocation goes through
    ``emonk-harness plugin ls`` via the linter's argparse tree.
    """
    parser = argparse.ArgumentParser(prog="emonk-harness plugin")
    sub = parser.add_subparsers(dest="sub", required=True)
    p_ls = sub.add_parser("ls", help="List registered backends")
    p_ls.add_argument("--kind", default=None)
    p_ls.add_argument("--source", default=None)
    p_ls.add_argument("--strict", action="store_true")
    p_ls.set_defaults(func=plugin_ls)
    ns = parser.parse_args(argv)
    return int(ns.func(ns))


if __name__ == "__main__":
    sys.exit(main())
