"""Shared path/filename noise rules used by indexing and ANN ranking."""

from __future__ import annotations

LOCKFILE_NAMES = frozenset(
    {
        "pnpm-lock.yaml",
        "package-lock.json",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "poetry.lock",
        "uv.lock",
        "composer.lock",
        "gemfile.lock",
        "go.sum",
    }
)

__all__ = ["LOCKFILE_NAMES"]
