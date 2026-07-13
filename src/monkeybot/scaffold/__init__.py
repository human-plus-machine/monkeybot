"""Compatibility shim — scaffolding lives in ``monkeybot_cli.scaffold``.

Docker images and older imports still use ``monkeybot.scaffold.run_new``. Prefer
``from monkeybot_cli.scaffold import run_new`` in new code.
"""

from __future__ import annotations

try:
    from monkeybot_cli.scaffold import (  # noqa: F401
        ensure_memory,
        ensure_workspace,
        install_config_bundle,
        install_env_example,
        install_setup_script,
        run_new,
        write_active_config,
    )
except ImportError as exc:  # pragma: no cover - clearer error when CLI not installed
    raise ImportError(
        "Scaffolding moved to the monkeybot-cli package. "
        "Install monkeybot-cli (e.g. `uv tool install monkeybot-cli` or `pip install ./cli`)."
    ) from exc

__all__ = [
    "ensure_memory",
    "ensure_workspace",
    "install_config_bundle",
    "install_env_example",
    "install_setup_script",
    "run_new",
    "write_active_config",
]
