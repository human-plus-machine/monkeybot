"""Import this module first so ``.env`` and ``monkeybot_config/monkeybot.yaml`` apply before other imports."""

from monkeybot.gateway.bootstrap import ensure_gateway_runtime_env

ensure_gateway_runtime_env()
