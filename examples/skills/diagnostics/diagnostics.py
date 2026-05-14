"""
Run system diagnostics to verify monkey-bot deployment health.

This skill checks environment configuration, Python runtime, and basic
computation to confirm the full deployment pipeline is working.
"""

import json
import logging
import os
import platform
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# Environment variables to check (never log values, only presence)
EXPECTED_ENV_VARS = [
    "AGENT_NAME",
    "MODEL_PROVIDER",
    "VERTEX_AI_PROJECT_ID",
    "SKILLS_DIR",
]


@dataclass
class DiagnosticResult:
    """Structured diagnostic result."""

    timestamp: str
    status: str  # "healthy" or "degraded"
    issues: List[str] = field(default_factory=list)
    python_version: Optional[str] = None
    os_info: Optional[str] = None
    cwd: Optional[str] = None
    env_check: Optional[dict] = None
    computation_check: Optional[str] = None


@tool
async def run_diagnostics(
    check_type: str = "full",
) -> str:
    """Run system diagnostics to verify the monkey-bot deployment is working.

    Checks environment variables, Python runtime, and basic computation
    to confirm the full deployment pipeline is operational. Results are
    returned as structured JSON with clear pass/fail indicators.

    Args:
        check_type: Type of diagnostic check to run.
            - "full": All diagnostics including env, runtime, and computation (default)
            - "quick": Just timestamp and computation check
            - "env": Just environment variable check

    Returns:
        JSON string with diagnostic results including status ("healthy" or "degraded"),
        any issues found, and detailed check results.

    Example:
        Run full diagnostics:
            run_diagnostics(check_type="full")
        Quick health check:
            run_diagnostics(check_type="quick")
    """
    logger.info(f"Running diagnostics: check_type={check_type}")

    issues: List[str] = []
    now = datetime.now(timezone.utc).isoformat()

    result = DiagnosticResult(
        timestamp=now,
        status="healthy",
    )

    # Computation check (always run)
    try:
        computed = sum(range(1000))
        if computed == 499500:
            result.computation_check = "pass"
        else:
            result.computation_check = "fail"
            issues.append(f"Computation check failed: expected 499500, got {computed}")
    except Exception as e:
        result.computation_check = "fail"
        issues.append(f"Computation check error: {str(e)}")

    if check_type in ("full", "env"):
        # Environment variable check
        env_check = {}
        for var in EXPECTED_ENV_VARS:
            if os.getenv(var):
                env_check[var] = "set"
            else:
                env_check[var] = "missing"
                issues.append(f"{var} not set")
        result.env_check = env_check

    if check_type in ("full",):
        # Runtime info
        result.python_version = sys.version
        result.os_info = platform.platform()
        result.cwd = os.getcwd()

    # Set final status
    if issues:
        result.status = "degraded"
    result.issues = issues

    # Build output dict, excluding None values
    output = {k: v for k, v in asdict(result).items() if v is not None}

    logger.info(f"Diagnostics complete: status={result.status}, issues={len(issues)}")

    return json.dumps(output, indent=2)
