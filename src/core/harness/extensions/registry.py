"""Generic ``Registry[T]`` with the 7-tier precedence resolver.

See 1b-contracts.md §2 for the authoritative contract. One ``Registry`` instance
lives on each ABC (``Checkpointer.registry``, ``MemoryStore.registry`` ...) and
accumulates factory registrations from three sources:

1. Programmatic ``.register(...)`` calls (default source ``"programmatic"``).
2. Lazy entry-point discovery (opt-in, gated on ``HARNESS_PLUGINS_FROM_ENTRY_POINTS=1``).
3. Import-path specs at ``resolve()`` time (never mutates the registry tables).
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict

from .errors import BackendConfigError, BackendNotFound

T = TypeVar("T")

RegistrySource = Literal["builtin", "entry_point", "programmatic", "import_path"]

_ENTRY_POINT_GROUPS: dict[str, str] = {
    "checkpointer": "emonk.checkpointers",
    "memory_store": "emonk.memory_stores",
    "job_storage": "emonk.job_storage",
    "identity_source": "emonk.identity_sources",
    "secret_resolver": "emonk.secret_resolvers",
    "model_provider": "emonk.model_providers",
}

_TIER_RANK: dict[RegistrySource, int] = {
    "programmatic": 0,
    "entry_point": 1,
    "builtin": 2,
    "import_path": 3,
}


class RegistryEntry(BaseModel):
    """Metadata describing a single backend registration."""

    model_config = ConfigDict(frozen=True)
    name: str
    kind: str
    source: RegistrySource
    module: str
    factory_qualname: str


class Registry(Generic[T]):
    """One instance per base class; stores backend factories keyed by name.

    See class-module docstring and 1b-contracts.md §2 for the full precedence
    rules. Thread-safety: registration is expected to happen at module import
    time, so no internal locking is performed.
    """

    def __init__(self, kind: str, *, default: str | None = None) -> None:
        self._kind = kind
        self._default = default
        self._entries: dict[str, RegistryEntry] = {}
        self._factories: dict[str, Callable[..., T]] = {}
        self._shadowed: list[RegistryEntry] = []
        self._ep_loaded = False

    @property
    def kind(self) -> str:
        """The ABC kind this registry belongs to (e.g. ``"checkpointer"``)."""
        return self._kind

    def register(
        self,
        name: str,
        factory: Callable[..., T],
        *,
        source: RegistrySource = "programmatic",
        module: str | None = None,
        overwrite: bool = False,
    ) -> None:
        """Register a factory under ``name``.

        Args:
            name: Backend name used in discriminated-union specs.
            factory: Callable returning an instance of ``T``.
            source: Registration source tag (used for precedence + audit).
            module: Optional override for the module attribute recorded in
                the :class:`RegistryEntry`.
            overwrite: When ``True``, an existing same-tier entry is replaced
                silently. When ``False`` (default), re-registration raises
                :class:`BackendConfigError`.

        Raises:
            BackendConfigError: Same-tier collision with ``overwrite=False``.
        """
        module_name = module or getattr(factory, "__module__", "<unknown>")
        qualname = getattr(factory, "__qualname__", getattr(factory, "__name__", repr(factory)))
        new_entry = RegistryEntry(
            name=name,
            kind=self._kind,
            source=source,
            module=module_name,
            factory_qualname=f"{module_name}:{qualname}",
        )
        existing = self._entries.get(name)
        if existing is not None:
            existing_rank = _TIER_RANK[existing.source]
            new_rank = _TIER_RANK[source]
            if existing.source == source:
                if source == "programmatic" and not overwrite:
                    raise BackendConfigError(
                        f"{self._kind}:{name} already registered from {existing.source} "
                        f"({existing.factory_qualname}); pass overwrite=True to replace"
                    )
                self._shadowed.append(existing)
                self._factories[name] = factory
                self._entries[name] = new_entry
                return
            if new_rank < existing_rank:
                self._shadowed.append(existing)
                self._factories[name] = factory
                self._entries[name] = new_entry
                return
            self._shadowed.append(new_entry)
            return
        self._factories[name] = factory
        self._entries[name] = new_entry

    def resolve(self, spec: Mapping[str, Any] | T | str | None) -> T:
        """Resolve a spec into a backend instance.

        Precedence (highest first):
            1. ``spec`` is already an instance of the registry's target type.
            2. ``spec`` is a ``"module:attr"`` string.
            3. ``spec["import_path"]`` is set.
            4. ``spec["backend"]`` matches a programmatic registration.
            5. ``spec["backend"]`` matches an entry-point registration (only
               with ``HARNESS_PLUGINS_FROM_ENTRY_POINTS=1``).
            6. ``spec["backend"]`` matches a builtin registration.
            7. ``backend`` omitted and a default is configured.

        Raises:
            BackendNotFound: No precedence tier yields a factory.
            BackendConfigError: Factory raised while constructing.
        """
        if spec is None:
            return self._resolve_default({})
        if hasattr(spec, "model_dump") and not isinstance(spec, Mapping):
            spec_mapping = spec.model_dump()
            return self._resolve_from_mapping(spec_mapping)
        if isinstance(spec, str):
            return self._resolve_import_path(spec, {})
        if isinstance(spec, Mapping):
            return self._resolve_from_mapping(spec)
        return spec  # type: ignore[return-value]

    def _resolve_from_mapping(self, spec: Mapping[str, Any]) -> T:
        import_path = spec.get("import_path")
        if import_path:
            kwargs = {k: v for k, v in spec.items() if k not in ("backend", "import_path")}
            return self._resolve_import_path(str(import_path), kwargs)
        backend = spec.get("backend")
        if backend is None:
            return self._resolve_default(spec)
        backend_name = str(backend)
        self._load_entry_points_once()
        entry = self._entries.get(backend_name)
        if entry is None:
            raise BackendNotFound(self._kind, backend_name)
        if entry.source == "entry_point" and os.environ.get("HARNESS_PLUGINS_FROM_ENTRY_POINTS") != "1":
            raise BackendNotFound(self._kind, backend_name)
        factory = self._factories[backend_name]
        kwargs = {k: v for k, v in spec.items() if k not in ("backend", "import_path")}
        try:
            return factory(**kwargs)
        except TypeError as exc:
            raise BackendConfigError(
                f"factory {entry.factory_qualname} rejected kwargs {sorted(kwargs)}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise BackendConfigError(
                f"factory {entry.factory_qualname} failed to construct: {exc}"
            ) from exc

    def _resolve_default(self, spec: Mapping[str, Any]) -> T:
        if self._default is None:
            raise BackendNotFound(self._kind, None)
        self._load_entry_points_once()
        entry = self._entries.get(self._default)
        if entry is None:
            raise BackendNotFound(self._kind, self._default)
        factory = self._factories[self._default]
        kwargs = {k: v for k, v in spec.items() if k not in ("backend", "import_path")}
        try:
            return factory(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise BackendConfigError(
                f"factory {entry.factory_qualname} failed to construct: {exc}"
            ) from exc

    def _resolve_import_path(self, import_path: str, kwargs: Mapping[str, Any]) -> T:
        if ":" not in import_path:
            raise BackendConfigError(
                f"import_path {import_path!r} must be of the form 'module:attr'"
            )
        module_name, attr = import_path.split(":", 1)
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise BackendConfigError(
                f"import_path {import_path!r} module not importable: {exc}"
            ) from exc
        try:
            target = getattr(module, attr)
        except AttributeError as exc:
            raise BackendConfigError(
                f"import_path {import_path!r} missing attribute {attr!r}"
            ) from exc
        try:
            instance = target(**dict(kwargs)) if kwargs else target()
        except TypeError as exc:
            raise BackendConfigError(
                f"import_path {import_path!r} rejected kwargs {sorted(kwargs)}: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise BackendConfigError(
                f"import_path {import_path!r} failed to construct: {exc}"
            ) from exc
        return instance  # type: ignore[no-any-return]

    def entries(self) -> list[RegistryEntry]:
        """Return all registry entries (loads entry-points lazily on first call)."""
        self._load_entry_points_once()
        return list(self._entries.values())

    def entry(self, name: str) -> RegistryEntry | None:
        """Return the entry registered under ``name`` or ``None``."""
        self._load_entry_points_once()
        return self._entries.get(name)

    def _shadowed_entries(self) -> list[RegistryEntry]:
        """Return entries that were shadowed by a higher-precedence registration."""
        return list(self._shadowed)

    def _load_entry_points_once(self) -> None:
        if self._ep_loaded:
            return
        if os.environ.get("HARNESS_PLUGINS_FROM_ENTRY_POINTS") != "1":
            self._ep_loaded = True
            return
        group = _ENTRY_POINT_GROUPS.get(self._kind)
        if group is None:
            self._ep_loaded = True
            return
        try:
            from importlib.metadata import entry_points
        except ImportError:  # pragma: no cover - stdlib since 3.8
            self._ep_loaded = True
            return
        try:
            eps = entry_points(group=group)
        except TypeError:  # pragma: no cover - Py < 3.10 compat
            eps = entry_points().get(group, [])  # type: ignore[attr-defined]
        for ep in eps:
            try:
                factory = ep.load()
            except Exception as exc:
                self._ep_loaded = True
                raise BackendConfigError(
                    f"entry-point {group}:{ep.name} failed to load: {exc}"
                ) from exc
            module = getattr(ep, "module", None) or getattr(factory, "__module__", "<unknown>")
            self.register(
                ep.name,
                factory,
                source="entry_point",
                module=module,
                overwrite=False,
            )
        self._ep_loaded = True


__all__ = [
    "Registry",
    "RegistryEntry",
    "RegistrySource",
]
