"""Configuration and secrets management for monkey-bot framework.

Provides utilities for loading secrets and configuring models:
- load_bot_config(): Load bot.yaml config file with defaults
- load_secrets(): Load from GCP Secret Manager (prod) or .env (dev)
- get_provider_config(): Resolve native :class:`~monkeybot.core.llm.provider.Provider` + model id

This package handles environment detection and secret loading for deployments.
"""

from __future__ import annotations

from .runtime_env import apply_monkeybot_runtime_env, reset_runtime_env_state_for_tests
from .settings import *  # noqa: F403
from .settings import _validate_provider_config
