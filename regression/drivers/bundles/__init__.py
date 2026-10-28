"""Discovered loop-driver bundles.

Adding a hook is a new module in this package that defines ``BUNDLE``.
Discovery sorts by ``order``. ``EvalHook`` stays in the loop driver and is
not a bundle.
"""

from __future__ import annotations

import importlib
import pkgutil

from regression.drivers.bundles.protocol import Bundle


def discover() -> tuple[Bundle, ...]:
    """Load each sibling module's ``BUNDLE`` and sort by ``order``."""
    found: list[Bundle] = []
    for info in pkgutil.iter_modules(__path__):
        if info.name == "protocol":
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        bundle = getattr(module, "BUNDLE", None)
        if isinstance(bundle, Bundle):
            found.append(bundle)
    found.sort(key=lambda bundle: bundle.order)
    return tuple(found)


def registered_hooks() -> frozenset[str]:
    """Hook names the driver accepts. Unknown names fail closed."""
    return frozenset(name for bundle in discover() for name in bundle.hooks)
