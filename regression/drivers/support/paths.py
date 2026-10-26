"""Checkout paths and scenario tokens shared by every bundle."""

from __future__ import annotations

from pathlib import Path

from monkeybot.core.context import SkillRef

_REPO = Path(__file__).resolve().parents[3]
_REPO_ROOT_TOKEN = "{repo_root}"
_WORKSPACE_TOKEN = "{workspace}"
_GRANT_ROOT_TOKEN = "{grant_root}"
# Filled from the tool-result JSON already in the provider messages.
_RESULT_FIELD_TOKENS = (("{job_id}", "job_id"), ("{cursor}", "cursor"))
_CONFIGS = _REPO / "regression" / "fixtures" / "configs"
_GRANT_FIXTURE = _REPO / "regression" / "fixtures" / "workspaces" / "grants_ws" / "granted"
_BASE_CONFIG = _CONFIGS / "agent.base.yaml"
_WORKSPACE_ROOTS = (
    _REPO / "regression" / "fixtures" / "workspaces",
    _REPO / "evals" / "fixtures" / "coding",
)
_FIXTURE_PREFIXES = (
    "regression/fixtures/workspaces/",
    "evals/fixtures/coding/",
    "fixtures/coding/",
)
# Scenario-only. Popped before the process env is pushed.
_GIT_FIXTURE_ENV = "MB_LOOP_GIT_FIXTURE"
_SESSION_ID_ENV = "MB_LOOP_SESSION_ID"
_PATCH_FAIL_ENV = "MB_LOOP_PATCH_FAIL"
_SECRET_SCANNER_ENV = "MB_SECRET_SCANNER"
_SECRET_CANARY = "mb-hook-canary"
_STRICT_MCP = frozenset({"1", "true", "yes"})
_OFF_VALUES = frozenset({"0", "false", "no", "off"})
_SESSION_ID = "loop-scenario"
# Scaffold skill. The fixture tree omits it so ## Skills stays diagnostics then plain.
_GOAL_SKILL = SkillRef(
    name="goal",
    description=(
        "Create one durable objective that continues across scheduled turns until completed."
    ),
)
_JUDGE_SYSTEM_PREFIX = "You are a background verifier."
