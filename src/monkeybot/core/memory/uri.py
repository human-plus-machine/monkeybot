"""Shared MemPalace URI helpers.

``layout.resolve_memory_storage_uri`` and ``palace.palace_path_from_uri`` both
need to recognize object-store schemes. Keep the scheme list in one place.
"""

from __future__ import annotations

_OBJECT_STORE_SCHEMES = ("gcs://", "s3://", "gs://")
_LOCAL_SCHEMES = frozenset({"local", "file"})

DEFAULT_LOCAL_MEMORY_RELPATH = "memory/mempalace"


def object_store_memory_scheme(raw: str) -> str | None:
    """Return the object-store scheme (including ``://``) if ``raw`` is one."""
    lowered = raw.strip().lower()
    for scheme in _OBJECT_STORE_SCHEMES:
        if lowered.startswith(scheme):
            return scheme
    if "://" not in raw.strip():
        return None
    scheme, _, rest = raw.strip().partition("://")
    if rest and scheme.lower() not in _LOCAL_SCHEMES:
        return f"{scheme.lower()}://"
    return None
