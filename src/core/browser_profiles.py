"""Browser profile sync from GCS for browser automation skills.

Part of the monkey-bot framework's optional ``browser`` extra. Syncs browser
profiles (session cookies/state) from GCS to local filesystem at container
startup.

Typical usage in a bot's FastAPI lifespan::

    from emonk.core.browser_profiles import sync_profiles_from_gcs
    sync_profiles_from_gcs()
"""
import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_PROFILE_DIR = "/tmp/browser-profiles"


def sync_profiles_from_gcs(
    profile_dir: str | None = None,
    gcs_bucket: str | None = None,
) -> None:
    """Sync browser profiles from GCS to local filesystem at container startup.

    Reads GCS_ENABLED, GCS_MEMORY_BUCKET, and BROWSER_PROFILE_DIR from environment
    if arguments are not provided. No-ops if GCS_ENABLED != "true".

    Never raises — logs warnings on failure so startup continues regardless.

    Args:
        profile_dir: Local directory for profiles. Defaults to BROWSER_PROFILE_DIR
                     env var, or /tmp/browser-profiles if not set.
        gcs_bucket: GCS bucket name. Defaults to GCS_MEMORY_BUCKET env var.
    """
    try:
        if os.environ.get("GCS_ENABLED", "false").lower() != "true":
            logger.info(
                "GCS_ENABLED is not true, skipping browser profile sync",
                extra={"component": "browser_profiles"},
            )
            return

        resolved_profile_dir = profile_dir or os.environ.get("BROWSER_PROFILE_DIR", _DEFAULT_PROFILE_DIR)
        resolved_bucket = gcs_bucket or os.environ.get("GCS_MEMORY_BUCKET", "")

        if not resolved_bucket:
            logger.warning(
                "GCS_MEMORY_BUCKET not set, skipping browser profile sync",
                extra={"component": "browser_profiles"},
            )
            return

        Path(resolved_profile_dir).mkdir(parents=True, exist_ok=True)

        gcs_source = f"gs://{resolved_bucket}/browser-profiles/"

        logger.info(
            "Syncing browser profiles from GCS",
            extra={
                "component": "browser_profiles",
                "gcs_source": gcs_source,
                "profile_dir": resolved_profile_dir,
            },
        )

        result = subprocess.run(
            ["gsutil", "-m", "rsync", "-r", gcs_source, resolved_profile_dir],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        if result.returncode != 0:
            logger.warning(
                "Browser profile sync from GCS failed",
                extra={
                    "component": "browser_profiles",
                    "returncode": result.returncode,
                    "stderr": result.stderr[:500],
                },
            )
            return

        logger.info(
            "Browser profile sync complete",
            extra={"component": "browser_profiles", "profile_dir": resolved_profile_dir},
        )

    except Exception as e:
        logger.warning(
            "Browser profile sync encountered an error",
            extra={"component": "browser_profiles", "error": str(e)},
        )
