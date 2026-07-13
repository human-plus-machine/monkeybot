"""Shim — realtime talk helpers live in ``monkeybot_cli.realtime.session``.

The user-facing ``monkeybot`` console script lives in the ``monkeybot-cli`` package
(``cli/``). This module re-exports so ``python -m monkeybot.cli`` and legacy imports
keep working when ``monkeybot-cli`` is installed (root ``dev`` group / ``cli/`` sync).
"""

from __future__ import annotations

from monkeybot_cli.realtime.session import (  # noqa: F401
    app,
    main,
    run_talk_session,
    talk,
    version,
)

__all__ = ["app", "main", "run_talk_session", "talk", "version"]


if __name__ == "__main__":
    main()
