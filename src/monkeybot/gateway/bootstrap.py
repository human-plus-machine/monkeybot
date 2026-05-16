"""Process-level bootstrap before the FastAPI app is constructed."""

from __future__ import annotations

from dotenv import load_dotenv


def ensure_gateway_runtime_env() -> None:
    """Load ``.env`` then optional ``monkeybot_config/monkeybot.yaml`` (see ``monkeybot.core.config.runtime_env``)."""
    load_dotenv()
    from monkeybot.core.config.runtime_env import apply_monkeybot_runtime_env

    apply_monkeybot_runtime_env()
