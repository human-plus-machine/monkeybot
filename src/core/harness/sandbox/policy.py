"""Runtime Policy dataclass — materialized form of PolicySpec used at exec time."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from ..specs import PolicySpec

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..extensions.base import SecretResolver


@dataclass(frozen=True)
class Policy:
    fs_allow: tuple[str, ...]
    fs_deny: tuple[str, ...]
    net_allow: tuple[str, ...]
    net_deny: tuple[str, ...]
    env_allow: tuple[str, ...]
    secret_handles: Mapping[str, str]
    timeout_seconds: int

    @classmethod
    def from_spec(cls, spec: PolicySpec, *, timeout_seconds: int) -> "Policy":
        return cls(
            fs_allow=tuple(spec.fs_allow),
            fs_deny=tuple(spec.fs_deny),
            net_allow=tuple(spec.net_allow),
            net_deny=tuple(spec.net_deny),
            env_allow=tuple(spec.env_allow),
            secret_handles=dict(spec.secret_handles),
            timeout_seconds=timeout_seconds,
        )

    def check_fs(self, path: str) -> bool:
        p = str(Path(path).resolve(strict=False))
        for pat in self.fs_deny:
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(path, pat):
                return False
        if not self.fs_allow:
            return True
        for pat in self.fs_allow:
            if fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(path, pat):
                return True
        return False

    def check_net(self, host: str) -> bool:
        for pat in self.net_deny:
            if fnmatch.fnmatch(host, pat):
                return False
        if not self.net_allow:
            return False
        for pat in self.net_allow:
            if fnmatch.fnmatch(host, pat):
                return True
        return False

    # BEGIN harness-extensibility story 6
    async def materialize(
        self, resolver: SecretResolver | None = None
    ) -> dict[str, str]:
        """Dereference ``secret_handles`` into a child-process env map.

        When ``resolver`` is ``None``, fall back to the legacy env-only path
        that reads ``os.environ[handle]`` directly (backward-compatible with
        pre-Story-6 call sites). When a resolver is supplied, every handle
        is resolved through it — the plaintext secret lives only in the
        returned dict, which callers forward to the child process env map
        and **never** log or persist.

        Args:
            resolver: Optional :class:`SecretResolver` used to resolve each
                configured handle. When ``None``, the legacy
                ``os.environ`` path is used.

        Returns:
            Mapping of child-process env var name → resolved secret value.
            Handles that cannot be resolved in the legacy path are silently
            skipped (preserving pre-Story-6 behaviour); resolver failures
            propagate verbatim.
        """
        env: dict[str, str] = {}
        for env_key, handle in self.secret_handles.items():
            if resolver is None:
                if handle in os.environ:
                    env[env_key] = os.environ[handle]
                continue
            secret = await resolver.resolve(handle)
            env[env_key] = secret.get_secret_value()
        return env
    # END harness-extensibility story 6
